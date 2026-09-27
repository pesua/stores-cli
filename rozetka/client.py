"""Thin client for the undocumented rozetka.com.ua JSON APIs.

No login or tokens: the hosts below answer plain GETs with a browser User-Agent and Referer.
Category listings are addressed by the site's own URL (filters live in the last path segment,
`/ua/notebooks/c80004/delivery=free-shipping-to-the-store;seller=rozetka/`), exactly as the
site's front end does it.
"""

import re
import threading
import time
import urllib.parse

import requests

SITE = "https://rozetka.com.ua"
COMMON = "https://common-api.rozetka.com.ua"
PRODUCT = "https://product-api.rozetka.com.ua"
SEARCH = "https://search.rozetka.com.ua/ua/search/api/v6/"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
BASE_PARAMS = {"front-type": "xl", "country": "UA", "lang": "ua"}
ROZETKA_SELLER_ID = 5
KYIV_MDM = "b205dde2-2e2e-4eb9-aef2-a67c82bbdf27"
# Default scope: goods sold or handled by Rozetka AND delivered free to Rozetka stores. The parent
# "Rozetka +" value covers rozetka_only / rozetka_fulfillment / rozetka_eu; EU goods never qualify for
# free store delivery, so the intersection is exactly "Rozetka + handled by Rozetka, free pickup".
DEFAULT_FILTERS = {"delivery": ["free-shipping-to-the-store"], "seller": ["rozetka"]}


class ApiError(RuntimeError):
    pass


class Rozetka:
    def __init__(self, timeout=30, delay=0.0):
        """delay: minimum seconds between HTTP requests, counted from construction too, so
        back-to-back CLI runs are spaced out as well as requests within one run."""
        self.timeout = timeout
        self.delay = delay
        self._last = time.monotonic()
        self._lock = threading.Lock()
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Referer": SITE + "/",
            "Origin": SITE,
            "Accept": "application/json, text/plain, */*",
        })

    def _throttle(self):
        if not self.delay:
            return
        with self._lock:
            wait = self._last + self.delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def get(self, url, params=None):
        self._throttle()
        r = self.s.get(url, params=params, timeout=self.timeout)
        try:
            data = r.json()
        except ValueError:
            raise ApiError(f"GET {r.url}: HTTP {r.status_code}, non-JSON body: {r.text[:200]}")
        if r.status_code >= 400:
            raise ApiError(f"GET {url}: HTTP {r.status_code}: {data.get('errors') if isinstance(data, dict) else data}")
        return data

    # --- listings -----------------------------------------------------------

    def catalog_page(self, site_url):
        """A category listing page: goods tiles, facets, sort, paginator.

        Returns (data, final_url). A URL the site considers non-canonical (unsorted segments,
        a filter value that doesn't exist in this category) comes back as a redirect; follow it once.
        """
        url = site_url
        for _ in range(3):
            data = self.get(COMMON + "/v1/api/pages/catalog/category",
                            {"country": "UA", "lang": "ua", "platform": "desktop", "url": url})["data"]
            if "redirect" not in data:
                return data, url
            url = data["redirect"]["url"]
        raise ApiError(f"too many redirects for {site_url}")

    def search(self, text, page=None, sort=None, filters=None):
        """Full-text search. filters: {name: [values]}; only a few facets work here
        (seller rozetka|other, delivery, producer, price). A query that matches a category
        comes back as data.meta.navigateTo with no goods."""
        params = dict(BASE_PARAMS, text=text)
        if page:
            params["page"] = page
        if sort:
            params["sort"] = sort
        for k, v in (filters or {}).items():
            params[k] = ",".join(v)
        return self.get(SEARCH, params)["data"]

    # --- products -----------------------------------------------------------

    def details(self, ids, chunk=60):
        """Listing-style records (price, seller, rating, sell_status) for many goods ids."""
        out = []
        ids = [str(i) for i in ids]
        for i in range(0, len(ids), chunk):
            out += self.get(COMMON + "/v1/api/product/details", {
                "country": "UA", "lang": "ua", "platform": "desktop", "ids": ",".join(ids[i:i + chunk]),
            }).get("data") or []
        return out

    def product_page(self, goods_id):
        """Everything the product page shows: product, variants, characteristics, description."""
        return self.get(COMMON + "/v1/api/pages/product/main", {
            "country": "UA", "lang": "ua", "platform": "desktop", "id": goods_id, "isGroup": "false",
        })["data"]["productData"]

    def comments(self, goods_id, page=1, sort="date"):
        """One page of reviews (the API ignores `limit`; a page holds ~36)."""
        return self.get(PRODUCT + "/v4/comments/get", dict(BASE_PARAMS, goods=goods_id, page=page, sort=sort))["data"]

    def deliveries(self, goods_id, price, city_mdm=KYIV_MDM):
        """Delivery options to a city, with cost and term ("Отримати сьогодні з 13:00")."""
        return self.get(PRODUCT + "/v4/deliveries/get-deliveries", {
            "country": "UA", "lang": "ua", "city_id": city_mdm, "cost": price, "product_id": goods_id,
        })["data"]

    # --- locations ----------------------------------------------------------

    def locations(self, name):
        """Settlements by name (without `name` the API returns a fixed list of big cities)."""
        data = self.get(COMMON + "/v2/locations/search", {"country": "UA", "lang": "ua", "name": name})
        return (data.get("content") or {}).get("records") or []


# --- URL helpers ------------------------------------------------------------

CAT_RE = re.compile(r"^/(?:ua/|ru/)?([^/]+)/c(\d+)/?(?:([^/]+)/?)?$")


def split_listing(ref):
    """Category id / site URL / path -> (base URL "https://rozetka.com.ua/ua/<slug>/c<id>/", {filter: [values]}).

    A bare id ("80004" or "c80004") gets a placeholder slug; the API redirects it to the canonical one.
    """
    ref = ref.strip()
    m = re.fullmatch(r"c?(\d+)", ref)
    if m:
        return f"{SITE}/ua/x/c{m.group(1)}/", {}
    site, path = SITE, ref
    if ref.startswith("http"):
        parts = urllib.parse.urlsplit(ref)
        path = parts.path
        if parts.netloc.endswith("rozetka.com.ua"):  # some sections live on build./bt./… subdomains
            site = f"https://{parts.netloc}"
    if not path.startswith("/"):
        path = "/" + path
    m = CAT_RE.match(path)
    if not m:
        raise ValueError(f"not a category reference: {ref} (expected an id, a site URL or /ua/<slug>/c<id>/)")
    slug, cat_id, seg = m.groups()
    return f"{site}/ua/{slug}/c{cat_id}/", parse_segment(seg or "")


def parse_segment(seg):
    """'delivery=x;producer=asus,lenovo' -> {'delivery': ['x'], 'producer': ['asus', 'lenovo']}."""
    out = {}
    for part in urllib.parse.unquote(seg).split(";"):
        key, sep, vals = part.partition("=")
        if sep and key:
            out[key] = [v for v in vals.split(",") if v]
    return out


def listing_url(base, filters):
    """Canonical listing URL: keys and values sorted the way the site sorts them (else it redirects):
    named keys alphabetically, then numeric property ids."""
    keys = sorted(filters, key=lambda k: (k[:1].isdigit(), k))
    parts = [f"{k}={','.join(sorted(map(str, filters[k])))}" for k in keys if filters[k]]
    return base + (";".join(parts) + "/" if parts else "")


def goods_id(ref):
    """Goods id from an id, a product URL (…/p588745930/) or 'p588745930'."""
    ref = ref.strip()
    m = re.search(r"/p(\d+)", ref) or re.fullmatch(r"p?(\d+)", ref)
    if not m:
        raise ValueError(f"not a product reference: {ref}")
    return int(m.group(1))
