"""
scheduler.py
------------
Use python-telegram-bot's JobQueue to schedule three daily reminder
jobs at fixed times (09:00, 13:00, 18:00). Times and messages are kept
as simple constants so they can be changed easily.
"""

import logging
from datetime import time
from typing import List, Tuple

from telegram.ext import Application, ContextTypes

import config
from database import Database
from handlers import SETTING_CHAT_ID

logger = logging.getLogger(__name__)

# Reminder schedule: (hour, minute, message)
REMINDERS: List[Tuple[int, int, str]] = [
    (12, 0, "Please submit Beginning of Shift update."),
    (15, 45, "Please submit Pre Lunch update."),
    (20, 40, "Please submit End Of Day update."),
]


async def _send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    application: Application = context.application
    db: Database = application.bot_data["db"]
    chat_id = db.get_setting(SETTING_CHAT_ID)
    if not chat_id:
        logger.debug("No registered chat_id yet; skipping reminder.")
        return

    tasks = db.list_pending()
    if not tasks:
        logger.debug("No pending tasks; skipping reminder.")
        return

    lines = [f"⏰ Reminder: {len(tasks)} pending task(s)"]
    lines += [f"• {t.title}" for t in tasks]
    text = "\n".join(lines)

    try:
        await application.bot.send_message(chat_id=chat_id, text=text)
        logger.info("Sent pending-task reminder to chat_id=%s", chat_id)
    except Exception:
        logger.exception("Failed to send reminder to chat_id=%s", chat_id)


def create_scheduler(application: Application):
    """Register daily jobs on the application's JobQueue and return it."""
    jq = application.job_queue

    if not config.REMINDERS_ENABLED:
        logger.info("Reminders disabled via config (REMINDERS_ENABLED=false).")
        return jq

    # schedule the three daily reminders
    for hour, minute, message in REMINDERS:
        # run_daily expects a time object (local timezone handling is left
        # to the environment; this is intentionally simple for the MVP).
        jq.run_daily(_send_reminder, time(hour, minute), days=(0, 1, 2, 3, 4, 5, 6))
        logger.info("Scheduled daily reminder at %02d:%02d", hour, minute)

    return jq