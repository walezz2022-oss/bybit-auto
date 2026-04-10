import os

# ─────────────────────────────
# Telegram
# ─────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

ADMIN_IDS = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip()
]

# ─────────────────────────────
# Bybit multi accounts
# ─────────────────────────────
BYBIT_API_KEY_1 = os.getenv("BYBIT_API_KEY_1")
BYBIT_API_SECRET_1 = os.getenv("BYBIT_API_SECRET_1")

BYBIT_API_KEY_2 = os.getenv("BYBIT_API_KEY_2")
BYBIT_API_SECRET_2 = os.getenv("BYBIT_API_SECRET_2")


BYBIT_ACCOUNTS = {
    "1": {"api_key": BYBIT_API_KEY_1, "api_secret": BYBIT_API_SECRET_1},
    "2": {"api_key": BYBIT_API_KEY_2, "api_secret": BYBIT_API_SECRET_2},
}
