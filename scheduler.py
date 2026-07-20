"""
scheduler.py
------------
Sets up an APScheduler AsyncIOScheduler that periodically pushes a
"pending tasks" reminder to the chat that ran /start (stored in the
settings table). Runs inside the same asyncio loop as the bot, so no
extra threads/processes are needed.
"""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from telegram.ext import Application

import config
from database import Database
from handlers import SETTING_CHAT_ID

logger = logging.getLogger(__name__)


async def send_pending_reminder(application: Application) -> None:
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


def create_scheduler(application: Application) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=config.TIMEZONE)

    if config.REMINDERS_ENABLED:
        scheduler.add_job(
            send_pending_reminder,
            trigger=IntervalTrigger(hours=config.REMINDER_INTERVAL_HOURS),
            args=[application],
            id="pending_task_reminder",
            replace_existing=True,
        )
        logger.info(
            "Scheduled pending-task reminders every %s hour(s)",
            config.REMINDER_INTERVAL_HOURS,
        )
    else:
        logger.info("Reminders disabled via config (REMINDERS_ENABLED=false).")

    return scheduler