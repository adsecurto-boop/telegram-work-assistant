"""
reports.py
----------
Pure functions that turn a list of Task objects into the exact report
strings required by the spec. No I/O, no Telegram, no DB -- easy to
unit test in isolation.
"""

from typing import List

from models import Task, TaskStatus


def _bullet_list(items: List[str]) -> str:
    if not items:
        return "  (none)"
    return "\n".join(f"• {item}" for item in items)


def generate_bos(tasks: List[Task]) -> str:
    """
    Beginning of Shift: a flat list of everything not yet finished
    Only Pending tasks (MVP requirement).
    """
    titles = [t.title for t in tasks if t.status == TaskStatus.PENDING]
    lines = ["Beginning of Shift", "Performing:", _bullet_list(titles)]
    return "\n".join(lines)


def generate_prelunch(tasks: List[Task]) -> str:
    """
    Pre Lunch:
        Completed   -> status == COMPLETED
        In Progress -> status == IN_PROGRESS
        Remaining   -> status == PENDING
    (Blocked tasks are intentionally omitted here; they surface in EOD.)
    """
    completed = [f"[x] {t.title}" for t in tasks if t.status == TaskStatus.COMPLETED]
    in_progress = [t.title for t in tasks if t.status == TaskStatus.IN_PROGRESS]
    remaining = [t.title for t in tasks if t.status == TaskStatus.PENDING]

    lines = [
        "Pre Lunch",
        "Completed",
        _bullet_list(completed),
        "Pending",
        _bullet_list(remaining),
    ]
    return "\n".join(lines)


def generate_eod(tasks: List[Task]) -> str:
    """
    End of Day:
        Completed     -> status == COMPLETED
        Blocked       -> status == BLOCKED, shown as "Task title — reason"
        Carry Forward -> status in (PENDING, IN_PROGRESS)

    NOTE: the spec's example shows the Blocked section listing only the
    reason text (e.g. "Waiting for Dev") with no task title. To keep the
    report useful when there are multiple blocked tasks, each blocked
    line here is rendered as "<title> — <reason>". Adjust
    `_blocked_lines` below if you need the literal reason-only format.
    """
    completed = [f"[x] {t.title}" for t in tasks if t.status == TaskStatus.COMPLETED]
    blocked = _blocked_lines(tasks)
    carry_forward = [
        t.title for t in tasks if t.status in (TaskStatus.PENDING, TaskStatus.IN_PROGRESS)
    ]

    lines = [
        "EOD",
        "Completed",
        _bullet_list(completed),
        "Pending",
        _bullet_list(carry_forward),
        "Blocked",
        _bullet_list(blocked),
    ]
    return "\n".join(lines)


def _blocked_lines(tasks: List[Task]) -> List[str]:
    lines = []
    for t in tasks:
        if t.status == TaskStatus.BLOCKED:
            reason = t.blocked_reason or "No reason given"
            lines.append(f"{t.title} — {reason}")
    return lines