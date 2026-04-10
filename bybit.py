import time
import hmac
import hashlib
import requests
import json
import logging
import uuid

BASE_URL = "https://api.bybit.com"

logger = logging.getLogger(__name__)

# ─────────────────────────────
# SIGNATURE (DYNAMIC)
# ─────────────────────────────
def generate_signature(api_key, api_secret, timestamp, payload, recv_window="5000"):
    raw = f"{timestamp}{api_key}{recv_window}{payload}"
    return hmac.new(
        api_secret.encode(),
        raw.encode(),
        hashlib.sha256
    ).hexdigest()


def get_headers(api_key, api_secret, payload=""):
    ts = str(int(time.time() * 1000))
    sign = generate_signature(api_key, api_secret, ts, payload)

    return {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-SIGN": sign,
        "X-BAPI-RECV-WINDOW": "5000",
        "Content-Type": "application/json"
    }


def post(api_key, api_secret, endpoint, body):
    url = BASE_URL + endpoint
    payload = json.dumps(body, separators=(",", ":"))
    headers = get_headers(api_key, api_secret, payload)

    try:
        r = requests.post(url, headers=headers, data=payload, timeout=10)
        return r.json()
    except Exception as e:
        logger.error(f"Bybit error {endpoint}: {e}")
        return {"retCode": -1, "retMsg": str(e)}


# ─────────────────────────────
# ACCOUNT HELPER
# ─────────────────────────────
def get_account(accounts, selected_id):
    return accounts.get(selected_id)


# ─────────────────────────────
# API FUNCTIONS (USE ACCOUNT)
# ─────────────────────────────
def get_ad_details(acc, ad_id):
    return post(acc["key"], acc["secret"], "/v5/p2p/item/info", {"itemId": ad_id})


def get_my_ads(acc):
    return post(acc["key"], acc["secret"], "/v5/p2p/item/personal/list", {})


def get_pending_orders(acc):
    return post(acc["key"], acc["secret"], "/v5/p2p/order/pending/simplifyList",
                {"status": 10, "page": 1, "size": 30})


def get_order_detail(acc, order_id):
    return post(acc["key"], acc["secret"], "/v5/p2p/order/info",
                {"orderId": order_id})


def modify_ad(acc, ad_id, price, ad_data):
    return post(acc["key"], acc["secret"], "/v5/p2p/item/update", {
        "id": ad_id,
        "price": price,
        "actionType": "MODIFY"
    })
