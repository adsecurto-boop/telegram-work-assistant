"""Deterministic shift and weekly reports generated only from stored facts."""
from collections import Counter
from datetime import datetime

from domain import task_line
from models import TaskStatus


def bullets(items):
    items = [str(item) for item in items if item not in (None, '')]
    return '\n'.join('• ' + item for item in items) if items else '• None recorded'


def _client_aliases(support):
    names = sorted({item['client'].strip() for item in support if item.get('client')}, key=str.casefold)
    return {name.casefold(): f'Client {index}' for index, name in enumerate(names, 1)}


def _support_data(activities, mask_clients=False):
    support = [item for item in activities if item['category'] == 'support']
    aliases = _client_aliases(support) if mask_clients else {}
    unique = {item['client'].strip().casefold() for item in support if item.get('client')}
    outcomes = Counter(item.get('outcome') for item in support if item.get('outcome'))
    keyed_resolved = {}
    for item in support:
        if item.get('outcome') == 'resolved' and item.get('issue_key'):
            key = item['issue_key'].casefold()
            keyed_resolved[key] = max(keyed_resolved.get(key, 0), item.get('query_count') or 1)
    resolved_queries = sum(keyed_resolved.values()) + sum(
        (item.get('query_count') or 0) for item in support
        if item.get('outcome') == 'resolved' and not item.get('issue_key'))
    unresolved_counts = any(item.get('query_count') is None for item in support)
    lines = []
    for item in support:
        raw_name = item.get('client')
        name = aliases.get(raw_name.casefold(), raw_name) if raw_name else 'Unnamed client'
        extras = []
        for label, value in (
            ('Product', item.get('support_product')), ('Category', item.get('query_category')),
            ('Ticket', item.get('support_ticket')), ('Follow-up', item.get('support_follow_up')),
            ('Queries', item.get('query_count'))):
            if value not in (None, ''):
                extras.append(f'{label}: {value}')
        line = f"{name} ({item.get('channel') or 'channel unspecified'}): {item['detail']}"
        line += f" [{item.get('outcome') or 'outcome unspecified'}]"
        if extras:
            line += ' — ' + '; '.join(extras)
        lines.append(line)
    stats = (
        f"Named clients: {len(unique)}; logged interactions: {len(support)}; "
        f"resolved interactions: {outcomes['resolved']}."
    )
    query_stats = f'Explicit resolved queries: {resolved_queries}.'
    if unresolved_counts:
        query_stats += ' Some legacy interactions have no query count.'
    completeness = ('Client total incomplete: unnamed interactions exist.'
                    if any(not item.get('client') for item in support)
                    else 'Counts cover logged interactions only.')
    return support, stats, query_stats, completeness, lines


def _testing_lines(activities, detailed=False):
    result = []
    for item in activities:
        if item['category'] != 'testing':
            continue
        extras = []
        for label, value in (
            ('Result', item.get('result')), ('Product', item.get('testing_product')),
            ('Environment', item.get('environment')), ('Build', item.get('build')),
            ('Defects', item.get('defects')), ('Ticket', item.get('testing_ticket')),
            ('Retest', item.get('retest'))):
            if value:
                extras.append(f'{label}: {value}')
        line = item.get('scenario') or item['detail']
        if extras and detailed:
            line += ' — ' + '; '.join(extras)
        else:
            standard = []
            for label, value in (('Result', item.get('result')), ('Defects', item.get('defects')),
                                 ('Ticket', item.get('testing_ticket')), ('Retest', item.get('retest'))):
                if value:
                    standard.append(f'{label}: {value}')
            if standard:
                line += ' — ' + '; '.join(standard)
        result.append(line)
    return result


def _learning_lines(activities, detailed=False):
    result = []
    for item in activities:
        if item['category'] != 'learning':
            continue
        extras = []
        for label, value in (
            ('Type', item.get('learning_type')), ('Product', item.get('learning_product')),
            ('Takeaway', item.get('takeaway')), ('Follow-up', item.get('learning_follow_up'))):
            if value:
                extras.append(f'{label}: {value}')
        line = item.get('topic') or item['detail']
        if extras and detailed:
            line += ' — ' + '; '.join(extras)
        elif item.get('takeaway'):
            line += f" — Takeaway: {item['takeaway']}"
        result.append(line)
    return result


def _latest_task_events(activities):
    return {item['task_id']: item for item in activities
            if item['category'] == 'task' and item.get('task_id')}


def report_sections(kind, shift, activities, tasks, style='standard', mask_clients=False,
                    cases=None, test_sessions=None):
    if style not in ('short', 'standard', 'detailed'):
        raise ValueError('Report style must be short, standard, or detailed.')
    detailed = style == 'detailed'
    planned_ids = {item['task_id'] for item in activities
                   if item['category'] == 'plan' and item.get('task_id')}
    current_plans = [task for task in tasks if task.id in planned_ids]
    carry = [task for task in tasks if task.id not in planned_ids and
             task.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED)]
    sections = {}
    sections['Planned priorities'] = bullets(task_line(task, detailed) for task in current_plans)
    sections['Open work / carry forward'] = bullets(task_line(task, detailed) for task in carry)
    if kind == 'tod':
        return sections

    latest = _latest_task_events(activities)
    planned_done = [event['detail'] for event in latest.values()
                    if event.get('outcome') == 'completed' and not event.get('unplanned')]
    unplanned_done = [event['detail'] for event in latest.values()
                      if event.get('outcome') == 'completed' and event.get('unplanned')]
    sections['Completed planned work'] = bullets(planned_done)
    sections['Additional unplanned work'] = bullets(unplanned_done)
    support, stats, query_stats, completeness, support_lines = _support_data(activities, mask_clients)
    sections['Client support'] = '\n'.join((stats, query_stats, completeness, bullets(support_lines)))
    cases = cases or []
    if cases:
        aliases = {name: f'Client {index}' for index, name in enumerate(sorted(
            {item['client'].casefold() for item in cases if item.get('client')}), 1)} if mask_clients else {}
        worked = [item for item in cases if item.get('participation') in ('owned','handled','assisted')]
        clients = {item['client'].casefold() for item in worked if item.get('client')}
        resolved = [item for item in worked if item.get('status') in ('resolved','client_updated','closed')]
        updated = [item for item in worked if item.get('client_updated') or item.get('status') in ('client_updated','closed')]
        case_lines = []
        for item in worked:
            client = item.get('client')
            if client and mask_clients:
                client = aliases[client.casefold()]
            line = f"CASE-{item['id']} {client or 'Unnamed client'}: {item['title']} "
            line += f"[{item['status']}; {item['participation']}]"
            if item.get('next_action'):
                line += f" — Next: {item['next_action']}"
            case_lines.append(line)
        case_stats = (
            f'Clients handled: {len(clients)}; queries worked: {len(worked)}; '
            f'verified resolved cases: {len(resolved)}; clients updated: {len(updated)}; '
            f'assisted cases: {sum(item.get("participation") == "assisted" for item in worked)}.')
        sections['Case workflow'] = case_stats + '\n' + bullets(case_lines)
    tests = _testing_lines(activities, detailed)
    test_sessions = test_sessions or []
    for item in test_sessions:
        line = f"TEST-{item['id']} {item['scenario']} — Result: {item['result']}"
        extras = []
        if item.get('environment'):
            extras.append('Environment: ' + item['environment'])
        if item.get('build'):
            extras.append('Build: ' + item['build'])
        if item.get('defects'):
            extras.append('Defects: ' + item['defects'])
        if item.get('retest_required'):
            extras.append('Retest required')
        if extras:
            line += '; ' + '; '.join(extras)
        tests.append(line)
    results = Counter(item.get('result') for item in activities
                      if item['category'] == 'testing' and item.get('result'))
    session_results = Counter(item.get('result') for item in test_sessions if item.get('result'))
    test_stats = (f"Logged tests: {len(tests)}; passed: {results['passed'] + session_results['passed']}; "
                  f"failed: {results['failed'] + session_results['failed']}; "
                  f"partial: {results['partial'] + session_results['partial']}; "
                  f"blocked: {results['blocked'] + session_results['blocked']}; "
                  f"retests pending: {sum(bool(item.get('retest_required')) and not item.get('retest_result') for item in test_sessions)}.")
    sections['Testing'] = test_stats + '\n' + bullets(tests)
    sections['Learning / KT'] = bullets(_learning_lines(activities, detailed))
    sections['Other work / notes'] = bullets(
        item['detail'] for item in activities if item['category'] == 'note')
    sections['In progress'] = bullets(task_line(task, detailed) for task in tasks
                                      if task.status == TaskStatus.IN_PROGRESS)
    sections['Pending for next shift' if kind == 'eod' else 'Remaining'] = bullets(
        task_line(task, detailed) for task in tasks if task.status == TaskStatus.PENDING)
    sections['Blocked'] = bullets(task_line(task, detailed) for task in tasks
                                  if task.status == TaskStatus.BLOCKED)
    if style == 'short':
        keep = {'Completed planned work', 'Additional unplanned work', 'Client support', 'Case workflow',
                'Testing', 'Learning / KT', 'Pending for next shift' if kind == 'eod' else 'Remaining',
                'Blocked'}
        sections = {name: value for name, value in sections.items()
                    if name in keep and value != '• None recorded'}
    return sections


def generate_report(kind, shift, activities, tasks, style='standard', mask_clients=False,
                    cases=None, test_sessions=None):
    title = {'tod': 'Beginning of Shift', 'pl': 'Pre Lunch', 'eod': 'EOD'}[kind]
    lines = [title, 'Shift: ' + shift['start'][:16].replace('T', ' ')]
    for heading, content in report_sections(kind, shift, activities, tasks, style, mask_clients,
                                            cases, test_sessions).items():
        lines.extend((heading, content))
    return '\n'.join(lines)


def generate_section(kind, section, shift, activities, tasks, style='standard', mask_clients=False,
                     cases=None, test_sessions=None):
    sections = report_sections(kind, shift, activities, tasks, style, mask_clients,
                               cases, test_sessions)
    matches = [name for name in sections if section.casefold() in name.casefold()]
    if len(matches) != 1:
        raise ValueError('Section must identify one of: ' + ', '.join(sections) + '.')
    name = matches[0]
    return name + '\n' + sections[name]


def weekly_summary(shifts, activities, tasks):
    shift_ids = {shift['id'] for shift in shifts}
    relevant = [item for item in activities if item['shift_id'] in shift_ids]
    latest = _latest_task_events(relevant)
    completed = sum(event.get('outcome') == 'completed' for event in latest.values())
    planned_ids = {item['task_id'] for item in relevant
                   if item['category'] == 'plan' and item.get('task_id')}
    planned_completed = sum(task_id in planned_ids and event.get('outcome') == 'completed'
                            for task_id, event in latest.items())
    completion_rate = round(planned_completed * 100 / len(planned_ids)) if planned_ids else 0
    support = [item for item in relevant if item['category'] == 'support']
    clients = {item['client'].casefold() for item in support if item.get('client')}
    resolved = sum(item.get('outcome') == 'resolved' for item in support)
    keyed_resolved = {item['issue_key'].casefold(): item.get('query_count') or 1 for item in support
                      if item.get('outcome') == 'resolved' and item.get('issue_key')}
    resolved_queries = sum(keyed_resolved.values()) + sum(
        item.get('query_count') or 0 for item in support
        if item.get('outcome') == 'resolved' and not item.get('issue_key'))
    tests = [item for item in relevant if item['category'] == 'testing']
    defects = sum(bool(item.get('defects')) for item in tests)
    learning = [item for item in relevant if item['category'] == 'learning']
    open_tasks = [task for task in tasks if task.status in
                  (TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED)]
    return '\n'.join((
        'Weekly Work Summary',
        f'Shifts recorded: {len(shifts)}',
        f'Tasks completed during these shifts: {completed}',
        f'Planned-task completion: {planned_completed}/{len(planned_ids)} ({completion_rate}%)',
        f'Unique named clients: {len(clients)}',
        f'Support interactions: {len(support)}; resolved interactions: {resolved}',
        f'Explicit resolved queries: {resolved_queries}',
        f'Testing records: {len(tests)}; records with defects: {defects}',
        f'Learning / KT records: {len(learning)}',
        f'Open carry-forward tasks: {len(open_tasks)}',
    ))
