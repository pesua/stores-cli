"""olx — search olx.ua classified ads from the command line (built for coding agents).

Searches the whole of Ukraine unless you pass --city / --region. Only reads ads: no login, no
phone numbers, no messages. Every row carries risk flags (no OLX Доставка, new account, inactive
seller, suspiciously cheap) because most ads come from private sellers of used goods.

Run `olx <command> -h` for details. Typical flow:
  olx search "шуруповерт makita"                     # ads + the categories they fall into
  olx filters "шуруповерт makita" --category 657     # what the category can be filtered by, with counts
  olx search "шуруповерт makita" --category 657 --state used --delivery --sort cheap
  olx search "велосипед" --city Дніпро --radius 30   # around one place, with the distance to each ad
  olx product 936565403                              # full ad, seller card, seller rating, flags
  olx compare 936565403 937503857 --diff
  olx seller --ad 936565403                          # the seller's other ads

Running several commands in a row? Append --delay to each one (a pause before every request).
"""

import argparse
import datetime
import html
import json
import os
import re
import sys
import unicodedata

from curl_cffi.requests.exceptions import RequestException

from client import MAX_LIMIT, MAX_RESULTS, PAGE_SIZE, ApiError, Olx, ad_id, distance_km
from flags import ad_flags, days_since, page_median, price_text, score_text, uah

DEFAULT_DELAY = 1.5
SORTS = {"relevance": "relevance:desc", "new": "created_at:desc",
         "cheap": "filter_float_price:asc", "expensive": "filter_float_price:desc"}
# filters with their own options, so -f doesn't offer them
OWN_OPTIONS = {"sort_by", "owner_type", "filter_float_price", "filter_enum_state"}
RELABEL = {"filter_enum_price": "Тип ціни"}


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


def fmt(x):
    return f"{int(x)}" if float(x).is_integer() else f"{x:g}"


def parse_range(text):
    lo, _, hi = text.partition("..") if ".." in text else text.partition("-")
    return (num(lo) if lo.strip() else None), (num(hi) if hi.strip() else None)


LOOKALIKES = str.maketrans("aceiopxykmhtb", "асеіорхукмнтв")  # OLX labels mix them in: "Kолір"


def fold(text):
    """Case- and accent-insensitive form, with the apostrophe variants unified and Latin letters that
    look Cyrillic turned Cyrillic (applied to both sides of a comparison, so Latin names still match)."""
    text = unicodedata.normalize("NFKD", str(text).strip().casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[’ʼ`']", "'", text).translate(LOOKALIKES)


def _match(options, wanted, what, key="label"):
    """Pick one option by exact value, exact label, or unique substring of the label."""
    w = fold(wanted)
    for o in options:
        if fold(o.get("value", "")) == w or fold(o.get("name", "")) == w:
            return o
    exact = [o for o in options if fold(o.get(key, "")) == w]
    if exact:
        return exact[0]
    partial = [o for o in options if w in fold(o.get(key, ""))]
    if len(partial) == 1:
        return partial[0]
    names = ", ".join(str(o.get(key)).strip() for o in (partial or options)[:40])
    die(f"{what} '{wanted}' is {'ambiguous' if partial else 'not found'}; options: {names}")


def one_line(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def plain(text):
    """Ad descriptions are HTML with <br />."""
    text = re.sub(r"<br\s*/?>\r?\n?", "\n", text or "", flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line.rstrip() for line in text.splitlines())).strip()


def ago(iso):
    d = days_since(iso)
    if d is None:
        return "?"
    return "сьогодні" if d == 0 else f"{d} дн. тому"


# --- catalog: categories and filter definitions -----------------------------

def cat_trail(catalog, cat):
    names, c = [], cat
    while c:
        names.append(c["name"])
        c = catalog["categories"].get(str(c.get("parentId")))
    return " › ".join(reversed(names))


def resolve_category(catalog, ref):
    """Category from an id, a slug (elektroinstrument) or a site path (dom-i-sad/instrumenty/elektroinstrument)."""
    cats = catalog["categories"]
    ref = str(ref).strip().strip("/")
    if ref.isdigit():
        if ref not in cats:
            die(f"no category {ref} (see `olx categories --find …`)")
        return cats[ref]
    ref = re.sub(r"^(https?://[^/]+)?/?(uk/)?", "", ref).strip("/")
    hits = [c for c in cats.values() if c["path"] == ref] or \
           [c for c in cats.values() if c["path"].rsplit("/", 1)[-1] == ref]
    if len(hits) == 1:
        return hits[0]
    die(f"category '{ref}' is {'ambiguous' if hits else 'not found'}: use its id (`olx categories --find …`)")


def category_filters(catalog, cat_id):
    """[{name, type, label, values, unit, order}] that apply to a category (0 = no category)."""
    out = []
    for name, defs in catalog["filters"].items():
        for d in defs:
            if cat_id in d["categories"]:
                out.append({**d, "name": name, "label": RELABEL.get(name, d["label"])})
                break
    return sorted(out, key=lambda f: (f["order"], f["label"]))


def is_range(f):
    return f["type"] in ("input", "price", "salary") and f["name"].startswith("filter_float_")


def apply_filters(params, catalog, cat_id, specs):
    fs = [f for f in category_filters(catalog, cat_id) if f["name"] not in OWN_OPTIONS
          and f["type"] != "dynamicmultichoice"]
    for spec in specs or []:
        key, sep, vals = spec.partition("=")
        if not sep:
            die(f"filter must look like NAME=VALUE[|VALUE2] or NAME=MIN..MAX, got: {spec}")
        if not fs:
            die("no filters here; pass --category ID (see `olx filters`)")
        f = _match(fs, key, "filter" + ("" if cat_id else " (without --category only general filters exist)"))
        if is_range(f):
            lo, hi = parse_range(vals)
            if lo is not None:
                params[f"{f['name']}:from"] = fmt(lo)
            if hi is not None:
                params[f"{f['name']}:to"] = fmt(hi)
        elif f["type"] == "checkbox":
            params[f["name"]] = 1
        elif f["values"]:
            chosen = [_match(f["values"], v, f"value of '{f['label']}'")["value"] for v in vals.split("|")]
            for i, v in enumerate(chosen):
                params[f"{f['name']}[{i}]"] = v
        else:
            params[f["name"]] = vals


# --- locations --------------------------------------------------------------

def resolve_city(api, ref):
    """{id, name, region, lat, lon} from a city id or a name; "Обухівка, Дніпро" narrows by
    municipality or region."""
    ref = str(ref).strip()
    if ref.isdigit():
        c = api.city(ref)
        region = next((r["name"] for r in api.regions() if r["id"] == c.get("region_id")), "?")
        return {"id": c["id"], "name": c["name"], "region_id": c.get("region_id"), "region": region,
                "lat": c.get("latitude"), "lon": c.get("longitude")}
    name, _, hint = ref.partition(",")
    found = places(api.geo(name.strip()))
    if hint.strip():
        h = fold(hint)
        found = [g for g in found if h in fold((g.get("municipality") or {}).get("name", ""))
                 or h in fold((g.get("region") or {}).get("name", ""))]
    if not found:
        die(f"no place '{ref}' on OLX (see `olx geo \"{name.strip()}\"`)")
    g = found[0]
    exact = [x for x in found if fold(x["city"]["name"]) == fold(name)]
    if len(exact) > 1:
        warn(f"several places named '{name.strip()}', took {g['city']['name']} ({g['region']['name']}); "
             f"give \"{name.strip()}, <район або область>\" or a city id from `olx geo`")
    return {"id": g["city"]["id"], "name": g["city"]["name"], "region_id": g["region"]["id"],
            "region": g["region"]["name"], "lat": g["city"].get("lat"), "lon": g["city"].get("lon")}


def places(found):
    """Geo matches without district entries: a city's districts come back as extra rows with the city's id."""
    out, seen = [], set()
    for g in found:
        if g.get("city") and g["city"]["id"] not in seen:
            seen.add(g["city"]["id"])
            out.append(g)
    return out


def resolve_region(api, ref):
    regions = [{**r, "label": r["name"], "value": r["normalized_name"]} for r in api.regions()]
    if str(ref).isdigit():
        return next((r for r in regions if str(r["id"]) == str(ref)), None) or die(f"no region {ref}")
    return _match(regions, ref, "region")


# --- listings ---------------------------------------------------------------

def build_params(api, args, catalog):
    """([(category or None, params)], city or None) from the CLI options: one search per --category."""
    base = {}
    if getattr(args, "url", None):
        base.update(api.url_params(args.url))
    if getattr(args, "query", None):
        base["query"] = args.query
    cats = [resolve_category(catalog, c) for c in args.category or []]
    if not cats and base.get("category_id"):
        cats = [c for c in [catalog["categories"].get(str(base["category_id"]))] if c]
    city = None
    if args.city:
        city = resolve_city(api, args.city)
        base.update(city_id=city["id"], region_id=city["region_id"])
        print(f"(місто: {city['name']}, {city['region']}, id {city['id']}"
              + (f", радіус {fmt(args.radius)} км)" if args.radius else ")"), file=sys.stderr)
        if args.radius:
            base["distance"] = fmt(args.radius)
    elif args.region:
        base["region_id"] = resolve_region(api, args.region)["id"]
    if args.state:
        base["filter_enum_state[0]"] = {"new": "new", "used": "used"}[args.state]
    if args.private or args.business:
        base["owner_type"] = "private" if args.private else "business"
    if args.delivery:
        base["courier"] = "true"
    if args.price:
        lo, hi = parse_range(args.price)
        if lo is not None:
            base["filter_float_price:from"] = fmt(lo)
        if hi is not None:
            base["filter_float_price:to"] = fmt(hi)
    if getattr(args, "sort", None):
        base["sort_by"] = SORTS[args.sort]
        if args.sort == "cheap" and "filter_float_price:from" not in base:
            base["filter_float_price:from"] = 1  # otherwise "обмін", "безкоштовно" and 0 ₴ come first
    searches = []
    for cat in cats or [None]:
        params = dict(base)
        if cat:
            params["category_id"] = cat["id"]
        apply_filters(params, catalog, cat["id"] if cat else 0, args.filter)
        searches.append((cat, params))
    return searches, city


def rows_of(res):
    promoted = set((res.get("metadata") or {}).get("promoted") or [])
    return [(ad, n in promoted) for n, ad in enumerate(res.get("data") or [])]


def fetch(api, args, params):
    """{total, rows: [(ad, promoted)], raw} for one search: the requested page or, with --all, every page
    the API gives (up to 1000 ads). Promoted ads repeat on every page, so rows are deduplicated."""
    if not getattr(args, "all", False):
        if args.page < 1 or (args.page - 1) * args.limit >= MAX_RESULTS:
            die(f"OLX returns only the first {MAX_RESULTS} ads of a search; narrow it with filters instead of paging")
        res = api.offers(params, args.limit, (args.page - 1) * args.limit)
        return {"total": (res.get("metadata") or {}).get("visible_total_count") or 0, "rows": rows_of(res), "raw": res}
    pages = [api.offers(params, MAX_LIMIT, 0)]
    total = (pages[0].get("metadata") or {}).get("visible_total_count") or 0
    offsets = range(MAX_LIMIT, min(total, MAX_RESULTS), MAX_LIMIT)
    if offsets:
        print(f"(завантажую ще {len(offsets)} стор. по {MAX_LIMIT})", file=sys.stderr)
    pages += [api.offers(params, MAX_LIMIT, o) for o in offsets]
    rows, seen = [], set()
    for res in pages:
        for ad, promoted in rows_of(res):
            if ad["id"] not in seen:
                seen.add(ad["id"])
                rows.append((ad, promoted))
    return {"total": total, "rows": rows, "raw": pages}


def title_has(ad, words):
    """Every query word is in the title, ignoring spaces and dashes inside it ("V 600" matches "v600")."""
    title = fold(ad.get("title") or "")
    compact = re.sub(r"[\s\-_/.]+", "", title)
    return all(w in title or re.sub(r"[\s\-_/.]+", "", w) in compact for w in words)


def sort_rows(rows, sort):
    """Local sort for results merged from several categories (the API sorts each one separately)."""
    if sort in ("cheap", "expensive"):
        priced = [r for r in rows if uah(r[0])]
        rest = [r for r in rows if not uah(r[0])]
        return sorted(priced, key=lambda r: uah(r[0]), reverse=sort == "expensive") + rest
    if sort == "new":
        return sorted(rows, key=lambda r: datetime.datetime.fromisoformat(r[0]["last_refresh_time"]), reverse=True)
    return rows


def seller_text(ad):
    u = ad.get("user") or {}
    name = u.get("company_name") or u.get("name") or "?"
    since = (u.get("created") or "")[:4]
    return f"{'бізнес' if ad.get('business') else 'приватна особа'}: {one_line(name, 40)}" + \
           (f", на OLX з {since}" if since else "")


def state_of(ad):
    p = next((x for x in ad.get("params") or [] if x.get("key") == "state"), None)
    return ((p or {}).get("value") or {}).get("label")


def place_text(ad, city=None):
    loc = ad.get("location") or {}
    text = (loc.get("city") or {}).get("name") or "?"
    if loc.get("district"):
        text += f" ({loc['district']['name']})"
    region = (loc.get("region") or {}).get("name") or ""
    if region:
        text += ", " + region.replace(" область", " обл.")
    m = ad.get("map") or {}
    if city:
        d = distance_km(city["lat"], city["lon"], m.get("lat"), m.get("lon"))
        if d is not None:
            text += f" — {d:.0f} км"
    return text


def print_ad(i, ad, promoted=False, median=None, city=None):
    print(f"{i}. {one_line(ad['title'], 200)} — {price_text(ad)}")
    extra = [f"id {ad['id']}"]
    if state_of(ad):
        extra.append(state_of(ad))
    added = f"додано {ago(ad.get('created_time'))}"
    if (days_since(ad.get("created_time")) or 0) - (days_since(ad.get("last_refresh_time")) or 0) > 1:
        added += f", піднято {ago(ad.get('last_refresh_time'))}"  # --sort new sorts by this date
    extra += [place_text(ad, city), seller_text(ad), added]
    if (ad.get("partner") or {}).get("code"):
        extra.append(f"партнер {ad['partner']['code']}")
    if promoted:
        extra.append("реклама")
    print("   " + " | ".join(extra))
    flags = ad_flags(ad, median)
    if flags:
        print("   ⚠ " + "; ".join(flags))
    print(f"   {ad['url']}")


def print_listing(results, args, city=None, words=None):
    """results: [(category or None, fetched)] from one or several searches, merged into one numbered list."""
    single_page = len(results) == 1 and not getattr(args, "all", False)
    totals = [f["total"] for _, f in results]
    note = f" (API віддає лише перші {MAX_RESULTS} на пошук: звужуйте фільтрами)" if max(totals) > MAX_RESULTS else ""
    if len(results) == 1:
        head = f"Знайдено оголошень: {totals[0]}{note}"
    else:
        head = "Знайдено оголошень: " + " + ".join(f"{c['name'] if c else '?'} {f['total']}" for c, f in results) + note
    rows, seen = [], set()
    for _, f in results:
        for ad, promoted in f["rows"]:
            if ad["id"] not in seen:
                seen.add(ad["id"])
                rows.append((ad, promoted))
    if single_page:
        pages = max(1, -(-min(totals[0], MAX_RESULTS) // args.limit))
        head += f", сторінка {args.page}/{pages}"
    else:
        head += f", завантажено {len(rows)}" if getattr(args, "all", False) else f", сторінка {args.page} з кожної категорії"
    if args.sort:
        head += f", sort={args.sort}"
    if args.no_ads:
        rows = [r for r in rows if not r[1]]
    if words:
        rows = [r for r in rows if title_has(r[0], words)]
        head += f"; у назві всі слова запиту: {len(rows)}"
        if not getattr(args, "all", False):
            head += " (лише на цій сторінці; --all перевірить усі)"
    if len(results) > 1:
        rows = sort_rows(rows, args.sort)
    print(head)
    median = page_median([ad for ad, _ in rows])
    start = (args.page - 1) * args.limit + 1 if single_page else 1
    for i, (ad, is_ad) in enumerate(rows, start):
        print_ad(i, ad, is_ad, median, city)
    if not rows:
        print("(нічого не знайдено)")


def title_words(args, params):
    if not getattr(args, "title", False):
        return None
    text = getattr(args, "query", None) or getattr(args, "find", None) or params.get("query")
    if not text:
        die("--title needs a search query")
    return fold(text).split()


def categories_text(catalog, facets, limit):
    """The categories a search falls into, biggest first, without parents that only repeat their children."""
    vals = facets.get("category") or []
    total = facets.get("total") or 1
    cats = catalog["categories"]
    vals = [v for v in vals if v["count"] >= max(2, total * 0.02)]

    def children_cover(v):
        kids = set(map(str, (cats.get(str(v["id"])) or {}).get("children") or []))
        return sum(w["count"] for w in vals if str(w["id"]) in kids) >= v["count"] * 0.8

    # a parent mostly made of listed children ("Дім і сад" over "Інструменти") adds nothing
    shown = sorted((v for v in vals if not children_cover(v)), key=lambda v: -v["count"])
    if shown:
        return "Категорії (--category ID): " + ", ".join(f"{v['label']} [{v['id']}] ({v['count']})" for v in shown[:limit])
    return ""


def print_categories(api, catalog, params, limit=12):
    """Categories line for a search without --category (an extra request)."""
    facets = api.facets({k: v for k, v in params.items() if k != "category_id"}, ["category"], limit=40)
    text = categories_text(catalog, facets, limit)
    if text:
        print(text)


# --- commands ---------------------------------------------------------------

def cmd_search(api, args):
    if not (args.query or args.category or args.url):
        die("give a search query, --category or --url")
    catalog = api.catalog()
    searches, city = build_params(api, args, catalog)
    results = [(cat, fetch(api, args, params)) for cat, params in searches]
    words = title_words(args, searches[0][1])
    if args.json:
        if len(results) == 1 and not args.all and not words:
            return dump(results[0][1]["raw"])
        rows = [r for _, f in results for r in f["rows"] if not words or title_has(r[0], words)]
        return dump({"searches": [{"category_id": c["id"] if c else None, "params": p, "total": f["total"]}
                                  for (c, p), (_, f) in zip(searches, results)],
                     "promoted": sorted({ad["id"] for ad, promoted in rows if promoted}),
                     "data": list({ad["id"]: ad for ad, _ in rows}.values())})
    for cat, _ in searches:
        if cat:
            print(f"Категорія: {cat_trail(catalog, cat)} [{cat['id']}]")
    if not searches[0][0] and searches[0][1].get("query"):
        print_categories(api, catalog, searches[0][1])
    print_listing(results, args, city, words)


def cmd_filters(api, args):
    if not (args.query or args.category or args.url):
        die("give a search query, --category or --url")
    catalog = api.catalog()
    searches, city = build_params(api, args, catalog)
    if len(searches) > 1:
        die("`filters` takes one --category")
    cat, params = searches[0]
    fs = category_filters(catalog, cat["id"]) if cat else []
    enums = [f for f in fs if f["type"] in ("select", "checkboxes") and f["name"].startswith("filter_enum_")
             and f["name"] != "filter_enum_price"]
    fields = (["category"] if not cat else []) + ["filter_enum_state", "owner_type"] + \
             [f["name"] for f in enums if f["name"] != "filter_enum_state"] + \
             (["city"] if params.get("region_id") and not params.get("city_id") else ["region"])
    facets = api.facets(params, fields)
    if args.json:
        return dump({"params": params, "facets": facets, "filters": fs})

    where = f" | Категорія: {cat_trail(catalog, cat)} [{cat['id']}]" if cat else ""
    print(f"Знайдено оголошень: {facets.get('total')}{where}")

    def counts(field, labels=None, limit=args.max_values):
        vals = sorted(facets.get(field) or [], key=lambda v: -v["count"])
        shown = vals if args.all else vals[:limit]
        text = ", ".join(f"{(labels or {}).get(str(v['id'])) or v.get('label') or v['id']} ({v['count']})" for v in shown)
        return text + (f" … +{len(vals) - len(shown)} (--all)" if len(shown) < len(vals) else "")

    if not cat and categories_text(catalog, facets, 20):
        print("\n" + categories_text(catalog, facets, 20))
    print()
    if facets.get("filter_enum_state"):
        print(f"Стан (--state new|used): {counts('filter_enum_state', {'new': 'нове', 'used': 'вживане'})}")
    if facets.get("owner_type"):
        print(f"Продавець (--private / --business): {counts('owner_type', {'private': 'приватні', 'business': 'бізнес'})}")
    if facets.get("city"):
        print(f"Міста (--city): {counts('city')}")
    elif facets.get("region"):
        print(f"Області (--region): {counts('region')}")
    print("Ціна (--price MIN-MAX, UAH); OLX Доставка (--delivery); сортування (--sort): " + ", ".join(SORTS))
    for f in fs:
        if f["name"] in OWN_OPTIONS or f["type"] == "dynamicmultichoice":
            continue
        unit = f" {f['unit']}" if f.get("unit") else ""
        if is_range(f):
            print(f"\n{f['label']} [{f['name']}] (діапазон{unit}): -f \"{f['label']}=MIN..MAX\"")
        elif f["type"] == "checkbox":
            print(f"\n{f['label']} [{f['name']}] (прапорець): -f \"{f['label']}=так\"")
        elif f["name"] in facets:
            labels = {v["value"]: v["label"] for v in f["values"]}
            print(f"\n{f['label']} [{f['name']}]: {counts(f['name'], labels)}")
        elif f["values"]:
            vals = f["values"] if args.all else f["values"][: args.max_values]
            more = f" … +{len(f['values']) - len(vals)} (--all)" if len(vals) < len(f["values"]) else ""
            print(f"\n{f['label']} [{f['name']}]: " + ", ".join(v["label"] for v in vals) + more)
        else:
            print(f"\n{f['label']} [{f['name']}] (текст): -f \"{f['label']}=…\"")
    if not cat:
        print("\nФільтри категорії з'являться з --category ID.")
    print("\nUse: -f \"<назва фільтра>=<значення>[|<значення2>]\" or -f \"<назва>=MIN..MAX\"")


def cmd_categories(api, args):
    catalog = api.catalog(refresh=args.refresh)
    cats = catalog["categories"]
    if args.json:
        return dump(cats)
    if args.find:
        words = fold(args.find).split()
        def starts(c):  # every word starts a word of the trail or the path ("дрилі" must not hit "еспадрилі")
            tokens = re.split(r"[^\w']+", fold(cat_trail(catalog, c) + " " + c["path"]))
            return all(any(t.startswith(w) for t in tokens) for w in words)

        hits = [c for c in cats.values() if starts(c)]
        hits.sort(key=lambda c: (c["level"], c["path"]))
        for c in hits[: args.max]:
            print(f"[{c['id']}] {cat_trail(catalog, c)} — {c['path']}")
        if len(hits) > args.max:
            print(f"… +{len(hits) - args.max} (--max)")
        if not hits:
            print("(нічого; спробуйте `olx search \"…\"`, він покаже категорії запиту)")
        return
    if args.category:
        root = resolve_category(catalog, args.category)
        print(f"[{root['id']}] {cat_trail(catalog, root)} — {root['path']}")
        level0 = root.get("children") or []
    else:
        level0 = sorted((int(c["id"]) for c in cats.values() if not c.get("parentId")),
                        key=lambda i: cats[str(i)]["name"])

    def show(ids, depth, indent):
        for i in ids:
            c = cats.get(str(i))
            if not c:
                continue
            kids = c.get("children") or []
            print(f"{'  ' * indent}[{c['id']}] {c['name']}" + (f" (+{len(kids)})" if kids and depth <= 1 else ""))
            if depth > 1:
                show(kids, depth - 1, indent + 1)

    show(level0, args.depth, 1 if args.category else 0)


def cmd_geo(api, args):
    found = api.geo(args.place)
    if args.json:
        return dump(found)
    for g in places(found):
        c = g["city"]
        muni = (g.get("municipality") or {}).get("name")
        print(f"[{c['id']}] {c['name']}" + (f", {muni} громада/район" if muni else "")
              + f", {g['region']['name']} — --city {c['id']}")
    if not found:
        print("(нічого не знайдено)")


def seller_card(api, ad=None, user_id=None):
    """(user, score, ads_total): /users/{id}, the khonor rating and the count of active ads."""
    uid = user_id or ad["user"]["id"]
    user = api.user(uid)
    return user, api.score(uid), api.total({"user_id": uid})


def print_seller(user, score, ads_total):
    bd = user.get("business_data") or {}
    name = bd.get("company_name") or user.get("name") or "?"
    facts = [f"id {user['id']}", "бізнес" if user.get("is_business") else "приватна особа"]
    if user.get("created"):
        facts.append(f"на OLX з {user['created'][:10]}")
    if user.get("is_online"):
        facts.append("зараз онлайн")
    elif user.get("last_seen"):
        facts.append(f"був(ла) на сайті {ago(user['last_seen'])}")
    facts.append(score_text(score))
    if ads_total is not None:
        facts.append(f"активних оголошень: {ads_total}")
    print(f"Продавець: {name}" + (f" ({user['name']})" if bd.get("company_name") and user.get("name") else ""))
    print("   " + " | ".join(facts))
    if bd.get("address_city"):
        print("   адреса: " + ", ".join(x for x in (bd.get("address_city"), bd.get("address_street"),
                                                     bd.get("address_number")) if x))
    if user.get("user_ads_url"):
        print(f"   {user['user_ads_url']}")


def cmd_product(api, args):
    try:
        aid = ad_id(args.ad)
    except ValueError as e:
        die(str(e))
    ad = api.offer(aid)
    user, score, ads_total = seller_card(api, ad)
    if args.json:
        return dump({"ad": ad, "user": user, "score": score, "seller_ads": ads_total})
    catalog = api.catalog()
    cat = catalog["categories"].get(str((ad.get("category") or {}).get("id")))
    print(ad["title"])
    print(f"Ціна: {price_text(ad)}" + (f" | Стан: {state_of(ad)}" if state_of(ad) else ""))
    if cat:
        print(f"Категорія: {cat_trail(catalog, cat)} [{cat['id']}]")
    print(f"Місце: {place_text(ad)}")
    print(f"Додано: {ad.get('created_time', '')[:10]} ({ago(ad.get('created_time'))}), "
          f"оновлено {ago(ad.get('last_refresh_time'))}, діє до {str(ad.get('valid_to_time'))[:10]}"
          + ("" if ad.get("status") == "active" else f" | статус: {ad.get('status')}"))
    sd = ad.get("safedeal") or {}
    if (ad.get("contact") or {}).get("courier"):
        weight = f" (посилка до {sd['weight_grams'] / 1000:g} кг)" if sd.get("weight_grams") else ""
        print(f"OLX Доставка: так{weight}; оплата на OLX із захистом покупця")
    else:
        print("OLX Доставка: ні (лише зустріч або домовленість з продавцем)")
    print_seller(user, score, ads_total)
    flags = ad_flags(ad, score=score)
    if flags:
        print("⚠ " + "; ".join(flags))
    print(f"URL: {ad['url']}")

    params = [p for p in ad.get("params") or [] if p.get("key") not in ("price", "state")]
    if params:
        print("\nПараметри:")
        for p in params:
            print(f"  {p['name']}: {param_value(p)}")
    desc = plain(ad.get("description"))
    if desc:
        if args.max_desc and len(desc) > args.max_desc:
            desc = desc[: args.max_desc] + " …"
        print(f"\nОпис:\n{desc}")
    print(f"\nФото: {len(ad.get('photos') or [])}")
    if args.photos:
        for ph in ad.get("photos") or []:
            print("  " + ph["link"].format(width=ph.get("width") or 1000, height=ph.get("height") or 1000))


def param_value(p):
    v = p.get("value") or {}
    if p.get("type") == "checkbox" and isinstance(v, dict):
        return "так" if str(v.get("key")) in ("1", "true") else "ні"
    if isinstance(v, list):
        return ", ".join(x.get("label") or str(x.get("key")) for x in v)
    return v.get("label") or str(v.get("key") or v.get("value") or "")


def cmd_seller(api, args):
    if args.ad:
        try:
            ad = api.offer(ad_id(args.ad))
        except ValueError as e:
            die(str(e))
        uid = ad["user"]["id"]
    elif args.user:
        uid = args.user
    else:
        die("give a seller id or --ad <ad id|URL>")
    user, score, ads_total = seller_card(api, user_id=uid)
    params = {"user_id": uid}
    if args.find:
        params["query"] = args.find
    if args.sort:
        params["sort_by"] = SORTS[args.sort]
    fetched = fetch(api, args, params)
    if args.json:
        return dump({"user": user, "score": score, "ads": fetched["raw"]})
    print_seller(user, score, ads_total)
    print(f"\n{'Пошук «' + args.find + '» серед оголошень' if args.find else 'Усі оголошення'} продавця:")
    print_listing([(None, fetched)], args, words=title_words(args, params))


def cmd_compare(api, args):
    try:
        ids = [ad_id(x) for x in args.ads]
    except ValueError as e:
        die(str(e))
    ads = [api.offer(i) for i in ids]  # sequential on purpose: it's a real marketplace's API
    scores = [api.score(a["user"]["id"]) for a in ads]
    if args.json:
        return dump({"ads": ads, "scores": scores})

    def cell(value, width=args.width):
        text = " ".join(str(value if value not in (None, "") else "—").split()).replace("|", "/")
        return text if not width or len(text) <= width else text[: width - 1] + "…"

    rows = [
        ("Назва", [a["title"] for a in ads]),
        ("Ціна", [price_text(a) for a in ads]),
        ("Стан", [state_of(a) for a in ads]),
        ("Місце", [place_text(a) for a in ads]),
        ("Продавець", [seller_text(a) for a in ads]),
        ("Рейтинг", [score_text(s).removeprefix("рейтинг").strip(": ") for s in scores]),
        ("OLX Доставка", ["так" if (a.get("contact") or {}).get("courier") else "ні" for a in ads]),
        ("Додано", [ago(a.get("created_time")) for a in ads]),
        ("⚠", ["; ".join(ad_flags(a, score=s)) for a, s in zip(ads, scores)]),
    ]
    specs, order = {}, []
    for n, a in enumerate(ads):
        for p in a.get("params") or []:
            if p.get("key") in ("price", "state"):
                continue
            if p["name"] not in specs:
                specs[p["name"]] = [None] * len(ads)
                order.append(p["name"])
            specs[p["name"]][n] = param_value(p)
    rows += [(name, specs[name]) for name in order]
    if args.diff:
        rows = rows[:1] + [r for r in rows[1:] if len({cell(v, 0) for v in r[1]}) > 1]
    print("| | " + " | ".join(str(i) for i in range(1, len(ids) + 1)) + " |")
    print("|---" * (len(ids) + 1) + "|")
    for name, vals in rows:
        print(f"| {cell(name, 40)} | " + " | ".join(cell(v) for v in vals) + " |")
    print()
    for n, a in enumerate(ads, 1):
        print(f"{n}. {a['url']}")
    if args.diff:
        print("(only rows that differ; drop --diff to see everything)")


# --- argparse ---------------------------------------------------------------

def add_search_args(p, sort=True):
    p.add_argument("--category", action="append",
                   help="category id, slug or site path (see the Категорії line or `categories --find`). "
                   "Repeatable: the searches are merged into one list (sorted locally)")
    p.add_argument("--url", help="an olx.ua listing URL: its category, place, query, filters and sorting are used")
    p.add_argument("-f", "--filter", action="append", metavar="NAME=VAL",
                   help="category filter (see `filters`): NAME=VALUE[|VALUE2] or NAME=MIN..MAX. Repeatable")
    p.add_argument("--state", choices=["new", "used"], help="нове / вживане")
    p.add_argument("--price", help="MIN-MAX, MIN- or -MAX (UAH)")
    who = p.add_mutually_exclusive_group()
    who.add_argument("--private", action="store_true", help="only private sellers")
    who.add_argument("--business", action="store_true", help="only business sellers")
    p.add_argument("--delivery", action="store_true", help="only ads with OLX Доставка (buyer protection)")
    where = p.add_mutually_exclusive_group()
    where.add_argument("--city", help="city/village name or id (default: all of Ukraine). "
                       "\"Обухівка, Дніпровський\" narrows a common name; rows then show the distance")
    where.add_argument("--region", help="oblast name or code (Дніпропетровська, dnp)")
    p.add_argument("--radius", type=float, metavar="KM", help="with --city: also ads within KM km")
    if sort:
        add_page_args(p)


def add_page_args(p):
    p.add_argument("--sort", choices=list(SORTS), help="relevance (default), new, cheap, expensive")
    p.add_argument("--page", type=int, default=1)
    p.add_argument("--limit", type=int, default=PAGE_SIZE, help=f"ads per page (default {PAGE_SIZE}, max {MAX_LIMIT}; "
                   "promoted ads come on top of that)")
    p.add_argument("--no-ads", action="store_true", help="hide promoted (paid TOP) ads")
    p.add_argument("--all", action="store_true",
                   help=f"load every page (up to {MAX_RESULTS} ads per search, {MAX_LIMIT} per request) instead of one")
    p.add_argument("--title", action="store_true",
                   help="keep only ads with every query word in the title (OLX search is fuzzy: \"V600\" also finds "
                   "other scanners). Checks the loaded page, so pair it with --all")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="olx", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print raw API JSON instead of text")
    common.add_argument("--delay", type=float, nargs="?", const=DEFAULT_DELAY, metavar="SECONDS",
                        help=f"pause before every HTTP request, including the first one ({DEFAULT_DELAY}s when "
                        "given without a value). REQUIRED when running several commands in a row. "
                        "Env OLX_DELAY sets the default")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("search", parents=[common], help="search ads (all of Ukraine unless --city/--region)")
    p.add_argument("query", nargs="?", default="")
    add_search_args(p)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("filters", parents=[common], help="what a search/category can be filtered by, with counts")
    p.add_argument("query", nargs="?", default="")
    add_search_args(p, sort=False)
    p.add_argument("--all", action="store_true", help="show every value")
    p.add_argument("--max-values", type=int, default=15)
    p.set_defaults(func=cmd_filters, sort=None)

    p = sub.add_parser("categories", parents=[common], help="category tree (cached weekly)")
    p.add_argument("category", nargs="?", help="browse below this category (id, slug or path)")
    p.add_argument("--find", metavar="TEXT", help="search category names and paths")
    p.add_argument("--depth", type=int, default=1)
    p.add_argument("--max", type=int, default=30)
    p.add_argument("--refresh", action="store_true", help="rebuild the cache (one page download)")
    p.set_defaults(func=cmd_categories)

    p = sub.add_parser("geo", parents=[common], help="find a city/village id for --city")
    p.add_argument("place")
    p.set_defaults(func=cmd_geo)

    p = sub.add_parser("product", parents=[common], help="one ad: price, params, description, seller card and rating, flags")
    p.add_argument("ad", help="ad id or URL")
    p.add_argument("--max-desc", type=int, default=1500, help="truncate description to N chars (0 = full)")
    p.add_argument("--photos", action="store_true", help="list photo URLs")
    p.set_defaults(func=cmd_product)

    p = sub.add_parser("seller", parents=[common], help="a seller's card, rating and other ads")
    p.add_argument("user", nargs="?", help="seller (user) id")
    p.add_argument("--ad", help="… or one of the seller's ads (id or URL)")
    p.add_argument("--find", metavar="QUERY", help="search among the seller's ads")
    add_page_args(p)
    p.set_defaults(func=cmd_seller)

    p = sub.add_parser("compare", parents=[common], help="side-by-side table of several ads")
    p.add_argument("ads", nargs="+", help="ad ids or URLs (2–6 is readable)")
    p.add_argument("--diff", action="store_true", help="show only rows where the ads differ")
    p.add_argument("--width", type=int, default=0, help="truncate cells to N chars (0 = no limit)")
    p.set_defaults(func=cmd_compare)

    args = ap.parse_args(argv)
    if getattr(args, "limit", None):
        args.limit = max(1, min(args.limit, MAX_LIMIT))
    delay = args.delay if args.delay is not None else float(os.environ.get("OLX_DELAY") or 0)
    api = Olx(delay=delay)
    try:
        args.func(api, args)
    except ApiError as e:
        die(str(e))
    except RequestException as e:
        die(f"network error: {e}")


if __name__ == "__main__":
    main()
