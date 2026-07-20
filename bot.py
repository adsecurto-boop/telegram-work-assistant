"""
bot.py
------
Application entry point. Wires together config, database, handlers and
the scheduler, then runs the bot with long polling.

Run with:  python bot.py
"""

import logging

from telegram.ext import Application, CommandHandler, MessageHandler, filters

import config
import handlers
from database import Database
from scheduler import create_scheduler
from utils import setup_logging

logger = logging.getLogger(__name__)


def build_application() -> Application:
    config.validate_config()

    application = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .post_init(_on_startup)
        .build()
    )

    # Shared, single Database instance stored on bot_data so every
    # handler can reach it via context.application.bot_data["db"].
    application.bot_data["db"] = Database(config.DB_PATH)

    # -- command handlers (MVP set) --
    application.add_handler(CommandHandler("start", handlers.start))
    application.add_handler(CommandHandler("help", handlers.help_command))
    application.add_handler(CommandHandler("task", handlers.add_task))
    application.add_handler(CommandHandler("todo", handlers.todo))
    application.add_handler(CommandHandler("pending", handlers.pending))
    application.add_handler(CommandHandler("done", handlers.mark_done))
    application.add_handler(CommandHandler("delete", handlers.delete_task))
    application.add_handler(CommandHandler("begin", handlers.bos_report))
    application.add_handler(CommandHandler("prelunch", handlers.prelunch_report))
    application.add_handler(CommandHandler("eod", handlers.eod_report))

    # Catch-all for anything that looks like an unrecognised command.
    application.add_handler(MessageHandler(filters.COMMAND, handlers.unknown_command))

    return application


async def _on_startup(application: Application) -> None:
    # Register scheduled reminders (JobQueue). The returned object is the
    # application's job_queue; there's no explicit start required.
    jq = create_scheduler(application)
    application.bot_data["scheduler"] = jq
    logger.info("Bot startup complete.")


def main() -> None:
    setup_logging()
    logger.info("Starting Telegram Work Assistant Bot...")

    application = build_application()
    application.run_polling(allowed_updates=None)


if __name__ == "__main__":
    main()

