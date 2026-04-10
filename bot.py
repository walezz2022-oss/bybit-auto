import asyncio
import logging
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
from config import TELEGRAM_TOKEN, ADMIN_IDS, BYBIT_ACCOUNTS
from bybit import (
    get_ad_details, get_my_ads, modify_ad,
    get_btc_usdt_price, get_max_float_pct, ping_api,
)

logger = logging.getLogger(__name__)


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


# ─────────────────────────────────────────
# 🗂️  Per-account state
#
# accounts_state[account_num] holds everything
# for that Bybit account independently.
# ─────────────────────────────────────────
def _fresh_account_state() -> dict:
    return {
        "user_settings": {
            "ad_id":        "",
            "bybit_uid":    "",
            "mode":         "fixed",
            "increment":    "0.05",
            "float_pct":    "",
            "ngn_usdt_ref": "",
            "interval":     2,
        },
        "ad_data":        {},
        "current_price":  Decimal("0"),
        "refresh_running": False,
        "refresh_task":    None,
        "user_state":      {},   # tracks pending text-input action per user
    }


accounts_state: dict[int, dict] = {
    num: _fresh_account_state() for num in BYBIT_ACCOUNTS
}

# Tracks which account each admin user is currently working with
# user_id -> account_num
user_active_account: dict[int, int] = {}


def get_active_account(user_id: int) -> int:
    """Return the active account number for a user, defaulting to the first."""
    if user_id not in user_active_account:
        user_active_account[user_id] = min(BYBIT_ACCOUNTS.keys())
    return user_active_account[user_id]


def get_creds(account_num: int) -> tuple[str, str]:
    acc = BYBIT_ACCOUNTS[account_num]
    return acc["key"], acc["secret"]


def get_state(account_num: int) -> dict:
    return accounts_state[account_num]


# ─────────────────────────────────────────
# 📊 Setup progress for an account
# ─────────────────────────────────────────
def setup_progress(account_num: int) -> tuple:
    s = get_state(account_num)
    us = s["user_settings"]
    steps = [
        bool(us.get("ad_id")),
        bool(us.get("bybit_uid")),
        bool(s["ad_data"]),
        bool(us.get("increment") or us.get("float_pct")),
        bool(us.get("interval")),
    ]
    done  = sum(steps)
    total = len(steps)
    bar   = "".join("✅" if x else "⬜" for x in steps)
    return done, total, bar


def next_setup_hint(account_num: int) -> str:
    s  = get_state(account_num)
    us = s["user_settings"]
    if not us.get("ad_id"):
        return "👉 Start by tapping *🆔 Set Ad ID*"
    if not us.get("bybit_uid"):
        return "👉 Next: tap *👤 Set UID* to set your Bybit user ID"
    if not s["ad_data"]:
        return "👉 Next: tap *📋 Fetch Ad Details* to load your ad from Bybit"
    mode = us.get("mode", "fixed")
    if mode == "fixed" and not us.get("increment"):
        return "👉 Next: tap *➕ Set Increment* to set your price step"
    if mode == "floating" and not us.get("float_pct"):
        return "👉 Next: tap *📊 Set Float %* to set your market percentage"
    if mode == "floating" and s["ad_data"].get("currencyId", "").upper() == "NGN" \
            and not us.get("ngn_usdt_ref"):
        return "👉 Next: tap *💱 Set NGN/USDT Ref* to set the reference rate"
    return "✅ *All set!* Tap *🟢 Start Auto-Update* to begin"


# ─────────────────────────────────────────
# 🏠 MAIN MENU  (account switcher lives here)
# ─────────────────────────────────────────
def main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    active = get_active_account(user_id)
    rows   = []

    # Account selector buttons
    acc_buttons = []
    for num in sorted(BYBIT_ACCOUNTS.keys()):
        label = f"{'✅ ' if num == active else ''}Account {num}"
        acc_buttons.append(
            InlineKeyboardButton(label, callback_data=f"switch_account_{num}")
        )
    # Split into rows of 2
    for i in range(0, len(acc_buttons), 2):
        rows.append(acc_buttons[i:i+2])

    s          = get_state(active)
    r_icon     = "🟢" if s["refresh_running"] else "📊"
    rows.append([InlineKeyboardButton(f"{r_icon} AD PRICE BOT", callback_data="section_ads")])
    rows.append([InlineKeyboardButton("📡 Bot Status",           callback_data="bot_status")])
    rows.append([InlineKeyboardButton("🔁 Reset This Account",   callback_data="reset_confirm")])
    return InlineKeyboardMarkup(rows)


def main_menu_text(user_id: int) -> str:
    active        = get_active_account(user_id)
    done, total, bar = setup_progress(active)
    s             = get_state(active)
    r_status      = "🟢 Running" if s["refresh_running"] else "🔴 Off"
    total_accounts = len(BYBIT_ACCOUNTS)

    return (
        f"🤖 *P2P Auto Price Bot*\n\n"
        f"🔑 Active: *Account {active}* of {total_accounts}\n"
        f"Setup: {bar} `{done}/{total}`\n\n"
        f"📊 Price Bot: {r_status}\n\n"
        "_Select an account then open AD PRICE BOT:_"
    )


def back_main():
    return [[InlineKeyboardButton("⬅️ Main Menu", callback_data="main_menu")]]


def back_section():
    return [[InlineKeyboardButton("⬅️ AD PRICE BOT", callback_data="section_ads")]]


# ─────────────────────────────────────────
# 📊 AD PRICE BOT SECTION
# ─────────────────────────────────────────
def ads_section_keyboard(user_id: int) -> InlineKeyboardMarkup:
    active     = get_active_account(user_id)
    s          = get_state(active)
    us         = s["user_settings"]
    mode       = us.get("mode", "fixed")
    mode_icon  = "💲" if mode == "fixed" else "📈"
    mode_label = f"{mode_icon} Mode: {mode.upper()}"
    ad_loaded  = bool(s["ad_data"])
    status     = "🟢 Stop Auto-Update" if s["refresh_running"] else "🔴 Start Auto-Update"

    rows = [
        [
            InlineKeyboardButton("🆔 Set Ad ID",         callback_data="set_ad_id"),
            InlineKeyboardButton("👤 Set UID",           callback_data="set_uid"),
        ],
        [
            InlineKeyboardButton("📋 Fetch Ad Details",  callback_data="fetch_ad"),
            InlineKeyboardButton("📃 My Ads List",       callback_data="fetch_my_ads"),
        ],
        [
            InlineKeyboardButton(mode_label,             callback_data="switch_mode"),
            InlineKeyboardButton("⏱ Interval",          callback_data="set_interval"),
        ],
    ]

    if mode == "fixed":
        rows.append([InlineKeyboardButton("➕ Set Increment", callback_data="set_increment")])
    else:
        rows.append([InlineKeyboardButton("📊 Set Float %",   callback_data="set_float_pct")])
        if s["ad_data"].get("currencyId", "").upper() == "NGN":
            rows.append([InlineKeyboardButton("💱 Set NGN/USDT Ref", callback_data="set_ngn_ref")])

    if ad_loaded:
        rows.append([InlineKeyboardButton("🔄 Update Once Now", callback_data="update_now")])

    rows.append([InlineKeyboardButton(status, callback_data="toggle_refresh")])
    rows += back_main()
    return InlineKeyboardMarkup(rows)


def ads_section_text(user_id: int) -> str:
    active = get_active_account(user_id)
    s      = get_state(active)
    us     = s["user_settings"]
    ad     = s["ad_data"]

    ad_id     = us.get("ad_id")       or "❗ Not set"
    uid       = us.get("bybit_uid")   or "❗ Not set"
    mode      = us.get("mode",        "fixed")
    interval  = us.get("interval",    2)
    increment = us.get("increment",   "0.05")
    float_pct = us.get("float_pct",   "") or "❗ Not set"
    ngn_ref   = us.get("ngn_usdt_ref","") or "❗ Not set"
    cur       = str(s["current_price"]) if s["current_price"] else "—"
    status    = "🟢 Running" if s["refresh_running"] else "🔴 Stopped"

    if ad:
        price    = ad.get("price",        "—")
        min_amt  = ad.get("minAmount",    "—")
        max_amt  = ad.get("maxAmount",    "—")
        qty      = ad.get("lastQuantity", ad.get("quantity", "—"))
        token    = ad.get("tokenId",      "—")
        currency = ad.get("currencyId",   "—")
        ad_stat  = {10: "🟢 Online", 20: "🔴 Offline", 30: "✅ Done"}.get(ad.get("status"), "?")
        max_pct  = get_max_float_pct(currency, token)
        ad_info  = (
            f"\n📋 *Loaded Ad:*\n"
            f"  💱 `{token}/{currency}` | 💲 `{price}`\n"
            f"  Min: `{min_amt}` | Max: `{max_amt}` | Qty: `{qty}`\n"
            f"  Status: {ad_stat} | Max float: `{max_pct}%`\n"
        )
    else:
        ad_info = "\n  ⚠️ No ad loaded yet\n"

    if mode == "fixed":
        mode_info = f"  ➕ Increment: `+{increment}` per cycle"
    else:
        mode_info = f"  📊 Float: `{float_pct}%`"
        if ad.get("currencyId", "").upper() == "NGN":
            mode_info += f" | 💱 NGN/USDT: `{ngn_ref}`"

    return (
        f"📊 *AD PRICE BOT — Account {active}*\n\n"
        f"🆔 Ad ID: `{ad_id}`\n"
        f"👤 UID: `{uid}`\n"
        f"🔀 Mode: `{mode.upper()}` | ⏱ Every `{interval}` min\n"
        f"{mode_info}\n"
        f"{ad_info}\n"
        f"📈 Session price: `{cur}` | {status}\n\n"
        f"_{next_setup_hint(active)}_"
    )


# ─────────────────────────────────────────
# 💲 Floating price calc
# ─────────────────────────────────────────
def calc_floating_price(ad_data: dict, float_pct: float, ngn_usdt_ref: float):
    btc = get_btc_usdt_price()
    if btc <= 0:
        return None, "Failed to fetch BTC/USDT from Bybit"
    currency = ad_data.get("currencyId", "").upper()
    if currency == "NGN":
        if ngn_usdt_ref <= 0:
            return None, "NGN/USDT reference price not set"
        raw = btc * ngn_usdt_ref * float_pct / 100
    else:
        raw = btc * float_pct / 100
    return (
        str(Decimal(str(raw)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
        None,
    )


# ─────────────────────────────────────────
# 🔄 PRICE UPDATE LOOP
# ─────────────────────────────────────────
async def auto_update_loop(bot, chat_id: int, account_num: int):
    s  = get_state(account_num)
    us = s["user_settings"]
    s["refresh_running"] = True

    interval  = us.get("interval", 2)
    increment = Decimal(str(us.get("increment", "0.05")))
    if us.get("mode") == "fixed":
        s["current_price"] = Decimal(str(s["ad_data"].get("price", "0")))

    api_key, api_secret = get_creds(account_num)
    logger.info(
        f"🚀 PRICE LOOP | account={account_num} mode={us.get('mode')} interval={interval}m"
    )
    cycle = 0

    while s["refresh_running"]:
        cycle += 1
        now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        mode = us.get("mode", "fixed")

        if mode == "fixed":
            new_p     = s["current_price"] + increment
            new_p_str = str(new_p.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP))
        else:
            float_pct    = float(us.get("float_pct", 0))
            ngn_usdt_ref = float(us.get("ngn_usdt_ref") or 0)
            new_p_str, err = calc_floating_price(s["ad_data"], float_pct, ngn_usdt_ref)
            if err:
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ *Acct {account_num} — Cycle {cycle} float error*\n`{err}`",
                    parse_mode="Markdown",
                )
                for _ in range(interval * 60):
                    if not s["refresh_running"]:
                        break
                    await asyncio.sleep(1)
                continue

        logger.info(f"[Acct {account_num}] Cycle {cycle} | {now} | {mode.upper()} | price={new_p_str}")
        result   = await asyncio.get_event_loop().run_in_executor(
            None, modify_ad, api_key, api_secret, us["ad_id"], new_p_str, s["ad_data"]
        )
        ret_code = result.get("retCode", result.get("ret_code", -1))
        ret_msg  = result.get("retMsg",  result.get("ret_msg", "Unknown"))

        if ret_code == 0:
            if mode == "fixed":
                s["current_price"] = new_p
            logger.info(f"[Acct {account_num}] Cycle {cycle} ✅ → {new_p_str}")
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"✅ *Acct {account_num} — Cycle {cycle}* `{now}`\n"
                    f"💲 `{new_p_str}` ({mode.upper()})"
                ),
                parse_mode="Markdown",
            )
        else:
            logger.error(f"[Acct {account_num}] Cycle {cycle} ❌ {ret_code} | {ret_msg}")
            extra = "\n💱 Update NGN/USDT ref if rate changed" \
                    if s["ad_data"].get("currencyId", "").upper() == "NGN" else ""
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"❌ *Acct {account_num} — Cycle {cycle} failed*\n"
                    f"`{ret_code}` — `{ret_msg}`{extra}"
                ),
                parse_mode="Markdown",
            )

        for _ in range(interval * 60):
            if not s["refresh_running"]:
                break
            await asyncio.sleep(1)

    logger.info(f"🛑 PRICE LOOP STOPPED | account={account_num}")


# ─────────────────────────────────────────
# /start  /menu
# ─────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("❌ Unauthorized")
        return
    await update.message.reply_text(
        main_menu_text(uid),
        reply_markup=main_menu_keyboard(uid),
        parse_mode="Markdown",
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


# ─────────────────────────────────────────
# 🏓 /pingbybit
# ─────────────────────────────────────────
async def ping_bybit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    active          = get_active_account(uid)
    api_key, api_secret = get_creds(active)
    await update.message.reply_text(f"⏳ Testing Account {active} API…")
    result   = await asyncio.get_event_loop().run_in_executor(
        None, ping_api, api_key, api_secret
    )
    ret_code = result.get("retCode", -1)
    if ret_code == 0:
        info     = result.get("result", {})
        perms    = info.get("permissions", {})
        ips      = info.get("ips", [])
        fiat_p2p = perms.get("FiatP2P", [])
        has_ads  = "Advertising" in fiat_p2p
        read_only = info.get("readOnly", 1)
        plines   = [
            f"  {'✅' if v else '➖'} {k}: {', '.join(v) if v else 'none'}"
            for k, v in perms.items()
        ]
        ad_stat = (
            "✅ Can edit ads" if has_ads and not read_only else
            "⚠️ Read only"   if has_ads else
            "❌ No P2P permission"
        )
        await update.message.reply_text(
            f"✅ *Account {active} connected!*\n\n"
            f"🔑 `...{info.get('apiKey','')[-6:]}`\n"
            f"🔒 Read only: `{'Yes' if read_only else 'No'}`\n"
            f"🌍 IPs: `{', '.join(ips) if ips else 'None'}`\n\n"
            f"🔓 *Permissions:*\n" + "\n".join(plines) +
            f"\n\n🛒 *P2P: {ad_stat}*",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"❌ *Account {active} API failed*\n`{result.get('retMsg','')}`",
            parse_mode="Markdown",
        )


# ─────────────────────────────────────────
# 🎛️ BUTTON HANDLER
# ─────────────────────────────────────────
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    uid     = query.from_user.id
    data    = query.data
    chat_id = query.message.chat_id

    if not is_admin(uid):
        return

    active              = get_active_account(uid)
    s                   = get_state(active)
    us                  = s["user_settings"]
    api_key, api_secret = get_creds(active)

    # ── 🔑 Switch account ──
    if data.startswith("switch_account_"):
        new_acc = int(data.split("_")[-1])
        if new_acc not in BYBIT_ACCOUNTS:
            await query.answer("Unknown account", show_alert=True)
            return
        user_active_account[uid] = new_acc
        await query.edit_message_text(
            main_menu_text(uid),
            reply_markup=main_menu_keyboard(uid),
            parse_mode="Markdown",
        )

    # ── 🏠 Main menu ──
    elif data == "main_menu":
        await query.edit_message_text(
            main_menu_text(uid),
            reply_markup=main_menu_keyboard(uid),
            parse_mode="Markdown",
        )

    # ── 📊 AD PRICE BOT section ──
    elif data == "section_ads":
        await query.edit_message_text(
            ads_section_text(uid),
            reply_markup=ads_section_keyboard(uid),
            parse_mode="Markdown",
        )

    # ── 📡 Bot Status ──
    elif data == "bot_status":
        lines = ["📡 *Bot Status — All Accounts*\n"]
        for num in sorted(BYBIT_ACCOUNTS.keys()):
            st   = get_state(num)
            su   = st["user_settings"]
            done, total, bar = setup_progress(num)
            r_st = "🟢 Running" if st["refresh_running"] else "🔴 Stopped"
            cur  = str(st["current_price"]) if st["current_price"] else "—"
            lines.append(
                f"*Account {num}* {'← active' if num == active else ''}\n"
                f"  Setup: {bar} `{done}/{total}`\n"
                f"  Price Bot: {r_st} | Price: `{cur}`\n"
                f"  Ad ID: `{su.get('ad_id') or 'Not set'}`\n"
                f"  Mode: `{su.get('mode','fixed').upper()}` | Interval: `{su.get('interval',2)} min`\n"
            )
        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(back_main()),
            parse_mode="Markdown",
        )

    # ── 🔁 Reset confirm ──
    elif data == "reset_confirm":
        await query.edit_message_text(
            f"⚠️ *Reset Account {active}?*\n\n"
            "This will clear all settings for this account and stop its price update loop.\n\n"
            "Are you sure?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, Reset", callback_data="reset_do")],
                [InlineKeyboardButton("❌ Cancel",     callback_data="main_menu")],
            ]),
            parse_mode="Markdown",
        )

    elif data == "reset_do":
        # Stop running loop for this account
        s["refresh_running"] = False
        if s.get("refresh_task"):
            s["refresh_task"].cancel()
            s["refresh_task"] = None
        accounts_state[active] = _fresh_account_state()
        await query.edit_message_text(
            f"✅ *Account {active} reset!* All settings cleared and price loop stopped.\n\n"
            "Tap /menu to start fresh.",
            parse_mode="Markdown",
        )

    # ── 🔀 Switch Mode ──
    elif data == "switch_mode":
        us["mode"] = "floating" if us.get("mode") == "fixed" else "fixed"
        note = " (takes effect next cycle)" if s["refresh_running"] else ""
        await query.edit_message_text(
            f"🔀 *Switched to {us['mode'].upper()}{note}*\n\n_{next_setup_hint(active)}_",
            reply_markup=InlineKeyboardMarkup(back_section()),
            parse_mode="Markdown",
        )

    # ── 🆔 Set Ad ID ──
    elif data == "set_ad_id":
        s["user_state"]["action"] = "ad_id"
        cur = us.get("ad_id", "") or "Not set"
        await query.edit_message_text(
            f"🆔 *Set Ad ID — Account {active}*\n\nCurrent: `{cur}`\n\n"
            "Send your Bybit Ad ID.\n"
            "💡 Use 📃 My Ads List to find it.\n\n"
            "Example: `2040156088201854976`",
            reply_markup=InlineKeyboardMarkup(back_section()),
            parse_mode="Markdown",
        )

    # ── 👤 Set UID ──
    elif data == "set_uid":
        s["user_state"]["action"] = "bybit_uid"
        cur = us.get("bybit_uid", "") or "Not set"
        await query.edit_message_text(
            f"👤 *Set Bybit UID — Account {active}*\n\nCurrent: `{cur}`\n\n"
            "Bybit App → Profile → copy UID under your username.\n\n"
            "Example: `520097760`",
            reply_markup=InlineKeyboardMarkup(back_section()),
            parse_mode="Markdown",
        )

    # ── 📃 My Ads ──
    elif data == "fetch_my_ads":
        await query.edit_message_text(f"⏳ Fetching ads for Account {active}…")
        result   = await asyncio.get_event_loop().run_in_executor(
            None, get_my_ads, api_key, api_secret
        )
        ret_code = result.get("retCode", result.get("ret_code", -1))
        if ret_code == 0:
            items = result.get("result", {}).get("items", [])
            if not items:
                await query.edit_message_text(
                    "📃 No ads found.",
                    reply_markup=InlineKeyboardMarkup(back_section()),
                )
                return
            bybit_uid = us.get("bybit_uid", "")
            lines     = [f"📃 *Account {active} — Your P2P Ads:*\n"]
            for item in items:
                if bybit_uid and str(item.get("userId", "")) != str(bybit_uid):
                    continue
                side = "BUY" if str(item.get("side", "")) == "0" else "SELL"
                stat = {10: "🟢", 20: "🔴", 30: "✅"}.get(item.get("status", 0), "❓")
                lines.append(
                    f"{stat} *{side}* `{item.get('tokenId','')}/{item.get('currencyId','')}`"
                    f" | 💲`{item.get('price','')}`\n"
                    f"🆔 `{item.get('id','')}`\n"
                )
            if len(lines) == 1:
                lines.append("No ads match your UID.")
            lines.append("\n_Tap any ID to copy → use 🆔 Set Ad ID_")
            msg = "\n".join(lines)
            if len(msg) > 4000:
                msg = msg[:4000] + "…(truncated)"
            await query.edit_message_text(
                msg,
                reply_markup=InlineKeyboardMarkup(back_section()),
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(
                f"❌ `{result.get('retMsg', result.get('ret_msg',''))}`",
                reply_markup=InlineKeyboardMarkup(back_section()),
                parse_mode="Markdown",
            )

    # ── 📋 Fetch Ad Details ──
    elif data == "fetch_ad":
        if not us.get("ad_id"):
            await query.edit_message_text(
                "❌ Set your Ad ID first (🆔 Set Ad ID).",
                reply_markup=InlineKeyboardMarkup(back_section()),
            )
            return
        await query.edit_message_text(f"⏳ Loading ad from Account {active}…")
        result   = await asyncio.get_event_loop().run_in_executor(
            None, get_ad_details, api_key, api_secret, us["ad_id"]
        )
        ret_code = result.get("retCode", result.get("ret_code", -1))
        if ret_code == 0:
            s["ad_data"] = result.get("result", {})
            ad           = s["ad_data"]
            token        = ad.get("tokenId",    "—")
            currency     = ad.get("currencyId", "—")
            max_pct      = get_max_float_pct(currency, token)
            ad_stat      = {10: "🟢 Online", 20: "🔴 Offline", 30: "✅ Done"}.get(ad.get("status"), "?")
            await query.edit_message_text(
                f"✅ *Account {active} — Ad Loaded!*\n\n"
                f"🆔 `{us['ad_id']}`\n"
                f"💱 `{token}/{currency}` | 💲 `{ad.get('price','')}`\n"
                f"Min: `{ad.get('minAmount','')}` | Max: `{ad.get('maxAmount','')}` | Qty: `{ad.get('lastQuantity','')}`\n"
                f"Status: {ad_stat} | Max float: `{max_pct}%`\n\n"
                f"✅ *Ready!* Now choose your mode and set increment or float %.\n"
                f"_{next_setup_hint(active)}_",
                reply_markup=InlineKeyboardMarkup(back_section()),
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(
                f"❌ `{result.get('retMsg', result.get('ret_msg',''))}`",
                reply_markup=InlineKeyboardMarkup(back_section()),
                parse_mode="Markdown",
            )

    # ── ➕ Set Increment ──
    elif data == "set_increment":
        s["user_state"]["action"] = "increment"
        await query.edit_message_text(
            f"➕ *Set Increment — Account {active}*\n\n"
            f"Current: `+{us.get('increment','0.05')}` per cycle\n\n"
            "Send the amount to add each cycle.\nExamples: `0.05` | `1` | `0.5`",
            reply_markup=InlineKeyboardMarkup(back_section()),
            parse_mode="Markdown",
        )

    # ── 📊 Float % ──
    elif data == "set_float_pct":
        if not s["ad_data"]:
            await query.edit_message_text(
                "❌ Fetch Ad Details first.",
                reply_markup=InlineKeyboardMarkup(back_section()),
            )
            return
        token    = s["ad_data"].get("tokenId",    "USDT").upper()
        currency = s["ad_data"].get("currencyId", "NGN").upper()
        max_pct  = get_max_float_pct(currency, token)
        s["user_state"]["action"] = "float_pct"
        cur = us.get("float_pct", "") or "Not set"
        await query.edit_message_text(
            f"📊 *Set Float % — Account {active}*\n\n"
            f"Pair: `{token}/{currency}` | Max: *{max_pct}%*\nCurrent: `{cur}`\n\n"
            f"Formula: `BTC/USDT {'× NGN/USDT ref ' if currency == 'NGN' else ''}× your% ÷ 100`\n\n"
            f"Send a value ≤ {max_pct}.\nExample: `105`",
            reply_markup=InlineKeyboardMarkup(back_section()),
            parse_mode="Markdown",
        )

    # ── 💱 NGN Ref ──
    elif data == "set_ngn_ref":
        s["user_state"]["action"] = "ngn_usdt_ref"
        cur = us.get("ngn_usdt_ref", "") or "Not set"
        await query.edit_message_text(
            f"💱 *NGN/USDT Reference — Account {active}*\n\nCurrent: `{cur}`\n\n"
            "Check Bybit P2P market for the current NGN/USDT rate.\nExample: `1580`",
            reply_markup=InlineKeyboardMarkup(back_section()),
            parse_mode="Markdown",
        )

    # ── ⏱ Interval ──
    elif data == "set_interval":
        s["user_state"]["action"] = "interval"
        await query.edit_message_text(
            f"⏱ *Set Interval — Account {active}*\n\n"
            f"Current: every `{us.get('interval', 2)}` min\n\n"
            "Send minutes between each price update.\nExamples: `2` | `5` | `10`",
            reply_markup=InlineKeyboardMarkup(back_section()),
            parse_mode="Markdown",
        )

    # ── 🔄 Update Once Now ──
    elif data == "update_now":
        if not s["ad_data"] or not us.get("ad_id"):
            await query.edit_message_text(
                "❌ Load ad details first.",
                reply_markup=InlineKeyboardMarkup(back_section()),
            )
            return
        mode = us.get("mode", "fixed")
        await query.edit_message_text(f"⏳ Updating Account {active} ({mode} mode)…")
        if mode == "fixed":
            price = str(s["current_price"]) if s["current_price"] else s["ad_data"].get("price", "0")
        else:
            float_pct    = float(us.get("float_pct", 0))
            ngn_usdt_ref = float(us.get("ngn_usdt_ref") or 0)
            price, err   = calc_floating_price(s["ad_data"], float_pct, ngn_usdt_ref)
            if err:
                await query.edit_message_text(
                    f"❌ `{err}`",
                    reply_markup=InlineKeyboardMarkup(back_section()),
                    parse_mode="Markdown",
                )
                return
        result   = await asyncio.get_event_loop().run_in_executor(
            None, modify_ad, api_key, api_secret, us["ad_id"], price, s["ad_data"]
        )
        rc = result.get("retCode", result.get("ret_code", -1))
        rm = result.get("retMsg",  result.get("ret_msg",  ""))
        if rc == 0:
            await query.edit_message_text(
                f"✅ *Account {active} Updated!* Price: `{price}` ({mode.upper()})\n\n"
                f"_{next_setup_hint(active)}_",
                reply_markup=InlineKeyboardMarkup(back_section()),
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(
                f"❌ `{rc}` — `{rm}`",
                reply_markup=InlineKeyboardMarkup(back_section()),
                parse_mode="Markdown",
            )

    # ── 🟢/🔴 Toggle Price Update ──
    elif data == "toggle_refresh":
        if s["refresh_running"]:
            s["refresh_running"] = False
            if s.get("refresh_task"):
                s["refresh_task"].cancel()
                s["refresh_task"] = None
            s["current_price"] = Decimal("0")
            await query.edit_message_text(
                f"🔴 *Account {active} — Price update stopped.*",
                reply_markup=InlineKeyboardMarkup(back_section()),
                parse_mode="Markdown",
            )
        else:
            if not s["ad_data"] or not us.get("ad_id"):
                await query.edit_message_text(
                    f"❌ Not ready:\n\n_{next_setup_hint(active)}_",
                    reply_markup=InlineKeyboardMarkup(back_section()),
                    parse_mode="Markdown",
                )
                return
            mode     = us.get("mode", "fixed")
            interval = us.get("interval", 2)
            task     = asyncio.create_task(auto_update_loop(context.bot, chat_id, active))
            s["refresh_task"] = task
            await query.edit_message_text(
                f"🟢 *Account {active} — Price update started!*\n"
                f"🔀 `{mode.upper()}` | ⏱ every `{interval}` min",
                reply_markup=InlineKeyboardMarkup(back_section()),
                parse_mode="Markdown",
            )


# ─────────────────────────────────────────
# 📝 TEXT INPUT HANDLER
# ─────────────────────────────────────────
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return

    text   = update.message.text.strip()
    active = get_active_account(uid)
    s      = get_state(active)
    us     = s["user_settings"]
    action = s["user_state"].get("action")

    async def reply(msg):
        await update.message.reply_text(msg, parse_mode="Markdown")

    if action == "ad_id":
        us["ad_id"] = text
        s["ad_data"].clear()
        s["user_state"]["action"] = None
        await reply(f"✅ Account {active} — Ad ID: `{text}`\n\n_{next_setup_hint(active)}_")

    elif action == "bybit_uid":
        us["bybit_uid"] = text
        s["user_state"]["action"] = None
        await reply(f"✅ Account {active} — UID: `{text}`\n\n_{next_setup_hint(active)}_")

    elif action == "increment":
        try:
            val = Decimal(text)
            if val <= 0:
                raise ValueError
            us["increment"] = text
            s["user_state"]["action"] = None
            await reply(f"✅ Account {active} — Increment: `+{text}` per cycle\n\n_{next_setup_hint(active)}_")
        except Exception:
            await reply("❌ Send a positive number like `0.05`")

    elif action == "float_pct":
        try:
            val      = float(text)
            if val <= 0:
                raise ValueError
            token    = s["ad_data"].get("tokenId",    "USDT").upper()
            currency = s["ad_data"].get("currencyId", "NGN").upper()
            max_pct  = get_max_float_pct(currency, token)
            if val > max_pct:
                await reply(f"❌ `{val}%` exceeds max for {token}/{currency}\nMax: *{max_pct}%*")
                return
            us["float_pct"] = text
            s["user_state"]["action"] = None
            await reply(f"✅ Account {active} — Float %: `{text}%`\n\n_{next_setup_hint(active)}_")
        except Exception:
            await reply("❌ Send a number like `105`")

    elif action == "ngn_usdt_ref":
        try:
            val = float(text)
            if val <= 0:
                raise ValueError
            us["ngn_usdt_ref"] = text
            s["user_state"]["action"] = None
            await reply(f"✅ Account {active} — NGN/USDT ref: `{text}`\n\n_{next_setup_hint(active)}_")
        except Exception:
            await reply("❌ Send a number like `1580`")

    elif action == "interval":
        try:
            val = int(text)
            if val < 1:
                raise ValueError
            us["interval"] = val
            s["user_state"]["action"] = None
            await reply(f"✅ Account {active} — Interval: every `{val}` min\n\n_{next_setup_hint(active)}_")
        except Exception:
            await reply("❌ Send a whole number like `2`")


# ─────────────────────────────────────────
# 🔧 BUILD BOT
# ─────────────────────────────────────────
def start_bot():
    application = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .updater(None)
        .build()
    )
    application.add_handler(CommandHandler("start",     start))
    application.add_handler(CommandHandler("menu",      menu_command))
    application.add_handler(CommandHandler("pingbybit", ping_bybit_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    logger.info("🤖 Bot handlers registered")
    return application
