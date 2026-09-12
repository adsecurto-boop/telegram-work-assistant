"""Application service for work case operations returning MutationResult."""
from database import Database
from application.task_service import MutationResult


class CaseService:
    def __init__(self, db: Database):
        self.db = db

    def create_case(self, title: str, client: str | None = None, product: str | None = None,
                    platform: str | None = None, channel: str | None = None, ticket: str | None = None,
                    priority: int = 1, status: str = 'new', participation: str = 'owned',
                    next_action: str | None = None, waiting_on: str | None = None,
                    follow_up_at: str | None = None, resolution: str | None = None,
                    client_updated: bool = False, review_state: str = 'approved',
                    source: str = 'manual', shift_id: int | None = None,
                    detail: str | None = None, event_type: str = 'created') -> MutationResult:
        case_id = self.db.create_case(
            title=title, client=client, product=product, platform=platform,
            channel=channel, ticket=ticket, priority=priority, status=status,
            participation=participation, next_action=next_action, waiting_on=waiting_on,
            follow_up_at=follow_up_at, resolution=resolution, client_updated=client_updated,
            review_state=review_state, source=source, shift_id=shift_id,
            detail=detail, event_type=event_type
        )
        self.db.index_fts_record('case', case_id, title, f"Case: {title} status {status}", client=client, product=product)
        return MutationResult(
            success=True,
            entity_type='case',
            entity_id=case_id,
            summary=f"Created CASE-{case_id}: {title}"
        )

    def update_case_status(self, case_id: int, status: str, shift_id: int | None = None,
                           detail: str | None = None, correlation_id: str | None = None) -> MutationResult:
        res = self.db.update_case(case_id, 'status', status, shift_id=shift_id, detail=detail, correlation_id=correlation_id)
        return MutationResult(
            success=True,
            correlation_id=res.get('correlation_id', correlation_id) if isinstance(res, dict) else correlation_id,
            reversible=True,
            entity_type='case',
            entity_id=case_id,
            summary=f"Updated CASE-{case_id} status to {status}"
        )

    def change_status(self, case_id: int, status: str, shift_id: int | None = None,
                      detail: str | None = None, correlation_id: str | None = None) -> MutationResult:
        return self.update_case_status(case_id, status, shift_id=shift_id, detail=detail, correlation_id=correlation_id)
