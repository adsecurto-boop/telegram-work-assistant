"""One periodic scan of persisted shift times; no reminders on days off."""
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import config
from maintenance import create_rotating_backup, prune_evidence_files

async def tick(context):
    db = context.application.bot_data['db']
    shift = await asyncio.to_thread(db.active_shift)
    if not shift or not config.REMINDERS_ENABLED:
        return
    current = datetime.now(timezone.utc)
    due = []
    for kind,stamp in [('tod',shift['start']),('pl',shift['lunch']),('eod',shift['end'])]:
        if stamp and datetime.fromisoformat(stamp) <= current and not await asyncio.to_thread(db.delivered,shift['id'],kind):
            due.append(kind)
    followups = await asyncio.to_thread(
        db.due_followups, datetime.now(ZoneInfo(config.TIMEZONE)).isoformat())
    review_cases = []
    end = datetime.fromisoformat(shift['end'])
    if current >= end.astimezone(timezone.utc) - timedelta(minutes=30) and not await asyncio.to_thread(
            db.delivered, shift['id'], 'case_review'):
        review_cases = [item for item in await asyncio.to_thread(db.cases_for_shift, shift['id'])
                        if item['status'] not in ('closed','client_updated')]
    if not due and not followups and not review_cases:
        return
    lines = []
    if due:
        lines.append('Shift check-in: ' + ', '.join('/' + kind for kind in due) +
                     '. Generate your updates when ready; /schedule adjusts times.')
    if followups:
        lines.append('Follow-ups due: ' + ', '.join(
            f"#{item['id']} CASE-{item['case_id']} {item['title'][:70]}" for item in followups))
    if review_cases:
        lines.append('Before shift end, review open cases: ' + ', '.join(
            f"CASE-{item['id']} [{item['status']}]" for item in review_cases[:10]))
    await context.bot.send_message(chat_id=config.OWNER_ID, text='\n'.join(lines))
    for kind in due:
        await asyncio.to_thread(db.record_delivery,shift['id'],kind)
    if followups:
        await asyncio.to_thread(db.remind_followups, [item['id'] for item in followups])
    if review_cases:
        await asyncio.to_thread(db.record_delivery, shift['id'], 'case_review')

async def maintenance(context):
    db = context.application.bot_data['db']
    await asyncio.to_thread(create_rotating_backup, db,
        config.BASE_DIR/'storage'/'backups', config.BACKUP_RETENTION)
    await asyncio.to_thread(prune_evidence_files, db,
        config.BASE_DIR/'storage'/'evidence', config.MEDIA_RETENTION_DAYS)


async def sync_connectors(context):
    from connectors import configured_connector, sync_connector
    db = context.application.bot_data['db']
    for name in config.ENABLED_CONNECTORS:
        try:
            connector = configured_connector(name)
            await asyncio.to_thread(sync_connector, db, connector)
        except Exception as exc:
            logging.getLogger(__name__).warning('Connector %s sync failed: %s',
                                                name, type(exc).__name__)

def create_scheduler(application):
    if application.job_queue is None:
        raise RuntimeError('Install requirements.txt, including the job-queue extra.')
    if config.REMINDERS_ENABLED:
        application.job_queue.run_repeating(tick,interval=60,first=3,name='shift-reminders')
    application.job_queue.run_repeating(
        maintenance, interval=config.BACKUP_INTERVAL_HOURS*3600,
        first=config.BACKUP_INTERVAL_HOURS*3600, name='database-backups')
    if config.ENABLED_CONNECTORS:
        application.job_queue.run_repeating(
            sync_connectors, interval=config.CONNECTOR_SYNC_MINUTES*60,
            first=30, name='work-connectors')
    return application.job_queue
