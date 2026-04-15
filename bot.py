import asyncio
import logging
import re
import uuid
import requests as http_requests
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)
from config import TELEGRAM_TOKEN, ADMIN_IDS, BYBIT_ACCOUNTS
from bybit import (
    get_ad_details, get_my_ads, modify_ad,
    get_btc_usdt_price, get_max_float_pct, ping_api,
    get_pending_orders, get_order_detail, get_counterparty_info,
    send_chat_message, release_order, get_payment_name,
)

logger = logging.getLogger(__name__)


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


# ═══════════════════════════════════════════════════════════════════
#  PER-ACCOUNT STATE
# ═══════════════════════════════════════════════════════════════════
def _fresh_account_state() -> dict:
    return {
        # ── Price bot ──────────────────────────────────────────────
        "user_settings": {
            "ad_id":        "",
            "bybit_uid":    "",
            "mode":         "fixed",
            "increment":    "0.05",
            "float_pct":    "",
            "ngn_usdt_ref": "",
            "interval":     2,
        },
        "ad_data":         {},
        "current_price":   Decimal("0"),
        "refresh_running": False,
        "refresh_task":    None,
        # ── Order monitor ──────────────────────────────────────────
        "order_monitor_running": False,
        "order_monitor_task":    None,
        "seen_order_ids":        set(),   # status-10 notifications already sent
        "paid_notified_ids":     set(),   # status-20 "release coin" notifications sent
        # ── Auto-message settings ──────────────────────────────────
        "auto_msg_text":  "",
        "auto_msg_count": 1,
        # ── Pending text-input action ──────────────────────────────
        "user_state": {},
    }


accounts_state: dict[int, dict] = {
    num: _fresh_account_state() for num in BYBIT_ACCOUNTS
}

# Which account each admin is currently working with
user_active_account: dict[int, int] = {}


def get_active_account(user_id: int) -> int:
    if user_id not in user_active_account:
        user_active_account[user_id] = min(BYBIT_ACCOUNTS.keys())
    return user_active_account[user_id]


def get_creds(account_num: int) -> tuple[str, str]:
    acc = BYBIT_ACCOUNTS[account_num]
    return acc["key"], acc["secret"]


def get_state(account_num: int) -> dict:
    return accounts_state[account_num]


# ═══════════════════════════════════════════════════════════════════
#  SETUP PROGRESS
# ═══════════════════════════════════════════════════════════════════
def setup_progress(account_num: int) -> tuple:
    s  = get_state(account_num)
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


# ═══════════════════════════════════════════════════════════════════
#  NAVIGATION HELPERS
# ═══════════════════════════════════════════════════════════════════
def back_main():
    return [[InlineKeyboardButton("⬅️ Main Menu", callback_data="main_menu")]]


def back_ads():
    return [[InlineKeyboardButton("⬅️ AD PRICE BOT", callback_data="section_ads")]]


def back_orders():
    return [[InlineKeyboardButton("⬅️ ORDER MONITOR", callback_data="section_orders")]]


# ═══════════════════════════════════════════════════════════════════
#  MAIN MENU
# ═══════════════════════════════════════════════════════════════════
def main_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    active = get_active_account(user_id)
    rows   = []

    # Account selector
    acc_buttons = []
    for num in sorted(BYBIT_ACCOUNTS.keys()):
        label = f"{'✅ ' if num == active else ''}Account {num}"
        acc_buttons.append(
            InlineKeyboardButton(label, callback_data=f"switch_account_{num}")
        )
    for i in range(0, len(acc_buttons), 2):
        rows.append(acc_buttons[i:i+2])

    s      = get_state(active)
    r_icon = "🟢" if s["refresh_running"]       else "📊"
    o_icon = "🔔" if s["order_monitor_running"] else "📦"

    rows.append([InlineKeyboardButton(f"{r_icon} AD PRICE BOT",   callback_data="section_ads")])
    rows.append([InlineKeyboardButton(f"{o_icon} ORDER MONITOR",  callback_data="section_orders")])
    rows.append([
        InlineKeyboardButton("🌐 My IP Address",    callback_data="my_ip"),
        InlineKeyboardButton("📡 Bot Status",       callback_data="bot_status"),
    ])
    rows.append([InlineKeyboardButton("🔁 Reset This Account",    callback_data="reset_confirm")])
    return InlineKeyboardMarkup(rows)


def main_menu_text(user_id: int) -> str:
    active           = get_active_account(user_id)
    done, total, bar = setup_progress(active)
    s                = get_state(active)
    r_status         = "🟢 Running" if s["refresh_running"]       else "🔴 Off"
    o_status         = "🔔 Active"  if s["order_monitor_running"] else "🔕 Off"
    total_accounts   = len(BYBIT_ACCOUNTS)
    return (
        f"🤖 *P2P Auto Bot*\n\n"
        f"🔑 Active: *Account {active}* of {total_accounts}\n"
        f"Setup: {bar} `{done}/{total}`\n\n"
        f"📊 Price Bot: {r_status}\n"
        f"📦 Order Monitor: {o_status}\n\n"
        "_Select a section below:_"
    )


# ═══════════════════════════════════════════════════════════════════
#  AD PRICE BOT SECTION
# ═══════════════════════════════════════════════════════════════════
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
            InlineKeyboardButton("🆔 Set Ad ID",        callback_data="set_ad_id"),
            InlineKeyboardButton("👤 Set UID",          callback_data="set_uid"),
        ],
        [
            InlineKeyboardButton("📋 Fetch Ad Details", callback_data="fetch_ad"),
            InlineKeyboardButton("📃 My Ads List",      callback_data="fetch_my_ads"),
        ],
        [
            InlineKeyboardButton(mode_label,            callback_data="switch_mode"),
            InlineKeyboardButton("⏱ Interval",         callback_data="set_interval"),
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

    ad_id     = us.get("ad_id")        or "❗ Not set"
    uid       = us.get("bybit_uid")    or "❗ Not set"
    mode      = us.get("mode",         "fixed")
    interval  = us.get("interval",     2)
    increment = us.get("increment",    "0.05")
    float_pct = us.get("float_pct",    "") or "❗ Not set"
    ngn_ref   = us.get("ngn_usdt_ref", "") or "❗ Not set"
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


# ═══════════════════════════════════════════════════════════════════
#  ORDER MONITOR SECTION
# ═══════════════════════════════════════════════════════════════════
def orders_section_keyboard(user_id: int) -> InlineKeyboardMarkup:
    active = get_active_account(user_id)
    s      = get_state(active)
    mon    = "🔔 Stop Monitoring" if s["order_monitor_running"] else "🔕 Start Monitoring"
    msg_set = "✅ Auto-Msg Set" if s["auto_msg_text"] else "💬 Set Auto-Message"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(mon,                callback_data="toggle_order_monitor")],
        [InlineKeyboardButton(msg_set,            callback_data="set_auto_msg")],
        [InlineKeyboardButton("🔢 Set Msg Count", callback_data="set_msg_count")],
        [InlineKeyboardButton("🗑 Clear Seen",    callback_data="clear_seen_orders")],
        *back_main(),
    ])


def orders_section_text(user_id: int) -> str:
    active = get_active_account(user_id)
    s      = get_state(active)
    status = "🔔 Active — checking every 10 s" if s["order_monitor_running"] else "🔕 Stopped"
    msg    = s["auto_msg_text"] or "❗ Not set"
    count  = s["auto_msg_count"]
    seen   = len(s["seen_order_ids"])
    paid   = len(s["paid_notified_ids"])
    return (
        f"📦 *ORDER MONITOR — Account {active}*\n\n"
        f"Status: {status}\n"
        f"Orders seen: `{seen}` | Release notified: `{paid}`\n\n"
        f"💬 Auto-message: _{msg}_\n"
        f"🔢 Send times: `{count}` (max 5)\n\n"
        "_When a new order arrives the bot will:_\n"
        "1️⃣ Send you full order details\n"
        "2️⃣ Send your auto-message to the buyer X times\n"
        "3️⃣ When buyer marks paid → resend details + 🚀 Release button"
    )


# ═══════════════════════════════════════════════════════════════════
#  ORDER MESSAGE FORMATTERS
# ═══════════════════════════════════════════════════════════════════
def _bank_name(pay_term: dict) -> str:
    """
    Return the best human-readable bank/payment name.
    Prefer bankName from the response; fall back to payment type map.
    """
    bank = (pay_term.get("bankName") or "").strip()
    if bank:
        return bank
    return get_payment_name(pay_term.get("paymentType", ""))


def format_new_order(order: dict, buyer_info: dict) -> str:
    """Message sent when a NEW order arrives (status 10 — waiting for buyer to pay)."""
    order_id   = order.get("id",         "—")
    amount     = order.get("amount",     "—")
    currency   = order.get("currencyId", "—")
    quantity   = order.get("quantity",   "—")
    token      = order.get("tokenId",    "—")
    price      = order.get("price",      "—")

    # Buyer info from counterparty endpoint
    buyer_name   = buyer_info.get("realName",          "") or \
                   buyer_info.get("nickName",           "—")
    rating       = buyer_info.get("goodAppraiseRate",  "—")
    total_orders = buyer_info.get("totalFinishCount",  "—")
    recent_orders = buyer_info.get("recentFinishCount","—")

    return (
        f"📦 *New Order — Account needs payment*\n"
        f"{'─' * 30}\n"
        f"🆔 `{order_id}`\n"
        f"💵 Amount: `{amount} {currency}`\n"
        f"🪙 Qty: `{quantity} {token}` @ `{price}`\n"
        f"{'─' * 30}\n"
        f"👤 Buyer: *{buyer_name}*\n"
        f"⭐ Rating: `{rating}%` | Orders: `{total_orders}` (30d: `{recent_orders}`)\n"
        f"{'─' * 30}\n"
        f"⏳ Waiting for buyer to make payment…"
    )


def format_paid_order(order: dict) -> str:
    """Message sent when buyer has marked order as paid (status 20)."""
    order_id = order.get("id",         "—")
    amount   = order.get("amount",     "—")
    currency = order.get("currencyId", "—")
    quantity = order.get("quantity",   "—")
    token    = order.get("tokenId",    "—")
    price    = order.get("price",      "—")

    # Payment details buyer sent money to
    pay_term   = order.get("confirmedPayTerm", {}) or {}
    if not pay_term:
        terms    = order.get("paymentTermList", [])
        pay_term = terms[0] if terms else {}

    bank      = _bank_name(pay_term)
    real_name = (pay_term.get("realName")  or "").strip() or "—"
    acct_no   = (pay_term.get("accountNo") or "").strip() or "—"

    return (
        f"💳 *Buyer Has Paid — Release Required*\n"
        f"{'─' * 30}\n"
        f"🆔 `{order_id}`\n"
        f"💵 Amount: `{amount} {currency}`\n"
        f"🪙 Qty: `{quantity} {token}` @ `{price}`\n"
        f"{'─' * 30}\n"
        f"🏦 Payment sent to:\n"
        f"  Bank/Channel: *{bank}*\n"
        f"  Name: `{real_name}`\n"
        f"  Account: `{acct_no}`\n"
        f"{'─' * 30}\n"
        f"✅ Verify payment received then tap *Release Coin* below."
    )


def release_button(order_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 RELEASE COIN", callback_data=f"release_{order_id}")]
    ])


# ═══════════════════════════════════════════════════════════════════
#  ORDER MONITOR LOOP
# ═══════════════════════════════════════════════════════════════════
async def order_monitor_loop(bot, chat_id: int, account_num: int):
    s                        = get_state(account_num)
    s["order_monitor_running"] = True
    api_key, api_secret      = get_creds(account_num)
    logger.info(f"🔔 ORDER MONITOR STARTED | account={account_num}")

    while s["order_monitor_running"]:
        try:
            # ── Status 10: new orders waiting for buyer payment ──────────
            res10    = await asyncio.get_event_loop().run_in_executor(
                None, get_pending_orders, api_key, api_secret, 10
            )
            if res10.get("retCode", res10.get("ret_code", -1)) == 0:
                for item in res10.get("result", {}).get("items", []):
                    order_id = item.get("id")
                    if not order_id or order_id in s["seen_order_ids"]:
                        continue

                    # Fetch full order detail for quantity/token
                    det = await asyncio.get_event_loop().run_in_executor(
                        None, get_order_detail, api_key, api_secret, order_id
                    )
                    if det.get("retCode", det.get("ret_code", -1)) != 0:
                        continue
                    order = det.get("result", {})

                    # Fetch buyer info
                    buyer_uid  = order.get("targetUserId", "")
                    buyer_info = {}
                    if buyer_uid:
                        bi = await asyncio.get_event_loop().run_in_executor(
                            None, get_counterparty_info,
                            api_key, api_secret, str(buyer_uid), order_id
                        )
                        if bi.get("retCode", bi.get("ret_code", -1)) == 0:
                            buyer_info = bi.get("result", {})

                    # Send new-order notification
                    await bot.send_message(
                        chat_id=chat_id,
                        text=format_new_order(order, buyer_info),
                        parse_mode="Markdown",
                    )
                    s["seen_order_ids"].add(order_id)
                    logger.info(f"[Orders] Acct {account_num} — new order notified: {order_id}")

                    # Send auto-message to buyer
                    msg_text  = s.get("auto_msg_text", "")
                    msg_count = s.get("auto_msg_count", 1)
                    if msg_text:
                        await asyncio.get_event_loop().run_in_executor(
                            None, send_chat_message,
                            api_key, api_secret, order_id, msg_text, msg_count
                        )
                        logger.info(
                            f"[Orders] Acct {account_num} — auto-msg x{msg_count} sent to {order_id}"
                        )

            # ── Status 20: buyer has marked as paid ─────────────────────
            res20 = await asyncio.get_event_loop().run_in_executor(
                None, get_pending_orders, api_key, api_secret, 20
            )
            if res20.get("retCode", res20.get("ret_code", -1)) == 0:
                for item in res20.get("result", {}).get("items", []):
                    order_id = item.get("id")
                    if not order_id or order_id in s["paid_notified_ids"]:
                        continue

                    # Fetch full detail for payment info
                    det = await asyncio.get_event_loop().run_in_executor(
                        None, get_order_detail, api_key, api_secret, order_id
                    )
                    if det.get("retCode", det.get("ret_code", -1)) != 0:
                        continue
                    order = det.get("result", {})

                    await bot.send_message(
                        chat_id=chat_id,
                        text=format_paid_order(order),
                        reply_markup=release_button(order_id),
                        parse_mode="Markdown",
                    )
                    s["paid_notified_ids"].add(order_id)
                    logger.info(
                        f"[Orders] Acct {account_num} — paid notification sent: {order_id}"
                    )

        except Exception as e:
            logger.error(f"[Orders] Acct {account_num} loop error: {e}")

        await asyncio.sleep(10)

    logger.info(f"🔕 ORDER MONITOR STOPPED | account={account_num}")


# ═══════════════════════════════════════════════════════════════════
#  PRICE UPDATE HELPERS
# ═══════════════════════════════════════════════════════════════════
def extract_bybit_price_from_error(ret_msg: str):
    tokens = re.findall(r'[\d,]+(?:\.\d+)?\.?', ret_msg)
    for token in reversed(tokens):
        clean = token.rstrip('.').replace(',', '')
        try:
            val = float(clean)
            if val > 0:
                return clean
        except ValueError:
            continue
    return None


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


async def _do_modify_with_retry(bot, chat_id, account_num, ad_id, price_str,
                                ad_data, cycle_label, mode, state):
    """
    Attempt modify_ad. On failure, extract Bybit's suggested price and retry once.
    Returns (success: bool, final_price_used: str).
    """
    api_key, api_secret = get_creds(account_num)
    result   = await asyncio.get_event_loop().run_in_executor(
        None, modify_ad, api_key, api_secret, ad_id, price_str, ad_data
    )
    rc = result.get("retCode", result.get("ret_code", -1))
    rm = result.get("retMsg",  result.get("ret_msg",  ""))

    if rc == 0:
        return True, price_str

    # Try to extract Bybit's own suggested price
    bybit_price = extract_bybit_price_from_error(rm)
    if bybit_price:
        logger.info(f"[Acct {account_num}] {cycle_label} — retrying with Bybit price: {bybit_price}")
        retry = await asyncio.get_event_loop().run_in_executor(
            None, modify_ad, api_key, api_secret, ad_id, bybit_price, ad_data
        )
        rrc = retry.get("retCode", retry.get("ret_code", -1))
        rrm = retry.get("retMsg",  retry.get("ret_msg",  ""))
        if rrc == 0:
            return True, bybit_price
        # Both failed
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"❌ *Acct {account_num} — {cycle_label} failed (retry also failed)*\n"
                f"1st: `{rc}` — `{rm}`\n"
                f"Retry: `{rrc}` — `{rrm}`"
            ),
            parse_mode="Markdown",
        )
        return False, price_str

    # No price hint in error — report as-is
    extra = "\n💱 Update NGN/USDT ref if rate changed" \
            if ad_data.get("currencyId", "").upper() == "NGN" else ""
    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"❌ *Acct {account_num} — {cycle_label} failed*\n"
            f"`{rc}` — `{rm}`{extra}"
        ),
        parse_mode="Markdown",
    )
    return False, price_str


# ═══════════════════════════════════════════════════════════════════
#  PRICE UPDATE LOOP
# ═══════════════════════════════════════════════════════════════════
async def auto_update_loop(bot, chat_id: int, account_num: int):
    s  = get_state(account_num)
    us = s["user_settings"]
    s["refresh_running"] = True

    interval  = us.get("interval", 2)
    increment = Decimal(str(us.get("increment", "0.05")))
    if us.get("mode") == "fixed":
        s["current_price"] = Decimal(str(s["ad_data"].get("price", "0")))

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

        logger.info(
            f"[Acct {account_num}] Cycle {cycle} | {now} | {mode.upper()} | price={new_p_str}"
        )
        ok, used_price = await _do_modify_with_retry(
            bot, chat_id, account_num,
            us["ad_id"], new_p_str, s["ad_data"],
            f"Cycle {cycle}", mode, s
        )
        if ok:
            if mode == "fixed":
                try:
                    s["current_price"] = Decimal(used_price)
                except Exception:
                    s["current_price"] = new_p
            note = " _(auto-corrected to Bybit limit)_" if used_price != new_p_str else ""
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"✅ *Acct {account_num} — Cycle {cycle}* `{now}`\n"
                    f"💲 `{used_price}` ({mode.upper()}){note}"
                ),
                parse_mode="Markdown",
            )

        for _ in range(interval * 60):
            if not s["refresh_running"]:
                break
            await asyncio.sleep(1)

    logger.info(f"🛑 PRICE LOOP STOPPED | account={account_num}")


# ═══════════════════════════════════════════════════════════════════
#  /start  /menu
# ═══════════════════════════════════════════════════════════════════
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


# ═══════════════════════════════════════════════════════════════════
#  /pingbybit
# ═══════════════════════════════════════════════════════════════════
async def ping_bybit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    active          = get_active_account(uid)
    api_key, api_secret = get_creds(active)
    await update.message.reply_text(f"⏳ Testing Account {active} API…")
    result    = await asyncio.get_event_loop().run_in_executor(
        None, ping_api, api_key, api_secret
    )
    ret_code  = result.get("retCode", -1)
    if ret_code == 0:
        info      = result.get("result", {})
        perms     = info.get("permissions", {})
        ips       = info.get("ips", [])
        fiat_p2p  = perms.get("FiatP2P", [])
        has_ads   = "Advertising" in fiat_p2p
        read_only = info.get("readOnly", 1)
        plines    = [
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
            f"🌍 Bound IPs: `{', '.join(ips) if ips else 'None'}`\n\n"
            f"🔓 *Permissions:*\n" + "\n".join(plines) +
            f"\n\n🛒 *P2P: {ad_stat}*",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"❌ *Account {active} API failed*\n`{result.get('retMsg','')}`",
            parse_mode="Markdown",
        )


# ═══════════════════════════════════════════════════════════════════
#  BUTTON HANDLER
# ═══════════════════════════════════════════════════════════════════
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

    # ── Switch account ────────────────────────────────────────────
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

    # ── Main menu ─────────────────────────────────────────────────
    elif data == "main_menu":
        await query.edit_message_text(
            main_menu_text(uid),
            reply_markup=main_menu_keyboard(uid),
            parse_mode="Markdown",
        )

    # ── 🌐 My IP ──────────────────────────────────────────────────
    elif data == "my_ip":
        await query.edit_message_text("⏳ Fetching IP address…")
        try:
            ip = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: http_requests.get("https://api.ipify.org", timeout=8).text.strip()
            )
            await query.edit_message_text(
                f"🌐 *Public IP Address*\n\n"
                f"`{ip}`\n\n"
                f"Whitelist this IP on your Bybit API key settings.",
                reply_markup=InlineKeyboardMarkup(back_main()),
                parse_mode="Markdown",
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ Could not fetch IP: `{e}`",
                reply_markup=InlineKeyboardMarkup(back_main()),
                parse_mode="Markdown",
            )

    # ── AD PRICE BOT section ──────────────────────────────────────
    elif data == "section_ads":
        await query.edit_message_text(
            ads_section_text(uid),
            reply_markup=ads_section_keyboard(uid),
            parse_mode="Markdown",
        )

    # ── ORDER MONITOR section ─────────────────────────────────────
    elif data == "section_orders":
        await query.edit_message_text(
            orders_section_text(uid),
            reply_markup=orders_section_keyboard(uid),
            parse_mode="Markdown",
        )

    # ── Toggle Order Monitor ──────────────────────────────────────
    elif data == "toggle_order_monitor":
        if s["order_monitor_running"]:
            s["order_monitor_running"] = False
            if s.get("order_monitor_task"):
                s["order_monitor_task"].cancel()
                s["order_monitor_task"] = None
            await query.edit_message_text(
                f"🔕 *Account {active} — Order monitoring stopped.*",
                reply_markup=InlineKeyboardMarkup(back_main()),
                parse_mode="Markdown",
            )
        else:
            task = asyncio.create_task(
                order_monitor_loop(context.bot, chat_id, active)
            )
            s["order_monitor_task"] = task
            await query.edit_message_text(
                f"🔔 *Account {active} — Order monitoring started!*\n"
                "Checking every 10 seconds for new orders.",
                reply_markup=InlineKeyboardMarkup(back_main()),
                parse_mode="Markdown",
            )

    # ── Set Auto-Message ──────────────────────────────────────────
    elif data == "set_auto_msg":
        s["user_state"]["action"] = "auto_msg_text"
        cur = s["auto_msg_text"] or "Not set"
        await query.edit_message_text(
            f"💬 *Set Auto-Message — Account {active}*\n\n"
            f"Current: _{cur}_\n\n"
            "This message is sent to the buyer automatically when a new order arrives.\n\n"
            "Send your message text now:",
            reply_markup=InlineKeyboardMarkup(back_orders()),
            parse_mode="Markdown",
        )

    # ── Set Message Count ─────────────────────────────────────────
    elif data == "set_msg_count":
        s["user_state"]["action"] = "auto_msg_count"
        await query.edit_message_text(
            f"🔢 *Set Message Send Count — Account {active}*\n\n"
            f"Current: `{s['auto_msg_count']}` times\n\n"
            "How many times should the auto-message be sent? (1–5)\n\n"
            "Send a number from 1 to 5:",
            reply_markup=InlineKeyboardMarkup(back_orders()),
            parse_mode="Markdown",
        )

    # ── Clear Seen Orders ─────────────────────────────────────────
    elif data == "clear_seen_orders":
        s["seen_order_ids"].clear()
        s["paid_notified_ids"].clear()
        await query.edit_message_text(
            "✅ Seen orders cleared. The bot will re-notify on the next check.",
            reply_markup=InlineKeyboardMarkup(back_orders()),
        )

    # ── 🚀 Release Coin ───────────────────────────────────────────
    elif data.startswith("release_"):
        order_id = data[8:]
        await query.edit_message_text(
            f"⏳ Releasing coin for order `{order_id}`…",
            parse_mode="Markdown",
        )
        result = await asyncio.get_event_loop().run_in_executor(
            None, release_order, api_key, api_secret, order_id
        )
        rc = result.get("retCode", result.get("ret_code", -1))
        rm = result.get("retMsg",  result.get("ret_msg",  ""))
        if rc == 0:
            await query.edit_message_text(
                f"🚀 *Coin Released!*\n\nOrder `{order_id}` completed successfully.",
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(
                f"❌ *Release failed*\n`{rc}` — `{rm}`\n\n"
                f"Order ID: `{order_id}`",
                reply_markup=release_button(order_id),
                parse_mode="Markdown",
            )

    # ── Bot Status ────────────────────────────────────────────────
    elif data == "bot_status":
        lines = ["📡 *Bot Status — All Accounts*\n"]
        for num in sorted(BYBIT_ACCOUNTS.keys()):
            st   = get_state(num)
            su   = st["user_settings"]
            done, total, bar = setup_progress(num)
            r_st = "🟢 Running" if st["refresh_running"]       else "🔴 Stopped"
            o_st = "🔔 Active"  if st["order_monitor_running"] else "🔕 Off"
            cur  = str(st["current_price"]) if st["current_price"] else "—"
            lines.append(
                f"*Account {num}*{'  ← active' if num == active else ''}\n"
                f"  Setup: {bar} `{done}/{total}`\n"
                f"  Price Bot: {r_st} | Price: `{cur}`\n"
                f"  Order Monitor: {o_st}\n"
                f"  Ad ID: `{su.get('ad_id') or 'Not set'}`\n"
                f"  Mode: `{su.get('mode','fixed').upper()}` | Interval: `{su.get('interval',2)} min`\n"
            )
        await query.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(back_main()),
            parse_mode="Markdown",
        )

    # ── Reset confirm ─────────────────────────────────────────────
    elif data == "reset_confirm":
        await query.edit_message_text(
            f"⚠️ *Reset Account {active}?*\n\n"
            "This clears all settings for this account and stops all its running tasks.\n\n"
            "Are you sure?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, Reset", callback_data="reset_do")],
                [InlineKeyboardButton("❌ Cancel",     callback_data="main_menu")],
            ]),
            parse_mode="Markdown",
        )

    elif data == "reset_do":
        s["refresh_running"]       = False
        s["order_monitor_running"] = False
        for task_key in ("refresh_task", "order_monitor_task"):
            if s.get(task_key):
                s[task_key].cancel()
                s[task_key] = None
        accounts_state[active] = _fresh_account_state()
        await query.edit_message_text(
            f"✅ *Account {active} reset!* All settings cleared and tasks stopped.\n\n"
            "Tap /menu to start fresh.",
            parse_mode="Markdown",
        )

    # ── Switch Mode ───────────────────────────────────────────────
    elif data == "switch_mode":
        us["mode"] = "floating" if us.get("mode") == "fixed" else "fixed"
        note = " (takes effect next cycle)" if s["refresh_running"] else ""
        await query.edit_message_text(
            f"🔀 *Switched to {us['mode'].upper()}{note}*\n\n_{next_setup_hint(active)}_",
            reply_markup=InlineKeyboardMarkup(back_ads()),
            parse_mode="Markdown",
        )

    # ── Set Ad ID ─────────────────────────────────────────────────
    elif data == "set_ad_id":
        s["user_state"]["action"] = "ad_id"
        cur = us.get("ad_id", "") or "Not set"
        await query.edit_message_text(
            f"🆔 *Set Ad ID — Account {active}*\n\nCurrent: `{cur}`\n\n"
            "Send your Bybit Ad ID.\n💡 Use 📃 My Ads List to find it.\n\n"
            "Example: `2040156088201854976`",
            reply_markup=InlineKeyboardMarkup(back_ads()),
            parse_mode="Markdown",
        )

    # ── Set UID ───────────────────────────────────────────────────
    elif data == "set_uid":
        s["user_state"]["action"] = "bybit_uid"
        cur = us.get("bybit_uid", "") or "Not set"
        await query.edit_message_text(
            f"👤 *Set Bybit UID — Account {active}*\n\nCurrent: `{cur}`\n\n"
            "Bybit App → Profile → copy UID under your username.\n\n"
            "Example: `520097760`",
            reply_markup=InlineKeyboardMarkup(back_ads()),
            parse_mode="Markdown",
        )

    # ── My Ads ────────────────────────────────────────────────────
    elif data == "fetch_my_ads":
        await query.edit_message_text(f"⏳ Fetching ads for Account {active}…")
        result   = await asyncio.get_event_loop().run_in_executor(
            None, get_my_ads, api_key, api_secret
        )
        ret_code = result.get("retCode", result.get("ret_code", -1))
        if ret_code == 0:
            items     = result.get("result", {}).get("items", [])
            bybit_uid = us.get("bybit_uid", "")
            if not items:
                await query.edit_message_text(
                    "📃 No ads found.",
                    reply_markup=InlineKeyboardMarkup(back_ads()),
                )
                return
            lines = [f"📃 *Account {active} — Your P2P Ads:*\n"]
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
                reply_markup=InlineKeyboardMarkup(back_ads()),
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(
                f"❌ `{result.get('retMsg', result.get('ret_msg',''))}`",
                reply_markup=InlineKeyboardMarkup(back_ads()),
                parse_mode="Markdown",
            )

    # ── Fetch Ad Details ──────────────────────────────────────────
    elif data == "fetch_ad":
        if not us.get("ad_id"):
            await query.edit_message_text(
                "❌ Set your Ad ID first (🆔 Set Ad ID).",
                reply_markup=InlineKeyboardMarkup(back_ads()),
            )
            return
        await query.edit_message_text(f"⏳ Loading ad for Account {active}…")
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
            ad_stat      = {10: "🟢 Online", 20: "🔴 Offline", 30: "✅ Done"}.get(
                ad.get("status"), "?"
            )
            await query.edit_message_text(
                f"✅ *Account {active} — Ad Loaded!*\n\n"
                f"🆔 `{us['ad_id']}`\n"
                f"💱 `{token}/{currency}` | 💲 `{ad.get('price','')}`\n"
                f"Min: `{ad.get('minAmount','')}` | Max: `{ad.get('maxAmount','')}` | "
                f"Qty: `{ad.get('lastQuantity','')}`\n"
                f"Status: {ad_stat} | Max float: `{max_pct}%`\n\n"
                f"_{next_setup_hint(active)}_",
                reply_markup=InlineKeyboardMarkup(back_ads()),
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(
                f"❌ `{result.get('retMsg', result.get('ret_msg',''))}`",
                reply_markup=InlineKeyboardMarkup(back_ads()),
                parse_mode="Markdown",
            )

    # ── Set Increment ─────────────────────────────────────────────
    elif data == "set_increment":
        s["user_state"]["action"] = "increment"
        await query.edit_message_text(
            f"➕ *Set Increment — Account {active}*\n\n"
            f"Current: `+{us.get('increment','0.05')}` per cycle\n\n"
            "Send the amount to add each cycle.\nExamples: `0.05` | `1` | `0.5`",
            reply_markup=InlineKeyboardMarkup(back_ads()),
            parse_mode="Markdown",
        )

    # ── Set Float % ───────────────────────────────────────────────
    elif data == "set_float_pct":
        if not s["ad_data"]:
            await query.edit_message_text(
                "❌ Fetch Ad Details first.",
                reply_markup=InlineKeyboardMarkup(back_ads()),
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
            f"Formula: `BTC/USDT {'× NGN/USDT ref ' if currency=='NGN' else ''}× your% ÷ 100`\n\n"
            f"Send a value ≤ {max_pct}. Example: `105`",
            reply_markup=InlineKeyboardMarkup(back_ads()),
            parse_mode="Markdown",
        )

    # ── Set NGN Ref ───────────────────────────────────────────────
    elif data == "set_ngn_ref":
        s["user_state"]["action"] = "ngn_usdt_ref"
        cur = us.get("ngn_usdt_ref", "") or "Not set"
        await query.edit_message_text(
            f"💱 *NGN/USDT Reference — Account {active}*\n\nCurrent: `{cur}`\n\n"
            "Check Bybit P2P market for the current NGN/USDT rate.\nExample: `1580`",
            reply_markup=InlineKeyboardMarkup(back_ads()),
            parse_mode="Markdown",
        )

    # ── Set Interval ──────────────────────────────────────────────
    elif data == "set_interval":
        s["user_state"]["action"] = "interval"
        await query.edit_message_text(
            f"⏱ *Set Interval — Account {active}*\n\n"
            f"Current: every `{us.get('interval', 2)}` min\n\n"
            "Send minutes between each price update.\nExamples: `2` | `5` | `10`",
            reply_markup=InlineKeyboardMarkup(back_ads()),
            parse_mode="Markdown",
        )

    # ── Update Once Now ───────────────────────────────────────────
    elif data == "update_now":
        if not s["ad_data"] or not us.get("ad_id"):
            await query.edit_message_text(
                "❌ Load ad details first.",
                reply_markup=InlineKeyboardMarkup(back_ads()),
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
                    reply_markup=InlineKeyboardMarkup(back_ads()),
                    parse_mode="Markdown",
                )
                return

        ok, used_price = await _do_modify_with_retry(
            context.bot, chat_id, active,
            us["ad_id"], price, s["ad_data"],
            "Manual update", mode, s
        )
        if ok:
            if mode == "fixed":
                try:
                    s["current_price"] = Decimal(used_price)
                except Exception:
                    pass
            note = " _(auto-corrected to Bybit limit)_" if used_price != price else ""
            await query.edit_message_text(
                f"✅ *Account {active} Updated!*\n"
                f"Price: `{used_price}` ({mode.upper()}){note}\n\n"
                f"_{next_setup_hint(active)}_",
                reply_markup=InlineKeyboardMarkup(back_ads()),
                parse_mode="Markdown",
            )

    # ── Toggle Price Update ───────────────────────────────────────
    elif data == "toggle_refresh":
        if s["refresh_running"]:
            s["refresh_running"] = False
            if s.get("refresh_task"):
                s["refresh_task"].cancel()
                s["refresh_task"] = None
            s["current_price"] = Decimal("0")
            await query.edit_message_text(
                f"🔴 *Account {active} — Price update stopped.*",
                reply_markup=InlineKeyboardMarkup(back_ads()),
                parse_mode="Markdown",
            )
        else:
            if not s["ad_data"] or not us.get("ad_id"):
                await query.edit_message_text(
                    f"❌ Not ready:\n\n_{next_setup_hint(active)}_",
                    reply_markup=InlineKeyboardMarkup(back_ads()),
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
                reply_markup=InlineKeyboardMarkup(back_ads()),
                parse_mode="Markdown",
            )


# ═══════════════════════════════════════════════════════════════════
#  TEXT INPUT HANDLER
# ═══════════════════════════════════════════════════════════════════
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

    elif action == "auto_msg_text":
        s["auto_msg_text"] = text
        s["user_state"]["action"] = None
        count = s["auto_msg_count"]
        await reply(
            f"✅ Account {active} — Auto-message set!\n\n"
            f"Message: _{text}_\n"
            f"Will be sent *{count}* time(s) when a new order arrives."
        )

    elif action == "auto_msg_count":
        try:
            val = int(text)
            if val < 1 or val > 5:
                raise ValueError
            s["auto_msg_count"] = val
            s["user_state"]["action"] = None
            await reply(f"✅ Account {active} — Message will be sent `{val}` time(s) per order.")
        except Exception:
            await reply("❌ Send a number between 1 and 5.")


# ═══════════════════════════════════════════════════════════════════
#  BUILD BOT
# ═══════════════════════════════════════════════════════════════════
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
