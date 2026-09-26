"""Thin client for the undocumented epicentrk.ua JSON API (api.epicentrk.ua).

The site is a Nuxt app backed by a Laravel API guarded by Sanctum: a GET to
/sanctum/csrf-cookie sets XSRF-TOKEN + PHPSESSID cookies, after which every
request must echo XSRF-TOKEN in the x-xsrf-token header. No login needed.
"""

import re
import threading
import time
import urllib.parse

import requests

API = "https://api.epicentrk.ua"
SITE = "https://epicentrk.ua"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
# Static token baked into the site's JS bundle for the stock endpoint.
LEFTOVER_TOKEN = "JqdvK7WSEzHnRDB3D7pzvb3BJZdPj9LB"

STORE_TYPES = {
    1: "Торговельний центр",
    2: "Нова лінія",
    3: "Інтерспорт",
    4: "Галереї Деко",
    6: "TAF",
    7: "4F",
    8: "ЦеТе",
    10: "Центр видачі замовлень",
    12: "Експрес",
    1000: "Поштомат",
}
KYIV_CITY_ID = 9695


class ApiError(RuntimeError):
    pass


class Epicentrk:
    def __init__(self, lang="ua", timeout=30, delay=0.0):
        """delay: minimum seconds between HTTP requests, counted from construction too, so
        back-to-back CLI runs are spaced out as well as requests within one run."""
        self.lang = lang
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
            "ssr-platform": "nuxt",
            "x-is-robot": "0",
        })
        self._csrf = False

    def _throttle(self):
        if not self.delay:
            return
        with self._lock:
            wait = self._last + self.delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def _ensure_session(self):
        if self._csrf:
            return
        self._throttle()
        self.s.get(API + "/sanctum/csrf-cookie", timeout=self.timeout).raise_for_status()
        token = self.s.cookies.get("XSRF-TOKEN")
        if not token:
            raise ApiError("No XSRF-TOKEN cookie from /sanctum/csrf-cookie")
        self.s.headers["x-xsrf-token"] = urllib.parse.unquote(token)
        self._csrf = True

    def _request(self, method, path, **kw):
        self._ensure_session()
        self._throttle()
        r = self.s.request(method, API + path, timeout=self.timeout, **kw)
        try:
            data = r.json()
        except ValueError:
            raise ApiError(f"{method} {path}: HTTP {r.status_code}, non-JSON body: {r.text[:200]}")
        if r.status_code >= 400 or (isinstance(data, dict) and data.get("status") == "ERR"):
            msg = data.get("message") if isinstance(data, dict) else data
            errors = data.get("errors") if isinstance(data, dict) else None
            raise ApiError(f"{method} {path}: HTTP {r.status_code}: {msg} {errors or ''}".strip())
        return data

    def get(self, path, params=None):
        return self._request("GET", path, params=params)

    # --- catalog -----------------------------------------------------------

    def catalog(self, slug=None, store_id=None):
        """Root catalog (22 top categories with their children) or one category's children."""
        params = {"lang": self.lang, "slug": slug, "store_id": store_id}
        return self.get("/api/v2/catalog", {k: v for k, v in params.items() if v is not None})

    # --- listing -----------------------------------------------------------

    def _listing_params(self, path, store_id=None, pickup=False, sort=None, page=None, page_size=None):
        params = {"query[]": path, "lang": self.lang}
        if store_id:
            params["store_id"] = store_id
        if pickup:
            params["pickup"] = 1
        if sort:
            params["sort"] = sort
        if page:
            params["page"] = page
        if page_size:
            params["page_size"] = page_size
        return params

    def section_data(self, path, store_id=None, pickup=False):
        return self.get("/api/v2/product/listing/section-data",
                        self._listing_params(path, store_id, pickup))["data"]

    def products(self, path, store_id=None, pickup=False, sort=None, page=1, page_size=60):
        return self.get("/api/v2/product/listing/products",
                        self._listing_params(path, store_id, pickup, sort, page, page_size))["data"]

    def filters(self, path, store_id=None, pickup=False):
        return self.get("/api/v1/product/list/filters",
                        self._listing_params(path, store_id, pickup))["data"]

    def listing_url(self, section_id, props=None, brands=None, sellers=None, promos=None,
                    price=None, pickup=False, sort=None):
        """Ask the API to encode a filter selection into a listing path.

        props: {prop_id: [value, ...]} — value hashes, or [min, max] for range props.
        """
        params = [("lang", self.lang), ("section", section_id)]
        if sort:
            params.append(("sort", sort))
        if pickup:
            params.append(("pickup", 1))
        for pid, values in (props or {}).items():
            params += [(f"prop_{pid}[]", v) for v in values]
        params += [("brands[]", b) for b in brands or []]
        params += [("seller[]", v) for v in sellers or []]
        params += [("promo[]", v) for v in promos or []]
        if price:
            params += [("price[min]", price[0]), ("price[max]", price[1])]
        return self.get("/api/v1/product/listing-url/", params)["data"]["url"]

    def search(self, query, store_id=None, page=None, brands=None, price=None, section=None):
        params = [("lang", self.lang), ("find", query)]
        if store_id:
            params.append(("store_id", store_id))
        if page:
            params.append(("page", page))
        if section:
            params.append(("section", section))
        params += [("vendor[]", b) for b in brands or []]
        if price:
            params += [("price_min", price[0]), ("price_max", price[1])]
        return self.get("/api/v1/search", params)

    # --- product -----------------------------------------------------------

    def product_card(self, slug, store_id=None):
        params = {"lang": self.lang, "slug": slug}
        if store_id:
            params["store_id"] = store_id
        return self.get("/api/v1/product/card/full", params)["data"]

    def product_info(self, articles, store_id=None, chunk=50):
        """Listing-style records (price, productId, availability) for 9-digit article ids, batched."""
        out = []
        for i in range(0, len(articles), chunk):
            params = [("lang", self.lang)] + [("product_ids[]", a) for a in articles[i:i + chunk]]
            if store_id:
                params.append(("store_id", store_id))
            out += self.get("/api/v1/product/info", params).get("data") or []
        return out

    def reviews(self, slug, limit=10, offset=0):
        return self.get("/api/v1/review/product_card_list", {
            "entity_type": "product", "show_main": 1, "type": "review", "slug": slug,
            "from": offset, "limit": limit, "product_page_type": "comments",
        })

    def leftovers(self, store_id, product_ids):
        """Stock in one store: {productId(str): {"amount": n, "measure": "шт."}}.

        Only Epicentr's own goods (seller == "epicentrk") are accepted; a single
        marketplace id makes the whole batch fail, so callers must pre-filter.
        """
        if not product_ids:
            return {}
        data = self._request("POST", "/api/v1/leftover",
                             json={"storeId": int(store_id), "products": [int(p) for p in product_ids]},
                             headers={"X-Security-Token": LEFTOVER_TOKEN})
        return data.get("items") or {}

    def pickup_stores(self, article, store_id, location_id=KYIV_CITY_ID):
        """All pickup points for a product (by 9-digit article id) with per-store amounts."""
        return self.get("/api/v1/addresses/pickup", {
            "lang": self.lang, "productId": article, "storeId": store_id, "locationId": location_id,
        })["data"]

    # --- stores & locations --------------------------------------------------

    def city_search(self, text):
        data = self.get("/api/v1/addresses/search/", {"lang": self.lang, "type": "city", "search": text})
        return data.get("cities") or []

    def stores(self, city_id=KYIV_CITY_ID):
        """Every store/pickup point in the country, sorted by distance from the city's centre."""
        return self.get("/api/v1/addresses/search", {
            "lang": self.lang, "type": "city", "id": city_id, "noPharm": 1, "primaryMarketFilter": 1,
        })

    def nearest_store(self, lat, lng):
        return self.get("/api/v1/addresses/location", {"lang": self.lang, "lat": lat, "lng": lng})


def listing_path(ref):
    """Normalise a category slug / site URL / path to the API's `query[]` form: /shop/<slug>/..."""
    ref = ref.strip()
    if ref.startswith("http"):
        ref = urllib.parse.urlsplit(ref).path
    ref = ref.split("?")[0]
    if not ref.startswith("/"):
        ref = f"/shop/{ref.strip('/')}/"
    ref = re.sub(r"^/(ua|ru)/", "/", ref)
    if not ref.endswith("/"):
        ref += "/"
    return ref


def product_slug(ref):
    """Product slug from a slug or a product page URL."""
    ref = ref.strip()
    if ref.startswith("http") or "/" in ref:
        ref = urllib.parse.urlsplit(ref).path.rstrip("/").rsplit("/", 1)[-1]
    return ref.removesuffix(".html")
