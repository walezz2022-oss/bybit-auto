import os
import asyncio
import threading
from flask import Flask, request
from telegram import Update

from bot import start_bot

app = Flask(__name__)

bot_app = None
loop = None


@app.route("/")
def home():
    return "Bot is running"


@app.route("/webhook", methods=["POST"])
def webhook():
    global bot_app, loop

    data = request.get_json()
    update = Update.de_json(data, bot_app.bot)

    asyncio.run_coroutine_threadsafe(
        bot_app.process_update(update),
        loop
    )

    return "ok"


def run_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


if __name__ == "__main__":
    url = os.getenv("RENDER_EXTERNAL_URL")
    port = int(os.getenv("PORT", 10000))

    loop = asyncio.new_event_loop()
    threading.Thread(target=run_loop, args=(loop,), daemon=True).start()

    bot_app = start_bot()

    asyncio.run_coroutine_threadsafe(bot_app.initialize(), loop).result()

    asyncio.run_coroutine_threadsafe(
        bot_app.bot.set_webhook(f"{url}/webhook"),
        loop
    ).result()

    app.run(host="0.0.0.0", port=port)
