import json
import pytest
from datetime import datetime, timezone
from fastapi.testclient import TestClient

from support_copilot.config import Settings
from support_copilot.database import create_db_engine, create_session_factory
from support_copilot.migration_runner import MigrationRunner
from support_copilot.knowledge_service import KnowledgeService, compute_content_hash
from support_copilot.models import KnowledgeArticle, KnowledgeArticleVersion, AuditEvent
from support_copilot.main import create_app


@pytest.fixture
def knowledge_env(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_file = data_dir / "knowledge_test.sqlite3"
    settings = Settings(
        data_dir=str(data_dir),
        db_path=f"sqlite:///{db_file}",
        api_tokens={
            "token-knowledge-writer": ["knowledge:write"],
            "token-knowledge-approver": ["knowledge:approve"],
            "token-knowledge-reader": ["knowledge:read"],
            "token-admin": ["knowledge:read", "knowledge:write", "knowledge:approve"],
        },
    )
    runner = MigrationRunner(settings)
    runner.run_upgrade("head")

    engine = create_db_engine(settings.db_path)
    session_factory = create_session_factory(engine)
    app = create_app(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        verify_schema=False,
        verify_auth=False,
    )
    client = TestClient(app)

    yield {"client": client, "session_factory": session_factory, "engine": engine}

    engine.dispose()


def test_article_and_version_creation(knowledge_env):
    db = knowledge_env["session_factory"]()
    try:
        art = KnowledgeService.create_article(
            db=db,
            stable_key="art-attendance-policy",
            title="Attendance Log Request Policy",
            product_scope="core_attendance",
            issue_type="missing_logs",
            initial_content="Clients must provide date ranges and system logs.",
            created_by="agent-1",
            client_scope="corp_client",
            required_facts=["date_range_specified", "log_type_specified"],
            prohibited_claims=["claim resolution"],
        )
        assert art.id is not None
        assert art.stable_key == "art-attendance-policy"
        assert art.lifecycle_status == "draft"

        # Check version 1
        v1 = db.query(KnowledgeArticleVersion).filter_by(article_id=art.id, version=1).first()
        assert v1 is not None
        assert v1.approval_state == "draft"
        assert "date_range_specified" in v1.required_facts
        assert "claim resolution" in v1.prohibited_claims

        # Check audit event
        audit = db.query(AuditEvent).filter_by(action="knowledge.article.created").first()
        assert audit is not None
        assert audit.actor == "agent-1"
    finally:
        db.close()


def test_duplicate_stable_key_rejected(knowledge_env):
    db = knowledge_env["session_factory"]()
    try:
        KnowledgeService.create_article(
            db=db,
            stable_key="unique-key-1",
            title="Title 1",
            product_scope="prod",
            issue_type="issue",
            initial_content="Content 1",
            created_by="agent-1",
        )
        with pytest.raises(ValueError) as exc:
            KnowledgeService.create_article(
                db=db,
                stable_key="unique-key-1",
                title="Title 2",
                product_scope="prod",
                issue_type="issue",
                initial_content="Content 2",
                created_by="agent-1",
            )
        assert "already exists" in str(exc.value)
    finally:
        db.close()


def test_approval_lifecycle_and_superseding(knowledge_env):
    db = knowledge_env["session_factory"]()
    try:
        art = KnowledgeService.create_article(
            db=db,
            stable_key="art-lifecycle",
            title="Lifecycle Policy",
            product_scope="prod",
            issue_type="issue",
            initial_content="Draft version 1 content",
            created_by="agent-1",
        )

        # Approve version 1
        v1 = KnowledgeService.approve_version(
            db=db,
            article_id=art.id,
            version_num=1,
            approved_by="approver-1",
        )
        assert v1.approval_state == "approved"
        assert v1.approved_at is not None
        assert v1.approved_by == "approver-1"

        db.refresh(art)
        assert art.lifecycle_status == "approved"

        # Cannot approve twice
        with pytest.raises(ValueError) as exc:
            KnowledgeService.approve_version(
                db=db,
                article_id=art.id,
                version_num=1,
                approved_by="approver-2",
            )
        assert "already approved" in str(exc.value)

        # Create version 2 (starts as draft)
        v2 = KnowledgeService.create_version(
            db=db,
            article_id=art.id,
            content="Draft version 2 content",
            created_by="agent-1",
        )
        assert v2.version == 2
        assert v2.approval_state == "draft"

        # Approving version 2 supersedes version 1 (retires version 1)
        v2_app = KnowledgeService.approve_version(
            db=db,
            article_id=art.id,
            version_num=2,
            approved_by="approver-1",
        )
        assert v2_app.approval_state == "approved"

        db.refresh(v1)
        assert v1.approval_state == "retired"
        assert v1.retired_at is not None
    finally:
        db.close()


def test_retirement_lifecycle(knowledge_env):
    db = knowledge_env["session_factory"]()
    try:
        art = KnowledgeService.create_article(
            db=db,
            stable_key="art-retire",
            title="Retire Policy",
            product_scope="prod",
            issue_type="issue",
            initial_content="Initial content",
            created_by="agent-1",
        )
        v1 = KnowledgeService.approve_version(
            db=db, article_id=art.id, version_num=1, approved_by="approver-1"
        )

        # Retire version 1
        v1_ret = KnowledgeService.retire_version(
            db=db, article_id=art.id, version_num=1, retired_by="approver-1"
        )
        assert v1_ret.approval_state == "retired"
        assert v1_ret.retired_at is not None

        db.refresh(art)
        assert art.lifecycle_status == "retired"

        # Cannot retire already retired version
        with pytest.raises(ValueError) as exc:
            KnowledgeService.retire_version(
                db=db, article_id=art.id, version_num=1, retired_by="approver-1"
            )
        assert "already retired" in str(exc.value)

        # Cannot approve retired version
        with pytest.raises(ValueError) as exc:
            KnowledgeService.approve_version(
                db=db, article_id=art.id, version_num=1, approved_by="approver-1"
            )
        assert "Cannot approve a retired version" in str(exc.value)
    finally:
        db.close()


def test_content_hash_determinism():
    h1 = compute_content_hash("Test content", ["fact_b", "fact_a"], ["claim_2", "claim_1"])
    h2 = compute_content_hash("Test content", ["fact_a", "fact_b"], ["claim_1", "claim_2"])
    h3 = compute_content_hash("Different content", ["fact_a", "fact_b"], ["claim_1", "claim_2"])
    assert h1 == h2
    assert h1 != h3


def test_knowledge_api_capability_authorization(knowledge_env):
    client = knowledge_env["client"]

    # 1. Unauthenticated request rejected
    res_unauth = client.post(
        "/v1/knowledge/articles",
        json={
            "stable_key": "api-art-1",
            "title": "API Art",
            "product_scope": "prod",
            "issue_type": "issue",
            "initial_content": "Content",
        },
    )
    assert res_unauth.status_code == 401

    # 2. Reader cannot write (insufficient capability)
    res_reader = client.post(
        "/v1/knowledge/articles",
        json={
            "stable_key": "api-art-1",
            "title": "API Art",
            "product_scope": "prod",
            "issue_type": "issue",
            "initial_content": "Content",
        },
        headers={"Authorization": "Bearer token-knowledge-reader"},
    )
    assert res_reader.status_code == 403

    # 3. Writer can create article
    res_writer = client.post(
        "/v1/knowledge/articles",
        json={
            "stable_key": "api-art-1",
            "title": "API Art",
            "product_scope": "prod",
            "issue_type": "issue",
            "initial_content": "Content",
        },
        headers={"Authorization": "Bearer token-knowledge-writer"},
    )
    assert res_writer.status_code == 200
    art_data = res_writer.json()
    art_id = art_data["id"]

    # 4. Writer cannot approve (requires knowledge:approve)
    res_approve_fail = client.post(
        f"/v1/knowledge/articles/{art_id}/versions/1/approve",
        headers={"Authorization": "Bearer token-knowledge-writer"},
    )
    assert res_approve_fail.status_code == 403

    # 5. Approver can approve
    res_approve_ok = client.post(
        f"/v1/knowledge/articles/{art_id}/versions/1/approve",
        headers={"Authorization": "Bearer token-knowledge-approver"},
    )
    assert res_approve_ok.status_code == 200
    assert res_approve_ok.json()["approval_state"] == "approved"
