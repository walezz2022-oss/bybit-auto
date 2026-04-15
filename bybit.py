import time
import hmac
import hashlib
import requests
import json
import logging
import uuid

logger = logging.getLogger(__name__)

BASE_URL = "https://api.bybit.com"

# ─────────────────────────────────────────
# Max floating % per currency and coin
# ─────────────────────────────────────────
MAX_FLOAT_PCT = {
    "NGN": {"BTC": 110, "ETH": 110, "USDT": 110, "USDC": 110},
    "USD": {"BTC": 130, "ETH": 130, "USDT": 120, "USDC": 120},
}

def get_max_float_pct(currency_id: str, token_id: str) -> int:
    return MAX_FLOAT_PCT.get(currency_id.upper(), {}).get(token_id.upper(), 110)


# ─────────────────────────────────────────
# Payment type ID → display name
# ─────────────────────────────────────────
PAYMENT_TYPE_MAP = {
    "14":  "Bank Transfer",
    "62":  "Moniepoint",
    "377": "Balance",
    "470": "PalmPay",
    "500": "Kuda",
    "520": "OPay",
    "522": "Paycom / OPay",
    "528": "PAGA",
    "570": "Sterling Bank",
    "571": "UBA",
    "572": "First Bank",
    "573": "Access Bank",
    "574": "GTBank",
    "575": "Zenith Bank",
    "576": "Wema Bank",
    "583": "OPay",
}

def get_payment_name(payment_type) -> str:
    """Return a human-readable payment name; fall back to bankName or raw type."""
    return PAYMENT_TYPE_MAP.get(str(payment_type), f"Type {payment_type}")


# ─────────────────────────────────────────
# 🔐 Auth helpers  (credentials passed in)
# ─────────────────────────────────────────
def _generate_signature(api_key: str, api_secret: str,
                        timestamp: str, payload: str,
                        recv_window: str = "5000") -> str:
    raw = f"{timestamp}{api_key}{recv_window}{payload}"
    return hmac.new(
        api_secret.encode("utf-8"),
        raw.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()


def _get_headers(api_key: str, api_secret: str, payload: str = "") -> dict:
    timestamp   = str(int(time.time() * 1000))
    recv_window = "5000"
    sign        = _generate_signature(api_key, api_secret, timestamp, payload, recv_window)
    return {
        "X-BAPI-API-KEY":     api_key,
        "X-BAPI-TIMESTAMP":   timestamp,
        "X-BAPI-SIGN":        sign,
        "X-BAPI-RECV-WINDOW": recv_window,
        "Content-Type":       "application/json",
    }


def _parse_response(response, label=""):
    logger.info(f"[Bybit]{label} HTTP {response.status_code} | {response.text[:600]}")
    if not response.text.strip():
        return {"retCode": -1, "retMsg": "Empty response — check IP whitelist"}
    if response.status_code == 404:
        return {"retCode": -1, "retMsg": "404 — endpoint not found"}
    if response.text.strip().startswith("<"):
        return {"retCode": -1, "retMsg": f"CDN block — HTTP {response.status_code}"}
    try:
        data = response.json()
        # Normalise old-style keys
        if "ret_code" in data and "retCode" not in data:
            data["retCode"] = data["ret_code"]
            data["retMsg"]  = data.get("ret_msg", "")
        return data
    except Exception as e:
        return {"retCode": -1, "retMsg": f"JSON error: {e}"}


def _post(api_key: str, api_secret: str, endpoint: str, body: dict) -> dict:
    url     = BASE_URL + endpoint
    payload = json.dumps(body, separators=(',', ':'))
    headers = _get_headers(api_key, api_secret, payload)
    try:
        response = requests.post(url, headers=headers, data=payload, timeout=10)
        return _parse_response(response, f" [{endpoint.split('/')[-1]}]")
    except requests.exceptions.Timeout:
        return {"retCode": -1, "retMsg": "Request timed out"}
    except Exception as e:
        logger.error(f"[Bybit] POST {endpoint} error: {e}")
        return {"error": str(e)}


# ─────────────────────────────────────────
# 🏓 Ping / API key info
# ─────────────────────────────────────────
def ping_api(api_key: str, api_secret: str) -> dict:
    try:
        r = requests.get(f"{BASE_URL}/v3/public/time", timeout=5)
        logger.info(f"[Bybit] Server time: {r.json().get('result',{}).get('timeSecond')}")
    except Exception as e:
        return {"retCode": -1, "retMsg": f"Cannot reach Bybit: {e}"}
    url     = BASE_URL + "/v5/user/query-api"
    headers = _get_headers(api_key, api_secret, "")
    try:
        return _parse_response(requests.get(url, headers=headers, timeout=10), " [ping]")
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────
# 💲 BTC/USDT spot price  (public)
# ─────────────────────────────────────────
def get_btc_usdt_price() -> float:
    try:
        r     = requests.get(
            f"{BASE_URL}/v5/market/tickers",
            params={"category": "spot", "symbol": "BTCUSDT"},
            timeout=10
        )
        items = r.json().get("result", {}).get("list", [])
        if items:
            price = float(items[0].get("lastPrice", 0))
            logger.info(f"[Bybit] BTC/USDT = {price}")
            return price
    except Exception as e:
        logger.error(f"[Bybit] BTC/USDT error: {e}")
    return 0.0


# ─────────────────────────────────────────
# 📋 Ad Details
# ─────────────────────────────────────────
def get_ad_details(api_key: str, api_secret: str, ad_id: str) -> dict:
    return _post(api_key, api_secret, "/v5/p2p/item/info", {"itemId": ad_id})


# ─────────────────────────────────────────
# 📃 My Ads List
# ─────────────────────────────────────────
def get_my_ads(api_key: str, api_secret: str) -> dict:
    url     = BASE_URL + "/v5/p2p/item/personal/list"
    headers = _get_headers(api_key, api_secret, "{}")
    try:
        return _parse_response(
            requests.post(url, headers=headers, data="{}", timeout=10),
            " [personal/list]"
        )
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────
# 📦 Pending / active orders
#   status 10 = waiting for buyer payment
#   status 20 = waiting for seller to release
# ─────────────────────────────────────────
def get_pending_orders(api_key: str, api_secret: str, status: int = 10) -> dict:
    return _post(api_key, api_secret,
                 "/v5/p2p/order/pending/simplifyList",
                 {"status": status, "page": 1, "size": 30})


# ─────────────────────────────────────────
# 📄 Order Detail
# ─────────────────────────────────────────
def get_order_detail(api_key: str, api_secret: str, order_id: str) -> dict:
    return _post(api_key, api_secret, "/v5/p2p/order/info", {"orderId": order_id})


# ─────────────────────────────────────────
# 👤 Counterparty Info
# ─────────────────────────────────────────
def get_counterparty_info(api_key: str, api_secret: str,
                          user_id: str, order_id: str) -> dict:
    return _post(api_key, api_secret, "/v5/p2p/user/order/personal/info", {
        "originalUid": str(user_id),
        "orderId":     str(order_id),
    })


# ─────────────────────────────────────────
# 💬 Send Chat Message  (repeat `count` times)
# ─────────────────────────────────────────
def send_chat_message(api_key: str, api_secret: str,
                      order_id: str, message: str, count: int = 1) -> dict:
    last = {}
    for _ in range(max(1, min(count, 5))):
        last = _post(api_key, api_secret, "/v5/p2p/order/message/send", {
            "orderId":     order_id,
            "message":     message,
            "contentType": "str",
            "msgUuid":     uuid.uuid4().hex,
        })
    return last


# ─────────────────────────────────────────
# 🚀 Release / finish order
# ─────────────────────────────────────────
def release_order(api_key: str, api_secret: str, order_id: str) -> dict:
    logger.info(f"[Bybit] RELEASE order: {order_id}")
    return _post(api_key, api_secret, "/v5/p2p/order/finish", {"orderId": order_id})


# ─────────────────────────────────────────
# 🔄 Modify Ad price
# ─────────────────────────────────────────
def modify_ad(api_key: str, api_secret: str,
              ad_id: str, new_price: str, ad_data: dict) -> dict:
    payment_terms = ad_data.get("paymentTerms", [])
    payment_ids   = [str(pt["id"]) for pt in payment_terms if pt.get("id")]
    tps           = ad_data.get("tradingPreferenceSet", {})
    trading_pref  = {k: str(tps.get(k, "0")) for k in [
        "hasUnPostAd", "isKyc", "isEmail", "isMobile", "hasRegisterTime",
        "registerTimeThreshold", "orderFinishNumberDay30", "completeRateDay30",
        "hasOrderFinishNumberDay30", "hasCompleteRateDay30", "hasNationalLimit",
    ]}
    trading_pref["nationalLimit"] = str(tps.get("nationalLimit", ""))

    body = {
        "id":            ad_id,
        "actionType":    "MODIFY",
        "priceType":     str(ad_data.get("priceType", "0")),
        "price":         str(new_price),
        "premium":       str(ad_data.get("premium", "")),
        "minAmount":     str(ad_data.get("minAmount", "")),
        "maxAmount":     str(ad_data.get("maxAmount", "")),
        "quantity":      str(ad_data.get("lastQuantity", ad_data.get("quantity", ""))),
        "paymentIds":    payment_ids,
        "paymentPeriod": str(ad_data.get("paymentPeriod", "15")),
        "remark":        str(ad_data.get("remark", "")),
        "tradingPreferenceSet": trading_pref,
    }
    logger.info(f"[Bybit] MODIFY {ad_id} → price={new_price}")
    result = _post(api_key, api_secret, "/v5/p2p/item/update", body)
    logger.info(f"[Bybit] MODIFY result: {result}")
    return result
