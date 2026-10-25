"""Thin client for olx.ua's own JSON API (https://www.olx.ua/api/v1/…), no login.

AWS WAF in front of www.olx.ua answers 403 to Python's own TLS stack (requests, urllib, httpx),
whatever the headers. curl_cffi (libcurl + BoringSSL) passes, so the session uses it.

Listing filters are plain query parameters, the same ones the site sends:
`query`, `category_id`, `region_id`, `city_id`, `distance` (km), `owner_type=private|business`,
`courier=true` (OLX Доставка), `sort_by`, `filter_enum_<x>[i]=<value>` (select/checkboxes),
`filter_bool_<x>=1`, `filter_float_<x>:from` / `:to` (ranges, price included).
The API returns at most 1000 ads per search (offset + limit <= 1000), 50 per request plus promoted ones.

The category tree and every filter definition (labels, values, the categories each applies to) only
ship inside a listing page's `window.__PRERENDERED_STATE__`, so `catalog()` caches them.
"""

import json
import math
import re
import string
import threading
import time
import urllib.parse
from pathlib import Path

from curl_cffi import requests

SITE = "https://www.olx.ua"
API = SITE + "/api/v1/"
SCORE_API = "https://khonor.eu-sharedservices.olxcdn.com/api/olx/ua/user/{}/score/rating"
PAGE_SIZE = 40  # the site's page
MAX_LIMIT = 50
MAX_RESULTS = 1000
CATALOG_CACHE = Path(__file__).with_name(".cache") / "catalog.json"
CATALOG_TTL = 7 * 86400
LABELLED = {"category", "category_without_exclusions", "region", "city", "district"}  # others fail with labels
BASE62 = string.digits + string.ascii_lowercase + string.ascii_uppercase


class ApiError(RuntimeError):
    pass


class Olx:
    def __init__(self, timeout=30, delay=0.0):
        """delay: minimum seconds between HTTP requests, counted from construction too, so
        back-to-back CLI runs are spaced out as well as requests within one run."""
        self.timeout = timeout
        self.delay = delay
        self._last = time.monotonic()
        self._lock = threading.Lock()
        self.s = requests.Session(impersonate="chrome")
        self.s.headers.update({"Accept-Language": "uk", "Referer": SITE + "/uk/"})

    def _throttle(self):
        if not self.delay:
            return
        with self._lock:
            wait = self._last + self.delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def _get(self, url, params=None):
        self._throttle()
        r = self.s.get(url, params=params, timeout=self.timeout)
        if r.status_code == 403 and "cloudfront" in r.text.lower():
            raise ApiError("HTTP 403 from OLX's WAF (CloudFront): the request was taken for a bot. "
                           "Wait a few minutes; don't retry in a loop")
        if r.status_code == 404:
            raise ApiError(f"not found: {url}")
        try:
            data = r.json()
        except ValueError:
            raise ApiError(f"HTTP {r.status_code}, non-JSON body: {r.text[:200]}")
        if r.status_code >= 400:
            err = data.get("error") if isinstance(data, dict) else None
            raise ApiError(f"HTTP {r.status_code}: {json.dumps(err, ensure_ascii=False) if err else r.text[:200]}")
        return data

    def get(self, path, params=None):
        return self._get(API + path, params)

    # --- listings ---

    def offers(self, params, limit=PAGE_SIZE, offset=0):
        """{data: [ad], metadata: {visible_total_count, promoted: [index], …}}."""
        return self.get("offers/", {**params, "limit": limit, "offset": offset})

    def facets(self, params, fields, limit=100, chunk=6):
        """{field: [{id, count, label?}], total} for the given facet fields of a search. Labels come only
        for places and categories. The endpoint fails (HTTP 500) on some fields and on long lists,
        so ask in chunks and retry a failing chunk one field at a time, skipping the fields that still fail."""
        out = {}

        def ask(names):
            spec = [{"field": f, "limit": limit, **({"fetchLabel": True} if f in LABELLED else {})} for f in names]
            data = self.get("offers/metadata/search/", {**params, "facets": json.dumps(spec)})["data"]
            out["total"] = data.get("visible_total_count")
            out.update(data.get("facets") or {})

        for i in range(0, len(fields), chunk):
            names = fields[i:i + chunk]
            try:
                ask(names)
            except ApiError:
                if len(names) == 1:
                    continue
                for f in names:
                    try:
                        ask([f])
                    except ApiError:
                        pass
        return out

    def total(self, params):
        return self.get("offers/metadata/search/", params)["data"].get("visible_total_count")

    # --- one ad, one seller ---

    def offer(self, ad_id):
        return self.get(f"offers/{ad_id}/")["data"]

    def user(self, user_id):
        return self.get(f"users/{user_id}/")["data"]

    def score(self, user_id):
        """Seller rating {label: none|poor|fair|good|super, score: 0..10, ratings: count}, or None."""
        try:
            body = self._get(SCORE_API.format(user_id)).get("body") or []
        except ApiError:
            return None
        return next((b.get("data") for b in body if b.get("name") == "rating"), None)

    # --- locations, URLs ---

    def geo(self, text):
        """[{city: {id, name, lat, lon}, municipality: {name}, region: {id, name, normalized_name}}]."""
        return self.get("geo-encoder/location-autocomplete/", {"query": text})["data"]

    def city(self, city_id):
        return self.get(f"geo-encoder/cities/{city_id}/")["data"]

    def regions(self):
        return self.get("geo-encoder/regions/")["data"]

    def url_params(self, url):
        """API parameters behind a listing URL of the site, filters and sorting included."""
        parts = urllib.parse.urlsplit(url)
        path = [p for p in parts.path.split("/") if p and p not in ("uk", "list", "d")]
        query = dict(urllib.parse.parse_qsl(parts.query))
        return self.get("friendly-links/query-params/" + ",".join(path), query)["data"]

    # --- catalog ---

    def catalog(self, refresh=False):
        """{categories: {id: {...}}, filters: {name: [definition]}, built: ts}, cached for a week."""
        if CATALOG_CACHE.exists() and not refresh:
            cached = json.loads(CATALOG_CACHE.read_text())
            if time.time() - cached["built"] < CATALOG_TTL:
                return cached
        self._throttle()
        r = self.s.get(SITE + "/uk/list/", timeout=self.timeout)
        m = re.search(r'window\.__PRERENDERED_STATE__\s*=\s*("(?:\\.|[^"\\])*")', r.text)
        if not m:
            raise ApiError(f"HTTP {r.status_code}: no catalog state on the listing page")
        state = json.loads(json.loads(m.group(1)))
        cats = {}
        for c in state["categories"]["list"].values():
            cats[str(c["id"])] = {k: c.get(k) for k in ("id", "name", "parentId", "path", "level", "children", "type")}
        filters = {}
        for name, defs in state["listing"]["filters"]["data"].items():
            filters[name] = [{"type": d["type"], "label": d["label"], "unit": d.get("unit"),
                              "values": [{"label": v["label"], "value": v["value"]} for v in d.get("values") or []],
                              "categories": sorted({c for o in d["options"] for c in o["categories"]}),
                              "order": min((o.get("orderForSearch") or 0) for o in d["options"])}
                             for d in defs]
        out = {"built": time.time(), "categories": cats, "filters": filters}
        CATALOG_CACHE.parent.mkdir(exist_ok=True)
        CATALOG_CACHE.write_text(json.dumps(out, ensure_ascii=False))
        return out


# --- references -----------------------------------------------------------

def ad_id(ref):
    """Numeric ad id from an id or an ad URL (…-ID11nJrB.html: base62 of the id)."""
    ref = str(ref).strip()
    if ref.isdigit():
        return int(ref)
    m = re.search(r"-ID([0-9A-Za-z]+)\.html", ref)
    if not m:
        raise ValueError(f"not an ad reference: {ref} (use the id or the ad URL)")
    n = 0
    for ch in m.group(1):
        n = n * 62 + BASE62.index(ch)
    return n


def distance_km(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return None
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 6371 * 2 * math.asin(math.sqrt(a))
