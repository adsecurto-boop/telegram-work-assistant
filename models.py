"""
models.py
---------
Plain data structures used across the application. Kept dependency-free
(no DB or Telegram imports) so they can be reused/tested in isolation.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class TaskStatus(str, Enum):
    """
    Lifecycle of a task.

    NOTE: The original spec only listed pending/completed/blocked as
    storage statuses. To satisfy the Pre-Lunch report format, which
    requires a "Completed / In Progress / Remaining" breakdown, an
    IN_PROGRESS status was added. Use /progress <task_id> to move a
    task from PENDING to IN_PROGRESS. Everything else behaves exactly
    as specified.
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


@dataclass
class Task:
    id: int
    title: str
    status: TaskStatus
    blocked_reason: Optional[str]
    created_at: str          # ISO-8601 string
    completed_at: Optional[str]
    priority: int = 0
    planned_shift_id: Optional[int] = None
    due_date: Optional[str] = None
    project: Optional[str] = None
    client: Optional[str] = None
    ticket: Optional[str] = None
    next_action: Optional[str] = None
    tags: Optional[str] = None
    completion_note: Optional[str] = None

    @classmethod
    def from_row(cls, row) -> "Task":
        """Build a Task from a mapping-like object (sqlite row or dict)."""
        data = dict(row)
        return cls(
            id=int(data["id"]),
            title=data["title"],
            status=TaskStatus(data["status"]),
            blocked_reason=data.get("blocked_reason"),
            created_at=data["created_at"],
            completed_at=data.get("completed_at"),
            priority=int(data.get("priority", 0)),
            planned_shift_id=data.get("planned_shift_id"),
            due_date=data.get("due_date"),
            project=data.get("project"),
            client=data.get("client"),
            ticket=data.get("ticket"),
            next_action=data.get("next_action"),
            tags=data.get("tags"),
            completion_note=data.get("completion_note"),
        )
