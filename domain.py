"""Parsing and formatting helpers for the Telegram command surface."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
from models import CASE_LIFECYCLE, CaseLifecycleSpec, CaseStatus, TaskStatus, TestResult

PRIORITIES = {'low': 0, 'normal': 1, 'high': 2, 'urgent': 3}
PRIORITY_NAMES = {value: key for key, value in PRIORITIES.items()}
OUTCOMES = {'resolved', 'investigated', 'escalated', 'pending', 'assisted'}
TEST_RESULTS = {r.value for r in TestResult}
CASE_STATUSES = {s.value for s in CaseStatus}
PARTICIPATION = {'owned','handled','assisted','assigned','observed'}


def fields(text, count):
    values = [part.strip() for part in text.split('|', count - 1)]
    values += [''] * (count - len(values))
    return [None if value in ('', '-', '?') else value for value in values]


def parse_due(value, reference=None):
    if not value:
        return None
    current = reference or datetime.now(ZoneInfo(config.TIMEZONE))
    lowered = value.casefold()
    if lowered == 'today':
        return current.date().isoformat()
    if lowered == 'tomorrow':
        return (current.date() + timedelta(days=1)).isoformat()
    return datetime.strptime(value, '%Y-%m-%d').date().isoformat()


def parse_priority(value):
    if not value:
        return PRIORITIES['normal']
    lowered = value.casefold()
    if lowered in PRIORITIES:
        return PRIORITIES[lowered]
    number = int(value)
    if number not in PRIORITY_NAMES:
        raise ValueError('Priority must be low, normal, high, or urgent.')
    return number


def parse_task(text, reference=None):
    title, priority, due, project, client, ticket, next_action, tags = fields(text, 8)
    if not title:
        raise ValueError('Usage: /task title | priority | due | project | client | ticket | next action | tags')
    return {
        'title': title, 'priority': parse_priority(priority), 'due_date': parse_due(due, reference),
        'project': project, 'client': client, 'ticket': ticket,
        'next_action': next_action, 'tags': tags,
    }


def parse_support(text):
    client, channel, outcome, detail, product, category, follow_up, ticket, count, issue_key = fields(text, 10)
    if not channel or not outcome or not detail:
        raise ValueError(
            'Usage: /support client | channel | outcome | query/action | product | category | follow-up | ticket | query count | issue key'
        )
    outcome = outcome.casefold()
    if outcome not in OUTCOMES:
        raise ValueError('Outcome: resolved, investigated, escalated, pending, or assisted.')
    query_count = int(count) if count else 1
    if not 1 <= query_count <= 100:
        raise ValueError('Query count must be between 1 and 100.')
    return {
        'client': client, 'channel': channel, 'outcome': outcome, 'detail': detail,
        'product': product, 'query_category': category, 'follow_up': follow_up,
        'ticket': ticket, 'query_count': query_count, 'issue_key': issue_key,
    }


def parse_testing(text):
    scenario, result, product, environment, build, defects, ticket, retest = fields(text, 8)
    if not scenario:
        raise ValueError(
            'Usage: /testing scenario | result | product | environment | build | defects | ticket | retest'
        )
    if result:
        result = result.casefold().replace(' ', '_')
        if result not in TEST_RESULTS:
            raise ValueError('Test result: passed, failed, partial, blocked, or not_run.')
    return {
        'scenario': scenario, 'result': result, 'product': product,
        'environment': environment, 'build': build, 'defects': defects,
        'ticket': ticket, 'retest': retest,
    }


def parse_learning(text):
    topic, learning_type, product, takeaway, follow_up = fields(text, 5)
    if not topic:
        raise ValueError('Usage: /learning topic | type | product | takeaway | follow-up')
    return {
        'topic': topic, 'learning_type': learning_type, 'product': product,
        'takeaway': takeaway, 'follow_up': follow_up,
    }


def parse_when(value, reference=None):
    if not value:
        return None
    current = reference or datetime.now(ZoneInfo(config.TIMEZONE))
    low = value.casefold().strip()
    if low.startswith('tomorrow'):
        clock = value.split(maxsplit=1)[1] if len(value.split(maxsplit=1)) == 2 else '10:00'
        moment = datetime.combine(current.date() + timedelta(days=1),
                                  datetime.strptime(clock, '%H:%M').time(), current.tzinfo)
        return moment.isoformat()
    if low.startswith('today'):
        clock = value.split(maxsplit=1)[1] if len(value.split(maxsplit=1)) == 2 else current.strftime('%H:%M')
        moment = datetime.combine(current.date(), datetime.strptime(clock, '%H:%M').time(), current.tzinfo)
        return moment.isoformat()
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo(config.TIMEZONE))
    return moment.isoformat()


def parse_case(text, reference=None):
    (title, client, channel, status, participation, product, platform, ticket,
     priority, next_action, waiting_on, follow_up) = fields(text, 12)
    if not title:
        raise ValueError(
            'Usage: /case title | client | channel | status | participation | product | platform | ticket | priority | next action | waiting on | follow-up')
    status = (status or 'new').casefold().replace(' ', '_')
    participation = (participation or 'owned').casefold()
    if status not in CASE_STATUSES:
        raise ValueError('Case status: ' + ', '.join(sorted(CASE_STATUSES)) + '.')
    if participation not in PARTICIPATION:
        raise ValueError('Participation: ' + ', '.join(sorted(PARTICIPATION)) + '.')
    return {
        'title': title, 'client': client, 'channel': channel, 'status': status,
        'participation': participation, 'product': product, 'platform': platform,
        'ticket': ticket, 'priority': parse_priority(priority), 'next_action': next_action,
        'waiting_on': waiting_on, 'follow_up_at': parse_when(follow_up, reference),
    }


def parse_test_session(text):
    (case_id, scenario, result, environment, build, expected, actual, defects,
     retest, notified, preconditions, steps) = fields(text, 12)
    if not scenario:
        raise ValueError(
            'Usage: /testsession case ID | scenario | result | environment | build | expected | actual | defects | retest yes/no | developer notified yes/no | preconditions | steps')
    result = (result or 'not_run').casefold().replace(' ', '_')
    if result not in TEST_RESULTS | {'inconclusive'}:
        raise ValueError('Test result: passed, failed, partial, blocked, not_run, or inconclusive.')
    return {
        'case_id': int(case_id) if case_id else None, 'scenario': scenario, 'result': result,
        'environment': environment, 'build': build, 'expected': expected, 'actual': actual,
        'defects': defects, 'retest_required': str(retest or '').casefold() in ('yes','true','1','required'),
        'developer_notified': str(notified or '').casefold() in ('yes','true','1'),
        'preconditions': preconditions, 'steps': steps,
    }


def case_line(item, detailed=False):
    line = f"CASE-{item['id']} [{item['status']}] [{item['participation']}] {item['title']}"
    if item.get('client'):
        line += f" — {item['client']}"
    if item.get('follow_up_at'):
        line += f" — follow-up {item['follow_up_at'][:16].replace('T', ' ')}"
    if detailed:
        metadata = []
        for label, value in (
            ('Channel', item.get('channel')), ('Product', item.get('product')),
            ('Platform', item.get('platform')), ('Ticket', item.get('ticket')),
            ('Next', item.get('next_action')), ('Waiting on', item.get('waiting_on')),
            ('Resolution', item.get('resolution')),
            ('Client updated', 'yes' if item.get('client_updated') else None)):
            if value:
                metadata.append(f'{label}: {value}')
        if metadata:
            line += '\n' + '\n'.join(metadata)
    return line


def task_line(task, detailed=False):
    priority = PRIORITY_NAMES.get(task.priority, str(task.priority))
    line = f'#{task.id} [{task.status.value}] [{priority}] {task.title}'
    if task.due_date:
        line += f' — due {task.due_date}'
    if task.status == TaskStatus.BLOCKED and task.blocked_reason:
        line += f' — blocked: {task.blocked_reason}'
    if detailed:
        metadata = []
        for label, value in (
            ('Project', task.project), ('Client', task.client), ('Ticket', task.ticket),
            ('Next', task.next_action), ('Tags', task.tags), ('Blocked', task.blocked_reason),
            ('Completion', task.completion_note)):
            if value:
                metadata.append(f'{label}: {value}')
        if metadata:
            line += '\n' + '\n'.join(metadata)
    return line


def task_is_open(task):
    return task.status not in (TaskStatus.COMPLETED, TaskStatus.CANCELLED)
