"""Daily task orchestration over existing task, shift, report and audit records."""
import re
import json
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
from models import TaskStatus


async def handle_daily(update, context):
    from handlers import authorized, reply, make_report
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    if not authorized(update) or update.callback_query:
        return False
    text = (getattr(update.effective_message, 'text', None) or '').strip()
    low = text.casefold().rstrip('.!')
    db = context.application.bot_data['db']
    shift = db.active_shift()
    deletion = re.fullmatch(r'(?:delete|clear|remove) (?:all |every )?(?:(pending|completed|in.progress|blocked|today\x27?s) )?tasks?', low)
    single = re.fullmatch(r'(?:delete|remove) task #?(\d+)', low)
    if deletion or single:
        scope = (deletion.group(1) or 'all') if deletion else 'all'
        scope = 'today' if scope.startswith('today') else scope.replace(' ', '_')
        count, correlation = db.delete_tasks(scope, shift['id'] if shift else None,
                                             int(single.group(1)) if single else None)
        audit = db.get_last_reversible_audit() if count else None
        markup = InlineKeyboardMarkup([[InlineKeyboardButton('Undo', callback_data=f"audit:undo:{audit['id']}")]]) if audit else None
        await reply(update, f'Deleted {count} tasks.', markup)
        return True
    if low in ('delete everything', 'clear everything', 'remove everything'):
        await reply(update, 'What should I delete? For tasks, say “delete all tasks”.')
        return True
    head, _, argument = text.partition(' ')
    command = head.lower().split('@')[0]
    if command == '/startday':
        if not shift:
            from nlp import DeterministicParser, NLIntent, NaturalLanguagePipeline
            schedule, _, remaining = argument.partition('|')
            interpreted = DeterministicParser.parse(schedule)
            if interpreted and interpreted.intent == NLIntent.SET_SHIFT:
                response, result = await NaturalLanguagePipeline(db).process(schedule, None, update.update_id)
                shift = db.active_shift()
                await reply(update, response)
                if not shift:
                    return True
                argument = remaining
        if not shift:
            if not argument:
                await reply(update, 'Set today’s hours with /shift START END, then /startday task one | task two. Set lunch with /schedule END LUNCH.')
                return True
            await reply(update, 'Start your shift first with /shift START END. Your pending work remains available with /todo.')
            return True
        existing = {task.title.casefold().strip(): task for task in db.list_tasks() if task.status != TaskStatus.CANCELLED}
        added = []
        for title in [p.strip() for p in argument.split('|') if p.strip()]:
            if title.casefold() in existing:
                added.append(f"Already exists: #{existing[title.casefold()].id} {title}")
            else:
                task = db.add_task(title, shift_id=shift['id'])
                existing[title.casefold()] = task
                added.append(f'Planned #{task.id}: {task.title}')
        key = f'daily_plan:{shift["id"]}'
        if not db.get_setting(key):
            db.set_setting(key, json.dumps([t.id for t in db.tasks_for_shift(shift['id'])]))
        await reply(update, '\n'.join(added) + '\nOpen work:\n' + '\n'.join(
            f'#{t.id} [{t.status.value}] {t.title}' for t in db.list_tasks()
            if t.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED)) +
            '\nReview priorities with /edittask ID | priority | VALUE. Generate /tod when ready.')
        return True
    if command in ('/checkpoint', '/timeline', '/resume', '/tomorrow', '/carrytask'):
        if not shift:
            await reply(update, 'Start a shift first with /shift START END.')
            return True
        if command == '/checkpoint':
            planned = json.loads(db.get_setting(f'daily_plan:{shift["id"]}') or '[]')
            if planned:
                lines = []
                for ident in planned:
                    task = db.get_task(ident)
                    lines.append(f'#{ident}: {task.title} — {task.status.value}' if task else f'#{ident}: removed from task list')
                await reply(update, 'Start-of-day plan comparison:\n' + '\n'.join(lines))
            await make_report(update, context, 'pl')
        elif command == '/timeline':
            rows = db.activities(shift['id'])
            await reply(update, '\n'.join(f"{r['created_at']} · #{r['id']} [{r['category']}] {r['detail']}" for r in rows) or 'No work recorded in this shift.')
        elif command == '/carrytask':
            task = db.get_task(int(argument))
            if not task:
                raise ValueError('Task not found.')
            due = (datetime.now(ZoneInfo(config.TIMEZONE)).date() + timedelta(days=1)).isoformat()
            db.update_task(task.id, 'due_date', due)
            db.record_audit('carry-' + uuid.uuid4().hex, 'carry_task', 'owner', 'tasks', task.id,
                            json.dumps({'due_date': task.due_date}), json.dumps({'due_date': due}))
            await reply(update, f'Task #{task.id} rescheduled to {due}; status and blocker preserved. /undo to revert.')
        else:
            tasks = [t for t in db.list_tasks() if t.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED)]
            tasks.sort(key=lambda t: (-t.priority, t.id))
            await reply(update, '\n'.join(f'#{t.id} [{t.status.value}] {t.title}' +
                (f' — blocked: {t.blocked_reason}' if t.blocked_reason else '') +
                (f' — next: {t.next_action}' if t.next_action else '') for t in tasks) or 'No unfinished tasks.')
        return True
    progress = re.fullmatch(r'(?:started|start working on) (.+)', text, re.I)
    blocked = re.fullmatch(r'(.+?) is blocked because (.+)', text, re.I)
    if progress or blocked:
        reference = (progress.group(1) if progress else blocked.group(1)).strip()
        tasks = [t for t in db.list_tasks() if reference.casefold() == t.title.casefold()
                 or reference in (str(t.id), f'task {t.id}')]
        if len(tasks) != 1:
            await reply(update, 'Specify the task ID using /progress ID or /block ID reason. No task changed.')
            return True
        task = tasks[0]
        db.mark_status(task.id, TaskStatus.IN_PROGRESS if progress else TaskStatus.BLOCKED,
                       blocked_reason=None if progress else blocked.group(2), shift_id=shift['id'] if shift else None,
                       correlation_id='daily-' + uuid.uuid4().hex, actor='owner')
        await reply(update, f'Updated task #{task.id}. /undo to revert.')
        return True
    return False
