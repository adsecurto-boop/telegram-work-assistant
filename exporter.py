"""Owner-requested local exports with spreadsheet-formula protection."""
import csv
from datetime import datetime
from pathlib import Path

from domain import task_line


def _safe(value):
    text = '' if value is None else str(value)
    if text.startswith(('=', '+', '-', '@', '\t', '\r')):
        return "'" + text
    return text


def create_export(database, export_format, directory):
    if export_format not in ('csv', 'markdown', 'md'):
        raise ValueError('Export format must be csv or markdown.')
    target_dir = Path(directory)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    tasks = database.list_tasks()
    shifts = database.shifts_since('0001-01-01T00:00:00+00:00')
    activities = []
    for shift in shifts:
        activities.extend(database.activities(shift['id']))
    cases = database.list_cases(limit=100000)
    sessions = database.test_sessions(limit=100000)
    followups = database.list_followups(limit=100000)
    if export_format == 'csv':
        path = target_dir / f'work-export-{stamp}.csv'
        columns = ['record_type', 'id', 'shift_id', 'category_status', 'title_detail',
                   'client', 'channel', 'outcome_result', 'due_date', 'project_product',
                   'ticket', 'created_at']
        with path.open('w', newline='', encoding='utf-8-sig') as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for task in tasks:
                writer.writerow({key: _safe(value) for key, value in {
                    'record_type': 'task', 'id': task.id, 'shift_id': task.planned_shift_id,
                    'category_status': task.status.value, 'title_detail': task.title,
                    'client': task.client, 'outcome_result': task.completion_note,
                    'due_date': task.due_date, 'project_product': task.project,
                    'ticket': task.ticket, 'created_at': task.created_at}.items()})
            for item in activities:
                writer.writerow({key: _safe(value) for key, value in {
                    'record_type': 'activity', 'id': item['id'], 'shift_id': item['shift_id'],
                    'category_status': item['category'], 'title_detail': item['detail'],
                    'client': item.get('client'), 'channel': item.get('channel'),
                    'outcome_result': item.get('outcome') or item.get('result'),
                    'project_product': item.get('support_product') or item.get('testing_product')
                        or item.get('learning_product'),
                    'ticket': item.get('support_ticket') or item.get('testing_ticket'),
                    'created_at': item['created_at']}.items()})
            for item in cases:
                writer.writerow({key: _safe(value) for key, value in {
                    'record_type': 'case', 'id': item['id'],
                    'category_status': item['status'], 'title_detail': item['title'],
                    'client': item.get('client'), 'channel': item.get('channel'),
                    'outcome_result': item.get('resolution'), 'project_product': item.get('product'),
                    'ticket': item.get('ticket'), 'created_at': item.get('created_at')}.items()})
            for item in sessions:
                writer.writerow({key: _safe(value) for key, value in {
                    'record_type': 'test_session', 'id': item['id'], 'shift_id': item.get('shift_id'),
                    'category_status': item['result'], 'title_detail': item['scenario'],
                    'outcome_result': item.get('actual'), 'project_product': item.get('environment'),
                    'ticket': item.get('case_id'), 'created_at': item.get('created_at')}.items()})
            for item in followups:
                writer.writerow({key: _safe(value) for key, value in {
                    'record_type': 'followup', 'id': item['id'], 'shift_id': item.get('shift_id'),
                    'category_status': item['status'], 'title_detail': item.get('note') or item['title'],
                    'outcome_result': item.get('waiting_on'), 'due_date': item.get('due_at'),
                    'ticket': item.get('case_id'), 'created_at': item.get('created_at')}.items()})
        return path
    path = target_dir / f'work-export-{stamp}.md'
    lines = ['# Work Assistant Export', '', '## Tasks', '']
    lines.extend(f'- {task_line(task, True).replace(chr(10), "; ")}' for task in tasks)
    lines.extend(('', '## Shifts and activities', ''))
    by_shift = {shift['id']: [] for shift in shifts}
    for item in activities:
        by_shift[item['shift_id']].append(item)
    for shift in shifts:
        lines.extend((f"### Shift {shift['id']} — {shift['start']}", ''))
        lines.extend(f"- [{item['category']}] {item['detail']}" for item in by_shift[shift['id']])
        lines.append('')
    lines.extend(('## Cases', ''))
    lines.extend(f"- CASE-{item['id']} [{item['status']}] {item['title']}" for item in cases)
    lines.extend(('', '## Test sessions', ''))
    lines.extend(f"- TEST-{item['id']} [{item['result']}] {item['scenario']}" for item in sessions)
    lines.extend(('', '## Pending follow-ups', ''))
    lines.extend(f"- #{item['id']} CASE-{item['case_id']} due {item['due_at']}: {item.get('note') or item['title']}"
                 for item in followups)
    path.write_text('\n'.join(lines), encoding='utf-8')
    return path
