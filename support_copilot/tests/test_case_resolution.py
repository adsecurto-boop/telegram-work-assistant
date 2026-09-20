import pytest
from datetime import datetime, timezone
from sqlalchemy.orm import sessionmaker

from support_copilot.config import Settings
from support_copilot.database import create_db_engine, create_session_factory
from support_copilot.migration_runner import MigrationRunner
from support_copilot.case_service import CaseResolutionService, CaseResolutionError
from support_copilot.models import SupportCase, Conversation, CapturedEvent


@pytest.fixture
def case_env(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_file = data_dir / "case_test.sqlite3"
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


def test_explicit_valid_case_resolves(case_env):
    db = case_env["session_factory"]()
    try:
        case = SupportCase(
            id="case-123",
            case_number="CASE-001",
            title="Attendance logs missing",
            status="open",
            client_identifier="client-corp",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(case)
        db.commit()

        outcome = CaseResolutionService.resolve_case(db=db, explicit_case_id="CASE-001")
        assert outcome.status == "resolved"
        assert outcome.resolved_case.id == "case-123"
        assert outcome.resolved_case.case_number == "CASE-001"
        assert outcome.resolution_method == "explicit_id"
    finally:
        db.close()


def test_nonexistent_explicit_case_raises_error(case_env):
    db = case_env["session_factory"]()
    try:
        with pytest.raises(CaseResolutionError) as exc:
            CaseResolutionService.resolve_case(db=db, explicit_case_id="NONEXISTENT-999")
        assert "does not exist" in str(exc.value)
    finally:
        db.close()


def test_verified_conversation_link_resolves(case_env):
    db = case_env["session_factory"]()
    try:
        case = SupportCase(
            id="case-link-1",
            case_number="CASE-LINK-1",
            title="Linked Case",
            status="open",
            client_identifier="client-corp",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(case)

        conv = Conversation(
            id="conv-1",
            provider="manual",
            external_conversation_id="conv-thread-100",
            case_id="case-link-1",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(conv)

        event = CapturedEvent(
            provider="manual",
            event_id="evt-conv-1",
            event_type="message.received",
            occurred_at=datetime.now(timezone.utc),
            actor_id="client-corp",
            actor_role="client",
            conversation_id="conv-thread-100",
            payload_text="Checking in on our case",
            schema_version=1,
            correlation_id="corr-conv-1",
        )
        db.add(event)
        db.commit()

        outcome = CaseResolutionService.resolve_case(db=db, captured_event=event)
        assert outcome.status == "resolved"
        assert outcome.resolved_case.id == "case-link-1"
        assert outcome.resolution_method == "conversation_link"
    finally:
        db.close()


def test_single_open_candidate_resolves(case_env):
    db = case_env["session_factory"]()
    try:
        case = SupportCase(
            id="case-single-1",
            case_number="CASE-SINGLE-1",
            title="Only Open Case",
            status="open",
            client_identifier="client-unique",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(case)
        db.commit()

        outcome = CaseResolutionService.resolve_case(db=db, client_identifier="client-unique")
        assert outcome.status == "resolved"
        assert outcome.resolved_case.id == "case-single-1"
        assert outcome.resolution_method == "single_candidate"
    finally:
        db.close()


def test_multiple_candidates_returns_resolution_required(case_env):
    db = case_env["session_factory"]()
    try:
        c1 = SupportCase(
            id="case-m1",
            case_number="CASE-M1",
            title="First Problem",
            status="open",
            client_identifier="client-multi",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        c2 = SupportCase(
            id="case-m2",
            case_number="CASE-M2",
            title="Second Problem",
            status="open",
            client_identifier="client-multi",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add_all([c1, c2])
        db.commit()

        outcome = CaseResolutionService.resolve_case(db=db, client_identifier="client-multi")
        assert outcome.status == "resolution_required"
        assert len(outcome.candidates) == 2
        candidate_numbers = {c.case_number for c in outcome.candidates}
        assert candidate_numbers == {"CASE-M1", "CASE-M2"}
    finally:
        db.close()


def test_closed_cases_are_not_selected_as_open_candidate(case_env):
    db = case_env["session_factory"]()
    try:
        c_closed = SupportCase(
            id="case-closed",
            case_number="CASE-CLOSED",
            title="Old Resolved Problem",
            status="closed",
            client_identifier="client-mixed",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(c_closed)
        db.commit()

        # Closed case is not chosen as open candidate
        outcome = CaseResolutionService.resolve_case(db=db, client_identifier="client-mixed")
        assert outcome.status == "unlinked"
    finally:
        db.close()


def test_cross_client_candidate_exclusion(case_env):
    db = case_env["session_factory"]()
    try:
        c_b = SupportCase(
            id="case-client-b",
            case_number="CASE-CLIENT-B",
            title="Client B's Problem",
            status="open",
            client_identifier="client-b",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(c_b)
        db.commit()

        # Querying for client-a must NOT return client-b's case
        outcome = CaseResolutionService.resolve_case(db=db, client_identifier="client-a")
        assert outcome.status == "unlinked"
    finally:
        db.close()
