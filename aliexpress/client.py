"""Thin client for AliExpress's undocumented web endpoints (aliexpress.com, ship-to Ukraine, UAH).

- Search: server-rendered /w/wholesale-<query>.html pages embed `_dida_config_._init_data_` JSON.
- Everything else goes through "mtop" (acs.aliexpress.com/h5/<api>/1.0/): the first call returns
  FAIL_SYS_TOKEN_EMPTY and sets the `_m_h5_tk` cookie; later calls are signed with
  md5(token & t & appKey & data), where token is the part of _m_h5_tk before "_".
- Reviews come from feedback.aliexpress.com as plain JSON.

Two sessions: an anonymous one for bulk browsing, and an account one (cookies from the
`aliexpress_cookie` variable in the repo's .env) used only for the order history and for
"price as me" checks. The account session is read-only by design: no cart, coupon, order or
message endpoints exist here.
"""

import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
from pathlib import Path

from curl_cffi import requests

SITE = "https://www.aliexpress.com"
ACS = "https://acs.aliexpress.com/h5"
FEEDBACK = "https://feedback.aliexpress.com/pc/searchEvaluation.do"
REPO_ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
CACHE = HERE / ".cache"
ANON_JAR = CACHE / "anon_cookies.json"
ACCOUNT_JAR = CACHE / "account_cookies.json"

CURRENCY, COUNTRY, LOCALE = "UAH", "UA", "en_US"
REGION_COOKIE = f"site=glo&c_tp={CURRENCY}&region={COUNTRY}&b_locale={LOCALE}"

APP_PDP = "12574478"     # pdp, orders
APP_SHOP = "24815441"    # store product search / groups

# Ultron order-list request: page 1 is a plain GET, later pages POST these components back.
ORDER_TIMEZONE = "GMT+0300"

CAPTCHA_HINT = ("AliExpress asked for a human check and the browser window is off (--no-browser). "
                "Rerun without --no-browser, or run `aliexpress unblock`, and pass the check in the window.")


CHALLENGE_COOKIES = ("x5secdata", "x5sectag")
TRANSPORT_FILE = CACHE / "transport.json"
DIRECT_COOLDOWN = 3 * 3600


def _read_json(path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return {}


class TextResponse:
    """What _get returns when the page came through the browser."""

    def __init__(self, text, url):
        self.text, self.url, self.status_code = text, url, 200

    def json(self):
        return json.loads(self.text)


class ApiError(RuntimeError):
    pass


class CaptchaError(ApiError):
    pass


class LoginRequired(ApiError):
    pass


def read_env_cookie():
    """Raw Cookie header for the account session: env var, or the repo's .env file."""
    for key in ("ALIEXPRESS_COOKIE", "aliexpress_cookie"):
        if os.environ.get(key):
            return os.environ[key].strip()
    try:
        for line in (REPO_ROOT / ".env").read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip().lower() == "aliexpress_cookie":
                return value.strip().strip("'\"")
    except FileNotFoundError:
        pass
    return None


def parse_cookie_header(raw):
    out = {}
    for part in raw.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name:
            out[name] = value
    return out


def is_captcha_text(text):
    return "_____tmd_____" in text or "x5secdata" in text or "/punish?" in text


class AliExpress:
    def __init__(self, account=False, delay=0.0, timeout=30, budget=None, browser_factory=None):
        """account: use the logged-in cookies from .env (raises LoginRequired if missing).
        delay: minimum seconds between HTTP requests, counted from construction too.
        budget: called before every account request (raises to stop over the daily limit).
        browser_factory(account): builds the browser transport used once plain HTTP gets captchas;
        None means captchas are raised as CaptchaError."""
        self.account = account
        self.budget = budget
        self.browser_factory = browser_factory
        self._browser = None
        self.delay = delay
        self.timeout = timeout
        self._last = time.monotonic()
        self._lock = threading.Lock()
        self.s = requests.Session(impersonate="chrome")
        self.s.headers.update({"Accept-Language": "en-US,en;q=0.9", "Referer": SITE + "/"})
        self.jar = ACCOUNT_JAR if account else ANON_JAR
        self._env_hash = None
        if account:
            raw = read_env_cookie()
            if not raw:
                raise LoginRequired("no aliexpress_cookie in the repo's .env (copy the Cookie header "
                                    "of any www.aliexpress.com request while logged in)")
            self._env_hash = hashlib.sha1(raw.encode()).hexdigest()
            for name, value in parse_cookie_header(raw).items():
                self._set_cookie(name, value)
        self._load_jar()
        self._set_cookie("aep_usuc_f", REGION_COOKIE)

    # --- cookies -------------------------------------------------------------

    def _set_cookie(self, name, value):
        self.s.cookies.set(name, value, domain=".aliexpress.com", path="/")

    def _cookie(self, name):
        for c in self.s.cookies.jar:
            if c.name == name:
                return c.value
        return None

    def _load_jar(self):
        """Cookies refreshed by earlier runs (_m_h5_tk, x5sec from `unblock`…)."""
        try:
            saved = json.loads(self.jar.read_text())
        except (FileNotFoundError, ValueError):
            return
        # A new cookie string in .env means a fresh login: drop what the old session learnt.
        if self.account and saved.get("env_hash") != self._env_hash:
            return
        for name, value in (saved.get("cookies") or {}).items():
            self._set_cookie(name, value)

    def reload_jar(self):
        """Pick up cookies another component (the browser fallback) wrote to the jar."""
        self._load_jar()
        self._set_cookie("aep_usuc_f", REGION_COOKIE)

    def save_jar(self):
        # Merge into what is on disk: the browser fallback may have added cookies meanwhile.
        try:
            keep = dict(json.loads(self.jar.read_text()).get("cookies") or {})
        except (FileNotFoundError, ValueError):
            keep = {}
        keep.update({c.name: c.value for c in self.s.cookies.jar if "aliexpress" in (c.domain or "")})
        for name in CHALLENGE_COOKIES:  # set by the captcha page itself; replaying them keeps us flagged
            keep.pop(name, None)
        CACHE.mkdir(exist_ok=True)
        self.jar.write_text(json.dumps({"env_hash": self._env_hash, "saved": time.time(), "cookies": keep}))

    # --- transport -------------------------------------------------------------

    def _throttle(self):
        if self.account and self.budget:
            self.budget()
        if not self.delay:
            return
        with self._lock:
            wait = self._last + self.delay - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    # Plain HTTP stays off for a while after a captcha: failed direct calls only add suspicion.
    def _blocked(self):
        state = _read_json(TRANSPORT_FILE)
        return time.time() < state.get("account" if self.account else "anon", 0)

    def _mark_blocked(self):
        state = _read_json(TRANSPORT_FILE)
        state["account" if self.account else "anon"] = time.time() + DIRECT_COOLDOWN
        CACHE.mkdir(exist_ok=True)
        TRANSPORT_FILE.write_text(json.dumps(state))

    @property
    def browser(self):
        if not self.browser_factory:
            raise CaptchaError(CAPTCHA_HINT)
        if not self._browser:
            self._browser = self.browser_factory(self.account)
        return self._browser

    def close(self):
        if self._browser:
            self._browser.close()
            self._browser = None
            self.reload_jar()
        self.save_jar()

    def _get(self, url, **kw):
        params = kw.get("params")
        full = url + ("?" + urllib.parse.urlencode(params) if params else "")
        self._throttle()
        r = self.s.get(url, timeout=self.timeout, **kw)
        if not is_captcha_text(r.text[:3000]):
            return r
        return TextResponse(self.browser.get_text(full), full)

    def mtop(self, api, data, app=APP_PDP, post=False, extra=None):
        """Signed mtop call; returns the parsed body's `data`. Falls back to the browser on a captcha."""
        body = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
        if not self._blocked():
            try:
                return self._mtop(api, body, app, post, extra)
            except CaptchaError:
                if not self.browser_factory:
                    raise
                self._mark_blocked()
        self._throttle()
        return self.browser.mtop(api, body, app if app != APP_PDP else None, post, extra)

    def _mtop(self, api, body, app, post, extra):
        for attempt in range(3):
            token = (self._cookie("_m_h5_tk") or "_").split("_")[0]
            t = str(int(time.time() * 1000))
            sign = hashlib.md5(f"{token}&{t}&{app}&{body}".encode()).hexdigest()
            params = {"jsv": "2.5.1", "appKey": app, "t": t, "sign": sign, "api": api, "v": "1.0",
                      "type": "originaljson", "dataType": "json", "timeout": "15000", **(extra or {})}
            url = f"{ACS}/{api.lower()}/1.0/"
            self._throttle()
            if post:
                r = self.s.post(url, params={**params, "post": "1"}, data={"data": body},
                                headers={"Origin": SITE}, timeout=self.timeout)
            else:
                r = self.s.get(url, params={**params, "data": body}, timeout=self.timeout)
            text = r.text
            if is_captcha_text(text[:3000]):
                raise CaptchaError(CAPTCHA_HINT)
            try:
                j = json.loads(text[text.find("(") + 1: text.rfind(")")] if not text.startswith("{") else text)
            except ValueError:
                raise ApiError(f"{api}: HTTP {r.status_code}, non-JSON body: {text[:200]}")
            ret = " ".join(j.get("ret") or [])
            if "TOKEN_EMPTY" in ret or "TOKEN_EXOIRED" in ret or "TOKEN_EXPIRED" in ret:
                continue  # the response set a fresh _m_h5_tk; sign again
            if "RGV587" in ret or "USER_VALIDATE" in ret:
                raise CaptchaError(CAPTCHA_HINT)
            if "SESSION_EXPIRED" in ret or "NOT_LOGIN" in ret.upper() or "LOGIN" in ret.split("::")[0]:
                raise LoginRequired(f"{api}: {ret} — refresh aliexpress_cookie in the repo's .env")
            if not ret.startswith("SUCCESS"):
                raise ApiError(f"{api}: {ret}")
            return j.get("data") or {}
        raise ApiError(f"{api}: could not obtain an mtop token")

    # --- search ------------------------------------------------------------------

    @staticmethod
    def search_url(query, params):
        slug = re.sub(r"[^A-Za-z0-9]+", "-", query or "").strip("-") or "products"
        qs = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")}, safe=":,;")
        return f"{SITE}/w/wholesale-{slug}.html" + (f"?{qs}" if qs else "")

    def search(self, query, params=None):
        """Search result page → `root.fields` of the embedded JSON (mods.itemList, searchRefineFilters…)."""
        r = self._get(self.search_url(query, params or {}))
        marker = "_init_data_= { data: "
        i = r.text.find(marker)
        if i < 0:
            raise ApiError(f"search page without data (HTTP {r.status_code})")
        data, _ = json.JSONDecoder().raw_decode(r.text[i + len(marker):])
        return data["data"]["root"]["fields"]

    # --- product -------------------------------------------------------------------

    def product(self, product_id):
        data = self.mtop("mtop.aliexpress.pdp.pc.query", {
            # _lang en_UA (not en_US) is what makes shipping come out for Ukraine, as the site sends it.
            "productId": str(product_id), "_lang": f"en_{COUNTRY}", "_currency": CURRENCY, "country": COUNTRY,
            "province": "", "city": "", "channel": "", "pdp_ext_f": "", "pdpNPI": "", "sourceType": "",
            "clientType": "pc", "ext": json.dumps({"site": "glo", "crawler": False, "x-m-biz-bx-region": "",
                                                   "signedIn": self.account, "host": "www.aliexpress.com"},
                                                  separators=(",", ":")),
        })
        result = data.get("result")
        if not result:
            raise ApiError(f"product {product_id}: empty result (removed or unavailable?)")
        return result

    def description(self, url):
        return self._get(url).text

    def reviews(self, product_id, page=1, page_size=10, filter="all", sort="complex_default"):
        r = self._get(FEEDBACK, params={
            "productId": product_id, "lang": LOCALE, "country": COUNTRY, "page": page,
            "pageSize": page_size, "filter": filter, "sort": sort,
        }, headers={"Accept": "application/json"})
        try:
            return r.json().get("data") or {}
        except ValueError:
            raise ApiError(f"reviews: HTTP {r.status_code}, non-JSON body: {r.text[:200]}")

    # --- stores ----------------------------------------------------------------------

    def store_products(self, seller_id, store_num, page=1, page_size=40, sort="orders_desc",
                       text="", filter_type=""):
        return self.mtop("mtop.ae.shop.search.product.group", {
            "locale": LOCALE, "country": COUNTRY, "currency": CURRENCY, "lang": "en", "buyerId": 0,
            "currentPage": page, "productGroupId": "", "pageSize": page_size, "sellerId": int(seller_id),
            "site": "glo", "storeNumber": int(store_num), "platform": "pc", "sortType": sort,
            "filterType": filter_type, "searchText": text, "cookieId": None,
        }, app=APP_SHOP)

    # --- orders (account session only) ---------------------------------------------------

    def _need_account(self):
        if not self.account:
            raise LoginRequired("this needs the account session")

    def orders_first_page(self, status="all", time_option="all"):
        """Page 1 of "My orders": the full ultron payload (orders + linkage for later pages)."""
        self._need_account()
        return self.mtop("mtop.aliexpress.trade.buyer.order.list", {
            "statusTab": None if status == "all" else status, "renderType": "init", "clientPlatform": "pc",
            "shipToCountry": COUNTRY, "_lang": LOCALE, "timeZone": ORDER_TIMEZONE,
        })

    def orders_page(self, first, page):
        """Later pages: POST the body + header-action components back with pageIndex changed."""
        self._need_account()
        comps = first["data"]
        wanted = {k: json.loads(json.dumps(v)) for k, v in comps.items()
                  if k.startswith(("pc_om_list_body", "pc_om_list_header_action"))}
        for k, v in wanted.items():
            if k.startswith("pc_om_list_body"):
                v["fields"]["pageIndex"] = page
        hierarchy = first.get("hierarchy") or {}
        params = {
            "data": json.dumps(wanted, separators=(",", ":")),
            "linkage": json.dumps(first.get("linkage") or {}, separators=(",", ":")),
            "hierarchy": json.dumps({"structure": hierarchy.get("structure") or {}}, separators=(",", ":")),
            "endpoint": json.dumps(first.get("endpoint") or {}, separators=(",", ":")),
            "operator": next(k for k in wanted if k.startswith("pc_om_list_body")),
        }
        body = {"params": json.dumps(params, separators=(",", ":")), "shipToCountry": COUNTRY, "_lang": LOCALE}
        return self.mtop("mtop.aliexpress.trade.buyer.order.list", body, post=True,
                         extra={"isSec": "1", "ecode": "1", "method": "POST", "dataType": "originaljsonp"})

    def order_detail(self, order_id):
        self._need_account()
        return self.mtop("mtop.aliexpress.trade.buyer.order.detail", {
            "tradeOrderId": str(order_id), "clientPlatform": "pc", "shipToCountry": COUNTRY,
            "_lang": LOCALE, "timeZone": ORDER_TIMEZONE,
        })


def product_id(ref):
    """Product id from an id or any aliexpress item URL."""
    ref = str(ref).strip()
    m = re.search(r"/item/(\d+)", ref) or re.fullmatch(r"(\d{10,20})", ref)
    if not m:
        raise ValueError(f"not an AliExpress product id or URL: {ref}")
    return m.group(1)
