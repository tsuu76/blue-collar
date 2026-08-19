"""
Optional Telegram bot notifications (spec section 22).

The Telegram Bot API itself is free with no paid tier — this is not a
cloud AI call, just a plain HTTPS message send using the user's OWN bot
token (created via @BotFather, never a shared/managed credential this
project provides). Strictly opt-in: NOTIFY_TELEGRAM defaults to false in
.env.example, and this function no-ops if disabled or unconfigured rather
than erroring, so a missing/blank token never breaks the pipeline.
"""
from __future__ import annotations

import logging

import requests

from src.config import settings

logger = logging.getLogger("job_hunter.notifications.telegram")

TELEGRAM_API_BASE = "https://api.telegram.org"


def send_telegram_notification(message: str) -> bool:
    """Returns True if the message was sent. Never raises."""
    if not settings.notify_telegram:
        return False
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.warning("NOTIFY_TELEGRAM is true but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are not set — skipping.")
        return False

    url = f"{TELEGRAM_API_BASE}/bot{settings.telegram_bot_token}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": settings.telegram_chat_id, "text": message}, timeout=10)
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.warning("Telegram notification failed: %s", exc)
        return False
