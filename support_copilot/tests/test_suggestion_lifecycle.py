import json
import pytest
from datetime import datetime, timezone
from sqlalchemy.orm import sessionmaker

from support_copilot.config import Settings
from support_copilot.database import create_db_engine, create_session_factory
from support_copilot.migration_runner import MigrationRunner
from support_copilot.suggestion_service import SuggestionService, ConflictError
from support_copilot.models import (
    CapturedEvent,
    ResponseSuggestion,
    SentResponse,
    ActivityEvent,
)


@pytest.fixture
def lifecycle_env(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_file = data_dir / "lifecycle_test.sqlite3"
    settings = Settings(
        data_dir=str(data_dir),
        db_path=f"sqlite:///{db_file}",
    )
    runner = MigrationRunner(settings)
    runner.run_upgrade("head")

    engine = create_db_engine(settings.db_path)
    session_factory = create_session_factory(engine)

    yield {"session_factory": session_factory, "engine": engine}

    engine.dispose()


def create_test_suggestion(db) -> ResponseSuggestion:
    event = CapturedEvent(
        provider="manual",
        event_id="evt-lc-1",
        event_type="message.received",
        occurred_at=datetime.now(timezone.utc),
        actor_id="client-1",
        actor_role="client",
        conversation_id="conv-lc-1",
        payload_text="Lifecycle test payload",
        schema_version=1,
        correlation_id="corr-lc-1",
    )
    db.add(event)
    db.commit()

    suggestion = ResponseSuggestion(
        id="sugg-lc-1",
        captured_event_id=event.id,
        lifecycle_status="suggested",
        draft="Original AI draft text",
        missing_facts="[]",
        assumptions="[]",
        prohibited_claim_evaluation="{}",
        confidence=0.9,
        recommended_action="reply",
        provider_identifier="manual",
        correlation_id="corr-lc-1",
        created_at=datetime.now(timezone.utc),
    )
    db.add(suggestion)
    db.commit()
    db.refresh(suggestion)
    return suggestion


def test_copy_updates_status_but_creates_no_sent_response(lifecycle_env):
    db = lifecycle_env["session_factory"]()
    try:
        sugg = create_test_suggestion(db)
        assert sugg.lifecycle_status == "suggested"

        copied = SuggestionService.copy_suggestion(db, sugg.id, actor="agent-1")
        assert copied.lifecycle_status == "copied"
        assert copied.copied_at is not None

        # Invariant: Copied text is NOT sent text
        sent_count = db.query(SentResponse).count()
        assert sent_count == 0

        # Activity event recorded
        act = db.query(ActivityEvent).filter_by(event_type="suggestion.copied").first()
        assert act is not None
        assert act.actor == "agent-1"

        # Repeated copy is idempotent
        copied2 = SuggestionService.copy_suggestion(db, sugg.id, actor="agent-1")
        assert copied2.lifecycle_status == "copied"
        assert db.query(SentResponse).count() == 0
    finally:
        db.close()


def test_reject_sets_terminal_status_and_blocks_confirm_sent(lifecycle_env):
    db = lifecycle_env["session_factory"]()
    try:
        sugg = create_test_suggestion(db)
        rejected = SuggestionService.reject_suggestion(db, sugg.id, actor="agent-1")
        assert rejected.lifecycle_status == "rejected"
        assert rejected.rejected_at is not None

        # Rejected suggestion cannot be confirmed sent
        with pytest.raises(ValueError) as exc:
            SuggestionService.confirm_sent(
                db=db,
                suggestion_id=sugg.id,
                exact_sent_text="Edited text",
                idempotency_key="key-reject-fail",
                actor="agent-1",
            )
        assert "Rejected suggestion cannot be confirmed sent" in str(exc.value)
        assert db.query(SentResponse).count() == 0
    finally:
        db.close()


def test_confirm_sent_records_exact_edited_text_and_activity(lifecycle_env):
    db = lifecycle_env["session_factory"]()
    try:
        sugg = create_test_suggestion(db)
        exact_text = "I edited the draft and sent this exact text to the client."

        sent = SuggestionService.confirm_sent(
            db=db,
            suggestion_id=sugg.id,
            exact_sent_text=exact_text,
            idempotency_key="send-key-001",
            actor="agent-1",
            correlation_id="corr-send-001",
        )

        assert sent.exact_sent_text == exact_text
        assert sent.confirmed_by_actor == "agent-1"
        assert sent.confirmation_idempotency_key == "send-key-001"

        db.refresh(sugg)
        assert sugg.lifecycle_status == "sent"
        assert sugg.sent_at is not None
        assert sugg.edited_text_hash == sent.final_text_hash

        # Activity event created
        act = db.query(ActivityEvent).filter_by(event_type="response.sent").first()
        assert act is not None
        assert act.actor == "agent-1"
        assert act.subject_id == sent.id
    finally:
        db.close()


def test_confirm_sent_idempotency_replay_and_conflict(lifecycle_env):
    db = lifecycle_env["session_factory"]()
    try:
        sugg = create_test_suggestion(db)
        text1 = "Exact message sent"
        key = "send-idemp-key-1"

        # First call
        r1 = SuggestionService.confirm_sent(
            db=db,
            suggestion_id=sugg.id,
            exact_sent_text=text1,
            idempotency_key=key,
            actor="agent-1",
        )

        # Idempotent replay with same text and key returns original response
        r2 = SuggestionService.confirm_sent(
            db=db,
            suggestion_id=sugg.id,
            exact_sent_text=text1,
            idempotency_key=key,
            actor="agent-1",
        )
        assert r1.id == r2.id
        assert db.query(SentResponse).count() == 1
        assert db.query(ActivityEvent).filter_by(event_type="response.sent").count() == 1

        # Replay with DIFFERENT text and SAME key raises ConflictError
        with pytest.raises(ConflictError) as exc:
            SuggestionService.confirm_sent(
                db=db,
                suggestion_id=sugg.id,
                exact_sent_text="Completely different sent text",
                idempotency_key=key,
                actor="agent-1",
            )
        assert "different sent text" in str(exc.value)
    finally:
        db.close()


def test_empty_sent_text_is_rejected(lifecycle_env):
    db = lifecycle_env["session_factory"]()
    try:
        sugg = create_test_suggestion(db)
        with pytest.raises(ValueError) as exc:
            SuggestionService.confirm_sent(
                db=db,
                suggestion_id=sugg.id,
                exact_sent_text="   \n\t  ",
                idempotency_key="key-empty-fail",
                actor="agent-1",
            )
        assert "empty" in str(exc.value)
    finally:
        db.close()
