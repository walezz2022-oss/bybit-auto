import os

# =========================
# TELEGRAM
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# =========================
# ADMINS (MULTIPLE)
# =========================
ADMIN_IDS = set()

admins = os.getenv("ADMIN_IDS", "")
for a in admins.split(","):
    if a.strip().isdigit():
        ADMIN_IDS.add(int(a.strip()))

if not ADMIN_IDS:
    raise ValueError("ADMIN_IDS not set")

# =========================
# MULTIPLE BYBIT ACCOUNTS
# =========================
BYBIT_ACCOUNTS = {}

i = 1
while True:
    key = os.getenv(f"BYBIT_API_KEY_{i}")
    secret = os.getenv(f"BYBIT_API_SECRET_{i}")

    if not key or not secret:
        break

    BYBIT_ACCOUNTS[f"Account {i}"] = {
        "key": key,
        "secret": secret
    }

    i += 1

if not BYBIT_ACCOUNTS:
    raise ValueError("No Bybit accounts found")
