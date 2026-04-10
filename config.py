import os
import logging
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# ── Multiple Bybit Accounts ──────────────────────────────────────────────────
# Set BYBIT_API_KEY_1, BYBIT_API_SECRET_1, BYBIT_API_KEY_2, BYBIT_API_SECRET_2
# etc. in your Render environment variables.
BYBIT_ACCOUNTS = {}
for _i in range(1, 6):
    _key    = os.getenv(f"BYBIT_API_KEY_{_i}")
    _secret = os.getenv(f"BYBIT_API_SECRET_{_i}")
    if _key and _secret:
        BYBIT_ACCOUNTS[_i] = {
            "key":    _key.strip().strip('"').strip("'"),
            "secret": _secret.strip().strip('"').strip("'"),
        }

if not BYBIT_ACCOUNTS:
    raise ValueError(
        "No Bybit accounts configured. "
        "Add BYBIT_API_KEY_1 + BYBIT_API_SECRET_1 (and optionally _2, _3…) "
        "to your Render environment variables."
    )

# ── Multiple Admin Telegram IDs ──────────────────────────────────────────────
# Set ADMIN_ID_1, ADMIN_ID_2, ADMIN_ID_3 … as SEPARATE variables on Render.
# Each must be a plain integer (your Telegram numeric user ID).
ADMIN_IDS: set[int] = set()

for _i in range(1, 20):          # supports up to 19 admins
    _raw = os.getenv(f"ADMIN_ID_{_i}")
    if _raw is None:
        continue
    # Strip all common accidental wrappers Render/dotenv might add
    _cleaned = _raw.strip().strip('"').strip("'").strip()
    if not _cleaned:
        continue
    try:
        ADMIN_IDS.add(int(_cleaned))
    except ValueError:
        # Log clearly so you can see exactly what went wrong at startup
        logger.warning(
            f"[Config] ADMIN_ID_{_i} could not be parsed as an integer. "
            f"Raw value was: {repr(_raw)}"
        )

# Legacy fallback — single ADMIN_TELEGRAM_ID variable
if not ADMIN_IDS:
    _raw = os.getenv("ADMIN_TELEGRAM_ID")
    if _raw:
        _cleaned = _raw.strip().strip('"').strip("'").strip()
        try:
            ADMIN_IDS.add(int(_cleaned))
        except ValueError:
            logger.warning(f"[Config] ADMIN_TELEGRAM_ID could not be parsed: {repr(_raw)}")

if not ADMIN_IDS:
    raise ValueError(
        "No admin IDs loaded. "
        "Add ADMIN_ID_1 (and optionally ADMIN_ID_2 …) to your Render environment. "
        "Values must be plain integers — your Telegram numeric user ID."
    )

# ── Startup confirmation log — always printed so you can verify ──────────────
# You will see this in your Render logs every time the app starts.
print(f"[Config] ✅ Bybit accounts loaded : {sorted(BYBIT_ACCOUNTS.keys())}")
print(f"[Config] ✅ Admin Telegram IDs    : {sorted(ADMIN_IDS)}")
