import json
import hashlib
import uuid
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from .schemas import EventEnvelope, CaptureResponse
from .models import CapturedEvent, IntegrationIdempotency, AuditEvent
from .logger import logger

class IdempotencyConflictError(Exception):
    """Raised when an idempotency key is reused with a different request payload."""
    pass

def compute_payload_hash(event: EventEnvelope) -> str:
    serialized = event.model_dump_json()
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

class CaptureService:
    def __init__(self, db: Session):
        self.db = db

    def capture_manual_message(
        self, event: EventEnvelope, actor: str
    ) -> CaptureResponse:
        payload_hash = compute_payload_hash(event)

        # Check for existing idempotency record first
        existing = (
            self.db.query(IntegrationIdempotency)
            .filter_by(provider=event.provider, event_id=event.event_id)
            .first()
        )

        if existing:
            if existing.payload_hash != payload_hash:
                logger.warning(
                    f"Idempotency conflict detected for provider='{event.provider}', event_id='{event.event_id}'",
                    extra={"failure_category": "idempotency_conflict"},
                )
                raise IdempotencyConflictError(
                    f"Idempotency key '{event.event_id}' reused with different payload."
                )
            captured = (
                self.db.query(CapturedEvent)
                .filter_by(provider=event.provider, event_id=event.event_id)
                .one()
            )
            return CaptureResponse(
                status=existing.status,  # type: ignore[arg-type]
                correlation_id=existing.correlation_id,
                captured_event_id=captured.id,
            )

        correlation_id = f"corr-{uuid.uuid4()}"

        captured_event = CapturedEvent(
            provider=event.provider,
            event_id=event.event_id,
            event_type=event.event_type,
            occurred_at=event.occurred_at,
            actor_id=event.actor.external_id,
            actor_role=event.actor.role,
            conversation_id=event.conversation.external_id,
            case_hint=event.conversation.case_hint,
            payload_text=event.payload.text,
            schema_version=event.schema_version,
            correlation_id=correlation_id,
        )

        idempotency_record = IntegrationIdempotency(
            provider=event.provider,
            event_id=event.event_id,
            payload_hash=payload_hash,
            correlation_id=correlation_id,
            status="accepted",
            response_json=json.dumps({"status": "accepted", "correlation_id": correlation_id}),
        )

        audit_record = AuditEvent(
            actor=actor,
            action="capture_manual_message",
            resource=f"event:{event.provider}:{event.event_id}",
            correlation_id=correlation_id,
            details_json=json.dumps({
                "event_type": event.event_type,
                "conversation_id": event.conversation.external_id,
                "actor_role": event.actor.role,
            }),
        )

        try:
            self.db.add(captured_event)
            self.db.add(idempotency_record)
            self.db.add(audit_record)
            self.db.commit()
            self.db.refresh(captured_event)
            return CaptureResponse(
                status="accepted",
                correlation_id=correlation_id,
                captured_event_id=captured_event.id,
            )
        except IntegrityError:
            self.db.rollback()
            try:
                conflict_record = (
                    self.db.query(IntegrationIdempotency)
                    .filter_by(provider=event.provider, event_id=event.event_id)
                    .first()
                )
                if conflict_record:
                    if conflict_record.payload_hash != payload_hash:
                        raise IdempotencyConflictError(
                            f"Idempotency key '{event.event_id}' reused with different payload."
                        )
                    captured = (
                        self.db.query(CapturedEvent)
                        .filter_by(provider=event.provider, event_id=event.event_id)
                        .one()
                    )
                    return CaptureResponse(
                        status=conflict_record.status,  # type: ignore[arg-type]
                        correlation_id=conflict_record.correlation_id,
                        captured_event_id=captured.id,
                    )
            except Exception:
                self.db.rollback()
                raise
            raise
        except Exception as exc:
            self.db.rollback()
            logger.error(
                f"Database transaction failure during capture: {type(exc).__name__}",
                extra={
                    "failure_category": "database_transaction_error",
                    "correlation_id": correlation_id,
                },
            )
            raise
