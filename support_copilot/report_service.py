import hashlib
import json
import uuid
from datetime import date, datetime, time, timezone
from typing import Any, Dict, List, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from .models import ActivityEvent, AuditEvent, ReportSnapshot
from .suggestion_service import ConflictError


VERIFIED_WORK_EVENT_TYPES = {
    "response.sent",
    "support.completed",
    "test.completed",
    "escalation.created",
    "followup.created",
    "outcome.verified",
}


def resolve_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone '{name}'.") from exc


def day_bounds(report_date: date, timezone_name: str) -> Tuple[datetime, datetime]:
    tz = resolve_timezone(timezone_name)
    start_local = datetime.combine(report_date, time.min, tzinfo=tz)
    end_local = datetime.combine(report_date, time.max, tzinfo=tz)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


class ReportService:
    @staticmethod
    def events_for_day(db: Session, report_date: date, timezone_name: str) -> List[ActivityEvent]:
        start_utc, end_utc = day_bounds(report_date, timezone_name)
        return (
            db.query(ActivityEvent)
            .filter(ActivityEvent.occurred_at >= start_utc, ActivityEvent.occurred_at <= end_utc)
            .order_by(ActivityEvent.occurred_at.asc(), ActivityEvent.id.asc())
            .all()
        )

    @staticmethod
    def facts(events: List[ActivityEvent]) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for event in events:
            try:
                details = json.loads(event.details_json) if event.details_json else {}
            except json.JSONDecodeError:
                details = {"invalid_details": True}
            result.append(
                {
                    "event_id": event.id,
                    "event_type": event.event_type,
                    "subject_type": event.subject_type,
                    "subject_id": event.subject_id,
                    "case_id": event.case_id,
                    "actor": event.actor,
                    "occurred_at": event.occurred_at.isoformat(),
                    "details": details,
                }
            )
        return result

    @staticmethod
    def facts_hash(facts: List[Dict[str, Any]]) -> str:
        canonical = json.dumps(facts, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def create_preview(
        cls,
        db: Session,
        report_type: str,
        report_date: date,
        timezone_name: str,
        actor: str,
    ) -> ReportSnapshot:
        if report_type not in {"tod", "eod"}:
            raise ValueError("Report type must be 'tod' or 'eod'.")
        resolve_timezone(timezone_name)
        facts = cls.facts(cls.events_for_day(db, report_date, timezone_name))
        completed = [fact for fact in facts if fact["event_type"] in VERIFIED_WORK_EVENT_TYPES]
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for fact in completed:
            grouped.setdefault(fact["case_id"] or "unassigned", []).append(fact)
        content = {
            "report_type": report_type,
            "report_date": report_date.isoformat(),
            "timezone": timezone_name,
            "summary": {
                "verified_work_items": len(completed),
                "non_completion_events_excluded": len(facts) - len(completed),
            },
            "work_by_case": grouped,
            "source_event_ids": [fact["event_id"] for fact in completed],
        }
        snapshot = ReportSnapshot(
            id=str(uuid.uuid4()),
            report_type=report_type,
            report_date=report_date.isoformat(),
            timezone_name=timezone_name,
            lifecycle_status="preview",
            facts_hash=cls.facts_hash(facts),
            content_json=json.dumps(content, sort_keys=True, ensure_ascii=False),
            created_by=actor,
            created_at=datetime.now(timezone.utc),
        )
        db.add(snapshot)
        db.add(
            AuditEvent(
                actor=actor,
                action="report.previewed",
                resource=f"report_snapshot/{snapshot.id}",
                correlation_id=str(uuid.uuid4()),
                timestamp=datetime.now(timezone.utc),
                details_json=json.dumps({"report_type": report_type, "facts_hash": snapshot.facts_hash}),
            )
        )
        db.commit()
        db.refresh(snapshot)
        return snapshot

    @classmethod
    def finalize(
        cls,
        db: Session,
        preview_id: str,
        expected_facts_hash: str,
        actor: str,
        report_type: str,
    ) -> ReportSnapshot:
        snapshot = db.query(ReportSnapshot).filter_by(id=preview_id).first()
        if snapshot is None:
            raise ValueError(f"Report preview '{preview_id}' not found.")
        if snapshot.report_type != report_type:
            raise ValueError("Report type does not match preview.")
        if snapshot.facts_hash != expected_facts_hash:
            raise ConflictError("The supplied facts hash does not match this preview.")
        if snapshot.lifecycle_status == "finalized":
            return snapshot
        current_facts = cls.facts(
            cls.events_for_day(db, date.fromisoformat(snapshot.report_date), snapshot.timezone_name)
        )
        if cls.facts_hash(current_facts) != snapshot.facts_hash:
            raise ConflictError("Report preview is stale because daily activity changed. Create a new preview.")
        snapshot.lifecycle_status = "finalized"
        snapshot.finalized_at = datetime.now(timezone.utc)
        db.add(
            AuditEvent(
                actor=actor,
                action="report.finalized",
                resource=f"report_snapshot/{snapshot.id}",
                correlation_id=str(uuid.uuid4()),
                timestamp=datetime.now(timezone.utc),
                details_json=json.dumps({"facts_hash": snapshot.facts_hash}),
            )
        )
        db.commit()
        db.refresh(snapshot)
        return snapshot
