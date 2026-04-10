import time
import hmac
import hashlib
import requests
import json

BASE_URL = "https://api.bybit.com"

# =========================
# FLOATING SETTINGS
# =========================
MAX_FLOAT_PCT = {
    "NGN": {"BTC": 110, "USDT": 110},
    "USD": {"BTC": 130, "USDT": 120},
}

def get_max_float(currency, coin):
    return MAX_FLOAT_PCT.get(currency.upper(), {}).get(coin.upper(), 110)


# =========================
# AUTH
# =========================
def sign(key, secret, payload=""):
    ts = str(int(time.time()*1000))
    recv = "5000"
    raw = f"{ts}{key}{recv}{payload}"
    sig = hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return ts, recv, sig


def headers(key, secret, payload=""):
    ts, recv, sig = sign(key, secret, payload)
    return {
        "X-BAPI-API-KEY": key,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-SIGN": sig,
        "X-BAPI-RECV-WINDOW": recv,
        "Content-Type": "application/json"
    }


def post(endpoint, key, secret, body):
    payload = json.dumps(body, separators=(",", ":"))
    h = headers(key, secret, payload)
    r = requests.post(BASE_URL+endpoint, headers=h, data=payload, timeout=10)
    return r.json()


# =========================
# MARKET PRICE
# =========================
def get_market_price():
    r = requests.get(
        f"{BASE_URL}/v5/market/tickers",
        params={"category": "spot", "symbol": "BTCUSDT"},
        timeout=10
    )
    return float(r.json()["result"]["list"][0]["lastPrice"])


# =========================
# GET ADS
# =========================
def get_ads(key, secret):
    return post("/v5/p2p/item/personal/list", key, secret, {})


# =========================
# MODIFY AD
# =========================
def modify_ad(key, secret, ad_id, price):
    return post("/v5/p2p/item/update", key, secret, {
        "id": ad_id,
        "actionType": "MODIFY",
        "price": str(price)
    })


# =========================
# FLOAT CALCULATION
# =========================
def calculate_price(base_price, float_pct, currency="NGN", coin="BTC"):
    max_pct = get_max_float(currency, coin)

    if float_pct > max_pct:
        float_pct = max_pct

    return base_price * (float_pct / 100)
