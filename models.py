"""
models.py
---------
Plain data structures used across the application. Kept dependency-free
(no DB or Telegram imports) so they can be reused/tested in isolation.
"""
from __future__ import annotations

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


class CaseStatus(str, Enum):
    NEW = "new"
    TRIAGED = "triaged"
    INVESTIGATING = "investigating"
    WAITING_CLIENT = "waiting_client"
    WAITING_INTERNAL = "waiting_internal"
    FIX_READY = "fix_ready"
    TESTING = "testing"
    RETEST_REQUIRED = "retest_required"
    RESOLVED = "resolved"
    CLIENT_UPDATED = "client_updated"
    CLOSED = "closed"


class TestResult(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    NOT_RUN = "not_run"


@dataclass(frozen=True)
class CaseLifecycleSpec:
    status: CaseStatus
    display_name: str
    is_terminal: bool
    is_active: bool
    kanban_column: str
    display_order: int


CASE_LIFECYCLE: dict[CaseStatus, CaseLifecycleSpec] = {
    CaseStatus.NEW: CaseLifecycleSpec(CaseStatus.NEW, "New", False, True, "New", 1),
    CaseStatus.TRIAGED: CaseLifecycleSpec(CaseStatus.TRIAGED, "Triaged", False, True, "Triaged", 2),
    CaseStatus.INVESTIGATING: CaseLifecycleSpec(CaseStatus.INVESTIGATING, "Investigating", False, True, "Investigating", 3),
    CaseStatus.WAITING_CLIENT: CaseLifecycleSpec(CaseStatus.WAITING_CLIENT, "Waiting Client", False, True, "Waiting Client", 4),
    CaseStatus.WAITING_INTERNAL: CaseLifecycleSpec(CaseStatus.WAITING_INTERNAL, "Waiting Internal", False, True, "Waiting Internal", 5),
    CaseStatus.FIX_READY: CaseLifecycleSpec(CaseStatus.FIX_READY, "Fix Ready", False, True, "Fix Ready", 6),
    CaseStatus.TESTING: CaseLifecycleSpec(CaseStatus.TESTING, "Testing", False, True, "Testing", 7),
    CaseStatus.RETEST_REQUIRED: CaseLifecycleSpec(CaseStatus.RETEST_REQUIRED, "Retest Required", False, True, "Retest Required", 8),
    CaseStatus.RESOLVED: CaseLifecycleSpec(CaseStatus.RESOLVED, "Resolved", True, False, "Resolved", 9),
    CaseStatus.CLIENT_UPDATED: CaseLifecycleSpec(CaseStatus.CLIENT_UPDATED, "Client Updated", True, False, "Client Updated", 10),
    CaseStatus.CLOSED: CaseLifecycleSpec(CaseStatus.CLOSED, "Closed", True, False, "Closed", 11),
}


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
