# bot.py

import asyncio
import logging
from decimal import Decimal
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

from config import TELEGRAM_TOKEN, ADMIN_IDS, BYBIT_ACCOUNTS
from bybit import (
    set_account,
    get_ad_details,
    modify_ad,
    get_btc_usdt_price,
    get_max_float_pct
)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────
# STATE
# ─────────────────────────────────────────
user_settings = {
    "ad_id": "",
    "mode": "fixed",
    "increment": "0.05",
    "float_pct": "",
    "ngn_ref": "",
    "interval": 2,
    "account": None
}

ad_data = {}
refresh_task = None
refresh_running = False

# ─────────────────────────────────────────
# ADMIN CHECK
# ─────────────────────────────────────────
def is_admin(uid):
    return uid in ADMIN_IDS


# ─────────────────────────────────────────
# UI
# ─────────────────────────────────────────
def account_keyboard():
    buttons = []
    for acc in BYBIT_ACCOUNTS.keys():
        buttons.append([InlineKeyboardButton(f"Account {acc}", callback_data=f"acc_{acc}")])
    return InlineKeyboardMarkup(buttons)


def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Setup Ad", callback_data="setup")],
        [InlineKeyboardButton("🟢 Start", callback_data="start")],
        [InlineKeyboardButton("🔴 Stop", callback_data="stop")],
        [InlineKeyboardButton("🔁 Switch Account", callback_data="switch")]
    ])


# ─────────────────────────────────────────
# FLOAT CALC
# ─────────────────────────────────────────
def calc_float(ad, pct, ref):
    btc = get_btc_usdt_price()
    if btc == 0:
        return None

    if ad["currencyId"].upper() == "NGN":
        return str(Decimal(btc * ref * pct / 100).quantize(Decimal("0.01")))
    else:
        return str(Decimal(btc * pct / 100).quantize(Decimal("0.01")))


# ─────────────────────────────────────────
# LOOP
# ─────────────────────────────────────────
async def price_loop(bot, chat_id):
    global refresh_running

    while refresh_running:
        mode = user_settings["mode"]

        if mode == "fixed":
            new_price = str(Decimal(ad_data["price"]) + Decimal(user_settings["increment"]))
        else:
            new_price = calc_float(
                ad_data,
                float(user_settings["float_pct"]),
                float(user_settings["ngn_ref"] or 0)
            )

        res = modify_ad(user_settings["ad_id"], new_price, ad_data)

        await bot.send_message(chat_id, f"Updated → {new_price}")

        await asyncio.sleep(user_settings["interval"] * 60)


# ─────────────────────────────────────────
# COMMANDS
# ─────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    await update.message.reply_text(
        "Select account:",
        reply_markup=account_keyboard()
    )


# ─────────────────────────────────────────
# BUTTON HANDLER
# ─────────────────────────────────────────
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global refresh_running, refresh_task

    q = update.callback_query
    await q.answer()

    data = q.data

    # ACCOUNT SELECT
    if data.startswith("acc_"):
        acc = data.split("_")[1]
        set_account(acc)
        user_settings["account"] = acc

        await q.edit_message_text(
            f"✅ Using Account {acc}",
            reply_markup=main_menu()
        )

    elif data == "switch":
        await q.edit_message_text("Select account:", reply_markup=account_keyboard())

    elif data == "setup":
        await q.edit_message_text("Send Ad ID")

        context.user_data["await"] = "ad"

    elif data == "start":
        if not ad_data:
            await q.edit_message_text("❌ Load ad first")
            return

        refresh_running = True
        refresh_task = asyncio.create_task(price_loop(context.bot, q.message.chat_id))

        await q.edit_message_text("🟢 Started", reply_markup=main_menu())

    elif data == "stop":
        refresh_running = False
        if refresh_task:
            refresh_task.cancel()

        await q.edit_message_text("🔴 Stopped", reply_markup=main_menu())


# ─────────────────────────────────────────
# TEXT INPUT
# ─────────────────────────────────────────
async def text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    if context.user_data.get("await") == "ad":
        user_settings["ad_id"] = update.message.text
        res = get_ad_details(user_settings["ad_id"])

        global ad_data
        ad_data = res.get("result", {})

        await update.message.reply_text("✅ Ad Loaded")
        context.user_data["await"] = None


# ─────────────────────────────────────────
# START BOT
# ─────────────────────────────────────────
def start_bot():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text))

    return app
