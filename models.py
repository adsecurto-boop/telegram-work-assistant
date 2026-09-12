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


# ==============================================================================
# Work Item, Workflow, Member, Role, Requirement & Testware Domain Models
# ==============================================================================

class WorkItemType(str, Enum):
    TASK = "task"
    CASE = "case"
    REQUIREMENT = "requirement"
    DEFECT = "defect"
    INVESTIGATION = "investigation"
    DEVOPS = "devops"


class OperationalStatus(str, Enum):
    PLANNED = "planned"
    PENDING = "pending"
    ACTIVE = "active"
    WAITING = "waiting"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ARCHIVED = "archived"


class DependencyType(str, Enum):
    MEMBER = "member"
    ROLE = "role"
    BUILD = "build"
    DEVELOPMENT = "development"
    CLIENT = "client"
    ENVIRONMENT = "environment"
    CREDENTIALS = "credentials"
    EXTERNAL_TICKET = "external_ticket"
    WORK_ITEM = "work_item"
    OTHER = "other"


@dataclass
class Member:
    id: int
    name: str
    email: Optional[str] = None
    telegram_handle: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[str] = None
    roles: list[str] = None

    def __post_init__(self):
        if self.roles is None:
            self.roles = []


@dataclass
class Role:
    id: int
    name: str
    description: Optional[str] = None
    created_at: Optional[str] = None


@dataclass
class WorkflowStage:
    id: int
    template_id: int
    name: str
    stage_order: int
    description: Optional[str] = None
    expected_role: Optional[str] = None
    expected_duration_hours: Optional[float] = None
    is_waiting: bool = False
    is_active: bool = True
    required_artifacts: list[str] = None
    checklist_items: list[str] = None

    def __post_init__(self):
        if self.required_artifacts is None:
            self.required_artifacts = []
        if self.checklist_items is None:
            self.checklist_items = []


@dataclass
class WorkflowTemplate:
    id: int
    name: str
    description: Optional[str] = None
    work_type: str = "requirement"
    is_default: bool = False
    created_at: Optional[str] = None
    stages: list[WorkflowStage] = None

    def __post_init__(self):
        if self.stages is None:
            self.stages = []


@dataclass
class BlockerDependency:
    id: int
    entity_type: str
    entity_id: int
    dependency_type: DependencyType
    description: str
    waiting_on_member_id: Optional[int] = None
    waiting_on_role: Optional[str] = None
    target_entity_type: Optional[str] = None
    target_entity_id: Optional[int] = None
    status: str = "active"  # active, resolved
    started_at: Optional[str] = None
    expected_resolution_date: Optional[str] = None
    resolved_at: Optional[str] = None
    waiting_on_name: Optional[str] = None


@dataclass
class Requirement:
    id: int
    title: str
    description: Optional[str] = None
    requirement_text: Optional[str] = None
    user_story: Optional[str] = None
    acceptance_criteria: Optional[str] = None
    client: Optional[str] = None
    product: Optional[str] = None
    ticket: Optional[str] = None
    priority: int = 1
    due_date: Optional[str] = None
    owner_member_id: Optional[int] = None
    owner_name: Optional[str] = None
    status: str = "active"
    workflow_template_id: Optional[int] = None
    current_stage_id: Optional[int] = None
    current_stage_name: Optional[str] = None
    operational_status: OperationalStatus = OperationalStatus.ACTIVE
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class TestCondition:
    id: int
    requirement_id: int
    title: str
    description: Optional[str] = None
    category: str = "functional"
    risk_level: str = "medium"
    status: str = "draft"  # draft, accepted, rejected, covered
    notes: Optional[str] = None
    created_at: Optional[str] = None


@dataclass
class TestCase:
    id: int
    requirement_id: Optional[int] = None
    test_condition_id: Optional[int] = None
    title: str = ""
    objective: Optional[str] = None
    preconditions: Optional[str] = None
    steps: Optional[str] = None
    test_data: Optional[str] = None
    expected_result: Optional[str] = None
    priority: int = 2
    automation_status: str = "manual"  # manual, automated, candidate
    latest_result: Optional[str] = "not_run"
    created_at: Optional[str] = None


@dataclass
class TestExecution:
    id: int
    test_case_id: int
    session_id: Optional[int] = None
    build: Optional[str] = None
    environment: Optional[str] = None
    result: str = "not_run"  # passed, failed, blocked, partial, not_run
    actual_result: Optional[str] = None
    defect_id: Optional[int] = None
    executed_by_member_id: Optional[int] = None
    executed_by_name: Optional[str] = None
    notes: Optional[str] = None
    executed_at: Optional[str] = None


@dataclass
class WorkItem:
    """
    Unified operational Work Item DTO aggregating Tasks, Cases, Requirements, and Defects.
    """
    entity_type: str  # task, case, requirement, defect
    entity_id: int
    display_id: str   # e.g., T12, C45, R03
    title: str
    operational_status: OperationalStatus
    workflow_template_id: Optional[int] = None
    workflow_template_name: Optional[str] = None
    current_stage_id: Optional[int] = None
    current_stage_name: Optional[str] = None
    priority: int = 1
    client: Optional[str] = None
    product: Optional[str] = None
    ticket: Optional[str] = None
    owner_member_id: Optional[int] = None
    owner_name: Optional[str] = None
    waiting_on: Optional[str] = None
    waiting_on_role: Optional[str] = None
    is_blocked: bool = False
    blocker_description: Optional[str] = None
    due_date: Optional[str] = None
    estimated_hours: Optional[float] = None
    actual_hours: Optional[float] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    completed_at: Optional[str] = None
    next_action: Optional[str] = None
    source_url: Optional[str] = None

