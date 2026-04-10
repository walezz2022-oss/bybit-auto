import logging
from flask import Flask
from bot import start_bot

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running 🚀"

if __name__ == "__main__":
    bot_app = start_bot()

    logger.info("🌐 Bot starting...")
    logger.info("🌍 Render URL will appear here when deployed")

    bot_app.run_polling()
