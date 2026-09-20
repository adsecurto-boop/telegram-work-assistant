import json
import pytest
from datetime import datetime, timezone
from sqlalchemy.orm import sessionmaker

from support_copilot.config import Settings
from support_copilot.database import create_db_engine, create_session_factory
from support_copilot.migration_runner import MigrationRunner
from support_copilot.knowledge_service import KnowledgeService
from support_copilot.suggestion_service import SuggestionService
from support_copilot.models import CapturedEvent, SupportCase, ResponseSuggestion, SuggestionSource
from support_copilot.schemas import SuggestedResponse, SourceRef
from support_copilot.ai_provider import (
    AIProvider,
    ProviderTimeoutError,
    ProviderUnavailableError,
    InvalidProviderOutputError,
)
from support_copilot.ai_gateway import AIProviderGateway


class FakeCooperativeProvider(AIProvider):
    def __init__(self, draft="Grounded reply draft", source_refs=None, missing_facts=None):
        self.draft = draft
        self.source_refs = source_refs or []
        self.missing_facts = missing_facts or []

    async def generate_suggestion(self, prompt, context, timeout=5.0):
        return SuggestedResponse(
            draft=self.draft,
            source_refs=self.source_refs,
            missing_facts=self.missing_facts,
            assumptions=[],
            prohibited_claims_detected=[],
            confidence=0.9,
            recommended_action="reply",
        )


class FakeTimeoutProvider(AIProvider):
    async def generate_suggestion(self, prompt, context, timeout=5.0):
        raise ProviderTimeoutError("Simulated provider timeout")


class FakeUnavailableProvider(AIProvider):
    async def generate_suggestion(self, prompt, context, timeout=5.0):
        raise ProviderUnavailableError("Simulated provider unavailable")


class FakeInvalidOutputProvider(AIProvider):
    async def generate_suggestion(self, prompt, context, timeout=5.0):
        raise InvalidProviderOutputError("Simulated malformed json output")


@pytest.fixture
def suggestion_env(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_file = data_dir / "sugg_test.sqlite3"
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


@pytest.mark.anyio
async def test_grounded_pipeline_with_approved_knowledge(suggestion_env):
    db = suggestion_env["session_factory"]()
    try:
        # Create approved knowledge
        art = KnowledgeService.create_article(
            db=db,
            stable_key="art-attendance-req",
            title="Attendance Request Guide",
            product_scope="attendance",
            issue_type="missing_logs",
            initial_content="Clients must provide date ranges and attendance system logs for investigation.",
            created_by="agent-1",
            required_facts=["date_range_specified", "log_type_specified"],
            prohibited_claims=["claim resolution"],
        )
        v1 = KnowledgeService.approve_version(db=db, article_id=art.id, version_num=1, approved_by="approver-1")

        # Captured event
        event = CapturedEvent(
            provider="manual",
            event_id="evt-sugg-1",
            event_type="message.received",
            occurred_at=datetime.now(timezone.utc),
            actor_id="client-1",
            actor_role="client",
            conversation_id="conv-sugg-1",
            payload_text="Why are my attendance records missing from yesterday?",
            schema_version=1,
            correlation_id="corr-sugg-1",
        )
        db.add(event)
        db.commit()

        provider = FakeCooperativeProvider(
            draft="Please provide the date range and system logs.",
            source_refs=[SourceRef(article_id=art.id, version=1)],
        )
        gw = AIProviderGateway(provider)

        outcome = await SuggestionService.generate_suggestion(
            db=db,
            captured_event_id=event.id,
            actor="agent-1",
            gateway=gw,
        )

        assert outcome.status == "suggested"
        assert outcome.suggestion is not None
        assert outcome.suggestion.lifecycle_status == "suggested"
        assert len(outcome.sources) == 1
        assert outcome.sources[0].article_id == art.id
        assert outcome.sources[0].version_number == 1
        assert outcome.sources[0].article_version_id == v1.id

        # Check missing facts: "log_type_specified" is missing
        missing_facts = json.loads(outcome.suggestion.missing_facts)
        assert "log_type_specified" in missing_facts
    finally:
        db.close()


@pytest.mark.anyio
async def test_no_approved_knowledge_does_not_call_ai(suggestion_env):
    db = suggestion_env["session_factory"]()
    try:
        # No approved knowledge articles exist
        event = CapturedEvent(
            provider="manual",
            event_id="evt-no-art",
            event_type="message.received",
            occurred_at=datetime.now(timezone.utc),
            actor_id="client-1",
            actor_role="client",
            conversation_id="conv-no-art",
            payload_text="How do I launch a spaceship to Mars?",
            schema_version=1,
            correlation_id="corr-no-art",
        )
        db.add(event)
        db.commit()

        # Provider that would fail if called
        class ExplodingProvider(AIProvider):
            async def generate_suggestion(self, prompt, context, timeout=5.0):
                raise AssertionError("AI Provider should NEVER be called when no approved knowledge exists!")

        gw = AIProviderGateway(ExplodingProvider())

        outcome = await SuggestionService.generate_suggestion(
            db=db,
            captured_event_id=event.id,
            actor="agent-1",
            gateway=gw,
        )

        assert outcome.status == "knowledge_unavailable"
        assert outcome.suggestion is None
        assert db.query(ResponseSuggestion).count() == 0
    finally:
        db.close()


@pytest.mark.anyio
async def test_ai_references_to_unsupplied_sources_are_rejected(suggestion_env):
    db = suggestion_env["session_factory"]()
    try:
        art = KnowledgeService.create_article(
            db=db,
            stable_key="art-valid-source",
            title="Valid Source Title",
            product_scope="prod",
            issue_type="issue",
            initial_content="Valid approved knowledge content for citation test.",
            created_by="agent-1",
        )
        v1 = KnowledgeService.approve_version(db=db, article_id=art.id, version_num=1, approved_by="approver-1")

        event = CapturedEvent(
            provider="manual",
            event_id="evt-hallucinated-src",
            event_type="message.received",
            occurred_at=datetime.now(timezone.utc),
            actor_id="client-1",
            actor_role="client",
            conversation_id="conv-hallucinated",
            payload_text="Valid approved knowledge query",
            schema_version=1,
            correlation_id="corr-hallucinated",
        )
        db.add(event)
        db.commit()

        # Model hallucinates a citation to an unprovided article_id="fake-unprovided-id"
        provider = FakeCooperativeProvider(
            draft="Draft citing hallucinated source",
            source_refs=[
                SourceRef(article_id="fake-unprovided-id", version=99),
                SourceRef(article_id=art.id, version=1),
            ],
        )
        gw = AIProviderGateway(provider)

        outcome = await SuggestionService.generate_suggestion(
            db=db,
            captured_event_id=event.id,
            actor="agent-1",
            gateway=gw,
        )

        assert outcome.status == "suggested"
        # Only the valid supplied source is stored in suggestion_sources
        stored_sources = db.query(SuggestionSource).filter_by(suggestion_id=outcome.suggestion.id).all()
        assert len(stored_sources) == 1
        assert stored_sources[0].article_id == art.id
    finally:
        db.close()


@pytest.mark.anyio
async def test_prohibited_resolution_claim_is_sanitized_when_unsupported(suggestion_env):
    db = suggestion_env["session_factory"]()
    try:
        art = KnowledgeService.create_article(
            db=db,
            stable_key="art-no-resolution",
            title="Investigation In Progress Policy",
            product_scope="prod",
            issue_type="issue",
            initial_content="We are investigating the logs. Never claim resolution until verified.",
            created_by="agent-1",
            prohibited_claims=["claim resolution"],
        )
        KnowledgeService.approve_version(db=db, article_id=art.id, version_num=1, approved_by="approver-1")

        event = CapturedEvent(
            provider="manual",
            event_id="evt-proh-claim",
            event_type="message.received",
            occurred_at=datetime.now(timezone.utc),
            actor_id="client-1",
            actor_role="client",
            conversation_id="conv-proh",
            payload_text="Investigating logs in progress",
            schema_version=1,
            correlation_id="corr-proh",
        )
        db.add(event)
        db.commit()

        # Model attempts to claim the issue is resolved
        provider = FakeCooperativeProvider(
            draft="Great news! Your issue is resolved and everything has been fixed.",
            source_refs=[SourceRef(article_id=art.id, version=1)],
        )
        gw = AIProviderGateway(provider)

        outcome = await SuggestionService.generate_suggestion(
            db=db,
            captured_event_id=event.id,
            actor="agent-1",
            gateway=gw,
        )

        assert outcome.status == "suggested"
        # Prohibited claim check must prevent the unsupported resolution claim from appearing
        assert "issue is resolved" not in outcome.suggestion.draft.lower()
        assert "resolved and everything has been fixed" not in outcome.suggestion.draft.lower()
    finally:
        db.close()


@pytest.mark.anyio
async def test_provider_failures_fail_safely(suggestion_env):
    db = suggestion_env["session_factory"]()
    try:
        art = KnowledgeService.create_article(
            db=db,
            stable_key="art-fail-safe",
            title="Fail Safe Policy",
            product_scope="prod",
            issue_type="issue",
            initial_content="Approved policy for failure handling.",
            created_by="agent-1",
        )
        KnowledgeService.approve_version(db=db, article_id=art.id, version_num=1, approved_by="approver-1")

        event = CapturedEvent(
            provider="manual",
            event_id="evt-fail-safe",
            event_type="message.received",
            occurred_at=datetime.now(timezone.utc),
            actor_id="client-1",
            actor_role="client",
            conversation_id="conv-fail",
            payload_text="Approved policy failure test",
            schema_version=1,
            correlation_id="corr-fail",
        )
        db.add(event)
        db.commit()

        # 1. Timeout
        outcome_timeout = await SuggestionService.generate_suggestion(
            db=db,
            captured_event_id=event.id,
            actor="agent-1",
            gateway=AIProviderGateway(FakeTimeoutProvider()),
        )
        assert outcome_timeout.status == "provider_timeout"
        assert outcome_timeout.suggestion is None

        # 2. Unavailable
        outcome_unavail = await SuggestionService.generate_suggestion(
            db=db,
            captured_event_id=event.id,
            actor="agent-1",
            gateway=AIProviderGateway(FakeUnavailableProvider()),
        )
        assert outcome_unavail.status == "provider_unavailable"
        assert outcome_unavail.suggestion is None

        # 3. Invalid output
        outcome_invalid = await SuggestionService.generate_suggestion(
            db=db,
            captured_event_id=event.id,
            actor="agent-1",
            gateway=AIProviderGateway(FakeInvalidOutputProvider()),
        )
        assert outcome_invalid.status == "invalid_provider_output"
        assert outcome_invalid.suggestion is None
    finally:
        db.close()
