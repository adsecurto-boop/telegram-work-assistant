"""Application service for task operations returning MutationResult."""
from dataclasses import dataclass, field
from typing import Any

from database import Database
from models import TaskStatus


@dataclass
class MutationResult:
    success: bool
    correlation_id: str | None = None
    audit_ids: list[int] = field(default_factory=list)
    reversible: bool = False
    entity_type: str | None = None
    entity_id: int | None = None
    summary: str = ""


class TaskService:
    def __init__(self, db: Database):
        self.db = db

    def create_task(self, title: str, shift_id: int | None = None, priority: int = 1,
                    due_date: str | None = None, project: str | None = None,
                    client: str | None = None, ticket: str | None = None,
                    next_action: str | None = None, tags: str | None = None,
                    project_id: int | None = None, assignee_member_id: int | None = None,
                    description: str | None = None, task_type: str = 'task', start_date: str | None = None,
                    correlation_id: str | None = None, actor: str = 'system') -> MutationResult:
        task = self.db.add_task(
            title=title, priority=priority, shift_id=shift_id,
            due_date=due_date, project=project, client=client, ticket=ticket,
            next_action=next_action, tags=tags, project_id=project_id,
            assignee_member_id=assignee_member_id, description=description,
            task_type=task_type, start_date=start_date
        )
        task_id = task.id
        self.db.index_fts_record('task', task_id, title, f"Task: {title}", client=client)
        return MutationResult(
            success=True,
            correlation_id=correlation_id,
            reversible=bool(correlation_id),
            entity_type='task',
            entity_id=task_id,
            summary=f"Created task #{task_id}: {title}"
        )

    def complete_task(self, task_id: int, shift_id: int | None = None,
                      completion_note: str | None = None,
                      correlation_id: str | None = None, actor: str = 'system') -> MutationResult:
        self.db.mark_status(
            task_id, TaskStatus.COMPLETED, shift_id=shift_id,
            completion_note=completion_note, correlation_id=correlation_id, actor=actor
        )
        return MutationResult(
            success=True,
            correlation_id=correlation_id,
            reversible=bool(correlation_id),
            entity_type='task',
            entity_id=task_id,
            summary=f"Completed task #{task_id}"
        )

    def carry_forward(self, task_id: int, target_date: str,
                      shift_id: int | None = None,
                      correlation_id: str | None = None, actor: str = 'system') -> MutationResult:
        res = self.db.carry_forward_task(
            task_id=task_id, target_date=target_date, shift_id=shift_id,
            correlation_id=correlation_id, actor=actor
        )
        return MutationResult(
            success=True,
            correlation_id=res.get('correlation_id', correlation_id),
            reversible=True,
            entity_type='task',
            entity_id=res.get('new_task_id'),
            summary=f"Carried forward task #{task_id} to {target_date}"
        )
