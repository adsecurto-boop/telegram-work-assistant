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

CALLBACK_PATTERN = (
    r'^wd:(copy|edit|saved|shared|cancelled):\d+:\d+$|'
    r'^(final|ai|close|organize|apply|dismiss):\d+$|^drop:\d+:\d+$|'
    r'^task:[a-z_]+:\d+(:[a-z0-9_]+)?$|^src:(accept|ignore):\d+$|'
    r'^case:[a-z_]+:\d+$|^follow:(done|snooze):\d+$|'
    r'^audit:undo:\d+$|^nl:choose:\d+:[a-z_]+$|'
    r'^prop:(accept|cancel):prop_[a-f0-9]+$|^prop:choose:prop_[a-f0-9]+:\d+$|'
    r'^plan:(confirm|cancel):\d+$|^checkpoint:update:\d+:[a-z_]+(:[a-z_]+)?$|'
    r'^corr:(confirm|cancel|pick_task|pick_client):prop_[a-f0-9]+(:[a-zA-Z0-9_ -]+)?$|'
    r'^late:(shift|cancel):prop_[a-f0-9]+(:\d+)?$'
)

async def gate(update,context):
    if not handlers.authorized(update):
        raise ApplicationHandlerStop
    if not await asyncio.to_thread(context.application.bot_data['db'].claim_update,update.update_id):
        if update.callback_query:
            await update.callback_query.answer('Already handled.')
        raise ApplicationHandlerStop

async def startup(application):
    create_scheduler(application)
    from work_messages import remind_work_drafts
    application.job_queue.run_repeating(remind_work_drafts, interval=60, first=10, name='work-draft-followups')
    if config.DASHBOARD_ENABLED:
        try:
            from dashboard import DashboardService
            application.bot_data['dashboard'] = DashboardService(
                application.bot_data['db'], config.DASHBOARD_HOST, config.DASHBOARD_PORT).start()
        except OSError as exc:
            logging.getLogger(__name__).warning('Dashboard unavailable: %s', type(exc).__name__)
    await application.bot.set_my_commands([
        BotCommand('shift','Start a flexible shift'), BotCommand('task','Plan a rich task'),
        BotCommand('startday','Plan today’s work'), BotCommand('checkpoint','Lunch progress report'),
        BotCommand('timeline','Current shift timeline'), BotCommand('resume','Prioritized open tasks'),
        BotCommand('tomorrow','Review unfinished work'), BotCommand('carrytask','Reschedule a task to tomorrow'),
        BotCommand('draft','Prepare or open a work message'), BotCommand('drafts','List work drafts'),
        BotCommand('draftedit','Revise a work draft'), BotCommand('drafthistory','Draft revision history'),
        BotCommand('draftfollowup','Schedule a draft follow-up'), BotCommand('workhandover','Work message handover'),
        BotCommand('todo','Show open tasks'), BotCommand('today','Show this shift plans'),
        BotCommand('undo','Undo last mutation safely'), BotCommand('understand','Preview NL intent'),
        BotCommand('unknowns','Review low-confidence messages'), BotCommand('correct','Save an NLP correction'),
        BotCommand('nlstats','Natural-language recognition stats'),
        BotCommand('casesummary','Factual case summary'), BotCommand('nextaction','Recommended action'),
        BotCommand('draftclient','Draft client reply'), BotCommand('draftescalation','Draft technical escalation'),
        BotCommand('analyzetest','Analyze testing findings'), BotCommand('shiftcalendar','7-day rotational shift'),
        BotCommand('shifttemplate','List shift templates'), BotCommand('clusters','Historical cluster suggestions'),
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
    application.add_handler(CallbackQueryHandler(handlers.handle, pattern=CALLBACK_PATTERN))
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
