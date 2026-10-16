"""prom — search prom.ua from the command line (built for coding agents).

Listings show only goods in stock by default (--any-stock lifts it). Every row names the seller
and its minimum order sum: many prom.ua sellers won't ship an order below it, so `shop` exists to
search one seller's store and to top up a cart.

Run `prom <command> -h` for details. Typical flow:
  prom search "кришка для сковороди 32 см"             # goods + categories; filters with -f
  prom filters "кришка 32 см" --category 15230302      # facets of a search (or --shop ID)
  prom search "кришка 32 см" --category 15230302 --sort cheap -f "Бренд=Flonal|Kamille"
  prom product 3190588358 --offers --reviews 5        # details, delivery, same model elsewhere
  prom compare 3190588358 2918735064 --diff
  prom shop 4157633 --find "ніж"                      # search inside one seller's store
  prom shop --cart 3190588358 --fill                  # what else to buy there to reach the minimum

Running several commands in a row? Append --delay to each one (a pause before every request).
"""

import argparse
import json
import os
import re
import sys
import unicodedata

import requests

from client import PAGE_SIZE, ApiError, Prom, company_url, product_id, product_url, seller_ref

DEFAULT_DELAY = 1.5
SORTS = {"relevance": None, "cheap": "price", "expensive": "-price", "new": "-date_created"}


# --- helpers ----------------------------------------------------------------

def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def warn(msg):
    print(f"warning: {msg}", file=sys.stderr)


def dump(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def money(x):
    x = num(x)
    if x is None:
        return "?"
    return f"{int(x)}" if x.is_integer() else f"{x:.2f}"


def parse_range(text):
    lo, _, hi = text.partition("..") if ".." in text else text.partition("-")
    return (num(lo) if lo.strip() else None), (num(hi) if hi.strip() else None)


def fold(text):
    """Case- and accent-insensitive form: "Śnieżka" -> "sniezka"."""
    text = unicodedata.normalize("NFKD", str(text).strip().casefold())
    return "".join(c for c in text if not unicodedata.combining(c)).replace("ł", "l")


def _match(options, wanted, what, key="title"):
    """Pick one option by exact value, exact title, or unique substring of the title."""
    w = fold(wanted)
    for o in options:
        if fold(o.get("value", "")) == w or fold(o.get("key", "")) == w:
            return o
    exact = [o for o in options if fold(o.get(key, "")) == w]
    if exact:
        return exact[0]
    partial = [o for o in options if w in fold(o.get(key, ""))]
    if len(partial) == 1:
        return partial[0]
    names = ", ".join(str(o.get(key)).strip() for o in (partial or options)[:30])
    die(f"{what} '{wanted}' is {'ambiguous' if partial else 'not found'}; options: {names}")


def one_line(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- facets -----------------------------------------------------------------

def facets(filters):
    """All facet kinds as one list of {key, title, kind: multi|single|range, values|min/max}."""
    out = []
    f = filters or {}
    for a in f.get("attributeFilters") or []:
        if a.get("type") in ("real", "integer") and not a.get("values"):
            out.append({"key": a["name"], "title": a["title"].strip(), "kind": "range",
                        "min": a.get("min"), "max": a.get("max"), "unit": a.get("measureUnit")})
        else:
            out.append({"key": a["name"], "title": a["title"].strip(), "kind": "multi", "values": a["values"]})
    for name, title, kind in (("categoryFilter", "Категорія", "single"), ("productGroupFilter", "Група магазину", "single"),
                              ("deliveryFilter", "Доставка", "multi"), ("binaryFilters", "Додатково", "multi"),
                              ("opinionsFilter", "Відгуки", "multi")):
        x = f.get(name)
        if x and x.get("values"):
            out.append({"key": x["name"], "title": title, "kind": kind, "values": x["values"]})
    return out


def add_value(params, facet, value):
    v = str(value)
    if facet["kind"] == "single":
        params[facet["key"]] = v
    else:
        cur = params.setdefault(facet["key"], [])
        if v not in cur:
            cur.append(v)


def apply_cli_filters(params, fs, args):
    """Resolve -f / --brand / --delivery / --group against the facets of the current listing."""
    for spec in args.filter or []:
        key, sep, vals = spec.partition("=")
        if not sep:
            die(f"filter must look like NAME=VALUE[|VALUE2], got: {spec}")
        f = _match(fs, key, "filter")
        if f["kind"] == "range":
            lo, hi = parse_range(vals)
            if lo is not None:
                params[f"{f['key']}__gte"] = money(lo)
            if hi is not None:
                params[f"{f['key']}__lte"] = money(hi)
            continue
        for v in vals.split("|"):
            add_value(params, f, _match(f["values"], v, f"value of '{f['title']}'")["value"])
    for title, wanted in (("Бренд", args.brand), ("Доставка", getattr(args, "delivery", None))):
        if not wanted:
            continue
        f = next((x for x in fs if x["title"] == title), None)
        if not f:
            die(f"this listing has no '{title}' filter")
        for w in wanted:
            add_value(params, f, _match(f["values"], w, title.lower())["value"])
    if getattr(args, "group", None):
        f = next((x for x in fs if x["title"] == "Група магазину"), None)
        if not f:
            die("this listing has no store groups")
        add_value(params, f, _match(f["values"], args.group, "group")["value"])


def needs_resolution(args):
    return bool(args.filter or args.brand or getattr(args, "delivery", None) or getattr(args, "group", None))


def base_params(args):
    params = {}
    if not args.any_stock:
        params["binary_filters"] = ["presence_available"]
    if args.category:
        params["category"] = str(args.category)
    if args.price:
        lo, hi = parse_range(args.price)
        if lo is not None:
            params["price_local__gte"] = money(lo)
        if hi is not None:
            params["price_local__lte"] = money(hi)
    return params


def fetch_listing(api, args, text, company_id=None, params=None):
    """(listing, params). With text: search (inside one store when company_id is set); without:
    the store's whole catalogue. CLI filters are resolved against a first, unfiltered page."""
    params = base_params(args) if params is None else params
    sort = SORTS.get(args.sort) if args.sort else None

    def call(p, limit, offset, sort=sort):
        if text:
            return api.search(text, p, company_id, sort, limit, offset)
        return api.company_listing(company_id, p, sort, limit, offset)

    if needs_resolution(args):
        apply_cli_filters(params, facets(call(params, 1, 0).get("filters")), args)
    listing = call(params, args.limit, (args.page - 1) * args.limit)
    page = listing.get("page") or {}
    if sort and page.get("total") and not page.get("products"):
        # API quirk: a sort combined with a numeric-range facet returns the total but no goods.
        # Fetch unsorted (up to 90) and sort that slice locally.
        warn("the API returned no goods for this sort; sorting an unsorted slice of up to 90 locally")
        listing = call(params, 90, 0, sort=None)
        items = (listing.get("page") or {}).get("products") or []
        if sort.lstrip("-") == "price":
            items.sort(key=lambda it: num(it["product"].get("discountedPrice")) or 0, reverse=sort.startswith("-"))
        start = (args.page - 1) * args.limit
        listing["page"]["products"] = items[start:start + args.limit]
    check_applied(params, listing.get("filters"))
    return listing, params


def check_applied(params, filters):
    """Warn about filter values the API silently ignored (they come back unselected)."""
    selected = set()
    for f in facets(filters):
        selected |= {(f["key"], str(v["value"])) for v in f.get("values") or [] if v.get("selected")}
    for k, vals in params.items():
        if k.endswith(("__gte", "__lte")) or k == "binary_filters":
            continue
        for v in vals if isinstance(vals, list) else [vals]:
            if (k, str(v)) not in selected:
                warn(f"filter {k}={v} is not shown as applied; the results may ignore it")


# --- output -----------------------------------------------------------------

def price_text(p):
    price, disc = num(p.get("price")), num(p.get("discountedPrice"))
    if disc is None:
        return "ціна н/д"
    text = f"{money(disc)} ₴"
    if price and price > disc:
        text += f" (було {money(price)})"
    if p.get("measureUnit") and p["measureUnit"] != "шт.":
        text += f"/{p['measureUnit']}"
    return text


def company_text(c, with_id=True):
    st = c.get("opinionStats") or {}
    parts = [c.get("name") or "?"]
    if st.get("opinionTotal"):
        parts.append(f"{st.get('opinionPositivePercent')}% з {st['opinionTotal']} відгуків")
    if c.get("regionName"):
        parts.append(c["regionName"])
    text = parts[0] + (f" ({', '.join(parts[1:])})" if len(parts) > 1 else "")
    return text + (f" [продавець id {c['id']}]" if with_id else "")


def print_item(i, item, in_shop=False):
    p = item["product"]
    rc = p.get("productOpinionCounters") or {}
    rating = f" ★{rc['rating']} ({rc['count']})" if rc.get("count") else ""
    print(f"{i}. {p['name']} — {price_text(p)}{rating}")
    extra = [f"id: {p['id']}"]
    if not in_shop:
        extra.append(company_text(p.get("company") or {}))
        if p.get("minOrderSum"):
            extra.append(f"мін. замовлення {money(p['minOrderSum'])} ₴")
    pres = (p.get("catalogPresence") or {}).get("title")
    if pres and pres != "В наявності":
        extra.append(pres)
    for w in p.get("wholesalePrices") or []:
        extra.append(f"опт: від {w['minimumOrderQuantity']} шт по {money(w['price'])} ₴")
        break
    m = item.get("productModel") or {}
    if (m.get("company_count") or 0) > 1:
        extra.append(f"модель у {m['company_count']} продавців: {money(m['min_price'])}–{money(m['max_price'])} ₴")
    if "premium_adv" in ((item.get("advert") or {}).get("source") or ""):
        extra.append("реклама")
    print("   " + " | ".join(extra))
    print(f"   {product_url(p)}")


def print_listing(listing, args, in_shop=False):
    page = listing.get("page") or {}
    total = page.get("total") or 0
    pages = max(1, -(-total // args.limit))
    if page.get("correctedSearchTerm"):
        print(f"(запит виправлено на: {page['correctedSearchTerm']})")
    print(f"{total} products{'' if args.any_stock else ' in stock'}, page {args.page}/{pages}"
          + (f", sort={args.sort}" if args.sort else ""))
    items = page.get("products") or []
    for i, item in enumerate(items, (args.page - 1) * args.limit + 1):
        print_item(i, item, in_shop)
    if not items:
        print("(nothing found)")


def print_categories(filters, limit=10):
    cat = next((f for f in facets(filters) if f["key"] == "category"), None)
    if cat:
        vals = sorted(cat["values"], key=lambda v: -(v.get("count") or 0))[:limit]
        print("Categories (--category ID): " + ", ".join(
            f"{'*' if v.get('selected') else ''}{v['title'].strip()} [{v['value']}] ({v.get('count')})" for v in vals))


# --- commands ---------------------------------------------------------------

def cmd_search(api, args, state):
    listing, _ = fetch_listing(api, args, args.query)
    if args.json:
        return dump(listing)
    print_categories(listing.get("filters"))
    print_listing(listing, args)


def cmd_filters(api, args, state):
    if not args.query and not args.shop:
        die("give a search query, --shop ID, or both")
    company_id = resolve_company(api, args.shop) if args.shop else None
    listing, _ = fetch_listing(api, args, args.query, company_id)
    if args.json:
        return dump(listing.get("filters"))
    f = listing.get("filters") or {}
    print(f"{f.get('total')} products" + ("" if args.any_stock else " in stock"))
    chart = (f.get("priceChartFilter") or {}).get("values") or []
    if chart:
        print(f"\nЦіна (--price MIN-MAX): {money(chart[0][0])}..{money(chart[-1][0])} ₴")
    print(f"Сортування (--sort): {', '.join(SORTS)}")
    for fc in facets(f):
        if fc["kind"] == "range":
            print(f"\n{fc['title']} [{fc['key']}, range: -f \"{fc['title']}=MIN..MAX\"]: "
                  f"{fc['min']}..{fc['max']} {fc.get('unit') or ''}".rstrip())
            continue
        vals = fc["values"]
        if fc["key"] == "category":
            vals = sorted(vals, key=lambda v: -(v.get("count") or 0))
        shown = vals if args.all or len(vals) <= args.max_values else vals[: args.max_values]
        text = ", ".join(f"{'*' if v.get('selected') else ''}{v['title'].strip()}"
                         + (f" [{v['value']}]" if fc["key"] in ("category", "product_group") else "")
                         + (f" ({v['count']})" if v.get("count") else "") for v in shown)
        more = f" … +{len(vals) - len(shown)} more (--all)" if len(shown) < len(vals) else ""
        flag = {"Бренд": "--brand", "Доставка": "--delivery", "Категорія": "--category",
                "Група магазину": "--group"}.get(fc["title"], "-f")
        print(f"\n{fc['title']} [{fc['key']}] ({flag}): {text}{more}")
    print("\n* = applied. Use: -f \"<facet title>=<value>[|<value2>]\"")


def resolve_company(api, ref):
    try:
        kind, ident = seller_ref(ref)
    except ValueError as e:
        die(str(e))
    if kind == "company":
        return ident
    product = api.product(ident).get("product")
    if not product:
        die(f"product {ident} not found")
    return product["company"]["id"]


def parse_cart(specs):
    """['3190588358', '123x2', 'https://prom.ua/ua/p123-x.html'] -> [(id, qty)]."""
    out = []
    for spec in specs:
        for part in spec.split(","):
            ref, _, qty = part.strip().rpartition("x") if re.search(r"x\d+$", part.strip()) else (part.strip(), "", "1")
            try:
                out.append((product_id(ref), int(qty)))
            except ValueError as e:
                die(str(e))
    return out


def print_company(c, min_sum):
    print(f"{company_text(c, with_id=False)} — {company_url(c)}")
    facts = [f"id {c['id']}"]
    if c.get("ageYears"):
        facts.append(f"на prom.ua {c['ageYears']} р.")
    if c.get("deliveredOrdersCategoryText"):
        facts.append(c["deliveredOrdersCategoryText"])
    if c.get("lastActivityTime"):
        facts.append(f"активність: {c['lastActivityTime']}")
    if c.get("phone"):
        facts.append(c["phone"])
    if c.get("isStatusBad") or c.get("isOperating") is False:
        facts.append("⚠ магазин неактивний")
    print("   " + " | ".join(facts))
    print(f"   Мінімальна сума замовлення: {money(min_sum) + ' ₴' if min_sum else 'немає'}")
    if c.get("description"):
        print(f"   {one_line(c['description'], 300)}")


def cmd_shop(api, args, state):
    cart = parse_cart(args.cart or [])
    cart_items = {}
    if cart:
        cart_items = {it["product"]["id"]: it for it in api.items([i for i, _ in cart])}
        missing = [i for i, _ in cart if i not in cart_items]
        if missing:
            die(f"product(s) not found: {', '.join(map(str, missing))}")
    if args.seller:
        company_id = resolve_company(api, args.seller)
    elif cart:
        company_id = cart_items[cart[0][0]]["product"]["company"]["id"]
    else:
        die("give a seller (company id, company/product URL) or --cart")
    others = {cart_items[i]["product"]["company"]["name"] for i, _ in cart
              if cart_items[i]["product"]["company"]["id"] != company_id}
    if others:
        warn(f"cart items from other sellers ignored: {', '.join(others)}")

    company = api.company(company_id)
    if not company:
        die(f"company {company_id} not found")

    total = sum(num(cart_items[i]["product"]["discountedPrice"]) * q for i, q in cart
                if cart_items[i]["product"]["company"]["id"] == company_id)
    min_sum = next((cart_items[i]["product"].get("minOrderSum") for i, _ in cart
                    if cart_items[i]["product"]["company"]["id"] == company_id), None)
    gap = 0

    params = base_params(args)
    if args.fill:
        if not cart:
            die("--fill needs --cart")
        if min_sum is None:
            min_sum = 0
        gap = max(0.0, (min_sum or 0) - total)
        if gap and not args.price:
            params["price_local__gte"] = money(gap)
        if not args.sort:
            args.sort = "cheap"
    listing, _ = fetch_listing(api, args, args.find or "", company_id, params)
    if min_sum is None:
        first = next(iter((listing.get("page") or {}).get("products") or []), None)
        min_sum = first["product"].get("minOrderSum") if first else None
    if args.json:
        return dump({"company": company, "listing": listing})

    print_company(company, min_sum)
    if cart:
        print("\nКошик:")
        for i, q in cart:
            p = cart_items[i]["product"]
            if p["company"]["id"] != company_id:
                continue
            print(f"  - {q} × {p['name']} — {money(num(p['discountedPrice']) * q)} ₴ ({product_url(p)})")
        gap = max(0.0, (min_sum or 0) - total)
        print(f"  Разом: {money(total)} ₴; мінімум: {money(min_sum or 0)} ₴; "
              + (f"бракує {money(gap)} ₴" if gap else "мінімальну суму досягнуто"))
        if args.fill and gap:
            print(f"  (нижче — товари від {money(gap)} ₴, які самі закривають різницю; "
                  "дешевші можна набрати кількома: --price -<сума>)")
    print(f"\n{'Пошук «' + args.find + '» у магазині' if args.find else 'Каталог магазину'}:")
    print_listing(listing, args, in_shop=True)


def cmd_product(api, args, state):
    try:
        pid = product_id(args.product)
    except ValueError as e:
        die(str(e))
    data = api.product(pid)
    p = data.get("product")
    if not p:
        die(f"product {pid} not found")
    if args.json:
        return dump(data)
    c = p.get("company") or {}
    rc = p.get("productOpinionCounters") or {}
    print(p["name"])
    print(f"Ціна: {price_text(p)}" + (f" | артикул {p['sku']}" if p.get("sku") else ""))
    for w in p.get("wholesalePrices") or []:
        print(f"  опт: від {w['minimumOrderQuantity']} шт — {money(w['price'])} ₴")
    print(f"Наявність: {(p.get('catalogPresence') or {}).get('title')} | "
          f"Відгуки на товар: {rc.get('rating') or '—'} ({rc.get('count') or 0})")
    print(f"Продавець: {company_text(c)} — {company_url(c)}")
    print(f"Мінімальна сума замовлення: {money(p['minOrderSum']) + ' ₴' if p.get('minOrderSum') else 'немає'}"
          + (f" (докупити в цьому магазині: prom shop --cart {pid} --fill)" if p.get("minOrderSum")
             and (num(p.get("discountedPrice")) or 0) < p["minOrderSum"] else ""))
    print(f"URL: {product_url(p)}")
    bb = p.get("buyBox") or {}
    if (bb.get("companyCount") or 0) > 1:
        print(f"Ця модель у {bb['companyCount']} продавців: {money(bb['minPrice'])}–{money(bb['maxPrice'])} ₴"
              + ("" if args.offers else " (--offers покаже їх)"))

    if p.get("deliveryOptions"):
        print("\nДоставка:")
        for d in p["deliveryOptions"]:
            comment = one_line(d.get("comment"), 160)
            print(f"  - {d['name']}" + (f": {comment}" if comment else ""))
    if p.get("paymentOptions"):
        print("Оплата: " + ", ".join(o["name"] for o in p["paymentOptions"]))

    variations = data.get("variationItems") or []
    if len(variations) > 1:
        print("\nВаріанти:")
        for v in variations:
            vp = ((v.get("productItem") or {}).get("product") or {})
            label = ", ".join(f"{a['name']}: {'/'.join(x['value'] for x in a['values'])}" for a in v.get("attributes") or [])
            mark = "→" if vp.get("id") == pid else " "
            avail = "" if (vp.get("presence") or {}).get("isAvailable") else " | немає"
            print(f"  {mark} {label} — {money(vp.get('discountedPrice'))} ₴{avail} | id {vp.get('id')}")

    if args.offers:
        print_offers(api, p)

    groups = {}
    for a in p.get("attributes") or []:
        groups.setdefault(a.get("group") or "", []).append(a)
    if groups:
        print("\nХарактеристики:")
        for group, attrs in groups.items():
            if group:
                print(f"  [{group}]")
            for a in attrs:
                print(f"  {a['name']}: {attr_values(a)}")

    desc = (p.get("descriptionPlain") or "").strip()
    if desc:
        if args.max_desc and len(desc) > args.max_desc:
            desc = desc[: args.max_desc] + " …"
        print(f"\nОпис:\n{desc}")

    print(f"\nВідгуки про продавця: {c.get('companyOpinionsUrl')}")
    if args.reviews:
        ops = ((data.get("productOpinionOnProductCardListing") or {}).get("opinions") or [])[: args.reviews]
        print(f"Відгуки на товар ({rc.get('count') or 0}; показано {len(ops)}):")
        for o in ops:
            buyer = f" ({o['fromBuyerText']})" if o.get("fromBuyerText") else ""
            print(f"- ★{o.get('rating')} {str(o.get('dateCreated') or '')[:10]} {o.get('authorName')}{buyer}: "
                  f"{one_line(o.get('text'), 600)}")
            if o.get("pros"):
                print(f"    + {one_line(o['pros'], 300)}")
            if o.get("cons"):
                print(f"    − {one_line(o['cons'], 300)}")


def attr_values(a):
    return ", ".join({"True": "так", "False": "ні"}.get(v["value"], v["value"]) for v in a["values"])


def print_offers(api, p):
    """Other sellers of the same model: found by searching the product's name (the API has no
    direct "offers of a model" query), then fetched by id."""
    model_id = p.get("newModelId") or (p.get("buyBox") or {}).get("modelId")
    listing = api.search(p["name"][:120], {}, limit=60)
    ids = next((it["productModel"]["model_product_ids"] for it in (listing.get("page") or {}).get("products") or []
                if (it.get("productModel") or {}).get("model_id") == model_id), None)
    if not ids:
        print("\nІнші продавці моделі: не вдалося знайти через пошук.")
        return
    items = sorted(api.items(ids), key=lambda it: num(it["product"].get("discountedPrice")) or 1e12)
    print(f"\nЦя модель в інших продавців ({len(items)}):")
    for it in items:
        q = it["product"]
        mark = "→" if q["id"] == p["id"] else "-"
        minimum = f" | мін. замовлення {money(q['minOrderSum'])} ₴" if q.get("minOrderSum") else ""
        avail = "" if (q.get("presence") or {}).get("isAvailable") else " | немає в наявності"
        print(f"  {mark} {price_text(q)} | {company_text(q.get('company') or {})}{minimum}{avail}")
        print(f"      {product_url(q)}")


def cmd_compare(api, args, state):
    try:
        ids = [product_id(x) for x in args.products]
    except ValueError as e:
        die(str(e))
    datas = [api.product(i) for i in ids]  # sequential on purpose: it's a real marketplace's API
    if args.json:
        return dump(datas)
    prods = [d["product"] for d in datas]

    def cell(value, width=args.width):
        text = " ".join(str(value if value not in (None, "") else "—").split()).replace("|", "/")
        return text if not width or len(text) <= width else text[: width - 1] + "…"

    rows = [
        ("Назва", [p["name"] for p in prods]),
        ("Ціна", [price_text(p) for p in prods]),
        ("Продавець", [company_text(p.get("company") or {}, with_id=False) for p in prods]),
        ("Мін. замовлення", [f"{money(p['minOrderSum'])} ₴" if p.get("minOrderSum") else "немає" for p in prods]),
        ("Наявність", [(p.get("catalogPresence") or {}).get("title") for p in prods]),
        ("Відгуки на товар", [f"{(p.get('productOpinionCounters') or {}).get('rating') or '—'} "
                              f"({(p.get('productOpinionCounters') or {}).get('count') or 0})" for p in prods]),
        ("Доставка", [", ".join(d["name"] for d in p.get("deliveryOptions") or []) for p in prods]),
    ]
    specs, order = {}, []
    for n, p in enumerate(prods):
        for a in p.get("attributes") or []:
            name = a["name"].strip()
            if name not in specs:
                specs[name] = [None] * len(prods)
                order.append(name)
            specs[name][n] = attr_values(a)
    rows += [(name, specs[name]) for name in order]
    if args.diff:
        rows = rows[:1] + [r for r in rows[1:] if len({cell(v, 0) for v in r[1]}) > 1]
    print("| | " + " | ".join(str(i) for i in range(1, len(ids) + 1)) + " |")
    print("|---" * (len(ids) + 1) + "|")
    for name, vals in rows:
        print(f"| {cell(name, 40)} | " + " | ".join(cell(v) for v in vals) + " |")
    print()
    for n, p in enumerate(prods, 1):
        print(f"{n}. {product_url(p)}")
    if args.diff:
        print("(only rows that differ; drop --diff to see everything)")


# --- argparse ---------------------------------------------------------------

def add_listing_args(p, sort=True):
    p.add_argument("-f", "--filter", action="append", metavar="NAME=VAL",
                   help="facet filter: NAME is the facet title (see `filters`), VAL a value title "
                   "(| for several, MIN..MAX for numeric facets). Repeatable")
    p.add_argument("--brand", action="append", help="brand, repeatable")
    p.add_argument("--delivery", action="append", help="delivery method, e.g. 'Rozetka', 'Нова Пошта', 'Самовивіз'")
    p.add_argument("--price", help="MIN-MAX, MIN- or -MAX (UAH)")
    p.add_argument("--category", type=int, help="category id (from the Categories line / `filters`)")
    p.add_argument("--any-stock", action="store_true", help="also list goods that aren't in stock")
    if sort:
        p.add_argument("--sort", choices=list(SORTS), help="relevance (default), cheap, expensive, new")
        p.add_argument("--page", type=int, default=1)
        p.add_argument("--limit", type=int, default=PAGE_SIZE, help=f"rows per page (default {PAGE_SIZE}, max 90)")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="prom", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print raw API JSON instead of text")
    common.add_argument("--delay", type=float, nargs="?", const=DEFAULT_DELAY, metavar="SECONDS",
                        help=f"pause before every HTTP request, including the first one ({DEFAULT_DELAY}s when "
                        "given without a value). REQUIRED when running several commands in a row. "
                        "Env PROM_DELAY sets the default")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("search", parents=[common], help="search all sellers")
    p.add_argument("query")
    add_listing_args(p)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("filters", parents=[common], help="facets of a search or of one seller's store")
    p.add_argument("query", nargs="?", default="")
    p.add_argument("--shop", help="seller (company id, company/product URL)")
    p.add_argument("--group", help="store group (with --shop)")
    add_listing_args(p, sort=False)
    p.add_argument("--all", action="store_true", help="show every value")
    p.add_argument("--max-values", type=int, default=15)
    p.set_defaults(func=cmd_filters, sort=None, page=1, limit=1)

    p = sub.add_parser("shop", parents=[common], help="one seller: info, minimum order, search/browse its store, top up a cart")
    p.add_argument("seller", nargs="?", help="company id, company page URL, cs<id>.prom.ua, or a product URL/id of that seller "
                   "(product: use its URL or p<id>)")
    p.add_argument("--find", metavar="QUERY", help="search inside the store (default: browse the whole catalogue)")
    p.add_argument("--group", help="store group title or id (see `filters --shop`)")
    p.add_argument("--cart", action="append", metavar="ID[xQTY]",
                   help="product(s) you plan to buy there (id, id x qty like 123x2, or URL; repeatable or comma-separated): "
                   "prints the total and how much is missing to the minimum order")
    p.add_argument("--fill", action="store_true",
                   help="with --cart: list goods priced at least the missing amount, cheapest first")
    add_listing_args(p)
    p.set_defaults(func=cmd_shop)

    p = sub.add_parser("product", parents=[common], help="details: price, seller, minimum order, delivery, specs, reviews")
    p.add_argument("product", help="product id or URL")
    p.add_argument("--offers", action="store_true", help="list other sellers of the same model (2–3 extra requests)")
    p.add_argument("--reviews", type=int, metavar="N", help="also show up to N product reviews")
    p.add_argument("--max-desc", type=int, default=1500, help="truncate description to N chars (0 = full)")
    p.set_defaults(func=cmd_product)

    p = sub.add_parser("compare", parents=[common], help="side-by-side table of several products")
    p.add_argument("products", nargs="+", help="product ids or URLs (2–6 is readable)")
    p.add_argument("--diff", action="store_true", help="show only rows where the products differ")
    p.add_argument("--width", type=int, default=0, help="truncate cells to N chars (0 = no limit)")
    p.set_defaults(func=cmd_compare)

    args = ap.parse_args(argv)
    if getattr(args, "limit", None):
        args.limit = max(1, min(args.limit, 90))
    delay = args.delay if args.delay is not None else float(os.environ.get("PROM_DELAY") or 0)
    api = Prom(delay=delay)
    try:
        args.func(api, args, {})
    except ApiError as e:
        die(str(e))
    except requests.RequestException as e:
        die(f"network error: {e}")


if __name__ == "__main__":
    main()
