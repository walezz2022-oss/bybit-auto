import logging
import socket
from bot import start_bot
from config import TELEGRAM_TOKEN

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

print("🌐 Bot starting...")

# Render IP display
hostname = socket.gethostname()
ip = socket.gethostbyname(hostname)
print(f"🌍 Render IP: {ip}")

bot_app = start_bot()

if __name__ == "__main__":
    print("🤖 Starting polling safely...")

    # IMPORTANT FIX FOR PYTHON 3.13+
    import asyncio

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    bot_app.run_polling()
