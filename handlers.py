"""Owner-only Telegram workflow for tasks, structured work logs, and reports."""
import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

import config
import reports
from ai import ORGANIZE_PROMPT_VERSION, REPORT_PROMPT_VERSION
from database import SCHEMA_VERSION
from domain import (PRIORITIES, case_line, parse_case, parse_due, parse_learning,
                    parse_support, parse_task, parse_test_session, parse_testing,
                    parse_when, task_is_open, task_line)
from models import TaskStatus
from shifts import clock_on_shift, new_shift, validate_schedule

MENU = ReplyKeyboardMarkup(
    [['Start Shift', 'My Tasks'], ['TOD', 'Pre-lunch', 'EOD'], ['End Shift', 'Help']],
    resize_keyboard=True)

HELP = '''Work Assistant
/shift [start] [end] — start now or use 08:00 17:00, 10:00 19:00, 12:00 21:00
/schedule END [LUNCH|none], /status, /default START END, /preset
/task title | priority | due | project | client | ticket | next action | tags
/todo, /today, /overdue, /taskdetails ID
/done ID | completion note, /progress ID, /reopen ID, /block ID reason
/edittask ID | field | value, /delete ID
/support client | channel | outcome | action | product | category | follow-up | ticket | query count | issue key
/testing scenario | result | product | environment | build | defects | ticket | retest
/case title | client | channel | status | participation | product | platform | ticket | priority | next action | waiting on | follow-up
/cases, /caseinfo ID, /caseevent ID | type | detail | outcome, /casestatus ID STATUS | note
/clientupdated ID | note, /mergecases SOURCE TARGET
/testsession case ID | scenario | result | environment | build | expected | actual | defects | retest | developer notified | preconditions | steps
/tests [CASE_ID], /inbox, /acceptmsg MESSAGE_ID [CASE_ID], /ignoremsg MESSAGE_ID
/followup CASE_ID | due | waiting on | note, /followups, /followupdone ID, /snooze ID MINUTES
/sync freshdesk|freshchat, /dashboard, /imports, /rollbackimport ID
/learning topic | type | product | takeaway | follow-up
/note text, /activity, /editlog ID text, /undo ID
/organize NOTE_ID — preview AI categorization
/proposal ID, /edititem PROPOSAL ITEM | field | value, /dropitem PROPOSAL ITEM
/accept PROPOSAL, /dismiss PROPOSAL
/tod [style], /pl [style], /eod [style] — styles: short, standard, detailed
/reportstyle STYLE, /privacy clients on|off, /section REPORT_KIND SECTION
/ai REPORT_ID [style], /report ID, /editreport ID replacement, /history
/summary, /week, /search text, /client name, /findtesting text
/export csv|markdown, /backup, /health, /endshift

Plain and forwarded messages are saved as notes first. Voice notes are transcribed;
photos, videos, and documents become local evidence. AI suggestions and imported work
require acceptance and never change an existing task status. Use ? or - for an unknown optional field.'''


def db(context):
    return context.application.bot_data['db']


async def reply(update, text, markup=None):
    chunks, chunk, units = [], '', 0
    for character in text:
        size = 2 if ord(character) > 0xFFFF else 1
        if units + size > 3500:
            chunks.append(chunk)
            chunk, units = '', 0
        chunk += character
        units += size
    chunks.append(chunk)
    for index, part in enumerate(chunks):
        await update.effective_message.reply_text(
            part, reply_markup=markup if index == len(chunks) - 1 else None)


async def active(context):
    shift = await asyncio.to_thread(db(context).active_shift)
    if not shift:
        raise ValueError('Start a shift with /shift first.')
    return shift


def task_keyboard(task):
    actions = []
    if task.status == TaskStatus.PENDING:
        actions.extend((('Start', 'progress'), ('Complete', 'done')))
    elif task.status == TaskStatus.IN_PROGRESS:
        actions.extend((('Complete', 'done'), ('Block', 'block')))
    elif task.status in (TaskStatus.BLOCKED, TaskStatus.COMPLETED):
        actions.append(('Reopen', 'reopen'))
    if task.status != TaskStatus.CANCELLED:
        actions.extend((('Edit', 'edit'), ('Archive', 'delete')))
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(label, callback_data=f'task:{action}:{task.id}')
        for label, action in actions
    ]])


async def show_tasks(update, context, tasks):
    tasks = [task for task in tasks if task_is_open(task)]
    if not tasks:
        await reply(update, 'No matching open tasks.')
        return
    for task in tasks:
        await reply(update, task_line(task, True), task_keyboard(task))


def case_keyboard(item):
    rows = []
    if item['status'] not in ('resolved','client_updated','closed'):
        rows.append([
            InlineKeyboardButton('Investigating', callback_data=f'case:investigating:{item["id"]}'),
            InlineKeyboardButton('Waiting internal', callback_data=f'case:waiting_internal:{item["id"]}')])
        rows.append([
            InlineKeyboardButton('Testing', callback_data=f'case:testing:{item["id"]}'),
            InlineKeyboardButton('Resolved', callback_data=f'case:resolved:{item["id"]}')])
    elif item['status'] == 'resolved' and not item.get('client_updated'):
        rows.append([InlineKeyboardButton('Client updated', callback_data=f'case:client_updated:{item["id"]}')])
    if item['status'] != 'closed':
        rows.append([InlineKeyboardButton('Close', callback_data=f'case:closed:{item["id"]}')])
    return InlineKeyboardMarkup(rows) if rows else None


def source_keyboard(item):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton('Accept as case', callback_data=f'src:accept:{item["id"]}'),
        InlineKeyboardButton('Ignore', callback_data=f'src:ignore:{item["id"]}')]])


def followup_keyboard(item):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton('Complete', callback_data=f'follow:done:{item["id"]}'),
        InlineKeyboardButton('Snooze 1h', callback_data=f'follow:snooze:{item["id"]}')]])


async def capture_voice(update, context):
    from ai import writer
    from media_capture import save_voice_data, voice_bytes
    from telegram_import import redact
    shift = await active(context)
    await reserve_ai(context)
    data, mime_type, telegram_file_id = await voice_bytes(update.effective_message)
    engine = writer(config.AI_PROVIDER, config.AI_KEY, config.AI_MODEL, config.AI_FALLBACK_MODEL)
    await reply(update, 'Transcribing voice update…')
    try:
        transcript = await engine.transcribe(data, mime_type)
    except Exception as exc:
        await asyncio.to_thread(db(context).record_ai_event, 'transcribe', config.AI_PROVIDER,
                                config.AI_MODEL, 'transcribe-v1', 'failed', type(exc).__name__)
        await reply(update, 'Voice transcription failed. Send the update as text; no work record was created.')
        return
    used_model = getattr(engine, 'last_model', None) or config.AI_MODEL
    await asyncio.to_thread(db(context).record_ai_event, 'transcribe', config.AI_PROVIDER,
                            used_model, 'transcribe-v1', 'success')
    source_key = hashlib.sha256(f'telegram_voice|{update.update_id}'.encode()).hexdigest()
    source, _ = await asyncio.to_thread(db(context).add_source_message,
        source_type='telegram_voice', source_key=source_key, chat_name='private bot',
        external_message_id=str(update.update_id), occurred_at=datetime.now(timezone.utc).isoformat(),
        author_name='owner', author_is_owner=True, text=transcript, redacted_text=redact(transcript),
        message_kind='voice', media=[telegram_file_id], classification='note', confidence=.9,
        review_status='accepted', shift_id=shift['id'])
    activity_id = await asyncio.to_thread(db(context).add_activity, shift['id'], 'note', transcript,
                                          None, None, None, source['id'])
    voice_path = voice_hash = None
    if not config.DELETE_VOICE_AFTER_TRANSCRIPTION:
        voice_path, voice_hash = await asyncio.to_thread(
            save_voice_data, data, config.BASE_DIR / 'storage' / 'evidence', mime_type)
    await asyncio.to_thread(db(context).add_evidence, 'voice', shift['id'], None, None,
                            voice_path, telegram_file_id, 'Transcribed voice update',
                            voice_hash, mime_type)
    await reply(update, f'Transcript saved as note #{activity_id}:\n\n{transcript}',
        InlineKeyboardMarkup([[InlineKeyboardButton(
            'Organize with AI', callback_data=f'organize:{activity_id}')]]))


async def capture_evidence(update, context):
    from media_capture import save_evidence
    shift = await active(context)
    caption = (update.effective_message.caption or '').strip()
    case_id = None
    test_session_id = None
    if caption.startswith('/attach'):
        head = caption.split('|', 1)[0].split()
        if len(head) >= 2:
            case_id = int(head[1])
        if len(head) >= 3:
            test_session_id = int(head[2])
    if case_id and not await asyncio.to_thread(db(context).case, case_id):
        raise ValueError('Case not found.')
    saved = await save_evidence(update.effective_message, config.BASE_DIR / 'storage' / 'evidence')
    evidence_id = await asyncio.to_thread(db(context).add_evidence,
        saved['kind'], shift['id'], case_id, test_session_id, saved['path'],
        saved['telegram_file_id'], saved['caption'], saved['sha256'], saved['mime_type'])
    if case_id:
        await asyncio.to_thread(db(context).add_case_event, case_id, 'evidence',
                                caption.split('|', 1)[-1].strip() or f'{saved["kind"]} evidence added',
                                shift['id'])
    await reply(update, f'Evidence #{evidence_id} saved locally' +
                (f' for CASE-{case_id}.' if case_id else '. Use /attach CASE_ID as the media caption next time.'))


async def save_plain_message(update, context, text):
    shift = await active(context)
    if getattr(update.effective_message, 'forward_origin', None):
        from telegram_import import classify, extract_metadata, redact
        category, confidence = classify(text)
        key = hashlib.sha256(f'telegram_forward|{update.update_id}'.encode()).hexdigest()
        source, _ = await asyncio.to_thread(db(context).add_source_message,
            source_type='telegram_forward', source_key=key, chat_name='forwarded to private bot',
            external_message_id=str(update.update_id), occurred_at=datetime.now(timezone.utc).isoformat(),
            author_name='owner', author_is_owner=True, text=text, redacted_text=redact(text),
            message_kind='forwarded_text', media=[], classification=category, confidence=confidence,
            review_status='pending', shift_id=shift['id'], metadata=extract_metadata(text))
        await reply(update, f'Forwarded message saved to review inbox #{source["id"]}.',
                    source_keyboard(source))
        return
    activity_id = await asyncio.to_thread(db(context).add_activity, shift['id'], 'note', text)
    await reply(update, f'Saved note #{activity_id}. Review it or organize it with AI.',
        InlineKeyboardMarkup([[InlineKeyboardButton(
            'Organize with AI', callback_data=f'organize:{activity_id}')]]))


async def report_preferences(context, requested=None):
    style = requested or await asyncio.to_thread(db(context).get_setting, 'report_style') or 'standard'
    if style not in ('short', 'standard', 'detailed'):
        raise ValueError('Report style must be short, standard, or detailed.')
    mask = (await asyncio.to_thread(db(context).get_setting, 'mask_client_names')) == 'true'
    return style, mask


async def make_report(update, context, kind, requested_style=None):
    shift = await active(context)
    style, mask = await report_preferences(context, requested_style)
    activities = await asyncio.to_thread(db(context).activities, shift['id'])
    tasks = await asyncio.to_thread(db(context).list_tasks)
    cases = await asyncio.to_thread(db(context).cases_for_shift, shift['id'])
    sessions = await asyncio.to_thread(db(context).test_sessions, shift['id'])
    text = reports.generate_report(kind, shift, activities, tasks, style, mask, cases, sessions)
    report_id = await asyncio.to_thread(db(context).save_report, shift['id'], kind, text, style)
    await asyncio.to_thread(db(context).record_delivery, shift['id'], kind)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton('Finalize', callback_data=f'final:{report_id}'),
        InlineKeyboardButton('AI draft', callback_data=f'ai:{report_id}')]])
    await reply(update, f'Draft #{report_id} ({style})\n\n{text}', keyboard)
    return report_id


async def reserve_ai(context):
    day = datetime.now(ZoneInfo(config.TIMEZONE)).date().isoformat()
    if not await asyncio.to_thread(db(context).reserve_ai, day, config.AI_DAILY_LIMIT):
        raise ValueError('Daily AI operation limit reached. Saved template reports remain available.')


async def ai_report(update, context, report_id, requested_style=None):
    from ai import writer
    report = await asyncio.to_thread(db(context).report, report_id)
    if not report:
        raise ValueError('Report not found.')
    style, _ = await report_preferences(context, requested_style or report.get('style'))
    engine = writer(config.AI_PROVIDER, config.AI_KEY, config.AI_MODEL, config.AI_FALLBACK_MODEL)
    await reserve_ai(context)
    await reply(update, 'Preparing an AI draft. Your original report remains saved.')
    try:
        text = await engine.draft(report['text'], style)
    except Exception as exc:
        await asyncio.to_thread(db(context).record_ai_event, 'report', config.AI_PROVIDER,
                                config.AI_MODEL, REPORT_PROMPT_VERSION, 'failed', type(exc).__name__)
        await reply(update, f'AI unavailable. Report #{report_id} is unchanged; use /report {report_id}.')
        return
    used_model = getattr(engine, 'last_model', None) or config.AI_MODEL
    await asyncio.to_thread(db(context).record_ai_event, 'report', config.AI_PROVIDER,
                            used_model, REPORT_PROMPT_VERSION, 'success')
    new_id = await asyncio.to_thread(
        db(context).save_report, report['shift_id'], report['kind'], text, style,
        config.AI_PROVIDER, used_model, REPORT_PROMPT_VERSION, report_id)
    await reply(update,
        f'AI draft #{new_id} ({style}, {used_model}) — verify outcomes and counts.\n\n{text}',
        InlineKeyboardMarkup([[InlineKeyboardButton('Finalize', callback_data=f'final:{new_id}')]]))


def proposal_text(proposal):
    lines = [f"Suggestion #{proposal['id']} ({proposal.get('model') or 'model unknown'})"]
    for index, entry in enumerate(proposal['entries'], 1):
        confidence = round(float(entry.get('confidence', 0.5)) * 100)
        line = f"{index}. [{entry['category']}] {entry['detail']} — confidence {confidence}%"
        metadata = []
        for label, value in (
            ('client', entry.get('client')), ('channel', entry.get('channel')),
            ('outcome', entry.get('outcome')), ('product', entry.get('product')),
            ('result', entry.get('result')), ('ticket', entry.get('ticket')),
            ('follow-up', entry.get('follow_up')), ('status', entry.get('status')),
            ('participation', entry.get('participation')), ('platform', entry.get('platform')),
            ('waiting on', entry.get('waiting_on'))):
            if value:
                metadata.append(f'{label}: {value}')
        if metadata:
            line += '\n   ' + '; '.join(metadata)
        if entry.get('needs_confirmation'):
            line += '\n   Confirm: ' + (entry.get('question') or 'Please verify this item before accepting.')
        lines.append(line)
    return '\n'.join(lines)


def proposal_keyboard(proposal):
    rows = [[InlineKeyboardButton(f'Remove item {index}', callback_data=f'drop:{proposal["id"]}:{index}')]
            for index in range(1, len(proposal['entries']) + 1)]
    rows.append([
        InlineKeyboardButton('Accept reviewed entries', callback_data=f'apply:{proposal["id"]}'),
        InlineKeyboardButton('Keep original note', callback_data=f'dismiss:{proposal["id"]}')])
    return InlineKeyboardMarkup(rows)


async def show_proposal(update, context, proposal_id):
    proposal = await asyncio.to_thread(db(context).proposal, proposal_id)
    if not proposal:
        raise ValueError('Suggestion not found.')
    await reply(update, proposal_text(proposal), proposal_keyboard(proposal))


async def organize(update, context, activity_id):
    from ai import writer
    shift = await active(context)
    rows = await asyncio.to_thread(db(context).activities, shift['id'])
    row = next((item for item in rows if item['id'] == activity_id and item['category'] == 'note'), None)
    if not row:
        raise ValueError('Choose a note in the active shift using /activity.')
    engine = writer(config.AI_PROVIDER, config.AI_KEY, config.AI_MODEL, config.AI_FALLBACK_MODEL)
    await reserve_ai(context)
    try:
        entries = await engine.organize(row['detail'])
    except Exception as exc:
        await asyncio.to_thread(db(context).record_ai_event, 'organize', config.AI_PROVIDER,
                                config.AI_MODEL, ORGANIZE_PROMPT_VERSION, 'failed', type(exc).__name__)
        await reply(update, 'AI unavailable. Your original note remains saved.')
        return
    used_model = getattr(engine, 'last_model', None) or config.AI_MODEL
    await asyncio.to_thread(db(context).record_ai_event, 'organize', config.AI_PROVIDER,
                            used_model, ORGANIZE_PROMPT_VERSION, 'success')
    proposal_id = await asyncio.to_thread(
        db(context).propose, activity_id, row['detail'], entries, used_model, ORGANIZE_PROMPT_VERSION)
    await show_proposal(update, context, proposal_id)


async def handle_callback(update, context):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(':')
    action = parts[0]
    if action == 'ai':
        await ai_report(update, context, int(parts[1]))
    elif action == 'organize':
        await organize(update, context, int(parts[1]))
    elif action == 'apply':
        shift = await active(context)
        await asyncio.to_thread(db(context).apply_proposal, int(parts[1]), shift['id'])
        await reply(update, 'Reviewed entries saved. Existing task statuses were not changed.')
    elif action == 'dismiss':
        await asyncio.to_thread(db(context).dismiss_proposal, int(parts[1]))
        await reply(update, 'Suggestion dismissed. The original note remains in your activity log.')
    elif action == 'drop':
        proposal_id, item = int(parts[1]), int(parts[2])
        await asyncio.to_thread(db(context).update_proposal_entry, proposal_id, item, None, True)
        await show_proposal(update, context, proposal_id)
    elif action == 'src':
        operation, message_id = parts[1], int(parts[2])
        if operation == 'accept':
            source = await asyncio.to_thread(db(context).source_message, message_id)
            if not source:
                raise ValueError('Inbox item not found.')
            shift_id = source.get('shift_id')
            case_id = await asyncio.to_thread(db(context).accept_source_message,
                                              message_id, shift_id)
            await reply(update, f'Inbox item accepted as CASE-{case_id}.')
        else:
            await asyncio.to_thread(db(context).ignore_source_message, message_id)
            await reply(update, 'Inbox item ignored.')
    elif action == 'case':
        status, case_id = parts[1], int(parts[2])
        shift = await active(context)
        item = await asyncio.to_thread(db(context).update_case, case_id, 'status', status,
                                       shift['id'], f'Case moved to {status}.')
        if status == 'client_updated':
            item = await asyncio.to_thread(db(context).update_case, case_id, 'client_updated',
                                           True, shift['id'], 'Client update recorded.')
        if not item:
            raise ValueError('Case not found.')
        await reply(update, case_line(item, True), case_keyboard(item))
    elif action == 'follow':
        operation, followup_id = parts[1], int(parts[2])
        if operation == 'done':
            await asyncio.to_thread(db(context).complete_followup, followup_id)
            await reply(update, 'Follow-up completed.')
        else:
            due = datetime.now(timezone.utc) + timedelta(hours=1)
            await asyncio.to_thread(db(context).snooze_followup, followup_id, due.isoformat())
            await reply(update, 'Follow-up snoozed for one hour.')
    elif action == 'final':
        report_id = int(parts[1])
        if not await asyncio.to_thread(db(context).report, report_id):
            raise ValueError('Report not found.')
        await asyncio.to_thread(db(context).finalize, report_id)
        await reply(update, f'Report #{report_id} finalized.')
    elif action == 'close':
        report_id = int(parts[1])
        shift = await active(context)
        await asyncio.to_thread(db(context).finalize_and_close, report_id, shift['id'])
        await reply(update, 'Shift closed. Unfinished tasks carry forward.', MENU)
    elif action == 'task':
        task_action, task_id = parts[1], int(parts[2])
        if task_action == 'edit':
            await reply(update, f'Use /edittask {task_id} | field | value')
            return
        if task_action == 'block':
            await reply(update, f'Use /block {task_id} reason so the blocker is recorded.')
            return
        shift = await active(context)
        status = {'progress': TaskStatus.IN_PROGRESS, 'done': TaskStatus.COMPLETED,
                  'reopen': TaskStatus.PENDING, 'delete': TaskStatus.CANCELLED}[task_action]
        task = await asyncio.to_thread(db(context).mark_status, task_id, status, None, shift['id'])
        if not task:
            raise ValueError('Task not found.')
        await reply(update, task_line(task, True), task_keyboard(task))


async def health_text(context):
    database = db(context)
    integrity = await asyncio.to_thread(database.integrity)
    shift = await asyncio.to_thread(database.active_shift)
    stats = await asyncio.to_thread(database.ai_stats)
    inbox_count = len(await asyncio.to_thread(database.inbox, 1000))
    case_count = len(await asyncio.to_thread(database.list_cases, None, 1000))
    backups = sorted((config.BASE_DIR / 'storage' / 'backups').glob('*.sqlite3'))
    jobs = context.application.job_queue.jobs() if context.application.job_queue else ()
    return '\n'.join((
        'Work Assistant Health',
        f'Database schema: v{SCHEMA_VERSION}; integrity: {integrity}',
        f'Active shift: {shift["id"] if shift else "none"}',
        f'Reminder/maintenance jobs: {len(jobs)}',
        f'Approved cases: {case_count}; review inbox: {inbox_count}',
        f'Local dashboard: {"running" if context.application.bot_data.get("dashboard") else "disabled/unavailable"}',
        f'Backups retained locally: {len(backups)}' +
            (f'; newest: {backups[-1].name}' if backups else ''),
        f'AI configured: {bool(config.AI_KEY and config.AI_MODEL)}; primary: {config.AI_MODEL or "none"}; fallback: {config.AI_FALLBACK_MODEL or "none"}',
        'AI event totals: ' + (', '.join(f"{item['status']}={item['count']}" for item in stats) or 'none'),
    ))


async def handle(update, context):
    if not authorized(update):
        return
    try:
        if update.callback_query:
            await handle_callback(update, context)
            return
        message = update.effective_message
        if getattr(message, 'voice', None):
            await capture_voice(update, context)
            return
        if (getattr(message, 'photo', None) or getattr(message, 'document', None)
                or getattr(message, 'video', None)):
            await capture_evidence(update, context)
            return
        text = (message.text or '').strip()
        text = {'Start Shift': '/shift', 'My Tasks': '/todo', 'TOD': '/tod',
                'Pre-lunch': '/pl', 'EOD': '/eod', 'End Shift': '/endshift',
                'Help': '/help'}.get(text, text)
        if not text.startswith('/'):
            await save_plain_message(update, context, text)
            return

        head, _, argument = text.partition(' ')
        command = head[1:].split('@')[0].lower()
        argument = argument.strip()
        args = argument.split()

        if command in ('start', 'help'):
            await reply(update, HELP, MENU)
        elif command in ('shift', 'preset'):
            if command == 'preset':
                args = (await asyncio.to_thread(db(context).get_setting, 'default_shift') or '12:00 21:00').split()
            if len(args) > 2:
                raise ValueError('Usage: /shift [HH:MM or YYYY-MM-DDTHH:MM] [HH:MM]')
            start, end = new_shift(config.TIMEZONE, *args)
            shift_id = await asyncio.to_thread(db(context).start_shift, start.isoformat(), end.isoformat())
            await reply(update,
                f'Shift #{shift_id} started: {start:%d %b %H:%M} to {end:%d %b %H:%M}. '
                'Add plans with /task, then generate /tod.', MENU)
        elif command == 'default':
            if len(args) != 2:
                raise ValueError('Usage: /default 12:00 21:00')
            start, end = new_shift(config.TIMEZONE, *args)
            await asyncio.to_thread(db(context).set_setting, 'default_shift', argument)
            await reply(update, f'Default shift saved: {start:%H:%M}–{end:%H:%M}.')
        elif command == 'schedule':
            shift = await active(context)
            if not 1 <= len(args) <= 2:
                raise ValueError('Usage: /schedule 21:00 [16:00|none]')
            start = datetime.fromisoformat(shift['start'])
            end = clock_on_shift(args[0], start)
            lunch = datetime.fromisoformat(shift['lunch']) if shift['lunch'] else None
            if len(args) == 2:
                lunch = None if args[1].casefold() == 'none' else clock_on_shift(args[1], start)
            validate_schedule(start, end, lunch)
            await asyncio.to_thread(db(context).schedule, shift['id'], end.isoformat(),
                                    lunch.isoformat() if lunch else None)
            await reply(update, 'Schedule updated. Use /status to view it.')
        elif command == 'status':
            shift = await active(context)
            await reply(update, f"Shift #{shift['id']} ({config.TIMEZONE})\nStart: {shift['start']}\nEnd: {shift['end']}\nLunch: {shift['lunch'] or 'Flexible'}")
        elif command in ('task', 'plan'):
            shift = await active(context)
            values = parse_task(argument, datetime.fromisoformat(shift['start']))
            task = await asyncio.to_thread(db(context).add_task, shift_id=shift['id'], **values)
            await reply(update, 'Planned ' + task_line(task, True), task_keyboard(task))
        elif command in ('todo', 'pending'):
            await show_tasks(update, context, await asyncio.to_thread(db(context).list_tasks))
        elif command == 'today':
            shift = await active(context)
            tasks = await asyncio.to_thread(db(context).list_tasks)
            await show_tasks(update, context, [task for task in tasks if task.planned_shift_id == shift['id']])
        elif command == 'overdue':
            today = datetime.now(ZoneInfo(config.TIMEZONE)).date().isoformat()
            tasks = await asyncio.to_thread(db(context).list_tasks)
            await show_tasks(update, context, [task for task in tasks if task.due_date and task.due_date < today])
        elif command == 'taskdetails':
            task = await asyncio.to_thread(db(context).get_task, int(argument))
            if not task:
                raise ValueError('Task not found.')
            await reply(update, task_line(task, True), task_keyboard(task))
        elif command == 'edittask':
            shift = await active(context)
            parts = [part.strip() for part in argument.split('|', 2)]
            if len(parts) != 3:
                raise ValueError('Usage: /edittask ID | field | value')
            task_id, field, value = int(parts[0]), parts[1].casefold().replace(' ', '_'), parts[2]
            if field == 'priority':
                value = PRIORITIES.get(value.casefold(), value)
            elif field == 'due_date' and value not in ('-', 'none'):
                value = parse_due(value)
            task = await asyncio.to_thread(db(context).update_task, task_id, field, value, shift['id'])
            if not task:
                raise ValueError('Task not found.')
            await reply(update, task_line(task, True), task_keyboard(task))
        elif command in ('done', 'progress', 'reopen', 'block', 'delete'):
            shift = await active(context)
            if not argument:
                raise ValueError(f'Usage: /{command} ID' + (' reason' if command == 'block' else ''))
            completion_note = None
            if command == 'done':
                left, _, completion_note = argument.partition('|')
                task_id = int(left.strip())
                completion_note = completion_note.strip() or None
                reason = None
            else:
                first, _, rest = argument.partition(' ')
                task_id, reason = int(first), rest.strip() or None
            if command == 'block' and not reason:
                raise ValueError('Provide a blocking reason.')
            status = {'done': TaskStatus.COMPLETED, 'progress': TaskStatus.IN_PROGRESS,
                      'reopen': TaskStatus.PENDING, 'block': TaskStatus.BLOCKED,
                      'delete': TaskStatus.CANCELLED}[command]
            task = await asyncio.to_thread(db(context).mark_status, task_id, status, reason,
                                           shift['id'], completion_note)
            if not task:
                raise ValueError('Task not found.')
            await reply(update, task_line(task, True), task_keyboard(task))
        elif command == 'support':
            shift = await active(context)
            values = parse_support(argument)
            activity_id = await asyncio.to_thread(db(context).add_support, shift['id'], **values)
            await reply(update, f'Logged support interaction #{activity_id}.')
        elif command == 'case':
            shift = await active(context)
            values = parse_case(argument, datetime.fromisoformat(shift['start']))
            case_id = await asyncio.to_thread(db(context).create_case, shift_id=shift['id'],
                                               detail=values['title'], **values)
            if values.get('follow_up_at'):
                await asyncio.to_thread(db(context).add_followup, case_id,
                                        values['follow_up_at'], values.get('next_action'),
                                        values.get('waiting_on'), shift['id'])
            item = await asyncio.to_thread(db(context).case, case_id)
            await reply(update, case_line(item, True), case_keyboard(item))
        elif command == 'cases':
            rows = await asyncio.to_thread(db(context).list_cases)
            if not rows:
                await reply(update, 'No approved cases.')
            else:
                for item in rows[:20]:
                    await reply(update, case_line(item, True), case_keyboard(item))
        elif command == 'caseinfo':
            item = await asyncio.to_thread(db(context).case, int(argument))
            if not item:
                raise ValueError('Case not found.')
            events = await asyncio.to_thread(db(context).case_events, item['id'])
            tests = await asyncio.to_thread(db(context).test_sessions, None, item['id'])
            evidence = await asyncio.to_thread(db(context).evidence, item['id'])
            detail = case_line(item, True)
            detail += '\n\nTimeline\n' + reports.bullets(
                f"{event['occurred_at'][:16].replace('T',' ')} [{event['event_type']}] {event['detail']}"
                for event in events[-20:])
            detail += f'\n\nTest sessions: {len(tests)}; evidence: {len(evidence)}.'
            await reply(update, detail, case_keyboard(item))
        elif command == 'caseevent':
            shift = await active(context)
            parts = [part.strip() for part in argument.split('|', 3)]
            if len(parts) < 3:
                raise ValueError('Usage: /caseevent CASE_ID | type | detail | outcome')
            case_id, event_type, detail = int(parts[0]), parts[1], parts[2]
            outcome = parts[3] if len(parts) == 4 and parts[3] not in ('','-','?') else None
            event_id = await asyncio.to_thread(db(context).add_case_event, case_id, event_type,
                                               detail, shift['id'], 'owner', outcome)
            await reply(update, f'Added event #{event_id} to CASE-{case_id}.')
        elif command == 'casestatus':
            shift = await active(context)
            first, separator, note = argument.partition('|')
            identifiers = first.split()
            if len(identifiers) != 2:
                raise ValueError('Usage: /casestatus CASE_ID STATUS | note')
            item = await asyncio.to_thread(db(context).update_case, int(identifiers[0]), 'status',
                                           identifiers[1].casefold().replace(' ', '_'), shift['id'],
                                           note.strip() if separator else None)
            if not item:
                raise ValueError('Case not found.')
            await reply(update, case_line(item, True), case_keyboard(item))
        elif command == 'clientupdated':
            shift = await active(context)
            first, _, note = argument.partition('|')
            case_id = int(first.strip())
            await asyncio.to_thread(db(context).update_case, case_id, 'client_updated', True,
                                    shift['id'], note.strip() or 'Client update recorded.')
            item = await asyncio.to_thread(db(context).update_case, case_id, 'status',
                                           'client_updated', shift['id'],
                                           note.strip() or 'Case marked client updated.')
            await reply(update, case_line(item, True), case_keyboard(item))
        elif command == 'mergecases':
            source_id, target_id = (int(value) for value in argument.split())
            await asyncio.to_thread(db(context).merge_cases, source_id, target_id)
            await reply(update, f'CASE-{source_id} merged into CASE-{target_id}.')
        elif command == 'testing':
            shift = await active(context)
            values = parse_testing(argument)
            activity_id = await asyncio.to_thread(db(context).add_testing, shift['id'], **values)
            await reply(update, f'Logged testing record #{activity_id}.')
        elif command == 'testsession':
            shift = await active(context)
            values = parse_test_session(argument)
            session_id = await asyncio.to_thread(db(context).add_test_session,
                                                  shift_id=shift['id'], **values)
            await reply(update, f'Logged test session TEST-{session_id}. Attach evidence with '
                        f'/attach {values.get("case_id") or "CASE_ID"} {session_id} as the media caption.')
        elif command == 'tests':
            case_id = int(argument) if argument else None
            rows = await asyncio.to_thread(db(context).test_sessions, None, case_id)
            await reply(update, reports.bullets(
                f"TEST-{item['id']} [{item['result']}] {item['scenario']}"
                + (f" — CASE-{item['case_id']}" if item.get('case_id') else '')
                for item in rows))
        elif command == 'inbox':
            rows = await asyncio.to_thread(db(context).inbox)
            if not rows:
                await reply(update, 'Review inbox is clear.')
            else:
                for item in rows[:10]:
                    detail = item.get('redacted_text') or item.get('text') or '(media only)'
                    await reply(update,
                        f"Inbox #{item['id']} [{item.get('classification') or 'unclassified'}] "
                        f"confidence {round(float(item.get('confidence') or 0)*100)}%\n{detail[:1200]}",
                        source_keyboard(item))
        elif command == 'acceptmsg':
            numbers = argument.split()
            if not 1 <= len(numbers) <= 2:
                raise ValueError('Usage: /acceptmsg MESSAGE_ID [CASE_ID]')
            source = await asyncio.to_thread(db(context).source_message, int(numbers[0]))
            if not source:
                raise ValueError('Inbox item not found.')
            case_id = await asyncio.to_thread(db(context).accept_source_message,
                int(numbers[0]), source.get('shift_id'), int(numbers[1]) if len(numbers) == 2 else None)
            await reply(update, f'Inbox item accepted into CASE-{case_id}.')
        elif command == 'ignoremsg':
            await asyncio.to_thread(db(context).ignore_source_message, int(argument))
            await reply(update, 'Inbox item ignored.')
        elif command == 'followup':
            shift = await active(context)
            parts = [part.strip() for part in argument.split('|', 3)]
            if len(parts) < 2:
                raise ValueError('Usage: /followup CASE_ID | due | waiting on | note')
            due = parse_when(parts[1], datetime.now(ZoneInfo(config.TIMEZONE)))
            waiting_on = parts[2] if len(parts) > 2 and parts[2] not in ('','-','?') else None
            note = parts[3] if len(parts) > 3 and parts[3] not in ('','-','?') else None
            followup_id = await asyncio.to_thread(db(context).add_followup, int(parts[0]), due,
                                                  note, waiting_on, shift['id'])
            await reply(update, f'Follow-up #{followup_id} scheduled for {due[:16].replace("T", " ")}.')
        elif command == 'followups':
            rows = await asyncio.to_thread(db(context).list_followups)
            if not rows:
                await reply(update, 'No pending follow-ups.')
            else:
                for item in rows[:20]:
                    await reply(update,
                        f"Follow-up #{item['id']} · CASE-{item['case_id']}\n"
                        f"Due: {item['due_at']}\nWaiting on: {item.get('waiting_on') or 'not specified'}\n"
                        f"{item.get('note') or item['title']}", followup_keyboard(item))
        elif command == 'followupdone':
            await asyncio.to_thread(db(context).complete_followup, int(argument))
            await reply(update, 'Follow-up completed.')
        elif command == 'snooze':
            followup_id, minutes = (int(value) for value in argument.split())
            due = datetime.now(timezone.utc) + timedelta(minutes=minutes)
            await asyncio.to_thread(db(context).snooze_followup, followup_id, due.isoformat())
            await reply(update, f'Follow-up snoozed for {minutes} minutes.')
        elif command == 'learning':
            shift = await active(context)
            values = parse_learning(argument)
            activity_id = await asyncio.to_thread(db(context).add_learning, shift['id'], **values)
            await reply(update, f'Logged learning record #{activity_id}.')
        elif command == 'note':
            shift = await active(context)
            if not argument:
                raise ValueError('Usage: /note text')
            activity_id = await asyncio.to_thread(db(context).add_activity, shift['id'], 'note', argument)
            await reply(update, f'Saved note #{activity_id}.')
        elif command == 'activity':
            shift = await active(context)
            rows = await asyncio.to_thread(db(context).activities, shift['id'])
            await reply(update, reports.bullets(
                f"#{item['id']} [{item['category']}] {item['detail']}" for item in rows))
        elif command == 'organize':
            await organize(update, context, int(argument))
        elif command == 'proposal':
            await show_proposal(update, context, int(argument))
        elif command in ('edititem', 'answer'):
            identifiers, separator, replacement = argument.partition('|')
            numbers = identifiers.split()
            if not separator or len(numbers) != 2 or not replacement.strip():
                raise ValueError('Usage: /edititem PROPOSAL ITEM | field | value')
            edit_parts = [part.strip() for part in replacement.split('|', 1)]
            if len(edit_parts) == 1:
                field, value = 'detail', edit_parts[0]
            else:
                field, value = edit_parts[0].casefold().replace(' ', '_'), edit_parts[1]
            await asyncio.to_thread(db(context).update_proposal_entry,
                                    int(numbers[0]), int(numbers[1]), value, False, field)
            await show_proposal(update, context, int(numbers[0]))
        elif command == 'dropitem':
            numbers = argument.split()
            if len(numbers) != 2:
                raise ValueError('Usage: /dropitem PROPOSAL ITEM')
            await asyncio.to_thread(db(context).update_proposal_entry,
                                    int(numbers[0]), int(numbers[1]), None, True)
            await show_proposal(update, context, int(numbers[0]))
        elif command == 'accept':
            shift = await active(context)
            await asyncio.to_thread(db(context).apply_proposal, int(argument), shift['id'])
            await reply(update, 'Reviewed entries saved. Existing task statuses were not changed.')
        elif command == 'dismiss':
            await asyncio.to_thread(db(context).dismiss_proposal, int(argument))
            await reply(update, 'Suggestion dismissed; original note retained.')
        elif command in ('editlog', 'undo'):
            shift = await active(context)
            first, _, replacement = argument.partition(' ')
            if command == 'editlog' and not replacement:
                raise ValueError('Usage: /editlog ID replacement text')
            await asyncio.to_thread(db(context).correct_activity, shift['id'], int(first),
                                    replacement if command == 'editlog' else None)
            await reply(update, 'Activity corrected. Generate a new report version.')
        elif command in ('tod', 'bos', 'pl', 'prelunch', 'eod', 'endshift'):
            kind = {'bos': 'tod', 'prelunch': 'pl', 'endshift': 'eod'}.get(command, command)
            report_id = await make_report(update, context, kind, argument or None)
            if command == 'endshift':
                await reply(update, 'Review the EOD, then close this shift.',
                    InlineKeyboardMarkup([[InlineKeyboardButton(
                        'Finalize EOD & close shift', callback_data=f'close:{report_id}')]]))
        elif command == 'reportstyle':
            style, _ = await report_preferences(context, argument)
            await asyncio.to_thread(db(context).set_setting, 'report_style', style)
            await reply(update, f'Default report style: {style}.')
        elif command == 'privacy':
            parts = argument.casefold().split()
            if len(parts) != 2 or parts[0] != 'clients' or parts[1] not in ('on', 'off'):
                raise ValueError('Usage: /privacy clients on|off')
            await asyncio.to_thread(db(context).set_setting, 'mask_client_names',
                                    'true' if parts[1] == 'on' else 'false')
            await reply(update, 'Client-name masking is ' + parts[1] + '.')
        elif command == 'section':
            parts = argument.split(maxsplit=1)
            if len(parts) != 2 or parts[0] not in ('tod', 'pl', 'eod'):
                raise ValueError('Usage: /section tod|pl|eod section-name')
            shift = await active(context)
            style, mask = await report_preferences(context)
            activities = await asyncio.to_thread(db(context).activities, shift['id'])
            tasks = await asyncio.to_thread(db(context).list_tasks)
            cases = await asyncio.to_thread(db(context).cases_for_shift, shift['id'])
            sessions = await asyncio.to_thread(db(context).test_sessions, shift['id'])
            await reply(update, reports.generate_section(parts[0], parts[1], shift,
                                                          activities, tasks, style, mask, cases, sessions))
        elif command == 'ai':
            parts = argument.split()
            if not parts:
                raise ValueError('Usage: /ai REPORT_ID [short|standard|detailed]')
            await ai_report(update, context, int(parts[0]), parts[1] if len(parts) > 1 else None)
        elif command == 'history':
            rows = await asyncio.to_thread(db(context).history)
            await reply(update, reports.bullets(
                f"#{item['id']} shift {item['shift_id']} {item['kind']} {item['style']} "
                f"{'final' if item['finalized'] else 'draft'} {item['model'] or 'template'}"
                for item in rows))
        elif command in ('report', 'editreport'):
            first, _, replacement = argument.partition(' ')
            report = await asyncio.to_thread(db(context).report, int(first))
            if not report:
                raise ValueError('Report not found.')
            if command == 'editreport':
                if not replacement:
                    raise ValueError('Usage: /editreport ID replacement text')
                report_id = await asyncio.to_thread(
                    db(context).save_report, report['shift_id'], report['kind'], replacement,
                    report['style'], None, None, None, report['id'])
                await reply(update, f'Saved revised draft #{report_id}.')
            else:
                await reply(update, f"Report #{report['id']}\n\n{report['text']}")
        elif command == 'summary':
            shift = await active(context)
            style, mask = await report_preferences(context)
            activities = await asyncio.to_thread(db(context).activities, shift['id'])
            tasks = await asyncio.to_thread(db(context).list_tasks)
            cases = await asyncio.to_thread(db(context).cases_for_shift, shift['id'])
            sessions = await asyncio.to_thread(db(context).test_sessions, shift['id'])
            await reply(update, reports.generate_report('eod', shift, activities, tasks, style, mask,
                                                        cases, sessions))
        elif command == 'week':
            start = datetime.now(ZoneInfo(config.TIMEZONE)) - timedelta(days=7)
            shifts = await asyncio.to_thread(db(context).shifts_since, start.isoformat())
            activities = await asyncio.to_thread(db(context).all_activities_since, start.isoformat())
            tasks = await asyncio.to_thread(db(context).list_tasks)
            await reply(update, reports.weekly_summary(shifts, activities, tasks))
        elif command == 'sync':
            from connectors import configured_connector, sync_connector
            parts = argument.split(maxsplit=1)
            if not parts:
                raise ValueError('Usage: /sync freshdesk|freshchat')
            connector = configured_connector(parts[0], parts[1] if len(parts) == 2 else None)
            result = await asyncio.to_thread(sync_connector, db(context), connector)
            await reply(update, f"{result['connector']} sync complete: {result['fetched']} fetched, "
                        f"{result['inserted']} new inbox items. Use /inbox.")
        elif command == 'dashboard':
            service = context.application.bot_data.get('dashboard')
            if not service:
                raise ValueError('Local dashboard is disabled or unavailable. Check /health.')
            await reply(update, 'Open the dashboard on this Windows PC only:\n' + service.url)
        elif command == 'imports':
            rows = await asyncio.to_thread(db(context).imports)
            await reply(update, reports.bullets(
                f"Import #{item['id']} [{item['status']}] {item['source_name']} — {item['completed_at'] or 'running'}"
                for item in rows))
        elif command == 'rollbackimport':
            count = await asyncio.to_thread(db(context).rollback_import, int(argument))
            await reply(update, f'Import batch rolled back; {count} source messages removed. '
                        'The original export files were not changed.')
        elif command in ('search', 'client', 'findtesting'):
            if not argument:
                raise ValueError(f'Usage: /{command} search text')
            category = {'client': 'support', 'findtesting': 'testing'}.get(command)
            rows = await asyncio.to_thread(db(context).search, argument, category)
            lines = []
            for item in rows:
                if item['type'] == 'task':
                    task = await asyncio.to_thread(db(context).get_task, item['id'])
                    lines.append(task_line(task) if task else f"Task #{item['id']}")
                elif item['type'] == 'case':
                    lines.append(f"CASE-{item['id']} [{item['status']}] {item['title']}")
                else:
                    lines.append(f"#{item['id']} [{item['category']}] {item['detail']}")
            await reply(update, reports.bullets(lines))
        elif command == 'export':
            from exporter import create_export
            export_format = argument.casefold() or 'markdown'
            path = await asyncio.to_thread(create_export, db(context), export_format,
                                           config.BASE_DIR / 'storage' / 'exports')
            with path.open('rb') as handle:
                await update.effective_message.reply_document(handle, filename=path.name,
                    caption='Private work export. Store it securely.')
        elif command == 'backup':
            name = datetime.now().strftime('%Y%m%d-%H%M%S%f') + '.sqlite3'
            path = await asyncio.to_thread(db(context).backup,
                                           config.BASE_DIR / 'storage' / 'backups' / name)
            await reply(update, f'Backup saved locally: {path.name}')
        elif command == 'health':
            await reply(update, await health_text(context))
        else:
            await reply(update, 'Unknown command. Use /help.')
    except (ValueError, OverflowError) as exc:
        await reply(update, str(exc))


def authorized(update):
    return bool(update.effective_user and update.effective_chat and
                update.effective_user.id == config.OWNER_ID and
                update.effective_chat.type == 'private' and
                update.effective_chat.id == config.OWNER_ID)


async def error_handler(update, context):
    logging.getLogger(__name__).error('Update failed (%s)', type(context.error).__name__)
    if update and authorized(update):
        await reply(update, 'The operation failed. Check /health and /activity before retrying.')
