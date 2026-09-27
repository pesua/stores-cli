"""aliexpress — search AliExpress from the command line (built for coding agents). Ship-to Ukraine, UAH.

Run `aliexpress <command> -h` for details. Typical flow:
  aliexpress categories "usb c cable"                  # which categories a query lands in (ids)
  aliexpress filters "usb c cable" --cat 100000356     # brands, attributes, ship-from, price bands
  aliexpress products "usb c cable" --cat 100000356 --brand UGREEN --sort orders --clean
  aliexpress product 1005004750348366 --reviews 10     # variants, bait flags, store, shipping, reviews
  aliexpress compare <id1> <id2> [<id3> ...] --diff
  aliexpress store <product-id>                        # seller profile; store-products lists its range
  aliexpress stores --brand UGREEN                     # which stores sell the brand (official first)
  aliexpress orders --find "кабель"                    # your purchase history (cached locally)

Every command waits 2 s before each HTTP request (--delay N to change). Never run them in parallel.
"""

import argparse
import datetime
import json
import os
import re
import sys
import time
from pathlib import Path

from bs4 import BeautifulSoup

import orders_db
from bait import card_flags, popular_variants, product_flags, score
from client import (CACHE, CURRENCY, AliExpress, ApiError, CaptchaError, LoginRequired, product_id,
                    read_env_cookie)
from curl_cffi.requests.exceptions import RequestException
from parse import (facets, order_detail, price_stats, product, reviews, search_page, sku_label,
                   store_item)

STATE_FILE = Path(__file__).with_name(".state.json")
CATEGORY_CACHE = CACHE / "categories.json"
STORE_CACHE = CACHE / "stores.json"
FACET_DIR = CACHE / "facets"
BRAND_CACHE = CACHE / "brands.json"
FACET_TTL = 86400
BRAND_TTL = 7 * 86400
ORDERS_TTL = 12 * 3600
DEFAULT_DELAY = 2.0
ACCOUNT_DAILY_LIMIT = int(os.environ.get("ALIEXPRESS_ACCOUNT_DAILY_LIMIT") or 80)
SORTS = {"best": None, "orders": "total_tranpro_desc", "price_asc": "price_asc", "price_desc": "price_desc"}
STORE_SORTS = {"best": "bestmatch_sort", "orders": "orders_desc", "new": "new_desc",
               "price_asc": "price_asc", "price_desc": "price_desc"}  # values from the store page's JS
STORE_FILTERS = {"free_ship": "free_ship", "top": "topSelling", "sale": "sale", "new": "newArrivals"}
SWITCHES = {"choice": "filterCode:choice_atm", "free_ship": "filterCode:freeshipping",
            "four_stars": "filterCode:4StarRating"}
SYM = "грн" if CURRENCY == "UAH" else CURRENCY


# --- helpers ----------------------------------------------------------------

def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def dump(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return default


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1))


def money(x):
    if x is None:
        return "—"
    return f"{x:,.2f}".replace(",", " ").rstrip("0").rstrip(".") if x < 100 else f"{round(x):,}".replace(",", " ")


def html_to_text(html):
    if not html:
        return ""
    text = BeautifulSoup(html, "html.parser").get_text("\n")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def cell(value, width):
    text = " ".join(str(value if value is not None else "—").split()).replace("|", "/")
    return text if not width or len(text) <= width else text[: width - 1] + "…"


def _match(options, wanted, what):
    """Pick one option by exact value, exact text, or unique substring of text (case-insensitive)."""
    w = wanted.strip().casefold()
    for o in options:
        if str(o["value"]).casefold() == w:
            return o
    exact = [o for o in options if o["text"].strip().casefold() == w]
    if exact:
        return exact[0]
    partial = [o for o in options if w in o["text"].casefold()]
    if len(partial) == 1:
        return partial[0]
    names = ", ".join(o["text"] for o in (partial or options)[:30])
    die(f"{what} '{wanted}' is {'ambiguous' if partial else 'not found'}; options: {names}")


# --- account budget -----------------------------------------------------------

def load_state():
    return load_json(STATE_FILE, {})


def make_budget():
    def charge():
        state = load_state()
        today = datetime.date.today().isoformat()
        calls = state.get("account_calls") or {}
        n = calls.get(today, 0)
        if n >= ACCOUNT_DAILY_LIMIT:
            raise ApiError(f"daily limit of {ACCOUNT_DAILY_LIMIT} account requests reached "
                           "(ALIEXPRESS_ACCOUNT_DAILY_LIMIT); cached data still works with --offline")
        state["account_calls"] = {today: n + 1}
        save_json(STATE_FILE, state)
    return charge


class Ctx:
    """Lazily built HTTP sessions: anonymous for browsing, account only when a command needs it.
    Both fall back to a real browser (browser.BrowserMtop) when AliExpress answers with captchas."""

    def __init__(self, delay, no_browser=False):
        self.delay = delay
        self.no_browser = no_browser
        self._anon = self._acct = None

    def _browser_factory(self, account):
        import browser
        return browser.BrowserMtop(account, allow_window=not self.no_browser)

    @property
    def anon(self):
        if not self._anon:
            self._anon = AliExpress(delay=self.delay, browser_factory=self._browser_factory)
        return self._anon

    @property
    def account(self):
        if not self._acct:
            self._acct = AliExpress(account=True, delay=self.delay, budget=make_budget(),
                                    browser_factory=self._browser_factory)
        return self._acct

    def api(self, as_me=False):
        return self.account if as_me else self.anon

    def close(self):
        for s in (self._anon, self._acct):
            if s:
                s.close()


# --- caches -----------------------------------------------------------------

def remember_categories(fs, query):
    cats = load_json(CATEGORY_CACHE, {})
    for g in fs:
        if g["type"] != "category":
            continue
        for v in g["values"]:
            c = cats.setdefault(str(v["value"]), {"name": v["text"], "queries": []})
            c["name"] = v["text"]
            if query and query not in c["queries"]:
                c["queries"] = (c["queries"] + [query])[-8:]
            c["seen"] = time.time()
    save_json(CATEGORY_CACHE, cats)


def remember_store(st):
    if not st.get("shop_id"):
        return
    stores = load_json(STORE_CACHE, {})
    stores[str(st["shop_id"])] = {**st, "seen": time.time()}
    save_json(STORE_CACHE, stores)


def find_store(ref):
    """Store profile from the cache by shop id, store-URL number or name substring."""
    stores = load_json(STORE_CACHE, {})
    m = re.search(r"/store/(\d+)", ref)
    key = m.group(1) if m else ref.strip()
    for st in stores.values():
        if key in (str(st.get("shop_id")), str(st.get("url_num")), str(st.get("seller_id"))):
            return st
    hits = [st for st in stores.values() if key.casefold() in (st.get("name") or "").casefold()]
    return hits[0] if len(hits) == 1 else None


def facet_cache_path(query, cat):
    slug = re.sub(r"[^a-z0-9]+", "-", (query or "").lower()).strip("-")
    return FACET_DIR / f"{slug or '_'}__{cat or 'all'}.json"


def get_facets(api, query, cat):
    path = facet_cache_path(query, cat)
    cached = load_json(path, None)
    if cached and time.time() - cached["built"] < FACET_TTL:
        return cached["facets"]
    fields = api.search(query, {"postCatIds": cat})
    fs = facets(fields)
    save_json(path, {"built": time.time(), "facets": fs})
    remember_categories(fs, query)
    return fs


def purchases_db():
    try:
        return orders_db.connect() if orders_db.DB.exists() else None
    except Exception:
        return None


def bought_note(db, pid):
    if not db:
        return ""
    rows = orders_db.bought(db, pid)
    if not rows:
        return ""
    r = rows[0]
    attrs = ", ".join(v for n, v in json.loads(r["attrs"] or "[]") if n != "Ships From")
    more = f" (+{len(rows) - 1})" if len(rows) > 1 else ""
    return f"вже купував {r['date']}: {attrs or '—'}, {money(r['price'])} {r['currency'] or ''}{more}"


# --- search ---------------------------------------------------------------------

def build_search_params(api, args):
    params = {"postCatIds": args.cat, "SortType": SORTS.get(args.sort), "page": args.page if args.page > 1 else None,
              "shpf_co": args.ship_from, "pr": args.price}
    switches = [SWITCHES[k] for k in ("choice", "free_ship", "four_stars") if getattr(args, k, False)]
    if switches:
        params["selectedSwitches"] = ",".join(switches)
    specs = list(args.filter or []) + [f"Brand={b}" for b in args.brand or []]
    attrs = []
    fs = None
    for spec in specs:
        if re.fullmatch(r"\d+-\d+", spec.strip()):
            attrs.append(spec.strip())
            continue
        key, sep, vals = spec.partition("=")
        if not sep:
            die(f"filter must look like NAME=VALUE[|VALUE2] or a raw id like 2250-4169245, got: {spec}")
        fs = fs or get_facets(api, args.query, args.cat)
        groups = [g for g in fs if g["type"] == "attribute"]
        g = _match([{"text": x["title"], "value": x["title"], "g": x} for x in groups], key, "filter")["g"]
        for v in vals.split("|"):
            attrs.append(_match(g["values"], v, f"value of '{g['title']}'")["value"])
    if attrs:
        params["attr"] = ";".join(attrs)
    return params


def print_card(i, c, flags, sc, note=""):
    price = f"{money(c['price'])} {SYM}"
    if c.get("orig") and c.get("price") and c["orig"] > c["price"] * 1.01:
        price += f" (було {money(c['orig'])})"
    rating = f" ★{c['rating']}" if c.get("rating") else ""
    if c.get("reviews"):
        rating += f" ({c['reviews']})"
    print(f"{i}. {c['title']} — {price}{rating} | {c.get('sold_text') or '0 sold'} | score {sc}")
    extra = [f"id: {c['id']}"] + ["Choice"] * bool(c.get("choice")) + [f"⚠ {f}" for f in flags]
    if note:
        extra.append(f"✓ {note}")
    print("   " + " | ".join(extra))


def cmd_products(ctx, args):
    if not args.query and not args.cat:
        die("give a search query and/or --cat ID")
    api = ctx.api(args.as_me)
    params = build_search_params(ctx.anon, args)
    fields = api.search(args.query or "", params)
    if args.json:
        return dump(fields)
    remember_categories(facets(fields), args.query)
    res = search_page(fields)
    db = purchases_db()
    rows = []
    for c in res["items"]:
        flags = card_flags(c)
        rows.append((c, flags, score(c, flags)))
    if args.clean:
        rows = [r for r in rows if not [f for f in r[1] if not f.startswith(("ad", "pack of", "new-user"))]]
    if args.sort_score:
        rows.sort(key=lambda r: -r[2])
    scope = " ".join(f"{k}={v}" for k, v in params.items() if v)
    print(f"'{args.query}' {scope}".strip())
    print(f"{res['total']} results, page {res['page']}, {len(rows)} shown"
          + (" (thin result: padding items dropped)" if res["padded"] else "")
          + (" [--clean: bait-flagged items hidden]" if args.clean else "")
          + ("" if args.as_me else "; prices as a guest — new-user deals are flagged"))
    offset = (args.page - 1) * (res["page_size"] or 60)
    for i, (c, flags, sc) in enumerate(rows, offset + 1):
        print_card(i, c, flags, sc, bought_note(db, c["id"]))


def cmd_filters(ctx, args):
    fs = get_facets(ctx.anon, args.query, args.cat)
    if args.json:
        return dump(fs)
    for g in fs:
        if g["type"] == "views":
            continue
        how = {"attribute": "-f 'NAME=VALUE'", "category": "--cat ID", "logistics": "--ship-from CODE",
               "priceRange": "--price MIN-MAX", "featureSwitchGroup": "--choice / --free-ship / --four-stars"}
        print(f"{g['title']}  [{how.get(g['type'], g['param'])}]")
        vals = g["values"] if args.all else g["values"][: args.max_values]
        for v in vals:
            note = f" ({v['note']})" if v.get("note") else ""
            print(f"  {v['text']} = {v['value']}{note}")
        if len(g["values"]) > len(vals):
            print(f"  … {len(g['values']) - len(vals)} more (--all)")


def cmd_categories(ctx, args):
    if args.query:
        fs = get_facets(ctx.anon, args.query, None)
        cats = [v for g in fs if g["type"] == "category" for v in g["values"]]
        if args.json:
            return dump(cats)
        print(f"Categories for '{args.query}' (use --cat ID):")
        for v in cats:
            print(f"  {v['value']}: {v['text']}")
        if not cats:
            print("  (none suggested; try a more specific query)")
        return
    cats = load_json(CATEGORY_CACHE, {})
    items = sorted(cats.items(), key=lambda kv: kv[1]["name"])
    if args.find:
        words = args.find.casefold().split()
        items = [(k, c) for k, c in items
                 if all(w in (c["name"] + " " + " ".join(c["queries"])).casefold() for w in words)]
    if args.json:
        return dump(dict(items))
    print(f"{len(items)} known categories (learnt from earlier searches; `categories \"query\"` finds more):")
    for k, c in items[: args.limit]:
        print(f"  {k}: {c['name']}  ← {', '.join(c['queries'][-3:])}")


# --- product ----------------------------------------------------------------------

def fetch_product(ctx, pid, as_me=False):
    raw = ctx.api(as_me).product(pid)
    p = product(raw)
    remember_store(p["store"])
    return raw, p


def print_skus(p, limit):
    skus = sorted(p["skus"], key=lambda s: (not s["salable"], s["price"] or 0))
    live = sum(s["salable"] for s in skus)
    # Attributes every variant shares (Ships From: China…) are printed once, not on every line.
    common = set(skus[0]["attrs"]).intersection(*(set(s["attrs"]) for s in skus[1:])) if len(skus) > 1 else set()
    print(f"\nВаріанти ({len(skus)}, в наявності {live}; дешеві вгорі):")
    if common:
        print("  спільне: " + "; ".join(f"{n}: {v}" for n, v in skus[0]["attrs"] if (n, v) in common))
    for s in skus[:limit]:
        s = {**s, "attrs": [a for a in s["attrs"] if a not in common]}
        parts = [f"{money(s['price'])} {SYM}"]
        if s["orig"] and s["price"] and s["orig"] > s["price"] * 1.01:
            parts[0] += f" (було {money(s['orig'])})"
        parts.append(f"stock {s['stock']}" if s["salable"] else "немає")
        if s["limit_hint"] and s["salable"] and not s["limit_hint"].endswith("available"):
            parts.append(s["limit_hint"])
        if s["ladder"]:
            parts.append("опт: " + ", ".join(f"від {q} шт {money(pr)}" for q, pr in s["ladder"]))
        if s["promo"]:
            parts.append(s["promo"].removesuffix("_atm"))
        if s.get("hot"):
            parts.append("хіт продажів")
        print(f"  {sku_label(s)} — " + " | ".join(parts) + f" | sku {s['id']}")
    if len(skus) > limit:
        print(f"  … ще {len(skus) - limit} (--max-skus)")


def store_line(st):
    bits = [f"{st.get('name')} (shop {st.get('shop_id')}, seller {st.get('seller_id')})"]
    if st.get("opened_text"):
        bits.append(f"з {st['opened_text']}")
    if st.get("positive_rate"):
        bits.append(f"{st['positive_rate']}% позитивних")
    if st.get("sold_180d"):
        bits.append(f"{st['sold_180d']} продано за 180 днів")
    if st.get("country"):
        bits.append(st["country"])
    return ", ".join(bits)


def shipping_lines(p):
    out = []
    for s in p["shipping"][:4]:
        days = f"{s['days'][0]}–{s['days'][1]} днів" if s["days"][0] else ""
        eta = f" ({s['eta'][0]}–{s['eta'][1]})" if s["eta"][0] else ""
        fee = "безкоштовно" if s["fee"] == 0 else f"{money(s['fee'])} {SYM}" if s["fee"] else "н/д"
        warn = f" ⚠ розраховано для {s['to']}, не UA" if s["to"] and s["to"] != "UA" else ""
        out.append(f"{s['option'] or '?'} з {s['from'] or '?'}: {days}{eta}, {fee}{warn}")
    return out


def cmd_product(ctx, args):
    pid = product_id(args.product)
    raw, p = fetch_product(ctx, pid, args.as_me)
    if args.json:
        return dump(raw)
    flags = product_flags(p)
    stats = price_stats(p["skus"])
    print(p["title"])
    if stats:
        rng = f"{money(stats['min'])}–{money(stats['max'])}" if stats["max"] > stats["min"] else money(stats["min"])
        print(f"Ціна: {rng} {SYM} (медіана {money(stats['median'])}, варіантів у наявності {stats['n']})"
              + ("" if args.as_me else " — як для гостя"))
    print(f"Рейтинг: {p['rating']} ({p['reviews']} відгуків) | {p['sold_text']} | score {score(p, flags)}")
    print(f"Магазин: {store_line(p['store'])}")
    print(f"Категорія: {p['category']} ({p['category_path']})")
    print(f"URL: {p['url']}")
    note = bought_note(purchases_db(), pid)
    if note:
        print(f"✓ {note}")
    for f in flags:
        print(f"⚠ {f}")
    for n in p["price_notes"]:
        print(f"  ({n})")
    if not args.no_variants:
        print_skus(p, args.max_skus)
    ship = shipping_lines(p)
    if ship:
        print("\nДоставка:\n  " + "\n  ".join(ship))
    if p["props"]:
        print("\nХарактеристики:")
        for n, v in p["props"]:
            print(f"  {n}: {v}")
    if args.desc and p["desc_url"]:
        html = ctx.anon.description(p["desc_url"])
        text = html_to_text(html)
        imgs = re.findall(r'<img[^>]+src="([^"]+)"', html)
        if args.max_desc and len(text) > args.max_desc:
            text = text[: args.max_desc] + " …"
        print(f"\nОпис ({len(imgs)} картинок{'' if args.images else ', --images для посилань'}):\n{text or '(лише картинки)'}")
        if args.images:
            print("\n".join(imgs[:40]))
    if p["manual"]:
        print(f"\nІнструкція (PDF): {p['manual']}")
    if args.reviews:
        print_reviews(ctx, pid, args.reviews, "all", "complex_default")


def print_reviews(ctx, pid, limit, flt, sort, page=1):
    data = reviews(ctx.anon.reviews(pid, page=page, page_size=min(limit, 20), filter=flt, sort=sort))
    stars = ", ".join(f"{k}★ {v}" for k, v in data["stars"].items() if v is not None)
    print(f"\nВідгуки: {data['total']} (середня {data['avg']}; {stars}; негативних {data['negative_rate']}%)")
    if data["filters"]:
        print("  фільтри: " + ", ".join(f"{k} {v}" for k, v in data["filters"].items()))
    if data["tags"]:
        print("  теги: " + ", ".join(f"{t} ({n})" for t, n in data["tags"][:10]))
    pop = popular_variants(data["items"])
    if pop:
        print("  що купують (на цій сторінці): " + "; ".join(f"{' '.join(k.split())} ×{n}" for k, n in pop[:5]))
    for r in data["items"][:limit]:
        pics = f" [{r['images']} фото]" if r["images"] else ""
        print(f"- {'★' * r['stars']} {r['date']} {r['country']} | {r['sku'] or ''}{pics}\n  {r['text']}")
        if r["additional"]:
            print(f"  + доповнення: {r['additional']}")


def cmd_reviews(ctx, args):
    pid = product_id(args.product)
    if args.json:
        return dump(ctx.anon.reviews(pid, args.page, min(args.limit, 20), args.filter, args.sort))
    print_reviews(ctx, pid, args.limit, args.filter, args.sort, args.page)


def cmd_compare(ctx, args):
    prods = []
    for ref in args.products:
        prods.append(fetch_product(ctx, product_id(ref), args.as_me)[1])
    if args.json:
        return dump(prods)

    def price(p):
        st = price_stats(p["skus"])
        if not st:
            return "—"
        return (f"{money(st['min'])}–{money(st['max'])}" if st["max"] > st["min"] else money(st["min"])) + f" {SYM}"

    rows = [
        ("Назва", [p["title"] for p in prods]),
        ("Ціна (в наявності)", [price(p) for p in prods]),
        ("Медіана варіантів", [money((price_stats(p["skus"]) or {}).get("median")) for p in prods]),
        ("Рейтинг", [f"{p['rating']} ({p['reviews']})" for p in prods]),
        ("Продано", [p["sold_text"] for p in prods]),
        ("Магазин", [p["store"].get("name") for p in prods]),
        ("Магазин з", [p["store"].get("opened_text") for p in prods]),
        ("% позитивних", [p["store"].get("positive_rate") for p in prods]),
        ("Доставка", [(shipping_lines(p) or ["—"])[0] for p in prods]),
        ("Прапорці", ["; ".join(product_flags(p)) or "—" for p in prods]),
        ("Score", [score(p, product_flags(p)) for p in prods]),
    ]
    specs, order = {}, []
    for i, p in enumerate(prods):
        for name, value in p["props"]:
            if name not in specs:
                specs[name] = [None] * len(prods)
                order.append(name)
            specs[name][i] = value if specs[name][i] is None else f"{specs[name][i]}, {value}"
    rows += [(n, specs[n]) for n in order]
    if args.diff:
        rows = rows[:1] + [r for r in rows[1:] if len({cell(v, 0) for v in r[1]}) > 1]
    w = args.width
    print("| | " + " | ".join(str(i) for i in range(1, len(prods) + 1)) + " |")
    print("|---" * (len(prods) + 1) + "|")
    for name, vals in rows:
        print(f"| {cell(name, 40)} | " + " | ".join(cell(v, w) for v in vals) + " |")
    print()
    for i, p in enumerate(prods, 1):
        print(f"{i}. {p['id']} {p['url']}")
    if args.diff:
        print("(only rows that differ; drop --diff to see everything)")


# --- stores -------------------------------------------------------------------------

def resolve_store(ctx, ref):
    ref = ref.strip()
    if re.search(r"/item/\d+", ref) or re.fullmatch(r"\d{15,20}", ref):
        return fetch_product(ctx, product_id(ref))[1]["store"]
    st = find_store(ref)
    if not st:
        die(f"unknown store '{ref}': pass a product id/URL from that store once (store ids come from product pages)")
    return st


def cmd_store(ctx, args):
    st = resolve_store(ctx, args.store)
    if args.json:
        return dump(st)
    print(store_line(st))
    print(f"URL: {st.get('url')}")
    if st.get("feedbacks"):
        print(f"Оцінок продавця: {st['feedbacks']}")
    db = purchases_db()
    if db:
        h = orders_db.store_history(db, st.get("name") or "")
        if h and h["n"]:
            print(f"✓ ти купував тут {h['n']} раз(и), останній {h['last']}")
    name = (st.get("name") or "").lower()
    kind = ("офіційний магазин бренду" if "official" in name else "авторизований продавець" if "authorized" in name
            else "звичайний магазин")
    print(f"Тип: {kind} (за назвою)")
    print(f"Асортимент: aliexpress store-products {st.get('shop_id')}")


def dedupe(items):
    """Same product listed under several ids: keep the one with most orders."""
    groups = {}
    for it in items:
        key = re.sub(r"[^a-z0-9]+", " ", it["title"].lower()).strip()
        groups.setdefault(key, []).append(it)
    out = []
    for group in groups.values():
        group.sort(key=lambda x: (-(x["orders"] or 0), -(x["reviews"] or 0)))
        best = dict(group[0])
        best["dupes"] = [g["id"] for g in group[1:]]
        out.append(best)
    return out


def cmd_store_products(ctx, args):
    st = resolve_store(ctx, args.store)
    api = ctx.api(args.as_me)
    data = api.store_products(st["seller_id"], st["shop_id"], args.page, args.limit, STORE_SORTS[args.sort],
                              args.find or "", STORE_FILTERS.get(args.filter, ""))
    if args.json:
        return dump(data)
    items = [store_item(x) for x in data.get("data") or []]
    if not args.keep_dupes:
        items = dedupe(items)
    db = purchases_db()
    rows = []
    for c in items:
        flags = card_flags(c)
        rows.append((c, flags, score(c, flags)))
    # The API ignores sortType (checked 2026-09 with every value the store page uses), so sort the page here.
    local = {"orders": lambda r: -(r[0]["orders"] or 0),
             "price_asc": lambda r: r[0].get("regular") or r[0]["price"] or 0,
             "price_desc": lambda r: -(r[0].get("regular") or r[0]["price"] or 0)}
    if args.sort_score:
        rows.sort(key=lambda r: -r[2])
    elif args.sort in local:
        rows.sort(key=local[args.sort])
    print(f"{st.get('name')}: {data.get('totalCount')} products, page {args.page}/{data.get('totalPage')}"
          + (f", '{args.find}'" if args.find else "")
          + (f", this page sorted by {args.sort}" if args.sort in local and not args.sort_score else ""))
    for i, (c, flags, sc) in enumerate(rows, (args.page - 1) * args.limit + 1):
        if c.get("new_user") and c.get("regular"):
            c = {**c, "price": c["regular"], "orig": None}
            flags = [f for f in flags if f != "new-user price"] + ["new-user deal hidden: звичайна ціна показана"]
        note = bought_note(db, c["id"])
        if c.get("dupes"):
            flags = flags + [f"дублі: {', '.join(c['dupes'][:3])}"]
        print_card(i, c, flags, sc, note)


def cmd_stores(ctx, args):
    cache = load_json(BRAND_CACHE, {})
    key = args.brand.casefold()
    hit = cache.get(key)
    if hit and not args.refresh and time.time() - hit["built"] < BRAND_TTL:
        stores = hit["stores"]
    else:
        fs = get_facets(ctx.anon, args.brand, None)
        brand_group = next((g for g in fs if g["type"] == "attribute" and g["title"].lower() == "brand"), None)
        params = {"SortType": SORTS["orders"]}
        if brand_group:
            opt = next((v for v in brand_group["values"] if v["text"].casefold() == key), None)
            if opt:
                params["attr"] = opt["value"]
        res = search_page(ctx.anon.search(args.brand, params))
        stores = {}
        for c in res["items"][: args.top]:
            try:
                p = fetch_product(ctx, c["id"])[1]
            except CaptchaError:
                break
            st = p["store"]
            entry = stores.setdefault(str(st["shop_id"]), {**st, "products": [], "brand_hits": 0})
            entry["products"].append({"id": p["id"], "title": p["title"], "sold": p["sold"], "rating": p["rating"]})
            brand_prop = next((v for n, v in p["props"] if n.lower() in ("brand name", "brand")), "")
            entry["brand_hits"] += key in (brand_prop + " " + p["title"]).casefold()
        stores = list(stores.values())
        cache[key] = {"built": time.time(), "stores": stores}
        save_json(BRAND_CACHE, cache)
    if args.json:
        return dump(stores)

    def rank(st):
        name = (st.get("name") or "").lower()
        return (-(key in name and "official" in name), -("official" in name or "authorized" in name),
                -(st.get("brand_hits") or 0), -(st.get("years") or 0))
    print(f"Stores selling '{args.brand}' (from the top {args.top} best-selling listings):")
    for st in sorted(stores, key=rank):
        name = (st.get("name") or "").lower()
        badge = "OFFICIAL" if key in name and "official" in name else "official?" if "official" in name else \
            "authorized" if "authorized" in name else ""
        print(f"- {store_line(st)} {('[' + badge + ']') if badge else ''}")
        print(f"  shop {st.get('shop_id')} | {len(st['products'])} з топ-лістингів, бренд у {st.get('brand_hits')}"
              f" | {st.get('url')}")
        for pr in st["products"][:3]:
            print(f"    {pr['id']}: {pr['title'][:90]} (★{pr['rating']}, {pr['sold']}+ sold)")


# --- orders ----------------------------------------------------------------------------

def ensure_orders(ctx, args, db):
    synced = orders_db.last_sync(db)
    need = args.sync or args.full or (not args.offline and (not synced or time.time() - synced > ORDERS_TTL))
    if not need:
        return
    full = args.full or not orders_db.is_complete(db)
    print(f"Syncing orders ({'full' if full else 'incremental'})…", file=sys.stderr)
    pages, changed = orders_db.sync(ctx.account, full=full, log=lambda *a, **k: print(*a, file=sys.stderr, **k))
    print(f"\n{pages} page(s) read, {changed} new/changed orders", file=sys.stderr)


def fmt_line(ln):
    attrs = ", ".join(f"{n}: {v}" for n, v in ln["attrs"] if n != "Ships From")
    return (f"    {ln['title'][:100]}\n      {attrs or '—'} × {ln['qty']} — {money(ln['price'])} {ln['currency'] or ''}"
            f" | product {ln['product_id']} sku {ln['sku_id']}")


def cmd_orders(ctx, args):
    db = orders_db.connect()
    ensure_orders(ctx, args, db)
    rows = orders_db.query(db, args.find, args.since, args.status, args.limit)
    if args.json:
        return dump(rows)
    total = db.execute("SELECT count(*) FROM orders").fetchone()[0]
    synced = orders_db.last_sync(db)
    when = datetime.datetime.fromtimestamp(synced).strftime("%Y-%m-%d %H:%M") if synced else "never"
    print(f"{len(rows)} of {total} cached orders (synced {when}"
          + ("" if orders_db.is_complete(db) else ", history incomplete — run with --full") + ")")
    for o, lines in rows:
        print(f"{o['date']} #{o['order_id']} {o['status']} — {o['store']} — {money(o['total'])} {o['currency'] or ''}")
        for ln in lines:
            print(fmt_line(ln))


def cmd_order(ctx, args):
    db = orders_db.connect()
    detail = None if args.refresh else orders_db.get_detail(db, args.order)
    if detail is None:
        detail = order_detail(ctx.account.order_detail(args.order))
        orders_db.save_detail(db, args.order, detail)
    if args.json:
        return dump(detail)
    row = db.execute("SELECT * FROM orders WHERE order_id=?", (args.order,)).fetchone()
    if row:
        print(f"{row['date']} #{row['order_id']} {row['status']} — {row['store']} — {money(row['total'])} {row['currency']}")
        for ln in orders_db.lines_of(db, args.order):
            print(fmt_line(ln))
    print(f"\nСтатус: {detail['status']}" + (f" — {detail['note']}" if detail.get("note") else ""))
    for title, value in detail["prices"]:
        print(f"  {title}: {value}")
    if detail.get("total"):
        print(f"  Total: {detail['total']}")
    if detail.get("eta"):
        print(detail["eta"])
    if detail.get("tracking"):
        print(f"Трекінг: {detail['tracking']}")


# --- session ------------------------------------------------------------------------------

def cmd_session(ctx, args):
    raw = read_env_cookie()
    names = [p.split("=", 1)[0].strip() for p in (raw or "").split(";") if "=" in p]
    print(f"Account cookie (.env aliexpress_cookie): {'yes, ' + str(len(names)) + ' cookies' if raw else 'missing'}"
          + (" — login cookies xman_t/_m_h5_tk present" if {"xman_t", "_m_h5_tk"} <= set(names) else ""))
    calls = (load_state().get("account_calls") or {}).get(datetime.date.today().isoformat(), 0)
    print(f"Account requests today: {calls}/{ACCOUNT_DAILY_LIMIT}")
    for label, path in [("anonymous jar", CACHE / "anon_cookies.json"), ("account jar", CACHE / "account_cookies.json")]:
        j = load_json(path, None)
        print(f"{label}: " + (datetime.datetime.fromtimestamp(j["saved"]).strftime("%Y-%m-%d %H:%M") if j else "none"))
    db = purchases_db()
    if db:
        n = db.execute("SELECT count(*) FROM orders").fetchone()[0]
        print(f"Orders cache: {n} orders, {'complete' if orders_db.is_complete(db) else 'partial'}")
    if args.check:
        data = ctx.account.mtop("mtop.aliexpress.trade.buyer.order.count", {"_lang": "en_US"})
        print("Login check: OK", json.dumps(data.get("module") or data, ensure_ascii=False)[:200])


def cmd_unblock(ctx, args):
    """Open a visible window on an AliExpress page and keep it until the page's own mtop works."""
    import browser
    browser.WINDOW_WAIT = args.timeout
    b = browser.BrowserMtop(args.account)
    probe = (("mtop.aliexpress.trade.buyer.order.count", {"_lang": "en_US"}) if args.account else
             ("mtop.aliexpress.pdp.pc.query", {"productId": browser.PROBE_PRODUCT, "_lang": "en_US",
                                               "_currency": CURRENCY, "country": "UA", "clientType": "pc"}))
    try:
        b._open_base(headed=True)
        b.mtop(probe[0], json.dumps(probe[1]))
        print("Passed; cookies saved.")
    finally:
        b.close()


# --- main ---------------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="aliexpress", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print raw JSON instead of text")
    common.add_argument("--delay", type=float, metavar="SECONDS",
                        help=f"pause before every HTTP request (default {DEFAULT_DELAY}; env ALIEXPRESS_DELAY)")
    priced = argparse.ArgumentParser(add_help=False)
    priced.add_argument("--as-me", action="store_true",
                        help="use your account session: your real prices instead of new-user deals (counts "
                        "against the daily account limit)")
    common.add_argument("--no-browser", action="store_true",
                        help="on a captcha, fail instead of opening a browser window for the slider check")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("products", parents=[common, priced], help="search listings with filters and bait flags")
    p.add_argument("query", nargs="?", default="")
    p.add_argument("--cat", help="category id (see `categories`)")
    p.add_argument("-f", "--filter", action="append", metavar="NAME=VAL",
                   help="attribute filter by name ('Brand=UGREEN|ANKER', 'Length=1 M') or raw id (2250-4169245); repeatable")
    p.add_argument("--brand", action="append", help="shortcut for -f Brand=X, repeatable")
    p.add_argument("--sort", choices=list(SORTS), default="best")
    p.add_argument("--price", metavar="MIN-MAX", help=f"price range in {CURRENCY}")
    p.add_argument("--ship-from", metavar="CODE", help="UA, CN, TR… (see `filters`)")
    p.add_argument("--choice", action="store_true", help="AliExpress Choice only")
    p.add_argument("--free-ship", action="store_true")
    p.add_argument("--four-stars", action="store_true", help="4★ & up")
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--clean", action="store_true", help="hide bait-flagged listings (bulk, decoy packs…)")
    p.add_argument("--sort-score", action="store_true", help="re-sort this page by score")
    p.set_defaults(func=cmd_products)

    p = sub.add_parser("filters", parents=[common], help="facets for a query: attributes, brands, categories, ship-from")
    p.add_argument("query")
    p.add_argument("--cat")
    p.add_argument("--all", action="store_true")
    p.add_argument("--max-values", type=int, default=15)
    p.set_defaults(func=cmd_filters)

    p = sub.add_parser("categories", parents=[common], help="categories for a query, or search the learnt cache")
    p.add_argument("query", nargs="?")
    p.add_argument("--find", metavar="TEXT", help="search categories seen in earlier searches (offline)")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_categories)

    p = sub.add_parser("product", parents=[common, priced],
                       help="variants with prices/stock, bait flags, store, shipping, specs, reviews")
    p.add_argument("product", help="product id or URL")
    p.add_argument("--reviews", type=int, metavar="N", help="also show N reviews (max 20)")
    p.add_argument("--desc", action="store_true", help="fetch the seller's description (often mostly images)")
    p.add_argument("--images", action="store_true", help="with --desc: list description image URLs")
    p.add_argument("--max-desc", type=int, default=2000)
    p.add_argument("--max-skus", type=int, default=40)
    p.add_argument("--no-variants", action="store_true")
    p.set_defaults(func=cmd_product)

    p = sub.add_parser("reviews", parents=[common], help="reviews with star breakdown and the variants people bought")
    p.add_argument("product")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--filter", default="all", help="all | image (with photos) | additional (follow-ups) | local (buyers from Ukraine)")
    p.add_argument("--sort", default="complex_default", help="complex_default | latest_default …")
    p.set_defaults(func=cmd_reviews)

    p = sub.add_parser("compare", parents=[common, priced], help="side-by-side table of several products")
    p.add_argument("products", nargs="+")
    p.add_argument("--diff", action="store_true")
    p.add_argument("--width", type=int, default=60)
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("store", parents=[common], help="seller profile (by product id/URL, shop id or cached name)")
    p.add_argument("store")
    p.set_defaults(func=cmd_store)

    p = sub.add_parser("store-products", parents=[common, priced], help="a store's range, deduplicated")
    p.add_argument("store", help="product id/URL from the store, shop id, or cached store name")
    p.add_argument("--find", metavar="TEXT", help="search inside the store")
    p.add_argument("--sort", choices=list(STORE_SORTS), default="orders",
                   help="orders / price_*: re-sorts the fetched page (the API ignores sorting); best, new: server order")
    p.add_argument("--filter", choices=list(STORE_FILTERS))
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--keep-dupes", action="store_true")
    p.add_argument("--sort-score", action="store_true")
    p.set_defaults(func=cmd_store_products)

    p = sub.add_parser("stores", parents=[common], help="stores selling a brand, official first (cached 7 days)")
    p.add_argument("--brand", required=True)
    p.add_argument("--top", type=int, default=6, help="best-selling listings to inspect (1 request each)")
    p.add_argument("--refresh", action="store_true")
    p.set_defaults(func=cmd_stores)

    p = sub.add_parser("orders", parents=[common], help="your purchase history from the local cache (auto-syncs every 12 h)")
    p.add_argument("--find", metavar="TEXT", help="title, variant, store, order id or product id")
    p.add_argument("--since", metavar="YYYY-MM-DD")
    p.add_argument("--status", help="completed, processing, shipped…")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--sync", action="store_true", help="sync now (incremental)")
    p.add_argument("--full", action="store_true", help="re-read the whole history")
    p.add_argument("--offline", action="store_true", help="never touch the account, cache only")
    p.set_defaults(func=cmd_orders)

    p = sub.add_parser("order", parents=[common], help="one order: lines + price/shipping breakdown (cached once final)")
    p.add_argument("order")
    p.add_argument("--refresh", action="store_true")
    p.set_defaults(func=cmd_order)

    p = sub.add_parser("session", parents=[common], help="account cookie, request budget and cache status")
    p.add_argument("--check", action="store_true", help="one account request to verify the login")
    p.set_defaults(func=cmd_session)

    p = sub.add_parser("unblock", parents=[common], help="open a browser window to solve a slider captcha")
    p.add_argument("--account", action="store_true", help="for the account session")
    p.add_argument("--url", help="page to open (default: a search page)")
    p.add_argument("--timeout", type=int, default=300)
    p.set_defaults(func=cmd_unblock)

    args = ap.parse_args(argv)
    delay = args.delay if args.delay is not None else float(os.environ.get("ALIEXPRESS_DELAY") or DEFAULT_DELAY)
    ctx = Ctx(delay, args.no_browser)
    try:
        args.func(ctx, args)
    except (LoginRequired, CaptchaError, ApiError, ValueError) as e:
        die(str(e))
    except RequestException as e:
        die(f"network error: {e}")
    finally:
        ctx.close()


if __name__ == "__main__":
    main()
