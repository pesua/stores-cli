"""Browser transport for when AliExpress refuses plain HTTP (RGV587 / punish).

Since mid-2026 AliExpress's anti-bot (baxia) signs mtop requests in page JavaScript, so a plain
HTTP client gets captchas while a real browser on the same IP passes. This module keeps one real
Chrome (persistent profile in .cache/browser-profile[-account]) per command, loads an AliExpress
page and runs our own mtop calls through the page's `lib.mtop.request`, which adds the anti-bot
tokens itself.

It starts headless. If a call still hits a human check, it reopens the same profile in a visible
window: the user passes the check (a slider or a picture puzzle) and the command carries on. The
window closes when the command ends. Cookies go back into the HTTP client's jar.
"""

import json
import sys
import time

from client import (ACCOUNT_JAR, ANON_JAR, CACHE, CAPTCHA_HINT, CHALLENGE_COOKIES, REGION_COOKIE, SITE,
                    ApiError, CaptchaError, LoginRequired, is_captcha_text, parse_cookie_header,
                    read_env_cookie)

PROBE_PRODUCT = "1005002965907702"  # any live product page loads lib.mtop
HEADLESS_WAIT = 25   # s: a call that hangs this long in headless is waiting for a human check
WINDOW_WAIT = 300    # s: time the user gets to pass the check in the visible window

MTOP_JS = """async ([opts, ms]) => {
    const call = window.lib.mtop.request(opts).then(r => r, e => e);
    const timer = new Promise(r => setTimeout(() => r({ret: ['CLIENT_TIMEOUT::']}), ms));
    const r = await Promise.race([call, timer]);
    return JSON.stringify({ret: (r && r.ret) || [String(r)], data: (r && r.data) || null});
}"""


def _profile(account):
    return CACHE / ("browser-profile-account" if account else "browser-profile")


class BrowserMtop:
    def __init__(self, account=False, allow_window=True):
        self.account = account
        self.allow_window = allow_window
        self.pw = self.ctx = self.page = self.tab = None
        self.headed = False

    # --- lifecycle ---------------------------------------------------------------

    def _launch(self, headed):
        from playwright.sync_api import sync_playwright
        self.close()
        self.tab = None
        self.pw = sync_playwright().start()
        # Real Chrome without automation markers: reCAPTCHA picture puzzles don't pass otherwise.
        opts = dict(headless=not headed, locale="en-US", no_viewport=True,
                    ignore_default_args=["--enable-automation"],
                    args=["--disable-blink-features=AutomationControlled", "--window-size=1280,900"])
        try:
            self.ctx = self.pw.chromium.launch_persistent_context(str(_profile(self.account)), channel="chrome", **opts)
        except Exception:  # no Google Chrome installed
            self.ctx = self.pw.chromium.launch_persistent_context(str(_profile(self.account)), **opts)
        self.ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        cookies = [{"name": "aep_usuc_f", "value": REGION_COOKIE, "domain": ".aliexpress.com", "path": "/"}]
        if self.account:
            cookies += [{"name": n, "value": v, "domain": ".aliexpress.com", "path": "/"}
                        for n, v in parse_cookie_header(read_env_cookie() or "").items() if n != "aep_usuc_f"]
        self.ctx.add_cookies(cookies)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.headed = headed

    def _open_base(self, headed):
        self._launch(headed)
        base = f"{SITE}/p/order/index.html" if self.account else f"{SITE}/item/{PROBE_PRODUCT}.html"
        try:
            self.page.goto(base, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            pass
        deadline = time.time() + (WINDOW_WAIT if headed else HEADLESS_WAIT)
        while time.time() < deadline:
            try:
                if self.page.evaluate("() => !!(window.lib && window.lib.mtop && window.lib.mtop.request)"):
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def _ensure(self):
        if self.page:
            return
        if not self._open_base(headed=False):
            self._escalate()

    def _escalate(self):
        """Headless is stuck behind a human check: reopen the profile in a visible window."""
        if not self.allow_window:
            self.close()
            raise CaptchaError(CAPTCHA_HINT)
        print("AliExpress wants a human check: a Chrome window is open. Pass it (a slider or a picture "
              "puzzle); the command continues by itself.", file=sys.stderr)
        if not self._open_base(headed=True):
            self.close()
            raise CaptchaError("the check was not completed in the window; rerun the command to try again")

    def close(self):
        if self.ctx:
            try:
                self._export()
                self.ctx.close()
            except Exception:
                pass
        if self.pw:
            try:
                self.pw.stop()
            except Exception:
                pass
        self.pw = self.ctx = self.page = self.tab = None

    def _export(self):
        """Merge the browser's cookies into the HTTP client's jar."""
        jar = ACCOUNT_JAR if self.account else ANON_JAR
        try:
            saved = json.loads(jar.read_text())
        except (FileNotFoundError, ValueError):
            saved = {}
        cookies = saved.get("cookies") or {}
        for c in self.ctx.cookies():
            if "aliexpress" in c["domain"] and c["name"] not in CHALLENGE_COOKIES:
                cookies[c["name"]] = c["value"]
        cookies["aep_usuc_f"] = REGION_COOKIE
        saved.update(cookies=cookies, saved=time.time())
        CACHE.mkdir(exist_ok=True)
        jar.write_text(json.dumps(saved))

    # --- calls ----------------------------------------------------------------------

    def mtop(self, api, body, app=None, post=False, extra=None):
        self._ensure()
        opts = {"api": api, "v": "1.0", "data": json.loads(body), "type": "POST" if post else "GET",
                "dataType": "json", "timeout": 15000, **(extra or {})}
        if app:
            opts["appKey"] = app
        for attempt in range(2):
            wait = (WINDOW_WAIT if self.headed else HEADLESS_WAIT) * 1000
            try:
                j = json.loads(self.page.evaluate(MTOP_JS, [opts, wait]))
            except Exception as e:  # page navigated away / crashed
                j = {"ret": [f"CLIENT_ERROR::{e}"]}
            ret = " ".join(j.get("ret") or [])
            if ret.startswith("SUCCESS"):
                return j.get("data") or {}
            if "SESSION_EXPIRED" in ret or "NOT_LOGIN" in ret.upper():
                raise LoginRequired(f"{api}: {ret} — refresh aliexpress_cookie in the .env file")
            if any(k in ret for k in ("RGV587", "USER_VALIDATE", "CLIENT_TIMEOUT", "CLIENT_ERROR")) and not self.headed:
                self._escalate()
                continue
            if any(k in ret for k in ("RGV587", "USER_VALIDATE", "CLIENT_TIMEOUT")):
                raise CaptchaError(CAPTCHA_HINT)
            raise ApiError(f"{api} (browser): {ret}")
        raise CaptchaError(CAPTCHA_HINT)

    def get_text(self, url):
        """Load a page (HTML search result or a JSON endpoint) and return its text."""
        self._ensure()
        for attempt in range(2):
            try:
                # A second tab, so the base page keeps lib.mtop for later calls.
                if not self.tab or self.tab.is_closed():
                    self.tab = self.ctx.new_page()
                tab = self.tab
                tab.goto(url, wait_until="domcontentloaded", timeout=45000)
                deadline = time.time() + (WINDOW_WAIT if self.headed else HEADLESS_WAIT)
                while True:
                    ctype = tab.evaluate("() => document.contentType")
                    text = (tab.evaluate("() => document.body ? document.body.innerText : ''")
                            if "json" in ctype else tab.content())
                    if not is_captcha_text(text[:5000]) and "_____tmd_____" not in tab.url:
                        return text
                    if time.time() > deadline:
                        break
                    time.sleep(1)
            except Exception:
                pass
            if self.headed:
                break
            self._escalate()
        raise CaptchaError(CAPTCHA_HINT)
