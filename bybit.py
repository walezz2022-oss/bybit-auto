# bybit.py

import time
import hmac
import hashlib
import requests
import json
import logging
from config import BYBIT_ACCOUNTS

logger = logging.getLogger(__name__)

BASE_URL = "https://api.bybit.com"

# ─────────────────────────────────────────
# 🧠 Active account (switchable)
# ─────────────────────────────────────────
CURRENT_ACCOUNT = {"key": None, "secret": None}

def set_account(account_id: str):
    acc = BYBIT_ACCOUNTS.get(account_id)
    if not acc:
        raise Exception(f"Account {account_id} not found")

    CURRENT_ACCOUNT["key"] = acc["api_key"]
    CURRENT_ACCOUNT["secret"] = acc["api_secret"]

    logger.info(f"[Bybit] Switched to account {account_id}")


# ─────────────────────────────────────────
# Floating limits
# ─────────────────────────────────────────
MAX_FLOAT_PCT = {
    "NGN": {"BTC": 110, "ETH": 110, "USDT": 110, "USDC": 110},
    "USD": {"BTC": 130, "ETH": 130, "USDT": 120, "USDC": 120},
}

def get_max_float_pct(currency_id: str, token_id: str) -> int:
    return MAX_FLOAT_PCT.get(currency_id.upper(), {}).get(token_id.upper(), 110)


# ─────────────────────────────────────────
# 🔐 SIGNATURE (dynamic account)
# ─────────────────────────────────────────
def generate_signature(timestamp: str, payload: str) -> str:
    raw = f"{timestamp}{CURRENT_ACCOUNT['key']}5000{payload}"
    return hmac.new(
        CURRENT_ACCOUNT["secret"].encode(),
        raw.encode(),
        hashlib.sha256
    ).hexdigest()


def get_headers(payload=""):
    timestamp = str(int(time.time() * 1000))
    return {
        "X-BAPI-API-KEY": CURRENT_ACCOUNT["key"],
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-SIGN": generate_signature(timestamp, payload),
        "X-BAPI-RECV-WINDOW": "5000",
        "Content-Type": "application/json"
    }


# ─────────────────────────────────────────
# CORE REQUEST
# ─────────────────────────────────────────
def _post(endpoint, body):
    payload = json.dumps(body, separators=(',', ':'))
    headers = get_headers(payload)

    try:
        r = requests.post(BASE_URL + endpoint, headers=headers, data=payload, timeout=10)
        return r.json()
    except Exception as e:
        logger.error(e)
        return {"retCode": -1, "retMsg": str(e)}


# ─────────────────────────────────────────
# PRICE DATA
# ─────────────────────────────────────────
def get_btc_usdt_price():
    try:
        r = requests.get(BASE_URL + "/v5/market/tickers",
                         params={"category": "spot", "symbol": "BTCUSDT"}, timeout=10)
        return float(r.json()["result"]["list"][0]["lastPrice"])
    except:
        return 0.0


# ─────────────────────────────────────────
# ADS
# ─────────────────────────────────────────
def get_ad_details(ad_id):
    return _post("/v5/p2p/item/info", {"itemId": ad_id})


def modify_ad(ad_id, new_price, ad_data):
    payment_ids = [str(x["id"]) for x in ad_data.get("paymentTerms", []) if x.get("id")]

    body = {
        "id": ad_id,
        "actionType": "MODIFY",
        "price": str(new_price),
        "quantity": str(ad_data.get("lastQuantity", ad_data.get("quantity", ""))),
        "minAmount": str(ad_data.get("minAmount", "")),
        "maxAmount": str(ad_data.get("maxAmount", "")),
        "paymentIds": payment_ids,
        "remark": str(ad_data.get("remark", "")),
    }

    return _post("/v5/p2p/item/update", body)
