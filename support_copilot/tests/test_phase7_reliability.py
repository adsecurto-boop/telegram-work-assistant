import json
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from support_copilot.config import Settings
from support_copilot.database import create_db_engine, create_session_factory
from support_copilot.knowledge_service import KnowledgeService
from support_copilot.main import create_app
from support_copilot.migration_runner import MigrationRunner
from support_copilot.models import (
    AuditEvent,
    Base,
    KnowledgeArticle,
    KnowledgeArticleVersion,
    ResponseLearningCandidate,
    ResponseSuggestion,
    SentResponse,
)
from support_copilot.retrieval_evaluation import evaluate_retrieval


TOKEN_ALL = "token-admin-all-capabilities"
TOKEN_WRITE_ONLY = "token-write-only-no-approve"
TOKEN_READ_ONLY = "token-read-only"


@pytest.fixture
def env(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_file = data_dir / "phase7_test.sqlite3"
    settings = Settings(
        data_dir=str(data_dir),
        db_path=f"sqlite:///{db_file}",
        api_tokens={
            TOKEN_ALL: ["knowledge:read", "knowledge:write", "knowledge:approve", "suggestion:review", "capture:write"],
            TOKEN_WRITE_ONLY: ["knowledge:read", "knowledge:write", "suggestion:review", "capture:write"],
            TOKEN_READ_ONLY: ["knowledge:read"],
        },
    )
    runner = MigrationRunner(settings)
    runner.run_upgrade("head")

    engine = create_db_engine(settings.db_path)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    app = create_app(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        verify_schema=False,
        verify_auth=False,
    )
    client = TestClient(app)

    yield {"client": client, "session_factory": session_factory, "engine": engine, "data_dir": data_dir, "settings": settings}

    engine.dispose()


def auth(token=TOKEN_ALL):
    return {"Authorization": f"Bearer {token}"}


def seed_suggestion_and_sent_response(db, confirmed=True):
    from support_copilot.models import CapturedEvent
    evt = CapturedEvent(
        provider="manual",
        event_id="evt-learn-1",
        event_type="message.received",
        occurred_at=datetime.now(timezone.utc),
        actor_id="client-1",
        actor_role="client",
        conversation_id="conv-1",
        payload_text="Need help with attendance logs.",
        schema_version=1,
        correlation_id="corr-learn-1",
    )
    db.add(evt)
    db.commit()

    sugg = ResponseSuggestion(
        id="sugg-learn-1",
        captured_event_id=evt.id,
        lifecycle_status="suggested",
        draft="Initial draft suggestion.",
        missing_facts="[]",
        assumptions="[]",
        prohibited_claim_evaluation="{}",
        confidence=0.9,
        recommended_action="review",
        provider_identifier="test",
        correlation_id="corr-learn-1",
        created_at=datetime.now(timezone.utc),
    )
    db.add(sugg)
    db.commit()
    db.refresh(sugg)

    if confirmed:
        sent = SentResponse(
            id="sent-learn-1",
            suggestion_id=sugg.id,
            exact_sent_text="Verified reply sent to client with exact date range check.",
            final_text_hash="hash-12345",
            confirmed_by_actor="agent-1",
            confirmation_idempotency_key="idemp-learn-1",
            confirmed_at=datetime.now(timezone.utc),
        )
        db.add(sent)
        db.commit()
    return sugg


def test_cannot_create_learning_candidate_from_unconfirmed_suggestion(env):
    client = env["client"]
    db = env["session_factory"]()
    try:
        sugg = seed_suggestion_and_sent_response(db, confirmed=False)
    finally:
        db.close()

    response = client.post(
        f"/v1/suggestions/{sugg.id}/learning-candidate",
        headers=auth(TOKEN_ALL),
        json={"candidate_title": "Attendance date range policy"},
    )
    assert response.status_code == 400
    assert "unconfirmed" in response.json()["detail"].lower()


def test_copied_unconfirmed_suggestion_cannot_create_learning_candidate(env):
    client = env["client"]
    db = env["session_factory"]()
    try:
        sugg = seed_suggestion_and_sent_response(db, confirmed=False)
        sugg.copied_at = datetime.now(timezone.utc)
        sugg.lifecycle_status = "copied"
        db.commit()
    finally:
        db.close()

    response = client.post(
        f"/v1/suggestions/{sugg.id}/learning-candidate",
        headers=auth(TOKEN_ALL),
        json={"candidate_title": "Attendance date range policy"},
    )
    assert response.status_code == 400
    assert "unconfirmed" in response.json()["detail"].lower()


def test_ai_cannot_approve_learning_candidate(env):
    from support_copilot.learning_service import LearningService
    client = env["client"]
    db = env["session_factory"]()
    try:
        sugg = seed_suggestion_and_sent_response(db, confirmed=True)
    finally:
        db.close()

    prop_resp = client.post(
        f"/v1/suggestions/{sugg.id}/learning-candidate",
        headers=auth(TOKEN_ALL),
        json={"candidate_title": "Attendance Policy"},
    )
    candidate_id = prop_resp.json()["id"]

    db = env["session_factory"]()
    try:
        for ai_actor in ["ai", "gemini", "copilot-ai", "system"]:
            with pytest.raises(ValueError) as exc:
                LearningService.review_candidate(
                    db=db,
                    candidate_id=candidate_id,
                    decision="approve",
                    actor=ai_actor,
                )
            assert "not authorized" in str(exc.value).lower()
    finally:
        db.close()


def test_learning_candidate_proposal_and_rejection_lifecycle(env):
    client = env["client"]
    db = env["session_factory"]()
    try:
        sugg = seed_suggestion_and_sent_response(db, confirmed=True)
    finally:
        db.close()

    # Propose candidate
    prop_resp = client.post(
        f"/v1/suggestions/{sugg.id}/learning-candidate",
        headers=auth(TOKEN_ALL),
        json={
            "candidate_title": "Attendance date range policy",
            "notes": "Learned from manual resolution.",
        },
    )
    assert prop_resp.status_code == 200, prop_resp.text
    candidate = prop_resp.json()
    assert candidate["lifecycle_status"] == "proposed"
    assert candidate["candidate_content"] == "Verified reply sent to client with exact date range check."

    # Reject candidate
    rej_resp = client.post(
        f"/v1/learning/candidates/{candidate['id']}/review",
        headers=auth(TOKEN_ALL),
        json={"decision": "reject"},
    )
    assert rej_resp.status_code == 200
    assert rej_resp.json()["lifecycle_status"] == "rejected"

    # Verify no new knowledge version was created
    db = env["session_factory"]()
    try:
        assert db.query(KnowledgeArticle).filter_by(title="Attendance date range policy").count() == 0
        assert db.query(AuditEvent).filter_by(action="learning.candidate_rejected").count() == 1
    finally:
        db.close()


def test_learning_candidate_approval_creates_immutable_knowledge_version_idempotently(env):
    client = env["client"]
    db = env["session_factory"]()
    try:
        sugg = seed_suggestion_and_sent_response(db, confirmed=True)
    finally:
        db.close()

    prop_resp = client.post(
        f"/v1/suggestions/{sugg.id}/learning-candidate",
        headers=auth(TOKEN_ALL),
        json={
            "candidate_title": "Attendance Policy Resolution",
            "target_stable_key": "learned-attendance-policy",
        },
    )
    candidate_id = prop_resp.json()["id"]

    # Candidate approval requires knowledge:approve capability
    denied = client.post(
        f"/v1/learning/candidates/{candidate_id}/review",
        headers=auth(TOKEN_WRITE_ONLY),
        json={"decision": "approve"},
    )
    assert denied.status_code == 403

    # Authorized approval
    app_resp = client.post(
        f"/v1/learning/candidates/{candidate_id}/review",
        headers=auth(TOKEN_ALL),
        json={"decision": "approve"},
    )
    assert app_resp.status_code == 200
    body = app_resp.json()
    assert body["lifecycle_status"] == "approved"
    assert body["resulting_article_version_id"] is not None

    # Idempotent second approval
    app_resp_2 = client.post(
        f"/v1/learning/candidates/{candidate_id}/review",
        headers=auth(TOKEN_ALL),
        json={"decision": "approve"},
    )
    assert app_resp_2.status_code == 200
    assert app_resp_2.json()["resulting_article_version_id"] == body["resulting_article_version_id"]

    # Verify immutable article and approved version exist in database
    db = env["session_factory"]()
    try:
        article = db.query(KnowledgeArticle).filter_by(stable_key="learned-attendance-policy").first()
        assert article is not None
        assert article.lifecycle_status == "approved"

        version = db.query(KnowledgeArticleVersion).filter_by(id=body["resulting_article_version_id"]).first()
        assert version is not None
        assert version.approval_state == "approved"
        assert "Verified reply sent to client" in version.immutable_content
    finally:
        db.close()


def test_learning_candidate_approval_updates_existing_article_version(env):
    client = env["client"]
    db = env["session_factory"]()
    try:
        # Pre-seed existing article with version 1
        existing_art = KnowledgeService.create_article(
            db=db,
            stable_key="existing-policy",
            title="Existing Policy Title",
            product_scope="core",
            issue_type="missing_logs",
            initial_content="Initial version 1 content.",
            created_by="admin",
        )
        KnowledgeService.approve_version(db=db, article_id=existing_art.id, version_num=1, approved_by="admin")
        sugg = seed_suggestion_and_sent_response(db, confirmed=True)
    finally:
        db.close()

    # Propose candidate pointing to existing article
    prop_resp = client.post(
        f"/v1/suggestions/{sugg.id}/learning-candidate",
        headers=auth(TOKEN_ALL),
        json={
            "candidate_title": "Existing Policy Update",
            "target_article_id": existing_art.id,
        },
    )
    assert prop_resp.status_code == 200
    candidate_data = prop_resp.json()
    candidate_id = candidate_data["id"]
    assert candidate_data["product_scope"] == "core"
    assert candidate_data["issue_type"] == "missing_logs"

    # Approve candidate - must create version 2 for existing article
    app_resp = client.post(
        f"/v1/learning/candidates/{candidate_id}/review",
        headers=auth(TOKEN_ALL),
        json={"decision": "approve"},
    )
    assert app_resp.status_code == 200
    body = app_resp.json()
    assert body["lifecycle_status"] == "approved"

    db = env["session_factory"]()
    try:
        v2 = db.query(KnowledgeArticleVersion).filter_by(id=body["resulting_article_version_id"]).one()
        assert v2.version == 2
        assert v2.approval_state == "approved"
        assert "Verified reply sent to client" in v2.immutable_content
    finally:
        db.close()


def test_detailed_health_reporting_and_secret_redaction(env):
    # Pre-create a backup manifest to verify timestamp extraction
    backups_dir = env["data_dir"] / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    manifest = backups_dir / "backup_20260921_010000_manifest.json"
    manifest.write_text(
        json.dumps({
            "backup_file": "backup_20260921_010000.sqlite3",
            "schema_revision": "005_phase7",
            "creation_timestamp": "2026-09-21T01:00:00Z",
            "sha256_checksum": "fake-hash",
        }),
        encoding="utf-8",
    )

    client = env["client"]
    resp = client.get("/v1/health/detailed")
    assert resp.status_code == 200
    data = resp.json()

    # Required structure
    assert data["status"] in {"healthy", "degraded", "unhealthy"}
    assert data["api_status"] == "healthy"
    assert data["database"]["connected"] is True
    assert data["database"]["integrity_check"] == "ok"
    assert data["database"]["schema_ready"] is True
    assert data["backup"]["last_backup_timestamp"] == "2026-09-21T01:00:00Z"

    # Redaction checks: no keys, secrets, or file paths exposed
    serialized = json.dumps(data)
    assert "token-" not in serialized
    assert "secret" not in serialized.lower() or "configured" in serialized.lower()
    assert "AIza" not in serialized
    assert "sk-" not in serialized


def test_retrieval_evaluation_runner(env, tmp_path):
    db = env["session_factory"]()
    try:
        art = KnowledgeService.create_article(
            db=db,
            stable_key="art-attendance-policy",
            title="Attendance Log Request Policy",
            product_scope="core",
            issue_type="missing_logs",
            initial_content="When attendance logs are missing, check date range and ingestion status.",
            created_by="tester",
        )
        KnowledgeService.approve_version(db, art.id, 1, "approver")
    finally:
        db.close()

    # Create temporary evaluation dataset
    dataset_file = tmp_path / "eval_test.json"
    dataset_file.write_text(
        json.dumps(
            [
                {
                    "case_id": "eval-test-1",
                    "query": "missing attendance logs for date range",
                    "product_scope": "core",
                    "issue_type": "missing_logs",
                    "expected_stable_key": "art-attendance-policy",
                    "negative_keys": [],
                    "rationale": "Query must match attendance article.",
                }
            ]
        ),
        encoding="utf-8",
    )

    db = env["session_factory"]()
    try:
        report = evaluate_retrieval(str(dataset_file), db)
        assert report.total_cases == 1
        assert report.recall_at_1 == 1.0
        assert report.recall_at_3 == 1.0
        assert report.mean_reciprocal_rank == 1.0
        assert len(report.failed_case_ids) == 0
    finally:
        db.close()

    # Empty dataset must fail
    empty_file = tmp_path / "empty_eval.json"
    empty_file.write_text("[]", encoding="utf-8")
    db = env["session_factory"]()
    try:
        with pytest.raises(ValueError) as exc:
            evaluate_retrieval(str(empty_file), db)
        assert "empty" in str(exc.value).lower()
    finally:
        db.close()
