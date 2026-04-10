import os

# ─────────────────────────────────────────
# 🤖 Telegram
# ─────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# Multiple admins: "12345,67890"
ADMIN_IDS = [
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()
]

# ─────────────────────────────────────────
# 🔐 Bybit Multi-Account Loader
# ─────────────────────────────────────────
def load_bybit_accounts():
    accounts = {}
    i = 1

    while True:
        key = os.getenv(f"BYBIT_API_KEY_{i}")
        secret = os.getenv(f"BYBIT_API_SECRET_{i}")

        if not key or not secret:
            break

        accounts[str(i)] = {
            "api_key": key,
            "api_secret": secret
        }

        i += 1

    return accounts


BYBIT_ACCOUNTS = load_bybit_accounts()
