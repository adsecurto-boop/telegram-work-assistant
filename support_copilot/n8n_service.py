import hashlib
import hmac
import json
import time
import uuid
from typing import Any, Dict

from sqlalchemy.orm import Session

from .models import AuditEvent, IntegrationIdempotency, ReportSnapshot
from .suggestion_service import ConflictError


EXPORT_CAPABILITY = "report:export"


def canonical_payload(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def signature_for(secret: str, timestamp: str, event_id: str, capability: str, payload: Dict[str, Any]) -> str:
    message = f"{timestamp}.{event_id}.{capability}.{canonical_payload(payload)}"
    return hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_request(
    secret: str,
    timestamp: str,
    event_id: str,
    capability: str,
    signature: str,
    payload: Dict[str, Any],
    max_age_seconds: int,
) -> None:
    if capability != EXPORT_CAPABILITY:
        raise PermissionError("Requested integration capability is not allowed.")
    try:
        request_time = int(timestamp)
    except ValueError as exc:
        raise PermissionError("Invalid integration timestamp.") from exc
    if abs(int(time.time()) - request_time) > max_age_seconds:
        raise PermissionError("Integration signature has expired.")
    expected = signature_for(secret, timestamp, event_id, capability, payload)
    if not hmac.compare_digest(signature, expected):
        raise PermissionError("Invalid integration signature.")


def export_finalized_report(
    db: Session,
    event_id: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    payload_text = canonical_payload(payload)
    payload_hash = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
    existing = db.query(IntegrationIdempotency).filter_by(provider="n8n", event_id=event_id).first()
    if existing:
        if existing.payload_hash != payload_hash:
            raise ConflictError("n8n event ID was reused with a different payload.")
        return json.loads(existing.response_json)

    query = db.query(ReportSnapshot).filter_by(
        report_type=payload["report_type"], lifecycle_status="finalized"
    )
    if payload.get("report_date"):
        query = query.filter_by(report_date=payload["report_date"])
    snapshot = query.order_by(ReportSnapshot.finalized_at.desc()).first()
    if snapshot is None:
        raise ValueError("No finalized report is available for export.")
    report = {
        "id": snapshot.id,
        "report_type": snapshot.report_type,
        "report_date": snapshot.report_date,
        "timezone_name": snapshot.timezone_name,
        "lifecycle_status": snapshot.lifecycle_status,
        "facts_hash": snapshot.facts_hash,
        "content": json.loads(snapshot.content_json),
        "created_at": snapshot.created_at.isoformat(),
        "finalized_at": snapshot.finalized_at.isoformat() if snapshot.finalized_at else None,
    }
    response = {"event_id": event_id, "report": report}
    correlation_id = str(uuid.uuid4())
    db.add(
        IntegrationIdempotency(
            provider="n8n",
            event_id=event_id,
            payload_hash=payload_hash,
            correlation_id=correlation_id,
            status="accepted",
            response_json=json.dumps(response, sort_keys=True),
        )
    )
    db.add(
        AuditEvent(
            actor="n8n_signed_integration",
            action="report.exported",
            resource=f"report_snapshot/{snapshot.id}",
            correlation_id=correlation_id,
            details_json=json.dumps({"event_id": event_id, "facts_hash": snapshot.facts_hash}),
        )
    )
    db.commit()
    return response
