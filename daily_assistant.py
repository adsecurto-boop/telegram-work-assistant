"""Daily task orchestration, persistent conversational planning, linking, updates, corrections, and handover."""
from __future__ import annotations

import difflib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config
from models import Task, TaskStatus


# ---------------------------------------------------------------------------
# Utility & Parsing Helpers
# ---------------------------------------------------------------------------

def parse_time_flexible(text: str) -> str | None:
    """Parse times like '12', '12:00', '4', '4pm', '4 pm', '16:00' into HH:MM."""
    clean = text.strip().lower()
    m = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b', clean)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    meridiem = m.group(3)
    if meridiem == 'pm' and hour < 12:
        hour += 12
    elif meridiem == 'am' and hour == 12:
        hour = 0
    elif not meridiem and hour < 7:
        hour += 12
    return f'{hour:02d}:{minute:02d}'


def parse_shift_range_flexible(text: str) -> tuple[str, str] | None:
    """Parse '12 to 9', '10am to 7pm', '10:00 to 19:00', '12-9'."""
    m = re.search(r'\b(?:shift\s+is\s+|from\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s*(?:to|-)\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b', text, re.I)
    if not m:
        return None
    start = parse_time_flexible(m.group(1))
    end = parse_time_flexible(m.group(2))
    if start and end:
        sh, sm = map(int, start.split(':'))
        eh, em = map(int, end.split(':'))
        end_raw = m.group(2).lower()
        if eh < sh and eh <= 12 and not ('am' in end_raw or 'pm' in end_raw):
            eh += 12
            if eh < 24:
                end = f'{eh:02d}:{em:02d}'
        return start, end
    return None



def parse_lunch_flexible(text: str) -> str | None:
    """Parse 'lunch at 4', 'lunch at 16:00', 'lunch at 4pm'."""
    m = re.search(r'\blunch\s+(?:is\s+)?(?:at\s+)?(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\b', text, re.I)
    if m:
        return parse_time_flexible(m.group(1))
    return None


def find_similar_task(title: str, existing_tasks: list[Task], threshold: float = 0.6) -> Task | None:
    """Find a task with a similar title if not an exact match."""
    t_clean = title.casefold().strip()
    for task in existing_tasks:
        existing_clean = task.title.casefold().strip()
        if t_clean == existing_clean:
            return task
        ratio = difflib.SequenceMatcher(None, t_clean, existing_clean).ratio()
        if ratio >= threshold or t_clean in existing_clean or existing_clean in t_clean:
            return task
    return None


def resolve_task_reference(reference: str, db, shift_id: int | None = None, reply_to_task_id: int | None = None) -> tuple[Task | None, list[Task]]:
    all_tasks = [t for t in db.list_tasks() if t.status != TaskStatus.CANCELLED]
    ref_clean = reference.strip()

    # 1. Explicit record ID
    id_match = re.search(r'#?(\d+)', ref_clean)
    if id_match and (ref_clean.startswith('#') or ref_clean.lower().startswith('task') or ref_clean.isdigit()):
        tid = int(id_match.group(1))
        t = db.get_task(tid)
        if t and t.status != TaskStatus.CANCELLED:
            return t, []

    # 2. Reply to a known record
    if reply_to_task_id:
        t = db.get_task(reply_to_task_id)
        if t and t.status != TaskStatus.CANCELLED:
            return t, []

    # 3. Context active task
    ctx = db.get_conversation_context('owner')
    if ctx and ctx.get('active_task_id'):
        t = db.get_task(ctx['active_task_id'])
        if t and t.status != TaskStatus.CANCELLED and ref_clean.casefold() in t.title.casefold():
            return t, []

    # 4. Exact match
    exact = [t for t in all_tasks if t.title.casefold() == ref_clean.casefold()]
    if len(exact) == 1:
        return exact[0], []

    # 5. Substring match
    matches = [t for t in all_tasks if ref_clean.casefold() in t.title.casefold()]
    if len(matches) == 1:
        return matches[0], []
    if len(matches) > 1:
        return None, matches

    # Fuzzy match
    fuzzy = []
    for t in all_tasks:
        if difflib.SequenceMatcher(None, ref_clean.casefold(), t.title.casefold()).ratio() > 0.5:
            fuzzy.append(t)
    if len(fuzzy) == 1:
        return fuzzy[0], []
    if len(fuzzy) > 1:
        return None, fuzzy

    return None, []


def get_known_clients(db) -> list[str]:
    """Retrieve all known client names from clients, tasks, and activities tables."""
    with db.connect() as conn:
        rows = conn.execute('''
            SELECT name FROM clients WHERE name IS NOT NULL AND name != ''
            UNION
            SELECT DISTINCT client FROM tasks WHERE client IS NOT NULL AND client != ''
            UNION
            SELECT DISTINCT client FROM activities WHERE client IS NOT NULL AND client != ''
        ''').fetchall()
        return [r[0] for r in rows if r[0]]


def categorize_late_detail(detail: str) -> str:
    """Categorize late work based on keyword patterns."""
    d = detail.casefold()
    if re.search(r'\b(?:test|tested|testing|retest|qa|verify|verified|verification|defect|regression)\b', d):
        return 'testing'
    if re.search(r'\b(?:learn|learned|kt|study|studied|research|training|course|tutorial|doc|docs|documentation)\b', d):
        return 'learning'
    if re.search(r'\b(?:ticket|call|meeting|support|client|customer|incident|resolved|issue|bug|sync|assisted|assist|query)\b', d):
        return 'support'
    if re.search(r'\b(?:deploy|deployed|config|setup|pr|build|code|coded|refactor|feature|implement|task|fix|release)\b', d):
        return 'task'
    return 'task'


# ---------------------------------------------------------------------------
# Main Daily Assistant Entry Point
# ---------------------------------------------------------------------------

async def handle_daily(update, context) -> bool:
    from handlers import authorized, reply, make_report
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    if not authorized(update):
        return False

    db = context.application.bot_data['db']
    callback_user = getattr(update.effective_user, 'id', None)
    owner_id = callback_user or config.OWNER_ID or 1

    # --- Callback query handling for daily assistant ---
    if update.callback_query:
        cq = update.callback_query
        data = cq.data or ''
        if data.startswith('plan:confirm:'):
            await cq.answer()
            conv_id = int(data.split(':')[2])
            await execute_plan_confirmation(update, context, db, conv_id)
            return True
        elif data.startswith('plan:cancel:'):
            await cq.answer()
            db.cancel_planning_conversation(owner_id)
            await reply(update, 'Daily planning session cancelled.')
            return True
        elif data.startswith('checkpoint:update:'):
            await cq.answer()
            parts = data.split(':')
            if len(parts) == 5:
                _, _, task_id_str, expected_status_str, target_status_str = parts
            else:
                _, _, task_id_str, target_status_str = parts
                expected_status_str = None

            tid = int(task_id_str)
            task = db.get_task(tid)
            if not task:
                await reply(update, f"Task #{tid} no longer exists.")
                return True

            if expected_status_str and task.status.value != expected_status_str:
                await reply(update, f"⚠️ Checkpoint action ignored: Task #{tid} status is now [{task.status.value}], not [{expected_status_str}]. Newer changes were preserved.")
                return True

            if expected_status_str and expected_status_str == target_status_str:
                await reply(update, f"Task #{tid} remains [{task.status.value}].")
                return True

            shift = db.active_shift()
            st = TaskStatus(target_status_str)
            db.mark_status(tid, st, shift_id=shift['id'] if shift else None, actor='owner')
            await reply(update, f"Updated task #{tid} to [{st.value}].")
            return True
        elif data.startswith('task:reschedule:'):
            await cq.answer()
            _, _, task_id_str, when = data.split(':')
            tid = int(task_id_str)
            task = db.get_task(tid)
            if not task:
                await reply(update, f"Task #{tid} no longer exists.")
                return True
            if when == 'tomorrow':
                tomorrow = (datetime.now(ZoneInfo(config.TIMEZONE)).date() + timedelta(days=1)).isoformat()
                db.update_task(tid, 'due_date', tomorrow)
                await reply(update, f"Moved task #{tid} ('{task.title}') to tomorrow ({tomorrow}). /undo to revert.")
            return True
        elif data.startswith('corr:pick_task:'):
            await cq.answer()
            _, _, prop_id, task_id_str = data.split(':')
            tid = int(task_id_str)
            target_task = db.get_task(tid)
            if not target_task:
                await reply(update, f"Task #{tid} no longer exists.")
                return True
            prop = db.get_nl_proposal(prop_id)
            if not prop or prop['status'] != 'pending':
                await reply(update, "Proposal expired or already resolved.")
                return True
            p_dict = prop['proposal']
            p_dict['task_id'] = tid
            if p_dict.get('field') == 'status_and_testing':
                p_dict['before'] = {'status': target_task.status.value, 'blocked_reason': target_task.blocked_reason}
                env = p_dict['after'].get('environment', '')
                with db.connect() as conn:
                    conn.execute("UPDATE nl_proposals SET proposal_json=? WHERE id=?", (json.dumps(p_dict), prop_id))
                buttons = [
                    [InlineKeyboardButton('Confirm Correction', callback_data=f'corr:confirm:{prop_id}'),
                     InlineKeyboardButton('Cancel', callback_data=f'corr:cancel:{prop_id}')]
                ]
                await reply(update,
                            f"Proposed correction for Task #{target_task.id} ('{target_task.title}'):\n"
                            f"• Status: {target_task.status.value} → blocked\n"
                            f"• Reason: Failed on {env}\n\nConfirm to apply?",
                            InlineKeyboardMarkup(buttons))
                return True
            elif p_dict.get('field') == 'client':
                p_dict['before'] = {'client': target_task.client}
                other_client = p_dict['after'].get('client')
                with db.connect() as conn:
                    conn.execute("UPDATE nl_proposals SET proposal_json=? WHERE id=?", (json.dumps(p_dict), prop_id))
                buttons = [
                    [InlineKeyboardButton('Confirm Correction', callback_data=f'corr:confirm:{prop_id}'),
                     InlineKeyboardButton('Cancel', callback_data=f'corr:cancel:{prop_id}')]
                ]
                await reply(update,
                            f"Proposed correction for Task #{target_task.id} ('{target_task.title}'):\n"
                            f"• Client: {target_task.client or 'Unassigned'} → {other_client}\n\nConfirm to apply?",
                            InlineKeyboardMarkup(buttons))
                return True
        elif data.startswith('corr:pick_client:'):
            await cq.answer()
            _, _, prop_id, client_name = data.split(':')
            prop = db.get_nl_proposal(prop_id)
            if not prop or prop['status'] != 'pending':
                await reply(update, "Proposal expired or already resolved.")
                return True
            p_dict = prop['proposal']
            p_dict['after']['client'] = client_name
            target_task = db.get_task(p_dict['task_id'])
            with db.connect() as conn:
                conn.execute("UPDATE nl_proposals SET proposal_json=? WHERE id=?", (json.dumps(p_dict), prop_id))
            buttons = [
                [InlineKeyboardButton('Confirm Correction', callback_data=f'corr:confirm:{prop_id}'),
                 InlineKeyboardButton('Cancel', callback_data=f'corr:cancel:{prop_id}')]
            ]
            title_text = f"Task #{target_task.id} ('{target_task.title}')" if target_task else "Task"
            before_c = p_dict.get('before', {}).get('client') or 'Unassigned'
            await reply(update,
                        f"Proposed correction for {title_text}:\n"
                        f"• Client: {before_c} → {client_name}\n\nConfirm to apply?",
                        InlineKeyboardMarkup(buttons))
            return True
        elif data.startswith('corr:confirm:'):
            await cq.answer()
            prop_id = data.split(':')[2]
            await execute_factual_correction(update, context, db, prop_id, owner_id)
            return True
        elif data.startswith('corr:cancel:'):
            await cq.answer()
            prop_id = data.split(':')[2]
            db.cancel_nl_proposal(prop_id, owner_id)
            await reply(update, 'Proposed correction cancelled.')
            return True
        elif data.startswith('late:shift:'):
            await cq.answer()
            parts = data.split(':')
            prop_id = parts[2]
            sid = int(parts[3])
            prop = db.get_nl_proposal(prop_id)
            if not prop or prop['status'] != 'pending':
                await reply(update, "Proposal expired or already resolved.")
                return True
            claimed = db.claim_nl_proposal(prop_id, owner_id)
            p_dict = claimed['proposal']
            detail = p_dict['detail']
            category = p_dict['category']
            occurred_at = p_dict.get('occurred_at')
            precision = p_dict.get('precision', 'exact')
            target_shift = db.shift(sid)
            active_s = db.active_shift()
            is_closed = bool(target_shift and (target_shift.get('closed_at') or (active_s and sid != active_s['id'])))
            aid = db.add_activity(sid, category, detail, occurred_at=occurred_at, time_precision=precision)
            db.finish_nl_proposal(prop_id, 'accepted')
            occ_display = occurred_at[:16].replace('T', ' ') if occurred_at else 'unknown'
            if is_closed:
                await reply(update,
                            f"Logged late work #{aid} ('{detail}') [category={category}, occurred={occ_display}, precision={precision}] for finalized Shift #{sid}.\n"
                            f"Existing finalized EOD is immutable. Generate a new revision with /eod revision.")
            else:
                await reply(update,
                            f"Logged late work #{aid} ('{detail}') [category={category}, occurred={occ_display}, precision={precision}]. /undo to revert.")
            return True
        elif data.startswith('late:cancel:'):
            await cq.answer()
            prop_id = data.split(':')[2]
            db.cancel_nl_proposal(prop_id, owner_id)
            await reply(update, 'Late work entry cancelled.')
            return True
        return False

    text = (getattr(update.effective_message, 'text', None) or '').strip()
    if not text:
        return False

    low = text.casefold().rstrip('.!')
    shift = db.active_shift()

    # --- 1. /cancel or explicit cancellation ---
    if low in ('/cancel', 'cancel planning', 'cancel my day', 'exit planning'):
        active_plan = db.get_active_planning_conversation(owner_id)
        if active_plan:
            db.cancel_planning_conversation(owner_id)
            await reply(update, 'Daily planning cancelled. Unfinished tasks remain available with /todo.')
            return True
        if low == '/cancel':
            await reply(update, 'No active conversation to cancel.')
            return True

    # --- 2. Deletion handling (Preserving existing owner-authorized commands) ---
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

    # --- 3. Multi-turn Active Planning Conversation Continuation ---
    active_conv = db.get_active_planning_conversation(owner_id)
    if active_conv and not text.startswith('/'):
        handled = await continue_planning_conversation(update, context, db, active_conv, text)
        if handled:
            return True

    # --- 4. Task Linking Commands & Natural Phrasing ---
    link_match = re.search(r'\b(?:link|connect)\s+task\s+#?(\d+)\s+(?:to\s+)?(draft|case|request)\s+#?(\d+)\b', low)
    unlink_match = re.search(r'\b(?:unlink|disconnect)\s+task\s+#?(\d+)\s+(?:from\s+)?(draft|case|request)\s+#?(\d+)\b', low)
    if link_match or unlink_match:
        m = link_match or unlink_match
        task_id = int(m.group(1))
        target_kind = 'work_draft' if m.group(2) in ('draft', 'request') else 'work_case'
        target_id = int(m.group(3))
        task = db.get_task(task_id)
        if not task:
            await reply(update, f"Task #{task_id} not found.")
            return True
        if link_match:
            db.link_records('task', task_id, target_kind, target_id)
            target_label = f"Draft #{target_id}" if target_kind == 'work_draft' else f"CASE-{target_id}"
            await reply(update, f"Linked Task #{task_id} ('{task.title}') to {target_label}.")
        else:
            db.unlink_records('task', task_id, target_kind, target_id)
            target_label = f"Draft #{target_id}" if target_kind == 'work_draft' else f"CASE-{target_id}"
            await reply(update, f"Unlinked Task #{task_id} from {target_label}.")
        return True

    if text.startswith('/linktask') or text.startswith('/unlinktask'):
        is_unlink = text.startswith('/unlinktask')
        parts = text.split()[1:]
        if not parts:
            await reply(update, 'Usage: /linktask <task_id> draft=<id> OR /linktask <task_id> case=<id>')
            return True
        task_id = int(re.search(r'\d+', parts[0]).group())
        target_type, target_id = None, None
        for p in parts[1:]:
            if p.startswith('draft='):
                target_type, target_id = 'work_draft', int(p.split('=')[1])
            elif p.startswith('case='):
                target_type, target_id = 'work_case', int(p.split('=')[1])
        if not target_type or not target_id:
            await reply(update, 'Specify draft=ID or case=ID.')
            return True
        task = db.get_task(task_id)
        if not task:
            await reply(update, f"Task #{task_id} not found.")
            return True
        if is_unlink:
            db.unlink_records('task', task_id, target_type, target_id)
            await reply(update, f"Unlinked Task #{task_id} from {target_type} #{target_id}.")
        else:
            db.link_records('task', task_id, target_type, target_id)
            await reply(update, f"Linked Task #{task_id} to {target_type} #{target_id}.")
        return True

    # --- 5. Startday initiation (/startday or natural startday) ---
    is_startday_cmd = text.startswith('/startday')
    is_startday_natural = bool(re.search(r'\b(?:start\s+my\s+day|startday|start\s+shift)\b', low))

    if is_startday_cmd or is_startday_natural:
        raw_input = text[len('/startday'):].strip() if is_startday_cmd else text
        return await initiate_startday_planning(update, context, db, owner_id, raw_input)

    # --- 6. Checkpoint, Timeline, Resume, Tomorrow, Carrytask Commands ---
    head, _, argument = text.partition(' ')
    command = head.lower().split('@')[0]
    if command in ('/checkpoint', '/timeline', '/resume', '/tomorrow', '/carrytask'):
        if not shift and command != '/tomorrow':
            await reply(update, 'Start a shift first with /startday or /shift START END.')
            return True

        if command == '/checkpoint':
            await run_checkpoint_flow(update, context, db, shift)
            return True

        elif command == '/timeline':
            rows = db.activities(shift['id'])
            lines = []
            for r in rows:
                time_disp = r.get('occurred_at') or r['created_at']
                prec = f" [{r.get('time_precision')}]" if r.get('time_precision') and r.get('time_precision') != 'exact' else ''
                lines.append(f"{time_disp[:16].replace('T', ' ')} · #{r['id']} [{r['category']}]{prec} {r['detail']}")
            await reply(update, '\n'.join(lines) or 'No work recorded in this shift.')
            return True

        elif command == '/carrytask':
            if not argument.strip():
                await reply(update, 'Usage: /carrytask <id>')
                return True
            task = db.get_task(int(argument.strip()))
            if not task:
                raise ValueError('Task not found.')
            due = (datetime.now(ZoneInfo(config.TIMEZONE)).date() + timedelta(days=1)).isoformat()
            db.update_task(task.id, 'due_date', due)
            db.record_audit('carry-' + uuid.uuid4().hex, 'carry_task', 'owner', 'tasks', task.id,
                            json.dumps({'due_date': task.due_date}), json.dumps({'due_date': due}))
            await reply(update, f'Task #{task.id} rescheduled to {due}; status and blocker preserved. /undo to revert.')
            return True

        elif command == '/tomorrow':
            tasks = [t for t in db.list_tasks() if t.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED)]
            tasks.sort(key=lambda t: (-t.priority, t.id))
            lines = []
            for t in tasks:
                line = f"#{t.id} [{t.status.value}] {t.title}"
                if t.blocked_reason:
                    line += f" — blocked: {t.blocked_reason}"
                if t.next_action:
                    line += f" — next: {t.next_action}"
                links = db.get_linked_records('task', t.id)
                if links:
                    line += f" (linked: {', '.join(f'{l["other_type"]}:{l["other_id"]}' for l in links)})"
                lines.append(line)
            await reply(update, "Unfinished work for review:\n" + ('\n'.join(lines) or 'No unfinished tasks.') +
                        "\n\nActions: /carrytask ID to move to tomorrow, /edittask ID | priority | VALUE, or /todo.")
            return True

        elif command == '/resume':
            tasks = [t for t in db.list_tasks() if t.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED)]
            tasks.sort(key=lambda t: (-t.priority, t.id))
            await reply(update, '\n'.join(f'#{t.id} [{t.status.value}] {t.title}' +
                (f' — blocked: {t.blocked_reason}' if t.blocked_reason else '') +
                (f' — next: {t.next_action}' if t.next_action else '') for t in tasks) or 'No unfinished tasks.')
            return True

    # --- 7. Natural Task Updates: Started, Finished, Blocked, Retest ---
    started_match = re.fullmatch(r'(?:started|start working on|working on)\s+(.+)', text, re.I)
    finished_match = re.fullmatch(r'(?:finished|completed|done with)\s+(.+)', text, re.I)
    blocked_match = re.fullmatch(r'(.+?)\s+is\s+blocked\s+because\s+(.+)', text, re.I)
    waiting_match = re.fullmatch(r'waiting\s+for\s+(.+)', text, re.I)

    if started_match or finished_match or blocked_match or waiting_match:
        target_ref = (started_match.group(1) if started_match else
                      finished_match.group(1) if finished_match else
                      blocked_match.group(1) if blocked_match else None)

        task = None
        candidates = []
        if target_ref:
            task, candidates = resolve_task_reference(target_ref, db, shift['id'] if shift else None)
        elif waiting_match:
            ctx = db.get_conversation_context('owner')
            if ctx and ctx.get('active_task_id'):
                task = db.get_task(ctx['active_task_id'])

        if not task:
            if len(candidates) > 1:
                cand_lines = [f"• Task #{c.id}: {c.title}" for c in candidates[:5]]
                await reply(update, f"Multiple matching tasks found. Please specify the task ID:\n" + "\n".join(cand_lines))
                return True
            if target_ref:
                await reply(update, f"Could not find a task matching '{target_ref}'. Use /todo to view open tasks.")
                return True

        if task:
            if started_match:
                db.mark_status(task.id, TaskStatus.IN_PROGRESS, shift_id=shift['id'] if shift else None,
                               correlation_id='daily-' + uuid.uuid4().hex, actor='owner')
                db.save_conversation_context('owner', active_task_id=task.id)
                await reply(update, f"Started working on Task #{task.id}: '{task.title}'. /undo to revert.")
                return True
            elif finished_match:
                db.mark_status(task.id, TaskStatus.COMPLETED, shift_id=shift['id'] if shift else None,
                               correlation_id='daily-' + uuid.uuid4().hex, actor='owner')
                await reply(update, f"Marked Task #{task.id} completed: '{task.title}'. /undo to revert.")
                return True
            elif blocked_match:
                reason = blocked_match.group(2).strip()
                db.mark_status(task.id, TaskStatus.BLOCKED, blocked_reason=reason,
                               shift_id=shift['id'] if shift else None, correlation_id='daily-' + uuid.uuid4().hex, actor='owner')
                await reply(update, f"Marked Task #{task.id} blocked ({reason}). /undo to revert.")
                return True

    # --- 8. Factual Corrections ---
    corr_fail_match = re.search(r'\bactually,?\s+it\s+failed\s+(?:on\s+)?(.+)', text, re.I)
    corr_client_match = re.search(r'\bthat\s+was\s+for\s+(?:the\s+other\s+client|client\s+(.+))\b', text, re.I)
    move_retest_match = re.search(r'\bmove\s+(?:the\s+)?retest\s+to\s+tomorrow\b', text, re.I)

    if move_retest_match:
        test_tasks = [t for t in db.list_tasks() if 'test' in t.title.casefold() and t.status != TaskStatus.COMPLETED]
        if not test_tasks:
            await reply(update, "No active retest tasks found to move.")
            return True
        elif len(test_tasks) == 1:
            t = test_tasks[0]
            tomorrow = (datetime.now(ZoneInfo(config.TIMEZONE)).date() + timedelta(days=1)).isoformat()
            db.update_task(t.id, 'due_date', tomorrow)
            await reply(update, f"Moved retest task #{t.id} ('{t.title}') to tomorrow ({tomorrow}). /undo to revert.")
            return True
        else:
            ctx = db.get_conversation_context('owner')
            active_tid = ctx.get('active_task_id') if ctx else None
            matching_active = [t for t in test_tasks if t.id == active_tid]
            if matching_active:
                t = matching_active[0]
                tomorrow = (datetime.now(ZoneInfo(config.TIMEZONE)).date() + timedelta(days=1)).isoformat()
                db.update_task(t.id, 'due_date', tomorrow)
                await reply(update, f"Moved retest task #{t.id} ('{t.title}') to tomorrow ({tomorrow}). /undo to revert.")
                return True
            buttons = [
                [InlineKeyboardButton(f"#{t.id}: {t.title[:30]}", callback_data=f"task:reschedule:{t.id}:tomorrow")]
                for t in test_tasks[:5]
            ]
            cand_lines = [f"• #{t.id} {t.title}" for t in test_tasks[:5]]
            await reply(update, "Multiple active test tasks found. Which retest would you like to move to tomorrow?\n" + "\n".join(cand_lines),
                        InlineKeyboardMarkup(buttons))
            return True

    if corr_fail_match or corr_client_match:
        ctx = db.get_conversation_context('owner')
        active_tid = ctx.get('active_task_id') if ctx else None
        target_task = db.get_task(active_tid) if active_tid else None

        explicit_id = re.search(r'#?(\d+)', text)
        if not target_task and explicit_id:
            tid_cand = int(explicit_id.group(1))
            t_cand = db.get_task(tid_cand)
            if t_cand and t_cand.status != TaskStatus.CANCELLED:
                target_task = t_cand

        open_tasks = [t for t in db.list_tasks() if t.status not in (TaskStatus.COMPLETED, TaskStatus.CANCELLED)]
        if not open_tasks:
            open_tasks = [t for t in db.list_tasks() if t.status != TaskStatus.CANCELLED][-5:]

        if corr_fail_match:
            env = corr_fail_match.group(1).strip()
            if not target_task:
                if not open_tasks:
                    await reply(update, "No tasks found to apply this failure correction to.")
                    return True
                proposal = {
                    'action': 'factual_correction',
                    'field': 'status_and_testing',
                    'after': {'status': 'blocked', 'blocked_reason': f'Failed on {env}', 'environment': env}
                }
                prop_id = db.create_nl_proposal(owner_id=owner_id, intent='factual_correction', proposal_dict=proposal)
                buttons = [
                    [InlineKeyboardButton(f"#{t.id}: {t.title[:30]}", callback_data=f"corr:pick_task:{prop_id}:{t.id}")]
                    for t in open_tasks[:5]
                ]
                buttons.append([InlineKeyboardButton("Cancel", callback_data=f"corr:cancel:{prop_id}")])
                await reply(update, f"Which task failed on {env}? Please select a task:", InlineKeyboardMarkup(buttons))
                return True

            proposal = {
                'action': 'factual_correction',
                'task_id': target_task.id,
                'field': 'status_and_testing',
                'before': {'status': target_task.status.value, 'blocked_reason': target_task.blocked_reason},
                'after': {'status': 'blocked', 'blocked_reason': f'Failed on {env}', 'environment': env}
            }
            prop_id = db.create_nl_proposal(owner_id=owner_id, intent='factual_correction', proposal_dict=proposal)
            buttons = [
                [InlineKeyboardButton('Confirm Correction', callback_data=f'corr:confirm:{prop_id}'),
                 InlineKeyboardButton('Cancel', callback_data=f'corr:cancel:{prop_id}')]
            ]
            await reply(update,
                        f"Proposed correction for Task #{target_task.id} ('{target_task.title}'):\n"
                        f"• Status: {target_task.status.value} → blocked\n"
                        f"• Reason: Failed on {env}\n\nConfirm to apply?",
                        InlineKeyboardMarkup(buttons))
            return True

        if corr_client_match:
            named_client = corr_client_match.group(1)
            if named_client:
                named_client = named_client.strip()

            if not target_task:
                if not open_tasks:
                    await reply(update, "No tasks found to apply this client correction to.")
                    return True
                if not named_client:
                    await reply(update, "Ambiguous client reference. Please specify: 'That was for client <name> on task #<id>'.")
                    return True
                proposal = {
                    'action': 'factual_correction',
                    'field': 'client',
                    'after': {'client': named_client}
                }
                prop_id = db.create_nl_proposal(owner_id=owner_id, intent='factual_correction', proposal_dict=proposal)
                buttons = [
                    [InlineKeyboardButton(f"#{t.id}: {t.title[:30]}", callback_data=f"corr:pick_task:{prop_id}:{t.id}")]
                    for t in open_tasks[:5]
                ]
                buttons.append([InlineKeyboardButton("Cancel", callback_data=f"corr:cancel:{prop_id}")])
                await reply(update, f"Which task was for client {named_client}? Please select a task:", InlineKeyboardMarkup(buttons))
                return True

            if named_client:
                other_client = named_client
            else:
                known_clients = [c for c in get_known_clients(db) if c.casefold() != (target_task.client or '').casefold()]
                if len(known_clients) == 1:
                    other_client = known_clients[0]
                elif len(known_clients) == 0:
                    await reply(update, f"No other client known in the system for Task #{target_task.id}. Please specify: 'That was for client <name>'.")
                    return True
                else:
                    proposal = {
                        'action': 'factual_correction',
                        'task_id': target_task.id,
                        'field': 'client',
                        'before': {'client': target_task.client},
                        'after': {}
                    }
                    prop_id = db.create_nl_proposal(owner_id=owner_id, intent='factual_correction', proposal_dict=proposal)
                    buttons = [
                        [InlineKeyboardButton(c, callback_data=f"corr:pick_client:{prop_id}:{c}")]
                        for c in known_clients[:5]
                    ]
                    buttons.append([InlineKeyboardButton("Cancel", callback_data=f"corr:cancel:{prop_id}")])
                    await reply(update, f"Which client was Task #{target_task.id} ('{target_task.title}') for?", InlineKeyboardMarkup(buttons))
                    return True

            proposal = {
                'action': 'factual_correction',
                'task_id': target_task.id,
                'field': 'client',
                'before': {'client': target_task.client},
                'after': {'client': other_client}
            }
            prop_id = db.create_nl_proposal(owner_id=owner_id, intent='factual_correction', proposal_dict=proposal)
            buttons = [
                [InlineKeyboardButton('Confirm Correction', callback_data=f'corr:confirm:{prop_id}'),
                 InlineKeyboardButton('Cancel', callback_data=f'corr:cancel:{prop_id}')]
            ]
            await reply(update,
                        f"Proposed correction for Task #{target_task.id} ('{target_task.title}'):\n"
                        f"• Client: {target_task.client or 'Unassigned'} → {other_client}\n\nConfirm to apply?",
                        InlineKeyboardMarkup(buttons))
            return True

    # --- 9. Late Work Entry (Stage D) ---
    late_yesterday = re.search(r'\byesterday\s+at\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)\s+i\s+(.+)', text, re.I)
    late_before_lunch = re.search(r'\bi\s+(?:handled|worked\s+on|did)\s+(.+?)\s+before\s+lunch\b', text, re.I)
    late_prev_shift = re.search(r'\bthis\s+happened\s+during\s+my\s+previous\s+shift:?\s*(.+)?', text, re.I)

    if late_yesterday or late_before_lunch or late_prev_shift:
        tz = ZoneInfo(config.TIMEZONE)
        now_dt = datetime.now(tz)
        occurred_at = None
        precision = 'exact'
        detail = ''

        if late_yesterday:
            t_str = parse_time_flexible(late_yesterday.group(1))
            detail = late_yesterday.group(2).strip()
            if t_str:
                h, m = map(int, t_str.split(':'))
                yest = (now_dt - timedelta(days=1)).replace(hour=h, minute=m, second=0, microsecond=0)
                occurred_at = yest.isoformat()

        elif late_before_lunch:
            detail = late_before_lunch.group(1).strip()
            precision = 'approximate'
            if shift and shift.get('lunch'):
                l_dt = datetime.fromisoformat(shift['lunch'])
                if l_dt.tzinfo is None:
                    l_dt = l_dt.replace(tzinfo=tz)
                occurred_at = (l_dt - timedelta(minutes=30)).isoformat()
            else:
                occurred_at = now_dt.isoformat()

        elif late_prev_shift:
            detail = (late_prev_shift.group(1) or 'Support assistance').strip()
            precision = 'approximate'
            occurred_at = (now_dt - timedelta(days=1)).isoformat()

        category = categorize_late_detail(detail)
        all_shifts = db.list_shifts(limit=20)
        target_shift = None

        if late_before_lunch:
            target_shift = shift
        elif late_prev_shift:
            prev_shifts = [s for s in all_shifts if s.get('closed_at') or (shift and s['id'] != shift['id'])]
            if len(prev_shifts) == 1:
                target_shift = prev_shifts[0]
            elif len(prev_shifts) > 1:
                proposal = {'action': 'late_work', 'detail': detail, 'category': category, 'occurred_at': occurred_at, 'precision': precision}
                prop_id = db.create_nl_proposal(owner_id=owner_id, intent='late_work', proposal_dict=proposal)
                buttons = [
                    [InlineKeyboardButton(f"Shift #{s['id']} ({s['start'][:10]} {s['start'][11:16]}-{s.get('end', '')[11:16] or 'closed'})", callback_data=f"late:shift:{prop_id}:{s['id']}")]
                    for s in prev_shifts[:4]
                ]
                buttons.append([InlineKeyboardButton('Cancel', callback_data=f"late:cancel:{prop_id}")])
                await reply(update, f"Multiple previous shifts found for late work ('{detail}'). Which shift does this belong to?", InlineKeyboardMarkup(buttons))
                return True
        elif occurred_at:
            occ_dt = datetime.fromisoformat(occurred_at)
            if occ_dt.tzinfo is None:
                occ_dt = occ_dt.replace(tzinfo=tz)

            matching_shifts = []
            for s in all_shifts:
                s_start = datetime.fromisoformat(s['start'])
                if s_start.tzinfo is None:
                    s_start = s_start.replace(tzinfo=tz)
                s_end_str = s.get('closed_at') or s.get('end')
                s_end = datetime.fromisoformat(s_end_str)
                if s_end.tzinfo is None:
                    s_end = s_end.replace(tzinfo=tz)
                if s_end < s_start:
                    s_end += timedelta(days=1)

                if (s_start - timedelta(minutes=30)) <= occ_dt <= (s_end + timedelta(minutes=30)):
                    matching_shifts.append(s)

            if len(matching_shifts) == 1:
                target_shift = matching_shifts[0]
            else:
                candidate_shifts = matching_shifts if len(matching_shifts) > 1 else all_shifts[:4]
                if not candidate_shifts:
                    await reply(update, f"Could not determine shift for late work ('{detail}'). Please start a shift first.")
                    return True
                proposal = {'action': 'late_work', 'detail': detail, 'category': category, 'occurred_at': occurred_at, 'precision': precision}
                prop_id = db.create_nl_proposal(owner_id=owner_id, intent='late_work', proposal_dict=proposal)
                buttons = [
                    [InlineKeyboardButton(f"Shift #{s['id']} ({s['start'][:10]} {s['start'][11:16]}-{s.get('end', '')[11:16] or ('active' if not s.get('closed_at') else 'closed')})", callback_data=f"late:shift:{prop_id}:{s['id']}")]
                    for s in candidate_shifts
                ]
                buttons.append([InlineKeyboardButton('Cancel', callback_data=f"late:cancel:{prop_id}")])
                msg_reason = "Multiple shifts match this timestamp" if len(matching_shifts) > 1 else "No shift directly matched this timestamp"
                await reply(update, f"{msg_reason} for late work ('{detail}'). Which shift does this belong to?", InlineKeyboardMarkup(buttons))
                return True

        if target_shift:
            sid = target_shift['id']
            is_closed = bool(target_shift.get('closed_at') or (shift and sid != shift['id']))
            aid = db.add_activity(sid, category, detail, occurred_at=occurred_at, time_precision=precision)
            occ_display = occurred_at[:16].replace('T', ' ') if occurred_at else 'unknown'

            if is_closed:
                await reply(update,
                            f"Logged late work #{aid} ('{detail}') [category={category}, occurred={occ_display}, precision={precision}] for finalized Shift #{sid}.\n"
                            f"Existing finalized EOD is immutable. Generate a new revision with /eod revision.")
            else:
                await reply(update,
                            f"Logged late work #{aid} ('{detail}') [category={category}, occurred={occ_display}, precision={precision}]. /undo to revert.")
            return True
        else:
            await reply(update, f"Could not determine shift for late work ('{detail}'). Please start a shift first.")
            return True

    # --- 10. Report Wording Review (Stage G) ---
    short_match = re.search(r'\bmake\s+(?:the\s+)?(?:eod|report)\s+shorter\b', text, re.I)
    bullets_match = re.search(r'\buse\s+bullet\s+points\b', text, re.I)
    env_match = re.search(r'\badd\s+(?:the\s+)?testing\s+environment\b', text, re.I)
    if short_match or bullets_match or env_match:
        if shift:
            reports_list = [r for r in db.list_reports(shift['id']) if r['kind'] == 'eod']
            if reports_list:
                last_rep = reports_list[-1]
                style = 'short' if short_match else 'detailed' if env_match else 'standard'
                snap_json = last_rep.get('facts_snapshot_json')
                if snap_json:
                    snap = json.loads(snap_json)
                    shift_data = snap.get('shift', shift)
                    activities = snap.get('activities', [])
                    from models import Task
                    tasks = [Task.from_row(t) if isinstance(t, dict) else t for t in snap.get('tasks', [])]
                    cases = snap.get('cases', [])
                    sessions = snap.get('sessions', [])
                    baseline_tasks = snap.get('baseline_snapshot')
                    mask = snap.get('mask', False)
                else:
                    shift_data = shift
                    activities = db.activities(shift['id'])
                    tasks = db.tasks_for_shift(shift['id'])
                    cases = db.cases_for_shift(shift['id'])
                    sessions = db.test_sessions(shift['id'])
                    baseline_snap = db.get_baseline_plan_snapshot(shift['id'])
                    baseline_tasks = baseline_snap.get('tasks') if baseline_snap else None
                    mask = (db.get_setting('mask_client_names') == 'true')
                from reports import generate_report
                new_text = generate_report('eod', shift_data, activities, tasks, style=style,
                                           mask_clients=mask, cases=cases, test_sessions=sessions,
                                           baseline_snapshot=baseline_tasks)
                rev_id = db.create_report_revision(last_rep['id'], new_text, style=style,
                                                   facts_hash=last_rep.get('facts_hash'),
                                                   facts_snapshot=snap_json)
                await reply(update, f"Updated EOD (Revision #{rev_id}):\n\n{new_text}")
                return True

    return False


# ---------------------------------------------------------------------------
# Stage A: Planning Flow Implementation
# ---------------------------------------------------------------------------

async def initiate_startday_planning(update, context, db, owner_id: int, text: str) -> bool:
    from handlers import reply

    shift_range = parse_shift_range_flexible(text)
    lunch_time = parse_lunch_flexible(text)

    tasks = []
    if '|' in text:
        parts = [p.strip() for p in text.split('|') if p.strip()]
        for p in parts:
            if not parse_shift_range_flexible(p) and not parse_lunch_flexible(p):
                subparts = [sp.strip() for sp in re.split(r'[,;]|\b(?:and|also)\b', p) if sp.strip()]
                for sp in subparts:
                    if not parse_shift_range_flexible(sp) and not parse_lunch_flexible(sp):
                        tasks.append(sp)
    else:
        task_match = re.search(r'\b(?:need\s+to|have\s+to|tasks?:?)\s+(.+)', text, re.I)
        if task_match:
            raw_tasks = task_match.group(1)
            split_items = re.split(r'\b(?:and|also)\b|[,;]', raw_tasks)
            for it in split_items:
                cleaned = it.strip().rstrip('.!')
                if cleaned and len(cleaned) > 3 and not parse_shift_range_flexible(cleaned):
                    tasks.append(cleaned)

    if not shift_range:
        db.save_planning_conversation(
            owner_id=owner_id,
            purpose='daily_planning',
            step='awaiting_shift_hours',
            proposed_values={'tasks': tasks, 'lunch': lunch_time},
            source_update_id=update.update_id
        )
        await reply(update, "What are your shift hours today? (e.g. 12 to 9 or 10am to 7pm)")
        return True

    start_str, end_str = shift_range
    if not lunch_time:
        db.save_planning_conversation(
            owner_id=owner_id,
            purpose='daily_planning',
            step='awaiting_lunch',
            proposed_values={
                'shift_start': start_str,
                'shift_end': end_str,
                'tasks': tasks
            },
            source_update_id=update.update_id
        )
        await reply(update, f"Shift set to {start_str}–{end_str}. When is your lunch break scheduled? (e.g. at 4pm)")
        return True

    return await present_proposed_plan(update, context, db, owner_id, {
        'shift_start': start_str,
        'shift_end': end_str,
        'lunch': lunch_time,
        'tasks': tasks
    })


async def continue_planning_conversation(update, context, db, active_conv: dict, text: str) -> bool:
    from handlers import reply

    step = active_conv['step']
    values = active_conv['proposed_values']
    owner_id = active_conv['owner_id']

    if step == 'awaiting_shift_hours':
        rng = parse_shift_range_flexible(text)
        if not rng:
            await reply(update, "Please state your shift hours as START to END (e.g. 12 to 9). /cancel to abort.")
            return True
        values['shift_start'], values['shift_end'] = rng
        if not values.get('lunch'):
            active_conv['step'] = 'awaiting_lunch'
            db.save_planning_conversation(owner_id, 'daily_planning', 'awaiting_lunch', values)
            await reply(update, f"Got shift {rng[0]}–{rng[1]}. When is your lunch break? (e.g. lunch at 4)")
            return True
        return await present_proposed_plan(update, context, db, owner_id, values)

    elif step == 'awaiting_lunch':
        lunch = parse_time_flexible(text) or parse_lunch_flexible(text)
        if not lunch and 'no lunch' not in text.lower():
            await reply(update, "Please state your lunch time (e.g. at 4pm) or say 'no lunch'. /cancel to abort.")
            return True
        values['lunch'] = lunch
        return await present_proposed_plan(update, context, db, owner_id, values)

    elif step == 'awaiting_confirmation':
        low = text.strip().lower()
        if low in ('confirm', 'yes', 'apply', 'ok', 'proceed', 'sure', 'start'):
            await execute_plan_confirmation(update, context, db, active_conv['id'])
            return True
        elif low in ('cancel', 'no', 'stop'):
            db.cancel_planning_conversation(owner_id)
            await reply(update, 'Daily planning cancelled.')
            return True

    return False


async def present_proposed_plan(update, context, db, owner_id: int, values: dict) -> bool:
    from handlers import reply
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    start_str = values['shift_start']
    end_str = values['shift_end']
    lunch_str = values.get('lunch')
    proposed_tasks = values.get('tasks') or []

    existing_tasks = [t for t in db.list_tasks() if t.status != TaskStatus.CANCELLED]
    existing_by_clean = {t.title.casefold().strip(): t for t in existing_tasks}

    task_lines = []
    final_task_plan = []
    for t_title in proposed_tasks:
        clean = t_title.casefold().strip()
        if clean in existing_by_clean:
            ex = existing_by_clean[clean]
            task_lines.append(f"• [Existing #{ex.id}] {t_title}")
            final_task_plan.append({'id': ex.id, 'title': ex.title, 'is_new': False})
        else:
            sim = find_similar_task(t_title, existing_tasks)
            if sim:
                task_lines.append(f"• [Review similar #{sim.id}: '{sim.title}'] {t_title}")
                final_task_plan.append({'id': None, 'title': t_title, 'similar_id': sim.id, 'is_new': True})
            else:
                task_lines.append(f"• [New] {t_title}")
                final_task_plan.append({'id': None, 'title': t_title, 'is_new': True})

    open_tasks = [t for t in existing_tasks if t.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED)]
    open_lines = [f"#{t.id} [{t.status.value}] {t.title}" for t in open_tasks[:5]]

    values['task_plan'] = final_task_plan
    conv_id = db.save_planning_conversation(owner_id, 'daily_planning', 'awaiting_confirmation', values)

    summary_lines = [
        "Proposed Daily Plan:",
        f"• Shift: {start_str} to {end_str}" + (f" (Lunch: {lunch_str})" if lunch_str else ""),
        "• Planned items:"
    ]
    summary_lines.extend(task_lines or ["• None specified (open tasks carry forward)"])
    if open_lines:
        summary_lines.append("• Open carried tasks:")
        summary_lines.extend(open_lines)

    summary_lines.append("\nConfirm this plan to start your shift and lock in the TOD baseline.")

    buttons = [
        [InlineKeyboardButton("Confirm Plan", callback_data=f"plan:confirm:{conv_id}"),
         InlineKeyboardButton("Cancel", callback_data=f"plan:cancel:{conv_id}")]
    ]
    await reply(update, '\n'.join(summary_lines), InlineKeyboardMarkup(buttons))
    return True


async def execute_plan_confirmation(update, context, db, conv_id: int):
    from handlers import reply

    with db.connect() as conn:
        row = conn.execute('SELECT * FROM planning_conversations WHERE id=?', (conv_id,)).fetchone()
        if not row:
            await reply(update, 'Planning session expired or not found.')
            return
        conv = dict(row)
        if conv['status'] != 'active':
            await reply(update, f"Planning session is already {conv['status']}.")
            return

    values = json.loads(conv['proposed_values_json'])
    owner_id = conv['owner_id']

    tz = ZoneInfo(config.TIMEZONE)
    now_dt = datetime.now(tz)
    start_time_str = values['shift_start']
    end_time_str = values['shift_end']
    lunch_time_str = values.get('lunch')

    sh, sm = map(int, start_time_str.split(':'))
    eh, em = map(int, end_time_str.split(':'))

    start_dt = now_dt.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end_dt = now_dt.replace(hour=eh, minute=em, second=0, microsecond=0)
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)

    lunch_dt = None
    if lunch_time_str:
        lh, lm = map(int, lunch_time_str.split(':'))
        lunch_dt = now_dt.replace(hour=lh, minute=lm, second=0, microsecond=0)
        if lunch_dt < start_dt:
            lunch_dt += timedelta(days=1)

    task_plan = values.get('task_plan') or []
    sid, confirmed_tasks = db.confirm_daily_plan_atomic(
        owner_id, conv_id, start_dt.isoformat(), end_dt.isoformat(),
        lunch_dt.isoformat() if lunch_dt else None, task_plan
    )

    lines = [
        f"Plan confirmed! Shift #{sid} active ({start_time_str} to {end_time_str}).",
        f"Confirmed TOD Baseline ({len(confirmed_tasks)} tasks):"
    ]
    for t in confirmed_tasks:
        lines.append(f"• #{t.id} [{t.status.value}] {t.title}")
    lines.append("\nGenerate your Beginning of Day draft with /tod.")

    await reply(update, '\n'.join(lines))


# ---------------------------------------------------------------------------
# Stage E: Checkpoint Flow Implementation
# ---------------------------------------------------------------------------

async def run_checkpoint_flow(update, context, db, shift: dict):
    from handlers import reply, make_report
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    baseline = db.get_baseline_plan_snapshot(shift['id'])
    current_tasks = {t.id: t for t in db.list_tasks()}

    if baseline and baseline.get('tasks'):
        base_items = baseline['tasks']
        completed = []
        in_prog = []
        blocked = []
        remaining = []

        for b in base_items:
            t = current_tasks.get(b['id'])
            st = t.status if t else TaskStatus.PENDING
            title = t.title if t else b['title']
            if st == TaskStatus.COMPLETED:
                completed.append(f"#{b['id']} {title}")
            elif st == TaskStatus.IN_PROGRESS:
                in_prog.append((b['id'], title))
            elif st == TaskStatus.BLOCKED:
                blocked.append(f"#{b['id']} {title} (blocked: {getattr(t, 'blocked_reason', 'unspecified')})")
            else:
                remaining.append(f"#{b['id']} {title}")

        base_ids = {b['id'] for b in base_items}
        unplanned = [t for t in db.tasks_for_shift(shift['id']) if t.id not in base_ids]

        report_lines = ["Start-of-day plan comparison:"]
        if completed:
            report_lines.append("• Completed: " + ", ".join(completed))
        if in_prog:
            report_lines.append("• In Progress: " + ", ".join(f"#{tid} {tname}" for tid, tname in in_prog))
        if blocked:
            report_lines.append("• Blocked: " + ", ".join(blocked))
        if remaining:
            report_lines.append("• Remaining: " + ", ".join(remaining))
        if unplanned:
            report_lines.append("• Unplanned work added: " + ", ".join(f"#{t.id} {t.title}" for t in unplanned))

        await reply(update, '\n'.join(report_lines))

        if in_prog:
            first_in_prog = in_prog[0]
            tid, tname = first_in_prog
            buttons = [
                [InlineKeyboardButton("Completed", callback_data=f"checkpoint:update:{tid}:in_progress:completed"),
                 InlineKeyboardButton("Blocked", callback_data=f"checkpoint:update:{tid}:in_progress:blocked"),
                 InlineKeyboardButton("Still In Progress", callback_data=f"checkpoint:update:{tid}:in_progress:in_progress")]
            ]
            await reply(update, f"'{tname}' is still in progress. Is it completed, blocked, or still in progress?",
                        InlineKeyboardMarkup(buttons))

    await make_report(update, context, 'pl')


# ---------------------------------------------------------------------------
# Stage C & G: Factual Correction Execution
# ---------------------------------------------------------------------------

async def execute_factual_correction(update, context, db, prop_id: str, owner_id: int):
    from handlers import reply

    proposal = db.get_nl_proposal(prop_id)
    if not proposal or proposal['status'] != 'pending':
        await reply(update, "Proposal has expired or already been handled.")
        return

    claimed = db.claim_nl_proposal(prop_id, owner_id)
    payload = claimed['proposal']
    task_id = payload['task_id']
    after = payload['after']
    before = payload.get('before') or {}

    task = db.get_task(task_id)
    if not task:
        db.finish_nl_proposal(prop_id, 'failed')
        await reply(update, f"Task #{task_id} no longer exists.")
        return

    # Stale-state protection: verify before state matches current task state
    conflicts = []
    if 'status' in before and task.status.value != before['status']:
        conflicts.append(f"status changed from '{before['status']}' to '{task.status.value}'")
    if 'blocked_reason' in before and (task.blocked_reason or None) != (before['blocked_reason'] or None):
        conflicts.append(f"blocker changed from '{before['blocked_reason']}' to '{task.blocked_reason}'")
    if 'client' in before and (task.client or '') != (before['client'] or ''):
        conflicts.append(f"client changed from '{before['client']}' to '{task.client}'")

    if conflicts:
        db.finish_nl_proposal(prop_id, 'failed')
        await reply(update, f"⚠️ Stale correction rejected for Task #{task_id}: {'; '.join(conflicts)}. Current state was modified after proposal was generated.")
        return

    correlation = 'corr-' + uuid.uuid4().hex
    shift = db.active_shift()
    if 'status' in after:
        db.mark_status(task_id, TaskStatus(after['status']),
                       blocked_reason=after.get('blocked_reason'),
                       shift_id=shift['id'] if shift else None,
                       correlation_id=correlation, actor='owner')
    if 'client' in after:
        db.update_task(task_id, 'client', after['client'])
        db.record_audit(correlation, 'update_task_client', 'owner', 'tasks', task_id,
                        json.dumps({'client': task.client}), json.dumps({'client': after['client']}))

    db.finish_nl_proposal(prop_id, 'accepted')
    audit = db.get_last_reversible_audit()
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    markup = InlineKeyboardMarkup([[InlineKeyboardButton('Undo', callback_data=f"audit:undo:{audit['id']}")]]) if audit else None
    await reply(update, f"Applied factual correction on Task #{task_id}. /undo to revert.", markup)
