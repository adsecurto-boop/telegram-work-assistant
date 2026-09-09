"""Windows-friendly long-polling entry point."""
import asyncio
import logging
from datetime import datetime
from telegram.ext import Application, TypeHandler, MessageHandler, CallbackQueryHandler, ApplicationHandlerStop, filters
from telegram import BotCommand, Update
import config
import handlers
from database import Database
from maintenance import create_rotating_backup
from scheduler import create_scheduler
from utils import setup_logging
from runtime import instance_lock

async def gate(update,context):
    if not handlers.authorized(update):
        raise ApplicationHandlerStop
    if not await asyncio.to_thread(context.application.bot_data['db'].claim_update,update.update_id):
        if update.callback_query:
            await update.callback_query.answer('Already handled.')
        raise ApplicationHandlerStop

async def startup(application):
    create_scheduler(application)
    if config.DASHBOARD_ENABLED:
        try:
            from dashboard import DashboardService
            application.bot_data['dashboard'] = DashboardService(
                application.bot_data['db'], config.DASHBOARD_HOST, config.DASHBOARD_PORT).start()
        except OSError as exc:
            logging.getLogger(__name__).warning('Dashboard unavailable: %s', type(exc).__name__)
    await application.bot.set_my_commands([
        BotCommand('shift','Start a flexible shift'), BotCommand('task','Plan a rich task'),
        BotCommand('todo','Show open tasks'), BotCommand('today','Show this shift plans'),
        BotCommand('support','Log a client interaction'), BotCommand('testing','Log testing'),
        BotCommand('learning','Log learning or KT'), BotCommand('tod','Create TOD report'),
        BotCommand('pl','Create pre-lunch report'), BotCommand('eod','Create EOD report'),
        BotCommand('summary','Preview current shift'), BotCommand('week','Weekly summary'),
        BotCommand('cases','Show active work cases'), BotCommand('inbox','Review imported work'),
        BotCommand('followups','Show pending follow-ups'), BotCommand('dashboard','Open local dashboard'),
        BotCommand('search','Search work history'), BotCommand('health','Check bot health'),
        BotCommand('help','Show all commands')])
    logging.getLogger(__name__).info('Bot ready; owner-only private chat enabled.')

async def shutdown(application):
    service = application.bot_data.get('dashboard')
    if service:
        await asyncio.to_thread(service.stop)


def build_application():
    config.validate_config()
    database=Database(config.DB_PATH)
    database.migrate_json(config.LEGACY_PATH)
    create_rotating_backup(database, config.BASE_DIR/'storage/backups',
                           config.BACKUP_RETENTION, 'startup')
    application=(Application.builder().token(config.BOT_TOKEN).post_init(startup)
                 .post_shutdown(shutdown).build())
    application.bot_data['db']=database
    application.add_handler(TypeHandler(Update,gate),group=-1)
    application.add_handler(CallbackQueryHandler(
        handlers.handle,
        pattern=(r'^(final|ai|close|organize|apply|dismiss):\d+$|^drop:\d+:\d+$|'
                 r'^task:[a-z_]+:\d+$|^src:(accept|ignore):\d+$|'
                 r'^case:[a-z_]+:\d+$|^follow:(done|snooze):\d+$')))
    supported = filters.TEXT | filters.VOICE | filters.PHOTO | filters.Document.ALL | filters.VIDEO
    application.add_handler(MessageHandler(supported & ~filters.UpdateType.EDITED_MESSAGE,handlers.handle))
    application.add_error_handler(handlers.error_handler)
    return application

def main():
    setup_logging()
    with instance_lock(config.DB_PATH):
        build_application().run_polling(allowed_updates=['message','callback_query'])

if __name__=='__main__':
    main()
