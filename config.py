import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# ── Multiple Bybit Accounts ──
# Set BYBIT_API_KEY_1, BYBIT_API_SECRET_1, BYBIT_API_KEY_2, BYBIT_API_SECRET_2 etc. in environment
BYBIT_ACCOUNTS = {}
for _i in range(1, 6):
    _key    = os.getenv(f"BYBIT_API_KEY_{_i}")
    _secret = os.getenv(f"BYBIT_API_SECRET_{_i}")
    if _key and _secret:
        BYBIT_ACCOUNTS[_i] = {"key": _key.strip(), "secret": _secret.strip()}

if not BYBIT_ACCOUNTS:
    raise ValueError(
        "No Bybit accounts configured. "
        "Add BYBIT_API_KEY_1 and BYBIT_API_SECRET_1 (and optionally _2, _3…) to your environment."
    )

# ── Multiple Admin IDs ──
# Set ADMIN_ID_1, ADMIN_ID_2 etc. in environment. At least ADMIN_ID_1 must be set.
ADMIN_IDS = set()
for _i in range(1, 10):
    _val = os.getenv(f"ADMIN_ID_{_i}")
    if _val:
        try:
            ADMIN_IDS.add(int(_val.strip()))
        except ValueError:
            pass

# Fallback to legacy single ADMIN_TELEGRAM_ID
if not ADMIN_IDS:
    _val = os.getenv("ADMIN_TELEGRAM_ID")
    if _val:
        try:
            ADMIN_IDS.add(int(_val.strip()))
        except ValueError:
            pass

if not ADMIN_IDS:
    raise ValueError(
        "No admin IDs set. Add ADMIN_ID_1 (and optionally ADMIN_ID_2…) to your environment."
    )
