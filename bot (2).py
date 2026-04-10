import asyncio
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

from config import TELEGRAM_TOKEN, ADMIN_IDS, BYBIT_ACCOUNTS
from bybit import get_market_price, modify_ad, calculate_price

sessions = {}


def is_admin(uid):
    return uid in ADMIN_IDS


def account_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(name, callback_data=f"acc_{name}")]
        for name in BYBIT_ACCOUNTS
    ])


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("Select Account:", reply_markup=account_keyboard())


async def button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    if q.data.startswith("acc_"):
        acc = q.data.replace("acc_", "")
        sessions[uid] = {
            "account": acc,
            "running": False,
            "ad_id": None,
            "float_pct": 100,
            "interval": 60
        }
        await q.edit_message_text(
            f"{acc} selected\n\nSend:\nAdID | Float% | Time(sec)\nExample:\n123456 | 105 | 60"
        )

    elif q.data == "start":
        sessions[uid]["running"] = True
        asyncio.create_task(loop(ctx.bot, q.message.chat_id, uid))
        await q.edit_message_text("✅ Started")

    elif q.data == "stop":
        sessions[uid]["running"] = False
        await q.edit_message_text("⛔ Stopped")

    elif q.data == "switch":
        await q.edit_message_text("Switch account:", reply_markup=account_keyboard())


async def loop(bot, chat_id, uid):
    while sessions.get(uid, {}).get("running"):
        s = sessions[uid]
        acc = BYBIT_ACCOUNTS[s["account"]]

        try:
            market_price = get_market_price()
            new_price = calculate_price(market_price, s["float_pct"])

            modify_ad(acc["key"], acc["secret"], s["ad_id"], new_price)

            await bot.send_message(
                chat_id,
                f"Updated\nMarket: {market_price}\nFloat: {s['float_pct']}%\nNew: {new_price}"
            )

        except Exception as e:
            await bot.send_message(chat_id, f"Error: {e}")

        await asyncio.sleep(s["interval"])


async def text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in sessions:
        return

    try:
        ad_id, pct, interval = update.message.text.split("|")

        sessions[uid]["ad_id"] = ad_id.strip()
        sessions[uid]["float_pct"] = float(pct.strip())
        sessions[uid]["interval"] = int(interval.strip())

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Start", callback_data="start")],
            [InlineKeyboardButton("Stop", callback_data="stop")],
            [InlineKeyboardButton("Switch", callback_data="switch")]
        ])

        await update.message.reply_text("✅ Config saved", reply_markup=kb)

    except:
        await update.message.reply_text("Format:\nAdID | Float% | Time(sec)")


def start_bot():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT, text))

    return app
