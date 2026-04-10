import os
import asyncio
import logging
import threading
import requests as http_requests
from flask import Flask, request, jsonify
from telegram import Update, BotCommand

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

app      = Flask(__name__)
bot_app  = None
bot_loop = None


# ─── Health check ────────────────────────────────────────────────────────────
@app.route("/")
def home():
    return "✅ P2P Price Bot is running"


# ─── Webhook endpoint ────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    global bot_app, bot_loop
    if bot_app is None or bot_loop is None:
        return jsonify({"status": "error", "detail": "bot not ready"}), 500
    try:
        data   = request.get_json(force=True)
        update = Update.de_json(data, bot_app.bot)
        future = asyncio.run_coroutine_threadsafe(
            bot_app.process_update(update), bot_loop
        )
        future.result(timeout=30)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.exception(f"Webhook error: {e}")
        return jsonify({"status": "error", "detail": str(e)}), 500


# ─── Real egress IP checker — open in browser to confirm ─────────────────────
@app.route("/myip")
def myip():
    """
    Visit  https://YOUR-APP.onrender.com/myip  in a browser.
    The IP shown is what Bybit sees — whitelist exactly this on both API keys.
    """
    try:
        ip = http_requests.get("https://api.ipify.org", timeout=8).text.strip()
        return (
            f"Render outbound IP: {ip}\n\n"
            f"Whitelist this exact IP on both Bybit accounts:\n"
            f"  Bybit → API Management → Edit → IP Restriction → add {ip}"
        )
    except Exception as e:
        return f"Could not fetch IP: {e}", 500


# ─── Async bot setup ─────────────────────────────────────────────────────────
async def run_bot_setup(render_url: str):
    global bot_app
    from bot import start_bot

    webhook_url = f"{render_url}/webhook"
    logger.info(f"Setting webhook → {webhook_url}")

    bot = start_bot()
    await bot.initialize()
    await bot.bot.set_webhook(url=webhook_url)
    await bot.bot.set_my_commands([
        BotCommand("start",     "Start the bot"),
        BotCommand("menu",      "Open control panel"),
        BotCommand("pingbybit", "Test active account API connection"),
    ])
    bot_app = bot
    logger.info("✅ Bot ready — webhook active")


# ─── Persistent background loop ──────────────────────────────────────────────
def start_background_loop(loop: asyncio.AbstractEventLoop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


# ─── Entry point ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logger.info("🟢 App starting…")

    # Detect real public egress IP — log from multiple services for accuracy
    logger.info("=" * 60)
    logger.info("  Detecting public outbound IP (this is what Bybit sees)…")
    detected_ip = None
    for svc in [
        "https://api.ipify.org",
        "https://api4.my-ip.io/ip",
        "https://checkip.amazonaws.com",
    ]:
        try:
            ip = http_requests.get(svc, timeout=8).text.strip()
            logger.info(f"  🌍 {svc} → {ip}")
            if not detected_ip:
                detected_ip = ip
        except Exception as e:
            logger.info(f"  ⚠️  {svc} → failed ({e})")
    if detected_ip:
        logger.info(f"  ✅ Whitelist this on Bybit API Management: {detected_ip}")
    logger.info("  👉 Or visit  https://your-app.onrender.com/myip  to confirm live")
    logger.info("=" * 60)

    render_url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not render_url:
        logger.error("❌ RENDER_EXTERNAL_URL env var is not set")
        raise SystemExit(1)

    bot_loop = asyncio.new_event_loop()
    t = threading.Thread(target=start_background_loop, args=(bot_loop,), daemon=False)
    t.start()
    logger.info("✅ Persistent event loop started")

    future = asyncio.run_coroutine_threadsafe(run_bot_setup(render_url), bot_loop)
    try:
        future.result(timeout=30)
    except Exception as e:
        logger.exception(f"❌ Failed to start bot: {e}")
        raise SystemExit(1)

    port = int(os.environ.get("PORT", 10000))
    logger.info(f"🚀 Flask listening on port {port}")
    app.run(host="0.0.0.0", port=port, threaded=True)
