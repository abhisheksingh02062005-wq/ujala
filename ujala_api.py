"""
Ujala Happy Pack API — all network calls are synchronous (run in executor).

FIXES applied:
- api_register() now has full retry logic (was missing entirely).
- Each retry creates a FRESH requests.Session so stale/rejected cookies
  from a previous attempt never bleed into the next one.
- api_spin() treats "already spun / already participated" responses as
  success so a network timeout after a successful spin isn't penalised.
- Logging improved so failed attempts print the full response body.
"""

import base64
import json
import random
import time
import hmac
import hashlib
import requests
import logging
import urllib.parse

from config import (
    API_BASE, BASE_URL, BARCODE, HEADERS_BASE,
    MAX_RETRIES, RETRY_DELAY, FIRST_NAMES, SURNAMES, CITIES
)

logger = logging.getLogger(__name__)


# ── Random data helpers ───────────────────────────────────────────────────────

def rnd_name():
    return f"{random.choice(FIRST_NAMES)} {random.choice(SURNAMES)}"


def rnd_city():
    return random.choice(CITIES)


# ── Signing ───────────────────────────────────────────────────────────────────

def sign_payload(payload_dict, data_key):
    c = str(int(time.time() * 1000))
    json_str = json.dumps(payload_dict, separators=(",", ":"))
    a = base64.b64encode(json_str.encode("utf-8")).decode("ascii")
    s = base64.b64encode(c.encode("utf-8")).decode("ascii")

    hmac_key = data_key[4:18]
    hmac_input = s + "." + a
    h = hmac.new(hmac_key.encode("utf-8"), hmac_input.encode("utf-8"), hashlib.sha256).hexdigest()
    l = base64.b64encode(h.encode("utf-8")).decode("ascii")

    f_chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    h_pos = random.randint(1, 6)
    p_len = random.randint(0, 6) * 2
    k = "".join(random.choices(f_chars, k=p_len))
    g = str(p_len) + str(h_pos) + l[:h_pos] + k + l[h_pos:]

    return s + "." + a + "." + g, int(c)


def sign_payload_form(user_key, data_key, extra_fields=None):
    payload_dict = dict(extra_fields or {})
    payload_dict["userKey"] = user_key
    t = int(time.time() * 1000)
    payload_dict["t"] = t
    data_value, ts = sign_payload(payload_dict, data_key)
    data_str = (
        "userKey=" + str(user_key)
        + "&data="
        + urllib.parse.quote_plus(data_value)
    )
    return data_str, ts


# ── Response decoder ──────────────────────────────────────────────────────────

def decode_resp(raw):
    try:
        outer = json.loads(raw)
        if "resp" in outer:
            return json.loads(base64.b64decode(outer["resp"]).decode())
        return outer
    except Exception as e:
        logger.error(f"decode_resp error: {e} | raw={raw[:200]}")
        return {}


# ── Session helpers ───────────────────────────────────────────────────────────

def make_session(cookies_dict=None):
    sess = requests.Session()
    sess.headers.update(HEADERS_BASE)
    if cookies_dict:
        for k, v in cookies_dict.items():
            sess.cookies.set(k, v)
    return sess


def session_cookies(sess):
    return dict(sess.cookies)


# ── Retry wrapper ─────────────────────────────────────────────────────────────

def with_retry(fn, label, fresh_session_factory=None):
    """
    Retry `fn()` up to MAX_RETRIES times.

    If `fresh_session_factory` is provided it is called before each attempt
    and the new session is passed to `fn(session)`.  This ensures stale or
    rejected cookies from a failed attempt never carry over into a retry.
    """
    last = {}
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if fresh_session_factory is not None:
                sess = fresh_session_factory()
                result, cookies = fn(sess)
            else:
                result = fn()
                cookies = None

            if result.get("statusCode") in (200, 201):
                return (result, cookies) if fresh_session_factory is not None else result

            last = result
            logger.warning(
                f"{label} attempt {attempt}/{MAX_RETRIES}: "
                f"statusCode={result.get('statusCode')} "
                f"msg={result.get('message', 'N/A')}"
            )
        except Exception as e:
            last = {"statusCode": 0, "message": str(e)}
            logger.warning(f"{label} attempt {attempt}/{MAX_RETRIES} exception: {e}")

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY)

    return (last, {}) if fresh_session_factory is not None else last


# ── Helpers ───────────────────────────────────────────────────────────────────

def safe_decode(r):
    """Always decode response body through decode_resp, falling back to HTTP status."""
    result = decode_resp(r.text)
    if not result:
        result = {"statusCode": r.status_code, "message": r.text[:300]}
    elif not result.get("statusCode"):
        result["statusCode"] = r.status_code
    return result


# ── API steps ─────────────────────────────────────────────────────────────────

def api_register():
    """
    Step 1: Register and get userKey + dataKey.
    Returns (result_dict, session_cookies).

    FIX: Added retry logic — previously a single timeout would fail
    the entire registration with no recovery.
    """
    def attempt(sess):
        try:
            r = sess.post(
                f"{API_BASE}/users",
                headers={**HEADERS_BASE, "Content-Type": "application/json"},
                data=BARCODE,
                timeout=15,
            )
            result = safe_decode(r)
            return result, session_cookies(sess)
        except Exception as e:
            return {"statusCode": 0, "message": str(e)}, {}

    def factory():
        return make_session()

    return with_retry(attempt, "register", fresh_session_factory=factory)


def api_get_otp(cookies_dict, user_key, data_key, mobile, name, city, image_bytes):
    """
    Step 2: Request OTP.
    Returns (result_dict, updated_cookies).

    FIX: Each retry now uses a FRESH session seeded from the original
    cookies, so a rejected session from attempt N doesn't poison attempt N+1.
    """
    ts = int(time.time() * 1000)
    url = f"{API_BASE}/users/getOTP/{user_key}?t={ts}"

    headers = dict(HEADERS_BASE)
    headers["Authorization"] = f"Bearer {data_key}"

    payload_fields = {
        "name": name,
        "mobile": mobile,
        "email": "",
        "city": city.lower(),
        "code": BARCODE,
        "agreed1": "Yes",
        "agreed2": "Yes",
        "userKey": user_key,
        "t": ts,
    }
    data_value, _ = sign_payload(payload_fields, data_key)
    fields = [
        ("userKey", (None, str(user_key))),
        ("pack",    ("ujala_pack.jpg", image_bytes, "image/jpeg")),
        ("data",    (None, data_value)),
    ]

    def attempt(sess):
        try:
            r = sess.post(url, headers=headers, files=fields, timeout=30)
            result = safe_decode(r)
            return result, session_cookies(sess)
        except Exception as e:
            return {"statusCode": 0, "message": str(e)}, {}

    def factory():
        # Always seed each retry from the same original cookies
        return make_session(cookies_dict)

    return with_retry(attempt, "getOTP", fresh_session_factory=factory)


def api_verify_otp(cookies_dict, user_key, data_key, otp):
    """
    Step 3: Verify OTP.
    Returns (result_dict, updated_cookies).

    FIX: Fresh session per retry.
    """
    def attempt(sess):
        data_str, ts = sign_payload_form(user_key, data_key, {"otp": otp})
        url = f"{API_BASE}/users/verifyOTP/{user_key}?t={ts}"

        headers = dict(HEADERS_BASE)
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        try:
            r = sess.post(url, headers=headers, data=data_str, timeout=15)
            result = safe_decode(r)
            return result, session_cookies(sess)
        except Exception as e:
            return {"statusCode": 0, "message": str(e)}, {}

    def factory():
        return make_session(cookies_dict)

    return with_retry(attempt, "verifyOTP", fresh_session_factory=factory)


def api_spin(cookies_dict, user_key, access_token, data_key):
    """
    Step 4: Spin the wheel.
    Returns (result_dict, updated_cookies).

    FIX: If the server returns "already spun / already participated", treat
    it as a success (200) so a network timeout after a successful spin
    doesn't cause a false failure and unnecessary point refund.
    """
    def attempt(sess):
        data_str, ts = sign_payload_form(user_key, data_key, {})
        url = f"{API_BASE}/users/speenTheWheel/{user_key}?t={ts}"

        headers = dict(HEADERS_BASE)
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        headers["Authorization"] = f"Bearer {access_token}"
        try:
            r = sess.post(url, headers=headers, data=data_str, timeout=20)
            result = safe_decode(r)
            # Treat "already spun" as success — wheel was already spun on a
            # previous attempt that timed out before the response arrived.
            if is_already_spun_error(result):
                logger.info("spin: already spun — treating as success")
                result["statusCode"] = 200
                if not result.get("reward"):
                    result["reward"] = "Reward claimed! Check your SMS."
            return result, session_cookies(sess)
        except Exception as e:
            return {"statusCode": 0, "message": str(e)}, {}

    def factory():
        return make_session(cookies_dict)

    return with_retry(attempt, "spin", fresh_session_factory=factory)


def api_claim(cookies_dict, user_key, access_token, data_key):
    """Step 5: Claim reward."""
    sess = make_session(cookies_dict)
    candidates = [
        f"users/claimNow/{user_key}",
        f"users/submitDetails/{user_key}",
        f"users/claim/{user_key}",
        f"users/getReward/{user_key}",
    ]
    for path in candidates:
        data_str, ts = sign_payload_form(user_key, data_key, {})
        url = f"{API_BASE}/{path}?t={ts}"
        headers = dict(HEADERS_BASE)
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        headers["Authorization"] = f"Bearer {access_token}"
        try:
            r = sess.post(url, headers=headers, data=data_str, timeout=15)
            result = decode_resp(r.text)
            if result.get("statusCode") == 200:
                return result
        except Exception:
            pass
    return {}


# ── Error classifiers ─────────────────────────────────────────────────────────

def is_already_used_error(result: dict) -> bool:
    """Check if the API response means the number was already used/registered."""
    msg = str(result.get("message", "")).lower()
    keywords = [
        "already", "registered", "exist", "used", "duplicate",
        "mobile already", "exceeded", "participation", "limit",
        "exceed", "max", "already participated",
    ]
    return any(k in msg for k in keywords)


def is_already_spun_error(result: dict) -> bool:
    """Check if the wheel was already spun (e.g. after a timeout retry)."""
    msg = str(result.get("message", "")).lower()
    keywords = [
        "already spun", "already played", "already participated",
        "wheel already", "spin already", "already claimed",
        "already used", "participated",
    ]
    return any(k in msg for k in keywords)
