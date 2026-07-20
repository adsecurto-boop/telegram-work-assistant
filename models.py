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


@dataclass
class Task:
    id: int
    title: str
    status: TaskStatus
    blocked_reason: Optional[str]
    created_at: str          # ISO-8601 string
    completed_at: Optional[str]
    priority: int = 0

    @classmethod
    def from_row(cls, row) -> "Task":
        """Build a Task from a sqlite3.Row."""
        return cls(
            id=row["id"],
            title=row["title"],
            status=TaskStatus(row["status"]),
            blocked_reason=row["blocked_reason"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
            priority=row["priority"],
        )