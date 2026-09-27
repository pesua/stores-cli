"""rozetka — search rozetka.com.ua from the command line (built for coding agents).

By default every listing and search is limited to goods sold or handled by Rozetka that are
delivered FREE to Rozetka stores (site filters "Rozetka +" and "Доставка в магазини ROZETKA").
--seller any / --any-delivery lift either half.

Run `rozetka <command> -h` for details. Typical flow:
  rozetka city set "Дніпро"                       # city for delivery terms ("Отримати сьогодні…")
  rozetka search "шуруповерт"                     # categories + goods; may jump straight into a category
  rozetka filters 152499                          # what the category can be filtered by
  rozetka products 152499 -f "Джерело живлення=Акумулятор" --brand Bosch --sort cheap
  rozetka product 252150181 --reviews 5
  rozetka compare 252150181 212218615 --diff

Running several commands in a row? Append --delay to each one (a pause before every request).
"""

import argparse
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from client import (DEFAULT_FILTERS, KYIV_MDM, ROZETKA_SELLER_ID, ApiError, Rozetka, goods_id,
                    listing_url, split_listing)

STATE_FILE = Path(__file__).with_name(".state.json")
DEFAULT_DELAY = 1.5
SORTS = ["rank", "cheap", "expensive", "novelty", "popularity", "action"]
SELLERS = ["rozetka", "rozetka_only", "rozetka_fulfillment", "rozetka_eu", "other", "any"]
SELL_STATUS = {"available": "в наявності", "limited": "закінчується", "out_of_stock": "немає в наявності",
               "unavailable": "недоступний", "waiting_for_supply": "очікується",
               "call_the_manager": "уточнюйте"}


# --- helpers ----------------------------------------------------------------

def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def warn(msg):
    print(f"warning: {msg}", file=sys.stderr)


def dump(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def html_to_text(html):
    if not html:
        return ""
    text = BeautifulSoup(html, "html.parser").get_text("\n")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def fmt_num(x):
    return str(int(x)) if float(x).is_integer() else str(x)


def fmt_money(x):
    return f"{x:.3g}" if x < 1 else f"{x:.2f}" if x < 100 else str(round(x))


def parse_range(text):
    lo, _, hi = text.partition("..") if ".." in text else text.partition("-")
    return (float(lo) if lo.strip() else None), (float(hi) if hi.strip() else None)


def city(state):
    """(mdm uuid, title) of the saved city; Kyiv (the site's default) when none is saved."""
    if state.get("city_mdm"):
        return state["city_mdm"], state.get("city_title")
    return KYIV_MDM, "Київ (за замовчуванням; `rozetka city set …`)"


# Quantity + unit in a product name: "… 14 кг", "… 2,5 л", "… (200 шт.)".
UNIT_RE = re.compile(r"(?:(\d+)\s*[хx×*]\s*)?(\d+(?:[.,]\d+)?)\s*(кг|г|мл|л|м²|м2|кв\.\s?м|шт)(?![а-яіїєґa-z])",
                     re.IGNORECASE)  # optional multipack count: "6 х 300 г", "12x0.5 л"
UNIT_NORM = {"кг": ("кг", 1), "г": ("кг", 0.001), "л": ("л", 1), "мл": ("л", 0.001),
             "м²": ("м²", 1), "м2": ("м²", 1), "кв.м": ("м²", 1), "шт": ("шт", 1)}


def unit_price(name, price):
    """Price per kg/l/m²/piece parsed from the pack size in the name, as (value, unit), or None."""
    if not price:
        return None
    found = [(float(q.replace(",", ".")) * int(n or 1), UNIT_NORM[u.lower().replace(" ", "")])
             for n, q, u in UNIT_RE.findall(name or "")]
    found = [(q * k, unit) for q, (unit, k) in found if q > 0]
    if not found:
        return None
    # Prefer weight/volume/area over a piece count; the last mention usually is the pack size.
    qty, unit = next((f for f in reversed(found) if f[1] != "шт"), found[-1])
    if unit == "шт" and qty <= 1:
        return None
    return price / qty, unit


def with_pack(name, pack):
    """Name for unit-price parsing: the selected variant's size ("2.4 кг") when the name has none."""
    if not pack or not UNIT_RE.search(pack) or UNIT_RE.search(name or ""):
        return name
    return f"{name} {pack}"


def page_pack(pd):
    """Selected variant values of a product page ("1.5 кг", "Синій", …), joined."""
    return " ".join((((pd.get("varParams") or {}).get("product") or {}).get("option_titles") or {}).values())


def unit_price_text(name, price):
    up = unit_price(name, price)
    return f"≈{fmt_money(up[0])} ₴/{up[1]}" if up else ""


def sort_by_unit(items):
    """Cheapest per unit first; items in the page's most common unit go before the rest."""
    ups = [(it, unit_price(with_pack(it["title"], it.get("pack")), it["price"])) for it in items]
    units = [u[1] for _, u in ups if u]
    main = max(set(units), key=units.count) if units else None
    return [it for it, u in sorted(ups, key=lambda x: (not x[1] or x[1][1] != main, x[1][0] if x[1] else 0))]


def fold(text):
    """Case- and accent-insensitive form: "Śnieżka" -> "sniezka"."""
    text = unicodedata.normalize("NFKD", str(text).strip().casefold())
    return "".join(c for c in text if not unicodedata.combining(c)).replace("ł", "l")


def _match(options, wanted, what):
    """Pick one option by exact name/id, exact title, or unique substring of title (case/accent-insensitive)."""
    w = fold(wanted)
    for o in options:
        if w in (fold(o.get("name", "")), fold(o.get("id", ""))):
            return o
    exact = [o for o in options if fold(o.get("title", "")) == w]
    if exact:
        return exact[0]
    partial = [o for o in options if w in fold(o.get("title", ""))]
    if len(partial) == 1:
        return partial[0]
    names = ", ".join(str(o.get("title")).strip() for o in (partial or options)[:30])
    die(f"{what} '{wanted}' is {'ambiguous' if partial else 'not found'}; options: {names}")


def flat_options(f):
    """A facet's values, with nested ones (seller: Rozetka + → Rozetka / обробляє Rozetka) flattened."""
    out = []
    for o in f.get("options") or []:
        out.append(o)
        out += (o.get("children") or {}).get("options") or []
    return out


# --- listing filters --------------------------------------------------------

def scope_filters(args):
    """Default scope (Rozetka + free delivery to Rozetka stores) adjusted by --seller / --any-delivery."""
    filters = {k: list(v) for k, v in DEFAULT_FILTERS.items()}
    if args.any_delivery:
        filters.pop("delivery")
    if args.seller == "any":
        filters.pop("seller")
    elif args.seller:
        filters["seller"] = [args.seller]
    return filters


def scope_label(filters):
    parts = []
    seller = filters.get("seller")
    if seller:
        parts.append({("rozetka",): "Rozetka + обробляє Rozetka", ("other",): "інші продавці"}.get(
            tuple(seller), "продавець " + ",".join(seller)))
    if "free-shipping-to-the-store" in filters.get("delivery", []):
        parts.append("безкоштовна доставка в магазини ROZETKA")
    return "; ".join(parts) or "усі товари"


def apply_cli_filters(filters, facets, args):
    """Resolve -f / --brand against the facets of the current listing and add them to `filters`."""
    by_key = facets
    for spec in args.filter or []:
        key, sep, vals = spec.partition("=")
        if not sep:
            die(f"filter must look like KEY=VALUE[|VALUE2], got: {spec}")
        f = _match(by_key, key, "filter")
        if f.get("type") == "slider":
            lo, hi = parse_range(vals)
            rng = f.get("range") or {}
            lo = rng.get("min", 0) if lo is None else lo
            hi = rng.get("max", 10**9) if hi is None else hi
            filters[f["name"]] = [f"{fmt_num(lo)}-{fmt_num(hi)}"]
            continue
        chosen = filters.setdefault(f["name"], [])
        for v in vals.split("|"):
            opt = _match(flat_options(f), v, f"value of '{f['title'].strip()}'")
            if opt["name"] not in chosen:
                chosen.append(opt["name"])
    if args.brand:
        f = next((x for x in by_key if x["name"] == "producer"), None)
        if not f:
            die("this listing has no brand filter")
        chosen = filters.setdefault("producer", [])
        for b in args.brand:
            opt = _match(flat_options(f), b, "brand")
            if opt["name"] not in chosen:
                chosen.append(opt["name"])


def price_filter(text):
    lo, hi = parse_range(text)
    return [f"{fmt_num(lo or 0)}-{fmt_num(hi if hi is not None else 10**8)}"]


def fetch_listing(api, args):
    """CLI args -> (page data, final URL, requested filters)."""
    try:
        base, url_filters = split_listing(args.category)
    except ValueError as e:
        die(str(e))
    filters = scope_filters(args)
    filters.update(url_filters)  # filters baked into a pasted URL win over the defaults
    if args.price:
        filters["price"] = price_filter(args.price)
    if args.sort:
        filters["sort"] = [args.sort]
    if args.filter or args.brand:
        data, url = api.catalog_page(listing_url(base, filters))
        if "goods" not in data:
            die(f"not a product listing: {url}")
        base, _ = split_listing(url)  # canonical slug after a redirect
        apply_cli_filters(filters, data["filters"]["list"], args)
    if args.page and args.page > 1:
        filters["page"] = [str(args.page)]
    data, url = api.catalog_page(listing_url(base, filters))
    if "goods" not in data:
        die(f"not a product listing (a portal page?): {url}")
    applied = set(((data.get("paginator") or {}).get("params") or {}).get("filter") or {})
    dropped = [k for k in filters if k not in applied]
    if dropped:
        warn(f"the site dropped filter(s) {', '.join(dropped)}: the value doesn't exist in this category "
             f"(now: {url})")
    return data, url, filters


# --- output -----------------------------------------------------------------

def from_tile(t, det):
    price = t.get("price") or {}
    groups = t.get("groups") or {}
    variant_ids = {x["id"] for g in groups.get("variants") or [] for x in g.get("list") or []}
    variant_ids |= {x["id"] for x in groups.get("colors") or []}
    pack = " ".join(x["title"] for g in groups.get("variants") or [] for x in g.get("list") or [] if x.get("active"))
    desc = t.get("description") or ""
    if isinstance(desc, list):  # [{"title": "Джерело живлення", "items": [{"title": "Акумулятор"}]}, …]
        desc = "; ".join(f"{d.get('title')}: {', '.join(i.get('title', '') for i in d.get('items') or [])}"
                         for d in desc)
    return {
        "id": t["id"], "title": (t.get("title") or {}).get("text", ""), "href": t.get("href"),
        "price": (price.get("current") or {}).get("value"), "old": (price.get("old") or {}).get("value"),
        "label": (t.get("label") or {}).get("text"), "desc": desc, "atp": t.get("isATP", True),
        "variants": len(variant_ids), "pack": pack, **seller_rating(det),
    }


def from_details(d):
    return {"id": d["id"], "title": d.get("title", ""), "href": d.get("href"), "price": d.get("price"),
            "old": d.get("old_price"), "label": None, "desc": "", "variants": 0, **seller_rating(d)}


def seller_rating(det):
    det = det or {}
    return {"seller": (det.get("seller") or {}).get("title"), "seller_id": det.get("seller_id"),
            "rating": det.get("comments_mark"), "reviews": det.get("comments_amount"),
            "status": det.get("sell_status")}


def price_text(price, old):
    text = f"{price} ₴" if price is not None else "ціна н/д"
    if old and price and old > price:
        text += f" (було {old})"
    return text


def print_row(i, it, max_desc=200):
    price = price_text(it["price"], it["old"])
    name = with_pack(it["title"], it.get("pack"))
    up = unit_price_text(name, it["price"])
    if up:
        price += f", {up}"
    rating = (f" ★{it['rating']} ({it['reviews']})" if it.get("rating") else f" ({it['reviews']} відг.)") \
        if it.get("reviews") else ""
    print(f"{i}. {name if name == it['title'] else it['title'] + ' [' + it['pack'] + ']'} — {price}{rating}")
    seller = it.get("seller") or "?"
    if it.get("seller_id") == ROZETKA_SELLER_ID:
        seller = "Rozetka"
    status = "" if it.get("status") in (None, "available") else SELL_STATUS.get(it["status"], it["status"])
    extra = [f"id: {it['id']}", f"продавець: {seller}", status,
             it.get("label") or ""]
    if it.get("variants", 0) > 1:
        extra.append(f"варіантів: {it['variants']}")
    print("   " + " | ".join(x for x in extra if x))
    desc = " ".join(it.get("desc", "").split())
    if desc:
        print(f"   {desc[:max_desc] + '…' if len(desc) > max_desc else desc}")


def details_map(api, ids):
    try:
        return {d["id"]: d for d in api.details(ids)} if ids else {}
    except ApiError as e:
        warn(f"seller/rating lookup failed: {e}")
        return {}


def delivery_cost(d):
    cost = (d.get("cost") or {}).get("new")
    if cost == 0:
        return "безкоштовно"
    return f"{cost} ₴" if isinstance(cost, (int, float)) else (d.get("cost") or {}).get("text", "")


def delivery_line(api, gid, price, city_mdm):
    """The "Самовивіз з магазинів Rozetka" option: cost and term. When the item can't go there (the site's
    "Доставка в магазини ROZETKA" facet includes some supplier-shipped goods), the cheapest alternative."""
    options = api.deliveries(gid, price or 0, city_mdm).get("deliveries") or []
    for d in options:
        if d.get("service_id") == 1 and d.get("method_id") == 1:
            return f"{delivery_cost(d)}, {d.get('time') or 'строк н/д'}"
    priced = [d for d in options if isinstance((d.get("cost") or {}).get("new"), (int, float))]
    if not priced:
        return "недоступний"
    d = min(priced, key=lambda d: d["cost"]["new"])
    return f"недоступний; найдешевше: {d.get('title')} — {delivery_cost(d)}, {d.get('time') or 'строк н/д'}"


def cell(value, width):
    text = " ".join(str(value if value is not None else "—").split()).replace("|", "/")
    return text if not width or len(text) <= width else text[: width - 1] + "…"


# --- commands ---------------------------------------------------------------

def cmd_products(api, args, state):
    data, url, filters = fetch_listing(api, args)
    if args.json:
        return dump(data)
    goods = data["goods"]
    pag = data.get("paginator") or {}
    print(f"{(data.get('category') or {}).get('title') or data.get('h1')}  [{scope_label(filters)}]")
    print(url)
    atp_pages = pag.get("totalPagesWithAtpGoods")
    stock = f"in stock on pages 1–{atp_pages}" if atp_pages else "nothing in stock"
    print(f"{goods.get('totalQuantity')} products incl. unavailable, page {pag.get('shownPage')}/{pag.get('totalPages')} "
          f"({stock}), sort={(goods.get('sort') or {}).get('selected')}"
          + (" (this page re-sorted by unit price)" if args.sort_unit else ""))
    tiles = [t for t in goods.get("tiles") or [] if not t.get("adv")]  # sponsored tiles ignore the filters
    hidden = 0
    if not args.unavailable:
        # The site lists in-stock goods first and has no availability filter; hide the rest.
        kept = [t for t in tiles if t.get("isATP", True)]
        hidden, tiles = len(tiles) - len(kept), kept
    items = [from_tile(t, None) for t in tiles]
    if args.sort_unit:
        items = sort_by_unit(items)
    items = items[: args.limit]
    dets = details_map(api, [it["id"] for it in items])  # seller names and ratings aren't in the tiles
    for it in items:
        it.update(seller_rating(dets.get(it["id"])))
    offset = ((pag.get("shownPage") or 1) - 1) * 60
    for i, it in enumerate(items, 1):
        print_row(offset + i if not args.sort_unit else i, it)
    shown_total = len(tiles)
    if shown_total > len(items):
        print(f"… +{shown_total - len(items)} more on this page (--limit 60)")
    if hidden:
        print(f"({hidden} unavailable goods on this page hidden; --unavailable shows them)")


def cmd_filters(api, args, state):
    data, url, filters = fetch_listing(api, args)
    if args.json:
        return dump(data["filters"])
    print(f"{(data.get('category') or {}).get('title')}  [{scope_label(filters)}]")
    print(f"{url}\n{data['goods'].get('totalQuantity')} products")
    sort = data["goods"].get("sort") or {}
    print(f"\nСортування (--sort): {', '.join(o['key'] for o in sort.get('options') or [])}"
          f" (зараз {sort.get('selected')})")
    for f in data["filters"]["list"]:
        title = f["title"].strip()
        if f.get("type") == "slider":
            rng, sel = f.get("range") or {}, f.get("selected") or {}
            applied = sel and sel != rng
            print(f"\n{title} [{f['name']}, range: -f \"{f['name']}=MIN..MAX\"]: "
                  f"{fmt_num(rng.get('min', 0))}..{fmt_num(rng.get('max', 0))}"
                  + (f"  *applied {fmt_num(sel['min'])}..{fmt_num(sel['max'])}" if applied else ""))
            continue
        opts = f.get("options") or []
        shown = opts if args.all or len(opts) <= args.max_values else opts[: args.max_values]

        def label(o):
            mark = "*" if o.get("status") in ("selected", "intermediate") else ""
            qty = f" ({o['goodsQuantity']})" if o.get("goodsQuantity") else ""
            kids = (o.get("children") or {}).get("options") or []
            sub = f" [{', '.join(label(k) for k in kids)}]" if kids else ""
            return f"{mark}{o['title'].strip()}{qty}{sub}"

        vals = ", ".join(label(o) for o in shown)
        more = f" … +{len(opts) - len(shown)} more (--all)" if len(shown) < len(opts) else ""
        flag = {"producer": "--brand", "seller": "--seller", "price": "--price"}.get(f["name"], "-f")
        print(f"\n{title} [{f['name']}] ({flag}): {vals}{more}")
    print("\n* = applied. Use: -f \"<name or [key]>=<value>[|<value2>]\"; brands with --brand")


def cmd_search(api, args, state):
    filters = scope_filters(args)
    if args.price:
        filters["price"] = price_filter(args.price)
    # Search knows only the parent seller values.
    if filters.get("seller") and filters["seller"][0] not in ("rozetka", "other"):
        filters["seller"] = ["rozetka"]
    # A plain query first: only an unfiltered search reveals a redirect into a category
    # ("ноутбук asus" → /ua/notebooks/c80004/producer=asus/), and it carries the brand list.
    data = api.search(args.query)
    nav = ((data.get("meta") or {}).get("navigateTo") or {})
    if "quantities" not in data and nav.get("url") and not nav.get("isInternal"):
        target = nav["url"].split("#")[0]
        print(f"Search redirects to a category listing: {target}\n")
        args.category = target
        return cmd_products(api, args, state)
    if args.brand:
        producer = next((o for o in data.get("options") or [] if o.get("option_name") == "producer"), None)
        if not producer:
            die("search results have no brand filter")
        opts = [{"name": v["option_value_name"], "title": v["option_value_title"]}
                for v in producer.get("option_values") or []]
        filters["producer"] = [_match(opts, b, "brand")["name"] for b in args.brand]
    data = api.search(args.query, args.page, args.sort, filters)
    if args.json:
        return dump(data)
    missing = set(filters) - set(data.get("chosen") or {})
    if missing:
        # When the filters match nothing, the site silently returns the unfiltered results instead.
        print(f"0 results [{scope_label(filters)}]: the search dropped filter(s) {', '.join(sorted(missing))}, "
              "which means nothing matches them. Try a category listing, --seller any or --any-delivery.")
        return

    cats = []
    for top in (data.get("categories") or {}).get("list_categories") or []:
        cats += top.get("children") or []
    cats.sort(key=lambda c: -(c.get("count") or 0))
    if cats:
        print("Categories (use `rozetka products <id>`):")
        for c in cats[:12]:
            print(f"- {c['title']} [{c['id']}] ({c.get('count')})")
    q = data.get("quantities") or {}
    pag = data.get("pagination") or {}
    ids = [g["id"] for g in data.get("goods") or []]
    print(f"\n{q.get('goods_quantity_total_found')} results [{scope_label(filters)}], "
          f"page {pag.get('shown_page')}/{pag.get('total_pages')}:")
    dets = details_map(api, ids)
    items = [from_details(dets[i]) for i in ids if i in dets]
    hidden = 0
    if not args.unavailable:
        kept = [it for it in items if it["status"] in (None, "available", "limited")]
        hidden, items = len(items) - len(kept), kept
    if args.sort_unit:
        items = sort_by_unit(items)
    for i, it in enumerate(items[: args.limit], 1):
        print_row(i, it)
    if hidden:
        print(f"({hidden} unavailable goods on this page hidden; --unavailable shows them)")
    print("\nSearch has few filters; for specs open a category: `rozetka filters <id>`, `rozetka products <id>`.")


def print_variants(api, pd, current):
    options = (pd.get("varParams") or {}).get("options") or []
    ids = {v["product"]["id"] for o in options for v in o.get("values") or [] if v.get("product")}
    if len(ids) <= 1:
        return
    info = details_map(api, sorted(ids))
    print("\nВаріанти (→ поточний):")
    for o in options:
        print(f"  {o.get('title')}:")
        for v in o.get("values") or []:
            p = v.get("product") or {}
            d = info.get(p.get("id")) or {}
            mark = "→" if p.get("id") == current else " "
            parts = [price_text(d.get("price"), d.get("old_price")) if d else "ціна н/д"]
            if not p.get("isAvailable"):
                parts.append("немає в наявності")
            elif d.get("seller"):
                parts.append(f"продавець: {d['seller'].get('title')}")
            parts.append(f"id: {p.get('id')}")
            print(f"   {mark} {v.get('title')}: " + " | ".join(parts))


def spec_rows(pd):
    rows = []
    for g in pd.get("characteristics") or []:
        for o in g.get("options") or []:
            # Values may carry HTML: "Wi-Fi 802.11ax<br />Bluetooth 5.2", "<span …>1080p</span>".
            vals = ", ".join(html_to_text(str(v.get("title"))).replace("\n", ", ") for v in o.get("values") or [])
            rows.append((g.get("groupTitle") or "", o.get("title", "").strip(), vals))
    return rows


def cmd_product(api, args, state):
    try:
        gid = goods_id(args.product)
    except ValueError as e:
        die(str(e))
    pd = api.product_page(gid)
    if args.json:
        return dump(pd)
    p = pd["product"]
    det = details_map(api, [gid]).get(gid) or {}
    price = price_text(p.get("price"), p.get("old_price"))
    up = unit_price_text(with_pack(p.get("title"), page_pack(pd)), p.get("price"))
    print(p["title"])
    print(f"Ціна: {price}" + (f", {up}" if up else ""))
    seller = (det.get("seller") or {}).get("title")
    votes = (det.get("seller") or {}).get("count_votes")
    print(f"Бренд: {p.get('brand')} | Продавець: {seller}"
          + (f" ({votes} оцінок продавця)" if votes and p.get("seller_id") != ROZETKA_SELLER_ID else ""))
    print(f"Наявність: {SELL_STATUS.get(p.get('sell_status')) or p.get('sell_status')} | "
          f"Рейтинг: {det.get('comments_mark')} ({det.get('comments_amount')} відгуків)")
    cat = p.get("last_category") or {}
    print(f"Категорія: {cat.get('title')} [{cat.get('id')}]")
    print(f"URL: {p.get('href')}")
    print(f"id: {gid} | артикул: {p.get('article')}")

    if not args.no_delivery:
        mdm, title = city(state)
        try:
            line = delivery_line(api, gid, p.get("price"), mdm)
        except ApiError as e:
            line = f"н/д ({e})"
        print(f"Самовивіз з магазинів Rozetka ({title}): {line}")

    for w in p.get("warning") or []:
        print(f"! {html_to_text(w)}")

    if not args.no_variants:
        print_variants(api, pd, gid)

    offers = pd.get("otherSellersOffers") or []
    if offers:
        print(f"\nЦя ж модель в інших продавців ({len(offers)}; фільтр доставки тут не застосовано):")
        for o in sorted(offers, key=lambda o: o.get("price") or 0)[:10]:
            seller = o.get("seller") or {}
            seller = seller.get("title") if isinstance(seller, dict) else seller
            status = "" if o.get("sell_status") == "available" else f" | {SELL_STATUS.get(o.get('sell_status'), o.get('sell_status'))}"
            print(f"  - {price_text(o.get('price'), o.get('old_price'))} | продавець: {seller} | id: {o['id']}{status}")

    rows = spec_rows(pd)
    if rows:
        print("\nХарактеристики:")
        group = None
        for g, name, vals in rows:
            if g and g != group:
                print(f"  [{g}]")
            group = g
            print(f"  {name}: {vals}")

    desc = pd.get("description") or {}
    text = html_to_text(desc.get("text") or desc.get("html"))
    if text:
        if args.max_desc and len(text) > args.max_desc:
            text = text[: args.max_desc] + " …"
        print(f"\nОпис:\n{text}")

    docs = p.get("documents") or []
    if docs:
        print("\nДокументи: " + "; ".join(f"{d.get('title')}: {d.get('url')}" for d in docs))

    if args.reviews:
        rv = api.comments(gid, sort=args.review_sort)
        tot = rv.get("total_comments") or {}
        dist = ", ".join(f"{m}★ {tot.get(f'comment_count_marks_{m}', 0)}" for m in range(5, 0, -1))
        print(f"\nВідгуки ({tot.get('comment_count_comments', 0)}, середня {round(tot.get('comment_avg_marks') or 0, 2)}; "
              f"{dist}):")
        reviews = [c for c in rv.get("comments") or [] if c.get("type") == "comment"]  # the rest are questions
        for c in reviews[: args.reviews]:
            date = (c.get("commentdatajson") or {}).get("created_iso", "")[:10]
            buyer = " (купив)" if c.get("from_buyer") else ""
            mark = f"★{c['mark']} " if c.get("mark") else ""
            print(f"- {mark}{date} {c.get('usertitle')}{buyer}: {' '.join((c.get('text') or '').split())}")
            if c.get("dignity"):
                print(f"    + {' '.join(c['dignity'].split())}")
            if c.get("shortcomings"):
                print(f"    − {' '.join(c['shortcomings'].split())}")


def cmd_compare(api, args, state):
    try:
        ids = [goods_id(x) for x in args.products]
    except ValueError as e:
        die(str(e))
    pages = [api.product_page(i) for i in ids]  # sequential on purpose: it's a real shop's API
    if args.json:
        return dump(pages)
    dets = details_map(api, ids)
    prods = [pd["product"] for pd in pages]

    rows = [
        ("Назва", [p["title"] for p in prods]),
        ("Ціна", [price_text(p.get("price"), p.get("old_price")) for p in prods]),
        ("Ціна за од.", [unit_price_text(with_pack(p.get("title"), page_pack(pd)), p.get("price")) or "—"
                         for p, pd in zip(prods, pages)]),
        ("Бренд", [p.get("brand") for p in prods]),
        ("Продавець", [((dets.get(i) or {}).get("seller") or {}).get("title") for i in ids]),
        ("Рейтинг", [f"{(dets.get(i) or {}).get('comments_mark')} ({(dets.get(i) or {}).get('comments_amount')})"
                     for i in ids]),
        ("Наявність", [SELL_STATUS.get(p.get("sell_status")) or p.get("sell_status") or "в наявності"
                       for p in prods]),
    ]
    if args.delivery:
        mdm, title = city(state)
        rows.append((f"Самовивіз Rozetka ({title.split(' (')[0]})",
                     [delivery_line(api, i, p.get("price"), mdm) for i, p in zip(ids, prods)]))

    specs, order = {}, []
    for n, pd in enumerate(pages):
        for _, name, vals in spec_rows(pd):
            if name not in specs:
                specs[name] = [None] * len(pages)
                order.append(name)
            specs[name][n] = vals
    rows += [(name, specs[name]) for name in order]
    if args.diff:
        rows = rows[:1] + [r for r in rows[1:] if len({cell(v, 0) for v in r[1]}) > 1]

    w = args.width
    print("| | " + " | ".join(str(i) for i in range(1, len(ids) + 1)) + " |")
    print("|---" * (len(ids) + 1) + "|")
    for name, vals in rows:
        print(f"| {cell(name, 40)} | " + " | ".join(cell(v, w) for v in vals) + " |")
    print()
    for n, p in enumerate(prods, 1):
        print(f"{n}. id: {p['id']} — {p.get('href')}")
    if args.diff:
        print("(only rows that differ; drop --diff to see everything)")


def cmd_city(api, args, state):
    if args.action in (None, "show"):
        mdm, title = city(state)
        return print(f"City: {title} (mdm {mdm})")
    if not args.name:
        die(f"usage: rozetka city {args.action} <name>")
    records = api.locations(args.name)
    if args.json:
        return dump(records)
    if not records:
        die(f"no settlement matches '{args.name}'")
    if args.action == "find":
        for r in records:
            print(f"- {r.get('type', '')} {r['title']}  (id {r['id']})".replace("-  ", "- "))
        return print("\nSave one with: rozetka city set \"<name>\" [--pick N]")
    w = args.name.strip().casefold()
    exact = [r for r in records if r["title"].split(" - ")[0].casefold() == w]
    if args.pick:
        if not 1 <= args.pick <= len(records):
            die(f"--pick must be 1..{len(records)}")
        match = records[args.pick - 1]
    elif len(exact) == 1 or (exact and len(records) == 1):
        match = exact[0]
    elif len(records) == 1:
        match = records[0]
    else:
        print(f"'{args.name}' is ambiguous; pick one with --pick N:")
        for n, r in enumerate(records, 1):
            print(f"  {n}. {r.get('type', '')} {r['title']}")
        sys.exit(1)
    state.update(city_title=match["title"], city_mdm=match["mdm_id"], city_id=match["id"])
    save_state(state)
    print(f"Selected city: {match['title']}")


# --- argparse ---------------------------------------------------------------

def add_scope_args(p):
    p.add_argument("--seller", choices=SELLERS, default=None,
                   help="default rozetka (= Rozetka + goods handled by Rozetka); other, rozetka_only, "
                   "rozetka_fulfillment, rozetka_eu, or any")
    p.add_argument("--any-delivery", action="store_true",
                   help="don't require free delivery to Rozetka stores")
    p.add_argument("--brand", action="append", help="brand name, repeatable")
    p.add_argument("--price", help="MIN-MAX, MIN- or -MAX (UAH)")


def add_listing_args(p):
    p.add_argument("category", help="category id (152499), site URL (filters in it are kept), or /ua/<slug>/c<id>/")
    p.add_argument("-f", "--filter", action="append", metavar="KEY=VAL",
                   help="facet filter: KEY is the facet title or [key], VAL a value title (| for several, "
                   "MIN..MAX for sliders). Repeatable")
    add_scope_args(p)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="rozetka", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print raw API JSON instead of text")
    common.add_argument("--delay", type=float, nargs="?", const=DEFAULT_DELAY, metavar="SECONDS",
                        help=f"pause before every HTTP request, including the first one ({DEFAULT_DELAY}s when "
                        "given without a value). REQUIRED when running several commands in a row. "
                        "Env ROZETKA_DELAY sets the default")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("search", parents=[common], help="full-text search (may jump into a category listing)")
    p.add_argument("query")
    add_scope_args(p)
    p.add_argument("-f", "--filter", action="append", metavar="KEY=VAL",
                   help="only used when the search redirects into a category")
    p.add_argument("--sort", choices=SORTS)
    p.add_argument("--page", type=int)
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--sort-unit", action="store_true", help="re-sort results by price per kg/l/m²/piece")
    p.add_argument("--unavailable", action="store_true", help="also list goods that are out of stock")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("filters", parents=[common], help="list a category's facets with counts")
    add_listing_args(p)
    p.add_argument("--all", action="store_true", help="show every value")
    p.add_argument("--max-values", type=int, default=15)
    p.set_defaults(func=cmd_filters, sort=None, page=None)

    p = sub.add_parser("products", parents=[common], help="list products in a category with filters")
    add_listing_args(p)
    p.add_argument("--sort", choices=SORTS, help="rank (default), cheap, expensive, novelty, popularity, action")
    p.add_argument("--page", type=int, default=1, help="60 goods per page")
    p.add_argument("--limit", type=int, default=30, help="rows to print from the page (max 60)")
    p.add_argument("--sort-unit", action="store_true",
                   help="re-sort this page by price per kg/l/m²/piece (the API can't sort by it)")
    p.add_argument("--unavailable", action="store_true", help="also list goods that are out of stock")
    p.set_defaults(func=cmd_products)

    p = sub.add_parser("product", parents=[common], help="product details: price, delivery, variants, specs, reviews")
    p.add_argument("product", help="goods id or product URL")
    p.add_argument("--reviews", type=int, metavar="N", help="also show N reviews (up to ~30)")
    p.add_argument("--review-sort", default="date",
                   choices=["date", "from_buyer", "positive_vote_count", "with_attachments"],
                   help="date (default), from_buyer, positive_vote_count (most useful), with_attachments")
    p.add_argument("--max-desc", type=int, default=0, help="truncate description to N chars (0 = full)")
    p.add_argument("--no-variants", action="store_true", help="skip the variants list")
    p.add_argument("--no-delivery", action="store_true", help="skip the pickup term in your city")
    p.set_defaults(func=cmd_product)

    p = sub.add_parser("compare", parents=[common], help="side-by-side spec table of several products")
    p.add_argument("products", nargs="+", help="goods ids or URLs (2–6 is readable)")
    p.add_argument("--diff", action="store_true", help="show only rows where the products differ")
    p.add_argument("--delivery", action="store_true", help="add the Rozetka-store pickup term (1 request each)")
    p.add_argument("--width", type=int, default=0, help="truncate cells to N chars (0 = no limit)")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("city", parents=[common], help="show, find or set the city used for delivery terms")
    p.add_argument("action", nargs="?", choices=["show", "find", "set"])
    p.add_argument("name", nargs="?")
    p.add_argument("--pick", type=int, help="with set: take the N-th match")
    p.set_defaults(func=cmd_city)

    args = ap.parse_args(argv)
    if getattr(args, "limit", None):
        args.limit = min(args.limit, 60)
    delay = args.delay if args.delay is not None else float(os.environ.get("ROZETKA_DELAY") or 0)
    api = Rozetka(delay=delay)
    try:
        args.func(api, args, load_state())
    except ApiError as e:
        die(str(e))
    except requests.RequestException as e:
        die(f"network error: {e}")


if __name__ == "__main__":
    main()
