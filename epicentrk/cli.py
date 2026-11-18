"""epicentrk — search epicentrk.ua from the command line (built for coding agents).

Run `epicentrk <command> -h` for details. Typical flow:
  epicentrk stores --address "вул. Хрещатик 1, Київ"   # find hypermarkets near you
  epicentrk store set 2                                 # remember one
  epicentrk search "шуруповерт"                         # find the right category
  epicentrk filters shurupoverty --today                # see what can be filtered
  epicentrk products shurupoverty --today -f "Тип акумулятора=Li-ion" --brand Makita --stock
  epicentrk product <slug> --stores 5 --reviews 5
  epicentrk compare <slug1> <slug2> [<slug3> ...] --diff
  epicentrk categories --find "ламінат"                 # or let it resolve the listing for you

Running several commands in a row? Append --delay to each one (a pause before every request).
"""

import argparse
import datetime
import json
import math
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from client import (KYIV_CITY_ID, STORE_TYPES, ApiError, Epicentrk, listing_path,
                    product_slug)

STATE_FILE = Path(__file__).with_name(".state.json")
CATEGORY_CACHE = Path(__file__).with_name(".cache") / "categories.json"
DEFAULT_DELAY = 1.5
SORTS = ["rating", "asc", "desc", "aktsii", "new", "amount"]
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


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


def dump(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def html_to_text(html):
    if not html:
        return ""
    text = BeautifulSoup(html, "html.parser").get_text("\n")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def haversine(lat1, lng1, lat2, lng2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def geocode(address):
    """Address -> (lat, lng) via OpenStreetMap Nominatim."""
    r = requests.get("https://nominatim.openstreetmap.org/search",
                     params={"q": address, "format": "json", "limit": 1, "countrycodes": "ua"},
                     headers={"User-Agent": "assistant-marketplaces-cli/0.1"}, timeout=30)
    r.raise_for_status()
    hits = r.json()
    if not hits:
        die(f"could not geocode address: {address}")
    return float(hits[0]["lat"]), float(hits[0]["lon"]), hits[0]["display_name"]


def parse_latlng(text):
    try:
        lat, lng = (float(x) for x in text.split(","))
        return lat, lng
    except ValueError:
        die(f"expected LAT,LNG, got: {text}")


def parse_range(text):
    lo, _, hi = text.partition("-") if ".." not in text else text.partition("..")
    return (float(lo) if lo else None), (float(hi) if hi else None)


def fmt_num(x):
    return str(int(x)) if float(x).is_integer() else str(x)


# Quantity + unit in a product name: "… 14 кг", "… 2,5 л", "… (200 шт.)".
UNIT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(кг|г|мл|л|м²|м2|кв\.\s?м|шт)(?![а-яіїєґa-z])", re.IGNORECASE)
UNIT_NORM = {"кг": ("кг", 1), "г": ("кг", 0.001), "л": ("л", 1), "мл": ("л", 0.001),
             "м²": ("м²", 1), "м2": ("м²", 1), "кв.м": ("м²", 1), "шт": ("шт", 1)}


def old_price_text(p):
    try:
        old, price = float(p.get("oldPrice") or 0), float(p.get("price") or 0)
    except (TypeError, ValueError):
        return ""
    return f" (було {p['oldPrice']})" if old > price else ""


def fmt_money(x):
    return f"{x:.3g}" if x < 1 else f"{x:.2f}" if x < 100 else str(round(x))


def unit_price(p):
    """Price per base unit as (value, unit, note), or None when it would just repeat the price."""
    try:
        price = float(p.get("price") or 0)
    except (TypeError, ValueError):
        return None
    if not price:
        return None
    measure = (p.get("measure") or "").strip()
    # Count-like measures (шт., уп., компл., меш.…) say nothing about quantity; parse the name instead.
    if measure and not re.match(r"(шт|уп|компл|наб|пар|меш)", measure) and "{" not in measure:
        # Price is already per m²/roll/etc.; measureRatio is how much of it a pack holds.
        unit = measure.rstrip(".")
        ratio = p.get("measureRatio")
        note = f"упак. {fmt_num(round(ratio, 3))} {unit} = {round(price * ratio)} ₴" if ratio and ratio != 1 else None
        return price, unit, note
    found = [(float(q.replace(",", ".")), UNIT_NORM[u.lower().replace(" ", "")])
             for q, u in UNIT_RE.findall(p.get("name") or "")]
    found = [(q * k, unit) for q, (unit, k) in found if q > 0]
    if not found:
        return None
    # Prefer weight/volume/area over a piece count; the last mention usually is the pack size.
    qty, unit = next((f for f in reversed(found) if f[1] != "шт"), found[-1])
    if unit == "шт" and qty <= 1:
        return None
    return price / qty, unit, None


def unit_price_text(p):
    up = unit_price(p)
    if not up:
        return ""
    value, unit, note = up
    text = f"≈{fmt_money(value)} ₴/{unit}"
    return f"{text} ({note})" if note else text


def sort_by_unit(items):
    """Cheapest per unit first; items in the page's most common unit go before the rest."""
    ups = [(p, unit_price(p)) for p in items]
    units = [u[1] for _, u in ups if u]
    main = max(set(units), key=units.count) if units else None
    return [p for p, u in sorted(ups, key=lambda x: (not x[1] or x[1][1] != main, x[1][0] if x[1] else 0))]


def today_schedule(store):
    for el in store.get("storeElements") or []:
        sched = el.get("schedule") or {}
        day = sched.get(WEEKDAYS[datetime.date.today().weekday()])
        if day:
            return f"{day.get('from')}–{day.get('to')}"
    return ""


def resolve_store(args, state, required=False):
    store_id = getattr(args, "store", None) or state.get("store_id")
    if required and not store_id:
        die("no store selected: run `epicentrk stores ...` then `epicentrk store set <id>`, or pass --store")
    return int(store_id) if store_id else None


def store_label(state, store_id):
    if store_id and state.get("store_id") == store_id:
        return f"{store_id} ({state.get('title')})"
    return str(store_id)


# --- filter resolution ------------------------------------------------------

def _match(options, wanted, key_name, key_value, what):
    """Pick one option by exact value, exact name, or unique substring of name (case-insensitive)."""
    w = wanted.strip().casefold()
    for o in options:
        if str(o.get(key_value, "")).casefold() == w:
            return o
    exact = [o for o in options if str(o.get(key_name, "")).strip().casefold() == w]
    if exact:
        return exact[0]
    partial = [o for o in options if w in str(o.get(key_name, "")).casefold()]
    if len(partial) == 1:
        return partial[0]
    names = ", ".join(str(o.get(key_name)).strip() for o in (partial or options)[:30])
    die(f"{what} '{wanted}' is {'ambiguous' if partial else 'not found'}; options: {names}")


def build_selection(fdata, args):
    """Merge filters already applied to the path (checked=true) with those from the CLI."""
    props, brands, sellers, promos, price = {}, [], [], [], None

    for f in fdata.get("filters") or []:
        pid = f["prop_id"]
        if f["type"] == "range":
            # chosenMin/Max echo the bounds when the range is not applied.
            if (f.get("chosenMin"), f.get("chosenMax")) != (f.get("min"), f.get("max")):
                props[pid] = [f.get("chosenMin"), f.get("chosenMax")]
        else:
            checked = [v["value"] for v in f.get("list") or [] if v.get("checked")]
            if checked:
                props[pid] = checked
    brands += [b["value"] for b in (fdata.get("brand") or {}).get("list") or [] if b.get("checked")]
    sellers += [s["value"] for s in (fdata.get("seller") or {}).get("list") or [] if s.get("checked")]
    promos += [p["value"] for p in (fdata.get("promo") or {}).get("list") or [] if p.get("checked")]
    rng = (fdata.get("price") or {}).get("rangeData") or {}
    if rng.get("chosenMinPrice") is not None or rng.get("chosenMaxPrice") is not None:
        price = (rng.get("chosenMinPrice") or rng.get("minPrice"), rng.get("chosenMaxPrice") or rng.get("maxPrice"))

    for spec in args.filter or []:
        key, sep, vals = spec.partition("=")
        if not sep:
            die(f"filter must look like KEY=VALUE[|VALUE2], got: {spec}")
        key = key.strip().removeprefix("prop_")
        f = _match(fdata.get("filters") or [], key, "name", "prop_id", "filter")
        if f["type"] == "range":
            lo, hi = parse_range(vals)
            props[f["prop_id"]] = [fmt_num(lo if lo is not None else f["min"]),
                                   fmt_num(hi if hi is not None else f["max"])]
            continue
        chosen = props.setdefault(f["prop_id"], [])
        for v in vals.split("|"):
            opt = _match(f["list"], v, "name", "value", f"value of '{f['name'].strip()}'")
            if opt["value"] not in chosen:
                chosen.append(opt["value"])

    for b in args.brand or []:
        opt = _match((fdata.get("brand") or {}).get("list") or [], b, "name", "value", "brand")
        if opt["value"] not in brands:
            brands.append(opt["value"])
    for s in args.seller or []:
        opt = _match((fdata.get("seller") or {}).get("list") or [], s, "name", "value", "seller")
        if opt["value"] not in sellers:
            sellers.append(opt["value"])
    for p in args.promo or []:
        opt = _match((fdata.get("promo") or {}).get("list") or [], p, "name", "value", "promo")
        if opt["value"] not in promos:
            promos.append(opt["value"])
    if args.price:
        lo, hi = parse_range(args.price)
        price = (fmt_num(lo if lo is not None else rng.get("minPrice", 0)),
                 fmt_num(hi if hi is not None else rng.get("maxPrice", 10**9)))

    return dict(props=props, brands=brands, sellers=sellers, promos=promos, price=price)


def has_cli_filters(args):
    return any([args.filter, args.brand, args.seller, args.promo, args.price])


def resolve_path(api, args, store_id):
    """Base path + CLI filters -> final listing path (query[] form)."""
    path = listing_path(args.category)
    if not has_cli_filters(args):
        return path
    fdata = api.filters(path, store_id, args.today)
    sel = build_selection(fdata, args)
    section_id = api.section_data(path, store_id)["sectionId"]
    url = api.listing_url(section_id, **sel)
    return listing_path(url)


# --- output -----------------------------------------------------------------

def print_product_row(i, p, stock=None):
    price = f"{p['price']} ₴"
    price += old_price_text(p)
    up = unit_price_text(p)
    if up:
        price += f", {up}"
    rating = f" ★{p['rating']} ({p['commentsCount']})" if p.get("commentsCount") else ""
    print(f"{i}. {p['name']} — {price}{rating}")
    avail = (p.get("availabilityStatus") or {}).get("name", "")
    extra = [f"slug: {product_slug(p['url'])}", f"seller: {p.get('seller')}", avail]
    badge = (p.get("badge") or {}).get("title")
    if badge:
        extra.append(badge)
    if p.get("modelsCount"):
        extra.append(f"модифікацій: {p['modelsCount']}")
    if stock is not None:
        extra.append(f"в магазині: {stock}")
    print("   " + " | ".join(x for x in extra if x))
    props = "; ".join(f"{pr['name']}: {' '.join(str(pr['values']).split())}" for pr in p.get("properties") or [])
    if props:
        print(f"   {props}")


def stock_map(api, store_id, items):
    own = [p["productId"] for p in items if p.get("seller") == "epicentrk" and p.get("productId")]
    try:
        raw = api.leftovers(store_id, own)
    except ApiError as e:
        print(f"(stock lookup failed: {e})", file=sys.stderr)
        return {}
    return {int(k): f"{fmt_num(v['amount'])} {v.get('measure', '')}".strip() for k, v in raw.items()}


def stock_cell(stock, p):
    if p.get("seller") != "epicentrk":
        return "н/д (маркетплейс)"
    return stock.get(p.get("productId"), "немає")


def print_variants(api, models, store_id):
    """Sibling models (size, weight, colour…) with their own price, unit price and stock."""
    articles = [it["productId"] for m in models for it in m.get("items") or [] if it.get("productId")]
    info = {x["id"]: x for x in api.product_info(articles, store_id)} if articles else {}
    stock = stock_map(api, store_id, list(info.values())) if store_id else {}
    print("\nМодифікації (→ поточна):")
    for m in models:
        print(f"  {m.get('name')}:")
        for it in m.get("items") or []:
            x = info.get(it.get("productId"), {})
            mark = "→" if it.get("status") == "ACTIVE" else " "
            parts = [f"{x['price']} ₴" if x.get("price") else "ціна н/д"]
            up = unit_price_text(x)
            if up:
                parts[0] += f", {up}"
            parts.append((it.get("availabilityStatus") or {}).get("name", ""))
            if store_id and x:
                parts.append(f"в магазині: {stock_cell(stock, x)}")
            parts.append(f"slug: {product_slug(it['url'])}")
            print(f"   {mark} {it.get('displayValue')}: " + " | ".join(p for p in parts if p))


def cell(value, width):
    text = " ".join(str(value if value is not None else "—").split()).replace("|", "/")
    return text if not width or len(text) <= width else text[: width - 1] + "…"


# --- category lookup --------------------------------------------------------
#
# `categories --find` asks the site search (at most 4 requests) and never needs the whole tree.
# .cache/categories.json only remembers listings seen along the way, so a repeated lookup is free.
# The whole tree is crawled only by an explicit `categories --full-crawl`.

KIND_RANK = {"final": 0, "section": 1, "portal": 2, "preset": 3}
CRAWL_BATCH = 20  # progress is saved after every batch


def load_category_cache():
    try:
        cache = json.loads(CATEGORY_CACHE.read_text())
        if isinstance(cache.get("items"), list):
            return cache
    except (FileNotFoundError, ValueError, AttributeError):
        pass
    return {"built": None, "items": []}


def save_category_cache(cache):
    """Atomic write; a cache that can't be written must never break a command."""
    try:
        CATEGORY_CACHE.parent.mkdir(exist_ok=True)
        tmp = CATEGORY_CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False))
        tmp.replace(CATEGORY_CACHE)
    except OSError as e:
        print(f"(category cache not saved: {e})", file=sys.stderr)


def cache_entries(cache):
    """Everything known locally: the lazily collected listings plus an unfinished full crawl."""
    return cache["items"] + ((cache.get("crawl") or {}).get("items") or [])


def category_entry(item, crumbs):
    """A catalog API item -> index entry, or None for items without a listing."""
    if not item.get("url") or not item.get("name"):
        return None
    kind = {"filters": "preset", "portalpage": "portal"}.get(item.get("type")) or \
        ("final" if item.get("final") else "section")
    return {"name": item["name"].strip(), "path": listing_path(item["url"]), "crumbs": crumbs, "kind": kind}


def merge_entries(items, entries):
    """Upsert entries into the list by path. Returns how many entries were added or improved."""
    by_path = {e["path"]: e for e in items}
    changed = 0
    for e in entries:
        if not e["name"]:
            continue  # a bare redirect target: nothing to match a later query against
        old = by_path.get(e["path"])
        if old is None:
            items.append(e)
            by_path[e["path"]] = e
            changed += 1
        else:
            # The search knows only the top-level parent; keep the fuller breadcrumbs.
            new = dict(e, crumbs=max(e["crumbs"], old["crumbs"], key=len))
            if new != old:
                old.clear()
                old.update(new)
                changed += 1
    return changed


def remember_categories(entries):
    """Add listings learnt from a response that was fetched anyway to the local cache."""
    keep = ("name", "path", "crumbs", "kind")
    entries = [{k: v for k, v in e.items() if k in keep} for e in entries if e]
    if not entries:
        return
    cache = load_category_cache()
    if merge_entries(cache["items"], entries):
        save_category_cache(cache)


def catalog_entries(data, known=(), crumbs=None):
    """Index entries from one /catalog response: the category itself, its children and their presets.

    The response has no breadcrumbs: pass the category's own `crumbs`, or they are taken from what
    is already known."""
    item = data.get("item") or {}
    out = []
    if item.get("url"):
        path = listing_path(item["url"])
        if crumbs is None:
            crumbs = next((e["crumbs"] for e in known if e["path"] == path and e["crumbs"]), [])
        out.append(category_entry(item, crumbs))
        crumbs = crumbs + [item["name"].strip()]
    crumbs = crumbs or []

    def walk(items, crumbs):
        for it in items or []:
            out.append(category_entry(it, crumbs))
            walk(it.get("subItems"), crumbs + [(it.get("name") or "").strip()])

    walk(data.get("subItems"), crumbs)
    return [e for e in out if e]


def search_entries(data):
    """Listings the site search points at, best first: a redirect target, or the categories of the
    goods it found, ranked by how many of those goods each one holds (`found`)."""
    if data.get("direct"):
        return [{"name": "", "path": listing_path(data["direct"]), "crumbs": [], "kind": "section",
                 "found": None}]
    d = data.get("data") or {}
    if not isinstance(d, dict):
        return []
    found = {}
    for it in d.get("items") or []:
        found[it.get("categoryId")] = found.get(it.get("categoryId"), 0) + 1
    tops = {sub.get("sectionId"): [top["name"].strip()] for top in d.get("sectionFilter") or []
            for sub in top.get("list") or [] if top.get("name")}
    out = []
    for s in d.get("horizontalSections") or []:
        if s.get("to") and s.get("name"):
            out.append({"name": s["name"].strip().removeprefix("Всі ").strip(), "path": listing_path(s["to"]),
                        "crumbs": tops.get(s.get("id"), []), "kind": "final" if s.get("final") else "section",
                        "found": found.get(s.get("id"), 0)})
    return sorted(out, key=lambda e: -e["found"])


def query_stems(query):
    # Crude stemming so "ламінату" finds "Ламінат": compare word prefixes.
    # Capped at 6 letters, so "сковорода" also finds "Сковорідки".
    return [w[: min(max(4, len(w) - 2), 6)] for w in query.casefold().split()]


def find_categories(index, query, names_only=False):
    stems = query_stems(query)
    hits = []
    for e in index:
        name = e["name"].casefold()
        if all(s in name for s in stems):
            score = 0
        elif not names_only and all(s in " ".join(e["crumbs"] + [e["name"]]).casefold() for s in stems):
            score = 1
        else:
            continue
        hits.append((score, KIND_RANK.get(e["kind"], 9), len(name), e))
    return [h[-1] for h in sorted(hits, key=lambda h: h[:3])]


def category_slug(path):
    """/shop/<slug>/... -> <slug>; None for portal pages, brands and other non-category paths."""
    m = re.match(r"/shop/([^/]+)/", path)
    return m.group(1) if m else None


def clean_crumb(name):
    """Breadcrumb names on the site end with an emoji: "Посуд 🍴" -> "Посуд"."""
    name = name.strip()
    while name and not (name[-1].isalnum() or name[-1] in ")»\"'’%"):
        name = name[:-1].rstrip()
    return name


def expand_category(api, target, known):
    """Subcategories and presets of one category, read with 1-2 requests.

    A category with subcategories lists them, with their presets, in its own /catalog response.
    A final category's presets are listed only in its parent's response, so the parent is looked up
    in the cache or, failing that, in the listing's breadcrumbs. Returns (entries, slug to browse)."""
    slug = category_slug(target["path"])
    base = f"/shop/{slug}/"
    cached = next((e for e in known if e["path"] == base), None)
    kind = target["kind"] if target["path"] == base and target["name"] else (cached or {}).get("kind")
    entries = []
    if kind == "section":
        return catalog_entries(api.catalog(slug), known), slug
    # Final category (or a redirect target of unknown kind): find the parent.
    crumbs = (cached or {}).get("crumbs") or []
    parent = next((e for e in known if crumbs and e["kind"] == "section" and e["name"] == crumbs[-1]
                   and e["crumbs"] == crumbs[:-1] and category_slug(e["path"])), None)
    if parent:
        parent_slug, parent_crumbs = category_slug(parent["path"]), parent["crumbs"]
    else:
        sdata = api.section_data(base)
        trail = [(clean_crumb(c.get("name") or ""), listing_path(c["url"]))
                 for c in sdata.get("breadcrumbs") or [] if c.get("url") and "/shop/" in c["url"]]
        trail = [(n, p) for n, p in trail if category_slug(p)]  # drops the "Каталог" root
        if trail and trail[-1][1] == base:
            trail.pop()
        if not trail:  # a top-level category after all
            return catalog_entries(api.catalog(slug), known), slug
        crumbs = [n for n, _ in trail]
        parent_slug, parent_crumbs = category_slug(trail[-1][1]), crumbs[:-1]
        own = clean_crumb(sdata.get("sectionName") or "") or target["name"]
        if own:
            entries.append({"name": own, "path": base, "crumbs": crumbs, "kind": "final"})
            # "Interlinks" are extra ready-made filtered listings shown on the category page.
            entries += [{"name": i["name"].strip(), "path": listing_path(i["link"]), "crumbs": crumbs + [own],
                         "kind": "preset"} for i in sdata.get("interlinks") or [] if i.get("name") and i.get("link")]
    entries += catalog_entries(api.catalog(parent_slug), known, parent_crumbs)
    return entries, parent_slug


def find_live(api, query, store_id=None, expand=1):
    """Resolve a product query through the site search, then read only the matching category.

    Returns (targets, related, hint): the listings the search points at; the subcategories/presets
    of the first `expand` of them whose names match the query; and the slug to browse for the rest.
    Costs the session handshake + 1 search + 1-2 requests per expanded category."""
    known = cache_entries(load_category_cache())
    by_path = {e["path"]: e for e in known}
    targets = search_entries(api.search(query, store_id))
    learnt, children, seen, hint = list(targets), [], set(), None
    for t in targets:
        slug = category_slug(t["path"])
        if not slug or slug in seen or len(seen) >= expand:
            continue
        seen.add(slug)
        try:
            entries, browse = expand_category(api, t, known)
        except ApiError:
            continue
        hint = hint or browse
        learnt += entries
        children += entries
    fresh = {e["path"]: e for e in children}
    for t in targets:  # fuller name/breadcrumbs when the category is known
        better = fresh.get(t["path"]) or by_path.get(t["path"]) or t
        if not t["name"] or len(better["crumbs"]) > len(t["crumbs"]):
            t.update(name=better["name"] or t["name"], crumbs=better["crumbs"], kind=better["kind"])
    target_paths = {t["path"] for t in targets}
    # Only what hangs off the expanded categories: the parent's response also lists their siblings.
    bases = tuple(f"/shop/{slug}/" for slug in seen)
    scope = [e for e in fresh.values() if e["path"] not in target_paths
             and (e["path"].startswith(bases) or any(c in e["crumbs"] for c in
                  [t["name"] for t in targets if category_slug(t["path"]) in seen and t["name"]]))]
    related = find_categories(scope, query)
    remember_categories(learnt)
    return targets, related, hint


def crawl_categories(api):
    """Walk the whole catalogue into the cache. Resumable: progress is saved after every batch."""
    cache = load_category_cache()
    state = cache.get("crawl")
    if state:
        print(f"Resuming the full crawl: {len(state['items'])} listings collected, "
              f"{len(state['queue'])} categories queued.", file=sys.stderr)
    else:
        state = cache["crawl"] = {"started": time.time(), "items": [], "queue": [], "queued": []}
    index, queue = state["items"], state["queue"]
    seen, queued = {e["path"] for e in index}, set(state["queued"])

    def add(item, crumbs):
        e = category_entry(item, crumbs)
        if e and e["path"] not in seen:
            seen.add(e["path"])
            index.append(e)

    def walk(items, crumbs):
        for it in items or []:
            add(it, crumbs)
            if it.get("type") == "section" and not it.get("final"):
                # Categories cross-link (the same child under several parents), so fetch each once.
                if it["code"] not in queued:
                    queued.add(it["code"])
                    queue.append([it["code"], crumbs + [it["name"].strip()]])
            else:  # presets hanging off a final category
                walk(it.get("subItems"), crumbs + [it["name"].strip()])

    def save():
        state["queued"] = sorted(queued)
        save_category_cache(cache)

    def fetch(code):
        try:
            return api.catalog(code).get("subItems") or []
        except ApiError:
            return []

    try:
        if not index:
            for top in api.catalog().get("subItems") or []:
                add(top, [])
                walk(top.get("subItems"), [top["name"].strip()])
            save()
        api._ensure_session()
        with ThreadPoolExecutor(8) as ex:
            while queue:
                batch = queue[:CRAWL_BATCH]
                for (_, crumbs), subs in zip(batch, ex.map(fetch, [code for code, _ in batch])):
                    walk(subs, crumbs)
                del queue[:len(batch)]
                save()
                print(f"  {len(index)} listings, {len(queue)} categories still queued", file=sys.stderr)
    except (KeyboardInterrupt, requests.RequestException) as e:
        save()
        die(f"full crawl interrupted ({type(e).__name__}); progress is saved "
            f"({len(index)} listings, {len(queue)} categories queued). "
            "Run `epicentrk categories --full-crawl` again to resume.")
    cache.update(built=time.time(), items=index)
    del cache["crawl"]
    save_category_cache(cache)
    return index


def print_category_hits(hits, limit, more="--limit"):
    for e in hits[:limit]:
        kind = "" if e["kind"] in ("final", "section") else f" [{e['kind']}]"
        crumbs = f"   ({' › '.join(e['crumbs'])})" if e["crumbs"] else ""
        found = f"   [{e['found']} goods found]" if e.get("found") else ""
        print(f"- {e['name'] or '(listing)'}{kind} → {e['path']}{crumbs}{found}")
    if len(hits) > limit:
        print(f"… +{len(hits) - limit} more ({more})")


def cache_summary(cache):
    n = len(cache_entries(cache))
    if cache.get("built"):
        day = datetime.date.fromtimestamp(cache["built"]).isoformat()
        return f"{n} listings, full crawl of {day}"
    return f"{n} listings collected from earlier lookups"


# --- commands ---------------------------------------------------------------

def cmd_category_find(api, args, state):
    cache = load_category_cache()
    if not args.live:
        # A name match in the cache answers the question for free; anything weaker goes to the site.
        hits = find_categories(cache_entries(cache), args.find, names_only=not args.offline)
        if hits or args.offline:
            if args.json:
                return dump(hits[: args.limit or 20])
            if not hits:
                return print(f"Nothing in the local cache matches '{args.find}' ({cache_summary(cache)}). "
                             "Drop --offline to ask the site.")
            print_category_hits(hits, args.limit or 20)
            print(f"\nFrom the local cache ({cache_summary(cache)}), 0 requests. "
                  "Not what you need? Add --live to ask the site search.")
            return
    targets, related, hint = find_live(api, args.find, resolve_store(args, state), args.expand)
    if args.json:
        return dump({"search": targets, "related": related[: args.limit or 10]})
    if not targets:
        return print(f"The site search suggests no category for '{args.find}'. Try other words, "
                     f"`epicentrk search \"{args.find}\"` to see the goods, or browse with "
                     "`epicentrk categories [slug]`.")
    print("Site search points at (categories of the goods it found, biggest first):"
          if targets[0].get("found") is not None else "Site search redirects to:")
    print_category_hits(targets, 5, more="less relevant; --json lists all")
    if related:
        print("\nMatching subcategories and presets there:")
        print_category_hits(related, args.limit or 10)
    if hint:
        print(f"\nEvery subcategory and preset around the first one: `epicentrk categories {hint} --depth 2`")
    print(f"({api.request_count} requests)")


def cmd_categories(api, args, state):
    if args.full_crawl:
        if args.find:
            die("--full-crawl and --find don't combine: --find never needs the full crawl")
        est = 550 * max(api.delay, 0.02) / 60
        print("WARNING: crawling the WHOLE category tree: about 550 HTTP requests, "
              f"roughly {est:.0f} min at the current --delay ({api.delay:g} s). `categories --find` does not "
              "need this. Progress is saved every "
              f"{CRAWL_BATCH} requests; an interrupted run resumes when started again.", file=sys.stderr)
        index = crawl_categories(api)
        return print(f"Category index rebuilt: {len(index)} listings ({api.request_count} requests)")
    if args.find:
        return cmd_category_find(api, args, state)
    data = api.catalog(args.slug)
    remember_categories(catalog_entries(data, cache_entries(load_category_cache())))
    if args.json:
        return dump(data)
    item = data.get("item")
    if item:
        print(f"{item['name']} [{item['code']}] id={item['sectionId']}{' (final)' if item.get('final') else ''}")

    def label(c):
        # "filters" items are preset filtered listings of some category, not real subcategories.
        if c.get("type") == "filters":
            return f"{c['name']} → {listing_path(c['url'])}"
        subs = c.get("subItems") or []
        presets = sum(1 for s in subs if s.get("type") == "filters")
        mark = "" if c.get("final") else "  (+)"
        if presets:
            mark += f"  ({presets} presets)"
        return f"{c['name']} [{c.get('code')}]{mark}"

    for c in data.get("subItems") or []:
        print(f"- {label(c)}")
        if args.depth > 1:
            for s in c.get("subItems") or []:
                print(f"    - {label(s)}")
    print("\n(+) = has subcategories: `epicentrk categories <slug>`; any [slug] or → path works with "
          "`epicentrk products`")


def cmd_search(api, args, state):
    store_id = resolve_store(args, state)
    price = parse_range(args.price) if args.price else None
    data = api.search(args.query, store_id, page=args.page, brands=args.brand, price=price)
    remember_categories(search_entries(data))
    if args.json:
        return dump(data)
    if data.get("direct"):
        path = listing_path(data["direct"])
        print(f"Search redirects to a category listing: {path}")
        print(f"→ use: epicentrk products '{path}' [--today] ...")
        return
    d = data.get("data") or {}
    for s in d.get("horizontalSections") or []:
        print(f"Category: {s['name']} → {listing_path(s['to'])}")
    items = d.get("items") or []
    if args.sort_unit:
        items = sort_by_unit(items)
    print(f"\n{data.get('total')} results, showing {min(len(items), args.limit)}:")
    stock = stock_map(api, store_id, items) if args.stock and store_id else {}
    for i, p in enumerate(items[: args.limit], 1):
        print_product_row(i, p, stock_cell(stock, p) if args.stock else None)
    print("\nNote: search has no pickup-today filter; use a category from above with "
          "`epicentrk products <category> --today`.")


def cmd_filters(api, args, state):
    store_id = resolve_store(args, state, required=args.today)
    path = resolve_path(api, args, store_id)
    fdata = api.filters(path, store_id, args.today)
    if args.json:
        return dump(fdata)
    print(f"Listing path: {path}" + (f"  [pickup today @ store {store_label(state, store_id)}]" if args.today else ""))
    rng = (fdata.get("price") or {}).get("rangeData") or {}
    if rng:
        print(f"\nЦіна (--price MIN-MAX): {round(rng.get('minPrice') or 0)}–{round(rng.get('maxPrice') or 0)}")
    for key, flag in (("brand", "--brand"), ("seller", "--seller"), ("promo", "--promo")):
        block = fdata.get(key) or {}
        opts = block.get("list") or []
        if not opts:
            continue
        shown = opts if args.all or len(opts) <= args.max_values else \
            sorted(opts, key=lambda o: -(o.get("productsAvailable") or 0))[: args.max_values]
        vals = ", ".join(f"{'*' if o.get('checked') else ''}{o['name']}"
                         + (f" ({o['productsAvailable']})" if o.get("productsAvailable") else "") for o in shown)
        more = f" … +{len(opts) - len(shown)} more (--all)" if len(shown) < len(opts) else ""
        print(f"\n{block.get('value', key)} ({flag}): {vals}{more}")
    for f in fdata.get("filters") or []:
        name = f["name"].strip()
        if f["type"] == "range":
            applied = (f.get("chosenMin"), f.get("chosenMax")) != (f.get("min"), f.get("max"))
            print(f"\n{name} [prop {f['prop_id']}, range: -f \"{f['prop_id']}=MIN..MAX\"]: {f['min']}..{f['max']}"
                  + (f"  *applied {f['chosenMin']}..{f['chosenMax']}" if applied else ""))
            continue
        opts = f.get("list") or []
        shown = opts if args.all or len(opts) <= args.max_values else opts[: args.max_values]
        vals = ", ".join(f"{'*' if o.get('checked') else ''}{o['name']} ({o.get('productsAvailable', 0)})" for o in shown)
        more = f" … +{len(opts) - len(shown)} more (--all)" if len(shown) < len(opts) else ""
        print(f"\n{name} [prop {f['prop_id']}]: {vals}{more}")
    print("\n* = currently applied. Use: -f \"<name or prop id>=<value>[|<value2>]\"")


def cmd_products(api, args, state):
    store_id = resolve_store(args, state, required=args.today or args.stock)
    path = resolve_path(api, args, store_id)
    data = api.products(path, store_id, args.today, args.sort, args.page, args.limit)
    if args.json:
        return dump(data)
    items = data.get("items") or []
    if args.sort_unit:
        items = sort_by_unit(items)
    scope = f"pickup today @ store {store_label(state, store_id)}" if args.today else "all Ukraine"
    print(f"{path}  [{scope}]")
    print(f"{data.get('totalCount')} products, page {data.get('pageIndex')}/{data.get('totalPages')}"
          + (f", sort={args.sort}" if args.sort else "")
          + (" (this page re-sorted by unit price)" if args.sort_unit else ""))
    stock = stock_map(api, store_id, items) if args.stock else {}
    offset = (args.page - 1) * args.limit
    for i, p in enumerate(items, offset + 1):
        print_product_row(i, p, stock_cell(stock, p) if args.stock else None)


def cmd_product(api, args, state):
    store_id = resolve_store(args, state)
    slug = product_slug(args.product)
    card = api.product_card(slug, store_id)
    if args.json:
        return dump(card)
    price = f"{card['price']} ₴"
    price += old_price_text(card)
    print(card["name"])
    up = unit_price_text(card)
    print(f"Ціна: {price} за {card.get('measure', '')}" + (f", {up}" if up else ""))
    print(f"Бренд: {(card.get('brand') or {}).get('name')} | Продавець: {(card.get('seller') or {}).get('name')}")
    print(f"Наявність: {(card.get('availabilityStatus') or {}).get('name')} | Рейтинг: {card.get('rating')} "
          f"({card.get('commentsCount')} відгуків)")
    print(f"Категорія: {card.get('sectionName')} [{card.get('sectionId')}]")
    print(f"URL: {card.get('url')}")
    print(f"slug: {slug} | article: {card.get('id')} | productId: {card.get('productId')}")

    if store_id:
        try:
            left = api.leftovers(store_id, [card["productId"]]).get(str(card["productId"]))
            amount = f"{fmt_num(left['amount'])} {left.get('measure', '')}" if left else "немає"
        except ApiError:
            amount = "н/д (товар маркетплейсу)"
        print(f"У магазині {store_label(state, store_id)}: {amount}")

    if card.get("models") and not args.no_variants:
        print_variants(api, card["models"], store_id)

    for line in card.get("redText") or []:
        print(f"! {html_to_text(line)}")
    for a in card.get("actions") or []:
        print(f"Акція: {a.get('name')}")

    props = card.get("properties") or {}
    groups = props.get("table") or []
    if groups:
        print("\nХарактеристики:")
        for g in groups:
            if g.get("name"):
                print(f"  [{g['name']}]")
            for pr in g.get("properties") or []:
                vals = ", ".join(str(v.get("value")) for v in pr.get("values") or [])
                hint = f" {pr['hint']}" if pr.get("hint") else ""
                print(f"  {pr['name']}: {vals}{hint}")

    desc = html_to_text(card.get("description"))
    if desc:
        if args.max_desc and len(desc) > args.max_desc:
            desc = desc[: args.max_desc] + " …"
        print(f"\nОпис:\n{desc}")

    docs = card.get("documentation") or []
    if docs:
        print("\nДокументи: " + "; ".join(f"{d.get('text')}: {d.get('link')}" for d in docs))

    if args.stores:
        if not store_id:
            die("--stores needs a selected store (distance is measured from it)")
        data = api.pickup_stores(card["id"], store_id, state.get("city_id", KYIV_CITY_ID))
        all_points = data.get("stores") or []
        stores = [s for s in all_points if args.all_types or s.get("storeTypeId") == 1]
        # The API's `dist` is from the city centre; re-measure from the selected store.
        origin = next(((float(s["lat"]), float(s["lng"])) for s in all_points if s["id"] == store_id), None)
        if not origin and state.get("store_id") == store_id and state.get("lat"):
            origin = (state["lat"], state["lng"])
        if origin:
            for s in stores:
                s["dist"] = round(haversine(origin[0], origin[1], float(s["lat"]), float(s["lng"])), 2)
            stores.sort(key=lambda s: s["dist"])
        in_stock = [s for s in stores if ((s.get("storeElements") or [{}])[0].get("pickup_amount") or 0) > 0]
        print(f"\nНайближчі {'точки' if args.all_types else 'гіпермаркети'} з наявністю "
              f"({len(in_stock)} з {len(stores)}):")
        for s in in_stock[: args.stores]:
            el = s["storeElements"][0]
            pickup = f", самовивіз: {el['pickup']}" if el.get("pickup") and el["pickup"] != "повідомимо додатково" else ""
            print(f"  {s['id']}: {s['title']} — {s.get('dist')} км, {el['pickup_amount']} "
                  f"{el.get('pickup_measure', '')}{pickup}")

    if args.reviews:
        rv = api.reviews(slug, limit=args.reviews)
        items = (rv.get("review") or {}).get("items") or []
        summary = ((rv.get("main") or {}).get("rating") or {})
        print(f"\nВідгуки ({(rv.get('main') or {}).get('review_count', 0)}, "
              f"розподіл {summary.get('items')}):")
        for r in items:
            print(f"- ★{r.get('rating')} {r.get('create')} {r.get('name')}: {r.get('message')}")
            if r.get("benefits"):
                print(f"    + {r['benefits']}")
            if r.get("disadvantages"):
                print(f"    − {r['disadvantages']}")


def cmd_compare(api, args, state):
    store_id = resolve_store(args, state)
    slugs = [product_slug(p) for p in args.products]
    with ThreadPoolExecutor(6) as ex:
        cards = list(ex.map(lambda s: api.product_card(s, store_id), slugs))
    if args.json:
        return dump(cards)

    stock = {}
    own = [c["productId"] for c in cards if not str(c.get("id", "")).startswith("MP")]
    if store_id and own:
        try:
            stock = {k: f"{fmt_num(v['amount'])} {v.get('measure', '')}".strip()
                     for k, v in api.leftovers(store_id, own).items()}
        except ApiError as e:
            print(f"(stock lookup failed: {e})", file=sys.stderr)

    def price(c):
        return f"{c['price']} ₴" + old_price_text(c)

    rows = [
        ("Назва", [c["name"] for c in cards]),
        ("Ціна", [price(c) for c in cards]),
        ("Ціна за од.", [unit_price_text(c) or "—" for c in cards]),
        ("Бренд", [(c.get("brand") or {}).get("name") for c in cards]),
        ("Продавець", [(c.get("seller") or {}).get("name") for c in cards]),
        ("Рейтинг", [f"{c.get('rating')} ({c.get('commentsCount')})" for c in cards]),
        ("Наявність", [(c.get("availabilityStatus") or {}).get("name") for c in cards]),
    ]
    if store_id:
        rows.append((f"У магазині {store_id}", [
            "н/д (маркетплейс)" if str(c.get("id", "")).startswith("MP") else stock.get(str(c["productId"]), "немає")
            for c in cards]))

    specs, order = {}, []
    for i, c in enumerate(cards):
        for g in (c.get("properties") or {}).get("table") or []:
            for pr in g.get("properties") or []:
                name = pr["name"].strip()
                if name == "Бренд":
                    continue
                if name not in specs:
                    specs[name] = [None] * len(cards)
                    order.append(name)
                vals = ", ".join(str(v.get("value")) for v in pr.get("values") or [])
                specs[name][i] = f"{vals} {pr['hint']}" if pr.get("hint") else vals
    rows += [(name, specs[name]) for name in order]
    if args.diff:
        rows = rows[:1] + [r for r in rows[1:] if len({cell(v, 0) for v in r[1]}) > 1]

    w = args.width
    print("| | " + " | ".join(str(i) for i in range(1, len(cards) + 1)) + " |")
    print("|---" * (len(cards) + 1) + "|")
    for name, vals in rows:
        print(f"| {cell(name, 40)} | " + " | ".join(cell(v, w) for v in vals) + " |")
    print()
    for i, (c, slug) in enumerate(zip(cards, slugs), 1):
        print(f"{i}. slug: {slug}")
    if args.diff:
        print("(only rows that differ; drop --diff to see everything)")


def cmd_stores(api, args, state):
    city_id = state.get("city_id", KYIV_CITY_ID)
    origin = None
    if args.city:
        cities = api.city_search(args.city)
        if not cities:
            die(f"city not found: {args.city}")
        city_id = cities[0]["id"]
        print(f"City: {cities[0]['name']} (id {city_id})")
    if args.address:
        lat, lng, name = geocode(args.address)
        origin = (lat, lng)
        print(f"Address resolved to: {name} ({lat:.5f},{lng:.5f})")
    elif args.near:
        origin = parse_latlng(args.near)
    data = api.stores(city_id)
    stores = data.get("stores") or []
    if args.type != "all":
        wanted = {int(t) for t in args.type.split(",")}
        stores = [s for s in stores if s.get("storeTypeId") in wanted]
    if origin:
        for s in stores:
            s["dist"] = round(haversine(origin[0], origin[1], float(s["lat"]), float(s["lng"])), 2)
        stores.sort(key=lambda s: s["dist"])
    if args.json:
        return dump(stores[: args.limit])
    print(f"{'id':>5}  {'км':>6}  {'сьогодні':<12} адреса")
    for s in stores[: args.limit]:
        kind = "" if s.get("storeTypeId") == 1 else f"  [{STORE_TYPES.get(s.get('storeTypeId'), s.get('storeTypeId'))}]"
        print(f"{s['id']:>5}  {s.get('dist', ''):>6}  {today_schedule(s):<12} {s['title']}{kind}")
    print("\nSelect one with: epicentrk store set <id>")


def cmd_store(api, args, state):
    if args.action == "show" or args.action is None:
        if not state.get("store_id"):
            print("No store selected. Use `epicentrk stores --address ...` then `epicentrk store set <id>`.")
        else:
            dump(state)
        return
    if args.action == "set":
        if not args.id:
            die("usage: epicentrk store set <id>")
        stores = api.stores(state.get("city_id", KYIV_CITY_ID)).get("stores") or []
        match = next((s for s in stores if s["id"] == int(args.id)), None)
        if not match:
            die(f"store id {args.id} not found")
        state.update(store_id=match["id"], title=match["title"], city_id=match.get("cityId"),
                     lat=float(match["lat"]), lng=float(match["lng"]),
                     type=STORE_TYPES.get(match.get("storeTypeId"), match.get("storeTypeId")))
        save_state(state)
        print(f"Selected store {match['id']}: {match['title']}")


# --- argparse ---------------------------------------------------------------

def add_listing_args(p):
    p.add_argument("category", help="category slug (shurupoverty), site URL, or listing path (/shop/kraski/fs/cvet-belyy/)")
    p.add_argument("-f", "--filter", action="append", metavar="KEY=VAL",
                   help="property filter: KEY is prop id or name, VAL is value name (| for several, MIN..MAX for ranges). Repeatable")
    p.add_argument("--brand", action="append", help="brand name, repeatable")
    p.add_argument("--seller", action="append", help="epicentrk | other (marketplace)")
    p.add_argument("--promo", action="append", help="promo name or code, e.g. 'економією'")
    p.add_argument("--price", help="MIN-MAX, MIN- or -MAX (UAH)")
    p.add_argument("--today", action="store_true", help="only goods you can pick up today in the selected store")
    p.add_argument("--store", type=int, help="store id (overrides the saved one)")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="epicentrk", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print raw API JSON instead of text")
    common.add_argument("--delay", type=float, nargs="?", const=DEFAULT_DELAY, metavar="SECONDS",
                        help=f"pause before every HTTP request, including the first one ({DEFAULT_DELAY}s when "
                        "given without a value). REQUIRED when running several commands in a row. "
                        "Env EPICENTRK_DELAY sets the default")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("categories", parents=[common], help="browse the category tree")
    p.add_argument("slug", nargs="?", help="category slug; omit for the top level")
    p.add_argument("--depth", type=int, default=1, help="2 = also print grandchildren")
    p.add_argument("--find", metavar="TEXT",
                   help="find the listing for a product query: a free hit from the local cache, otherwise "
                   "the site search plus the matching category's presets (at most 4 requests)")
    p.add_argument("--live", action="store_true", help="with --find: skip the local cache, ask the site")
    p.add_argument("--offline", action="store_true",
                   help="with --find: local cache only, no requests (also matches parent category names)")
    p.add_argument("--expand", type=int, default=1, metavar="N",
                   help="with --find: read subcategories/presets of the first N suggested categories "
                   "(1 request each, default 1; 0 = none)")
    p.add_argument("--limit", type=int, help="with --find: max results (default 20 from the cache, "
                   "10 presets from the site)")
    p.add_argument("--store", type=int, help=argparse.SUPPRESS)
    p.add_argument("--full-crawl", action="store_true",
                   help="crawl the WHOLE tree into the local cache: ~550 requests, 10-15 min with --delay. "
                   "Never needed for --find; resumable")
    p.set_defaults(func=cmd_categories)

    p = sub.add_parser("search", parents=[common], help="full-text product search (also suggests categories)")
    p.add_argument("query")
    p.add_argument("--brand", action="append")
    p.add_argument("--price", help="MIN-MAX")
    p.add_argument("--page", type=int)
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--stock", action="store_true", help="show stock in the selected store")
    p.add_argument("--sort-unit", action="store_true", help="re-sort results by price per kg/l/m²/piece")
    p.add_argument("--store", type=int)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("filters", parents=[common], help="list available filters for a category (counts reflect applied filters)")
    add_listing_args(p)
    p.add_argument("--all", action="store_true", help="show every value")
    p.add_argument("--max-values", type=int, default=15)
    p.set_defaults(func=cmd_filters)

    p = sub.add_parser("products", parents=[common], help="list products in a category with filters")
    add_listing_args(p)
    p.add_argument("--sort", choices=SORTS, help="rating (default), asc, desc, aktsii, new, amount")
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--limit", type=int, default=30, help="page size (max 60)")
    p.add_argument("--stock", action="store_true", help="show stock in the selected store (Epicentr's own goods)")
    p.add_argument("--sort-unit", action="store_true",
                   help="re-sort this page by price per kg/l/m²/piece (the API can't sort by it)")
    p.set_defaults(func=cmd_products)

    p = sub.add_parser("product", parents=[common], help="full product details: specs, description, stock, reviews")
    p.add_argument("product", help="product slug or URL")
    p.add_argument("--store", type=int)
    p.add_argument("--stores", type=int, metavar="N", help="also show stock in the N nearest hypermarkets")
    p.add_argument("--all-types", action="store_true", help="with --stores: include pickup points, express shops")
    p.add_argument("--reviews", type=int, metavar="N", help="also show N latest reviews")
    p.add_argument("--max-desc", type=int, default=0, help="truncate description to N chars (0 = full)")
    p.add_argument("--no-variants", action="store_true", help="skip the sizes/weights/colours list")
    p.set_defaults(func=cmd_product)

    p = sub.add_parser("compare", parents=[common], help="side-by-side spec table of several products")
    p.add_argument("products", nargs="+", help="product slugs or URLs (2–6 is readable)")
    p.add_argument("--diff", action="store_true", help="show only rows where the products differ")
    p.add_argument("--width", type=int, default=0, help="truncate cells to N chars (0 = no limit)")
    p.add_argument("--store", type=int)
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("stores", parents=[common], help="list stores, nearest first")
    p.add_argument("--address", help="sort by distance from this address (geocoded via OpenStreetMap)")
    p.add_argument("--near", help="sort by distance from LAT,LNG")
    p.add_argument("--city", help="sort by distance from this city's centre")
    p.add_argument("--type", default="1", help="store type ids, comma-separated, or 'all' (1 = hypermarket, "
                   "12 = Експрес, 10 = pickup point)")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(func=cmd_stores)

    p = sub.add_parser("store", parents=[common], help="show or set the default store")
    p.add_argument("action", nargs="?", choices=["show", "set"])
    p.add_argument("id", nargs="?")
    p.set_defaults(func=cmd_store)

    args = ap.parse_args(argv)
    if getattr(args, "limit", None) and args.cmd == "products":
        args.limit = min(args.limit, 60)
    delay = args.delay if args.delay is not None else float(os.environ.get("EPICENTRK_DELAY") or 0)
    api = Epicentrk(delay=delay)
    try:
        args.func(api, args, load_state())
    except ApiError as e:
        die(str(e))
    except requests.RequestException as e:
        die(f"network error: {e}")


if __name__ == "__main__":
    main()
