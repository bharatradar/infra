import os
import logging

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_ALERT_CHAT_ID = (
    os.environ.get("TELEGRAM_ALERT_CHAT_ID")
    or os.environ.get("TELEGRAM_CHAT_ID", "")
)


async def send_telegram_message(session, text):
    if not TELEGRAM_TOKEN or not TELEGRAM_ALERT_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_ALERT_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        async with session.post(url, json=payload, timeout=15) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.warning(f"Telegram API error: HTTP {resp.status}: {body[:200]}")
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")
