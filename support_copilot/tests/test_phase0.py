import os
import sys
import json
import uuid
import asyncio
import sqlite3
import subprocess
import datetime
from datetime import timezone
from pathlib import Path
import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import OperationalError
from alembic.config import Config
from alembic import command

from support_copilot.config import Settings, ALLOWED_LOOPBACK_HOSTS, resolve_and_validate_db_path
from support_copilot.main import create_app
from support_copilot.models import Base, CapturedEvent, IntegrationIdempotency, AuditEvent
from support_copilot.database import (
    create_db_engine,
    create_session_factory,
    verify_schema_readiness,
)
from support_copilot.migration_runner import (
    MigrationRunner,
    MigrationError,
    create_online_backup,
)
from support_copilot.logger import logger, JSONFormatter
from support_copilot.service import CaptureService, IdempotencyConflictError
from support_copilot.ai_provider import (
    ProviderTimeoutError,
    ProviderUnavailableError,
    InvalidProviderOutputError,
    InternalProviderError,
)
from support_copilot.ai_gateway import AIProviderGateway
from support_copilot.tests.fakes import (
    FakeSuccessAIProvider,
    FakeUncooperativeTimeoutAIProvider,
    FakeUnavailableAIProvider,
    FakeInvalidOutputAIProvider,
    FakeUnexpectedExceptionAIProvider,
)

VALID_TOKEN = "valid-secret-token-12345678"
UNAUTHORIZED_TOKEN = "unauthorized-secret-token-9999"
INSUFFICIENT_TOKEN = "insufficient-secret-token-0000"

@pytest.fixture
def test_settings(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_file = data_dir / "test_copilot.sqlite3"
    return Settings(
        api_host="127.0.0.1",
        api_port=8000,
        data_dir=str(data_dir),
        db_path=f"sqlite:///{db_file}",
        api_tokens={
            VALID_TOKEN: ["capture:write"],
            INSUFFICIENT_TOKEN: ["read:only"],
        },
    )

@pytest.fixture
def app_and_db(test_settings):
    engine = create_db_engine(test_settings.db_path)
    Base.metadata.create_all(bind=engine)
    session_factory = create_session_factory(engine)
    app = create_app(
        settings=test_settings,
        engine=engine,
        session_factory=session_factory,
        verify_schema=True,
        verify_auth=True,
    )
    client = TestClient(app)
    yield client, session_factory
    Base.metadata.drop_all(bind=engine)
    engine.dispose()

def create_valid_payload(event_id=None, text="Hello world"):
    if event_id is None:
        event_id = f"evt-{uuid.uuid4()}"
    return {
        "event": {
            "provider": "manual",
            "event_id": event_id,
            "event_type": "message.received",
            "occurred_at": datetime.datetime.now(timezone.utc).isoformat(),
            "actor": {"external_id": "client-1", "role": "client"},
            "conversation": {"external_id": "case-101", "case_hint": "billing"},
            "payload": {"text": text},
            "schema_version": 1,
        }
    }

# 1. Package import creates no file and opens no database
def test_package_import_creates_no_file_and_opens_no_database(tmp_path):
    repo_root = os.path.abspath(".")
    code = "import support_copilot.main, support_copilot.database, support_copilot.models"
    env = os.environ.copy()
    env["PYTHONPATH"] = repo_root
    res = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 0, f"Subprocess import failed: {res.stderr}"
    created_files = list(tmp_path.iterdir())
    assert len(created_files) == 0, f"Import created files in working directory: {created_files}"

# 2. Unsafe bind configuration is rejected
def test_unsafe_bind_configuration_is_rejected():
    with pytest.raises(ValidationError) as exc_info:
        Settings(api_host="0.0.0.0")
    assert "Unsafe bind host" in str(exc_info.value)

    with pytest.raises(ValidationError) as exc_info:
        Settings(api_host="192.168.1.10")
    assert "Unsafe bind host" in str(exc_info.value)

# 3. Application startup fails without explicit authentication configuration
def test_application_startup_fails_without_explicit_authentication_configuration(tmp_path):
    settings = Settings(
        api_tokens={},
        data_dir=str(tmp_path),
        db_path=f"sqlite:///{tmp_path / 'no_auth.sqlite3'}",
    )
    with pytest.raises(ValueError) as exc_info:
        settings.validate_tokens_for_startup()
    assert "Missing required API authentication configuration" in str(exc_info.value)

# 4. Weak, placeholder, or malformed credentials are rejected
def test_weak_placeholder_or_malformed_credentials_are_rejected():
    # Documented placeholder
    s0 = Settings(api_tokens={"<GENERATE-A-PRIVATE-RANDOM-TOKEN>": ["capture:write"]})
    with pytest.raises(ValueError) as exc:
        s0.validate_tokens_for_startup()
    assert "placeholder" in str(exc.value)

    # Legacy placeholder
    s1 = Settings(api_tokens={"local-dev-token": ["capture:write"]})
    with pytest.raises(ValueError) as exc:
        s1.validate_tokens_for_startup()
    assert "placeholder" in str(exc.value)

    # Too short
    s2 = Settings(api_tokens={"short": ["capture:write"]})
    with pytest.raises(ValueError) as exc:
        s2.validate_tokens_for_startup()
    assert "at least 16 characters" in str(exc.value)

    # Unknown capability
    s3 = Settings(api_tokens={"a-valid-length-token-1234": ["invalid:capability"]})
    with pytest.raises(ValueError) as exc:
        s3.validate_tokens_for_startup()
    assert "Unknown capability" in str(exc.value)

# 5. Non-loopback requests are rejected
def test_non_loopback_requests_are_rejected(app_and_db):
    client, _ = app_and_db
    client_remote = TestClient(client.app, client=("192.168.1.100", 12345))
    response = client_remote.post(
        "/v1/captures/manual-message",
        json=create_valid_payload(),
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "Unauthorized. Loopback interface only."}

# 6. Missing bearer credentials are rejected
def test_missing_bearer_credentials_are_rejected(app_and_db):
    client, _ = app_and_db
    response = client.post(
        "/v1/captures/manual-message",
        json=create_valid_payload(),
    )
    assert response.status_code == 401
    assert "Missing authorization credential" in response.json()["detail"]

# 7. Incorrect bearer credentials are rejected
def test_incorrect_bearer_credentials_are_rejected(app_and_db):
    client, _ = app_and_db
    response = client.post(
        "/v1/captures/manual-message",
        json=create_valid_payload(),
        headers={"Authorization": f"Bearer {UNAUTHORIZED_TOKEN}"},
    )
    assert response.status_code == 401
    assert "Invalid authorization token" in response.json()["detail"]

# 8. Insufficient capability is rejected
def test_insufficient_capability_is_rejected(app_and_db):
    client, _ = app_and_db
    response = client.post(
        "/v1/captures/manual-message",
        json=create_valid_payload(),
        headers={"Authorization": f"Bearer {INSUFFICIENT_TOKEN}"},
    )
    assert response.status_code == 403
    assert "Insufficient permissions" in response.json()["detail"]

# 9. Correct scoped authentication succeeds
def test_correct_scoped_authentication_succeeds(app_and_db):
    client, _ = app_and_db
    response = client.post(
        "/v1/captures/manual-message",
        json=create_valid_payload(),
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "accepted"
    assert data["correlation_id"].startswith("corr-")

# 10. Extra or malformed schema fields are rejected
def test_extra_or_malformed_schema_fields_are_rejected(app_and_db):
    client, _ = app_and_db
    payload = create_valid_payload()
    payload["event"]["unexpected_field"] = "injection"
    response = client.post(
        "/v1/captures/manual-message",
        json=payload,
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert response.status_code == 422
    assert response.json()["error_type"] == "validation_error"

# 11. Empty text is rejected
def test_empty_text_is_rejected(app_and_db):
    client, _ = app_and_db
    payload = create_valid_payload(text="   ")
    response = client.post(
        "/v1/captures/manual-message",
        json=payload,
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert response.status_code == 422

# 12. Naive timestamps are rejected
def test_naive_timestamps_are_rejected(app_and_db):
    client, _ = app_and_db
    payload = create_valid_payload()
    payload["event"]["occurred_at"] = "2026-09-20T18:00:00"
    response = client.post(
        "/v1/captures/manual-message",
        json=payload,
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert response.status_code == 422

# 13. A valid event creates exactly one captured event
def test_valid_event_creates_exactly_one_captured_event(app_and_db):
    client, session_factory = app_and_db
    event_id = f"evt-single-{uuid.uuid4()}"
    client.post(
        "/v1/captures/manual-message",
        json=create_valid_payload(event_id=event_id),
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    db = session_factory()
    try:
        events = db.query(CapturedEvent).filter_by(event_id=event_id).all()
        assert len(events) == 1
    finally:
        db.close()

# 14. A valid event creates exactly one idempotency outcome
def test_valid_event_creates_exactly_one_idempotency_outcome(app_and_db):
    client, session_factory = app_and_db
    event_id = f"evt-idemp-{uuid.uuid4()}"
    client.post(
        "/v1/captures/manual-message",
        json=create_valid_payload(event_id=event_id),
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    db = session_factory()
    try:
        records = db.query(IntegrationIdempotency).filter_by(event_id=event_id).all()
        assert len(records) == 1
    finally:
        db.close()

# 15. A valid event creates exactly one safe audit event
def test_valid_event_creates_exactly_one_safe_audit_event(app_and_db):
    client, session_factory = app_and_db
    event_id = f"evt-audit-{uuid.uuid4()}"
    res = client.post(
        "/v1/captures/manual-message",
        json=create_valid_payload(event_id=event_id),
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    corr_id = res.json()["correlation_id"]
    db = session_factory()
    try:
        audits = db.query(AuditEvent).filter_by(correlation_id=corr_id).all()
        assert len(audits) == 1
        assert VALID_TOKEN not in audits[0].details_json
    finally:
        db.close()

# 16. Duplicate delivery returns the original correlation ID
def test_duplicate_delivery_returns_original_correlation_id(app_and_db):
    client, _ = app_and_db
    event_id = f"evt-dup-id-{uuid.uuid4()}"
    payload = create_valid_payload(event_id=event_id)
    r1 = client.post(
        "/v1/captures/manual-message",
        json=payload,
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    r2 = client.post(
        "/v1/captures/manual-message",
        json=payload,
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["correlation_id"] == r2.json()["correlation_id"]

# 17. Duplicate delivery creates no additional mutation
def test_duplicate_delivery_creates_no_additional_mutation(app_and_db):
    client, session_factory = app_and_db
    payload = create_valid_payload()
    client.post(
        "/v1/captures/manual-message",
        json=payload,
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    db = session_factory()
    try:
        c1 = db.query(CapturedEvent).count()
        c2 = db.query(IntegrationIdempotency).count()
        c3 = db.query(AuditEvent).count()
        client.post(
            "/v1/captures/manual-message",
            json=payload,
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        assert db.query(CapturedEvent).count() == c1
        assert db.query(IntegrationIdempotency).count() == c2
        assert db.query(AuditEvent).count() == c3
    finally:
        db.close()

# 18. Concurrent duplicate delivery creates one outcome
def test_concurrent_duplicate_delivery_creates_one_outcome(app_and_db):
    import concurrent.futures
    client, session_factory = app_and_db
    event_id = f"evt-conc-{uuid.uuid4()}"
    payload = create_valid_payload(event_id=event_id)

    def submit():
        t_client = TestClient(client.app)
        return t_client.post(
            "/v1/captures/manual-message",
            json=payload,
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(submit) for _ in range(5)]
        responses = [f.result() for f in futures]

    assert all(r.status_code == 200 for r in responses)
    c_ids = set(r.json()["correlation_id"] for r in responses)
    assert len(c_ids) == 1

    db = session_factory()
    try:
        assert db.query(CapturedEvent).filter_by(event_id=event_id).count() == 1
    finally:
        db.close()

# 19. Same idempotency key with changed payload returns conflict
def test_same_idempotency_key_with_changed_payload_returns_conflict(app_and_db):
    client, _ = app_and_db
    event_id = f"evt-conflict-{uuid.uuid4()}"
    p1 = create_valid_payload(event_id=event_id, text="Original")
    p2 = create_valid_payload(event_id=event_id, text="Tampered")
    client.post(
        "/v1/captures/manual-message",
        json=p1,
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    r2 = client.post(
        "/v1/captures/manual-message",
        json=p2,
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    assert r2.status_code == 409
    assert r2.json()["error_type"] == "idempotency_conflict"

# 20. Commit failure explicitly rolls back
def test_commit_failure_explicitly_rolls_back(app_and_db):
    client, session_factory = app_and_db
    db = session_factory()
    service = CaptureService(db)
    event_id = f"evt-fail-commit-{uuid.uuid4()}"
    payload = create_valid_payload(event_id=event_id)
    from support_copilot.schemas import EventEnvelope
    envelope = EventEnvelope.model_validate(payload["event"])

    def broken_commit():
        raise OperationalError("Simulated commit failure", {}, None)

    db.commit = broken_commit
    with pytest.raises(OperationalError):
        service.capture_manual_message(envelope, actor="tester")
    db.close()

    check_db = session_factory()
    try:
        assert check_db.query(CapturedEvent).filter_by(event_id=event_id).first() is None
    finally:
        check_db.close()

# 21. Flush failure explicitly rolls back
def test_flush_failure_explicitly_rolls_back(app_and_db):
    client, session_factory = app_and_db
    db = session_factory()
    service = CaptureService(db)
    event_id = f"evt-fail-flush-{uuid.uuid4()}"
    payload = create_valid_payload(event_id=event_id)
    from support_copilot.schemas import EventEnvelope
    envelope = EventEnvelope.model_validate(payload["event"])

    def broken_flush():
        raise OperationalError("Simulated flush failure", {}, None)

    db.flush = broken_flush
    orig_add = db.add
    def failing_add(obj):
        orig_add(obj)
        db.flush()
    db.add = failing_add

    with pytest.raises(OperationalError):
        service.capture_manual_message(envelope, actor="tester")
    db.close()

    check_db = session_factory()
    try:
        assert check_db.query(CapturedEvent).filter_by(event_id=event_id).first() is None
    finally:
        check_db.close()

# 22. Session is usable after rollback
def test_session_is_usable_after_rollback(app_and_db):
    client, session_factory = app_and_db
    db = session_factory()
    service = CaptureService(db)

    event_id_fail = f"evt-first-fail-{uuid.uuid4()}"
    p_fail = create_valid_payload(event_id=event_id_fail)
    from support_copilot.schemas import EventEnvelope
    env_fail = EventEnvelope.model_validate(p_fail["event"])

    orig_commit = db.commit
    def broken_commit():
        raise OperationalError("Simulated failure", {}, None)
    db.commit = broken_commit

    with pytest.raises(OperationalError):
        service.capture_manual_message(env_fail, actor="tester")

    db.commit = orig_commit
    event_id_ok = f"evt-subsequent-ok-{uuid.uuid4()}"
    p_ok = create_valid_payload(event_id=event_id_ok)
    env_ok = EventEnvelope.model_validate(p_ok["event"])

    res = service.capture_manual_message(env_ok, actor="tester")
    assert res.status == "accepted"

    assert db.query(CapturedEvent).filter_by(event_id=event_id_ok).first() is not None
    db.close()

# 23. Alembic obtains its normal URL from Settings
def test_alembic_obtains_normal_url_from_settings(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    test_db = data_dir / "settings_db.sqlite3"
    monkeypatch.setenv("COPILOT_DATA_DIR", str(data_dir))
    monkeypatch.setenv("COPILOT_DB_PATH", f"sqlite:///{test_db}")
    cfg = Config("support_copilot/alembic.ini")
    from support_copilot.migrations.env import get_database_url
    url = get_database_url(cfg)
    assert str(test_db) in url

# 24. Explicit test URL override still works
def test_explicit_test_url_override_still_works(tmp_path):
    override_db = tmp_path / "override.sqlite3"
    cfg = Config("support_copilot/alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{override_db}")
    command.upgrade(cfg, "head")
    assert os.path.exists(override_db)
    engine = create_engine(f"sqlite:///{override_db}")
    verify_schema_readiness(engine)
    engine.dispose()

# 25. Initial migration succeeds
def test_initial_migration_succeeds(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    test_db = data_dir / "initial_mig.sqlite3"
    settings = Settings(data_dir=str(data_dir), db_path=f"sqlite:///{test_db}")
    runner = MigrationRunner(settings)
    runner.run_upgrade("head")
    engine = create_db_engine(f"sqlite:///{test_db}")
    verify_schema_readiness(engine)
    engine.dispose()

# 26. Repeated migration is safe
def test_repeated_migration_is_safe(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    test_db = data_dir / "repeat_mig.sqlite3"
    settings = Settings(data_dir=str(data_dir), db_path=f"sqlite:///{test_db}")
    runner = MigrationRunner(settings)
    runner.run_upgrade("head")
    runner.run_upgrade("head")
    engine = create_db_engine(f"sqlite:///{test_db}")
    verify_schema_readiness(engine)
    engine.dispose()

# 27. Current revision can be inspected
def test_current_revision_can_be_inspected(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    test_db = data_dir / "inspect_rev.sqlite3"
    settings = Settings(data_dir=str(data_dir), db_path=f"sqlite:///{test_db}")
    runner = MigrationRunner(settings)
    runner.run_upgrade("head")
    runner.inspect_current()

# 28. Downgrade behaves as documented and requires confirmation
def test_downgrade_behaves_as_documented_and_requires_confirmation(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    test_db = data_dir / "downgrade_test.sqlite3"
    settings = Settings(data_dir=str(data_dir), db_path=f"sqlite:///{test_db}")
    runner = MigrationRunner(settings)
    runner.run_upgrade("head")

    # Attempting downgrade without confirmation flag must fail
    with pytest.raises(MigrationError) as exc_info:
        runner.run_downgrade("base", allow_downgrade=False)
    assert "--allow-downgrade" in str(exc_info.value)

    # Downgrade with confirmation flag creates backup and proceeds
    backup = runner.run_downgrade("base", allow_downgrade=True)
    assert os.path.exists(backup)

    engine = create_db_engine(f"sqlite:///{test_db}")
    with pytest.raises(RuntimeError):
        verify_schema_readiness(engine)
    engine.dispose()

# 29. Real injected migration failure restores original data
def test_real_injected_migration_failure_restores_original_data(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    test_db = data_dir / "injected_failure.sqlite3"
    settings = Settings(data_dir=str(data_dir), db_path=f"sqlite:///{test_db}")
    runner = MigrationRunner(settings)
    runner.run_upgrade("head")

    engine = create_db_engine(f"sqlite:///{test_db}")
    session_factory = sessionmaker(bind=engine)
    db = session_factory()
    event = CapturedEvent(
        provider="manual",
        event_id="preserved-after-fail",
        event_type="message.received",
        occurred_at=datetime.datetime.now(timezone.utc),
        actor_id="client-1",
        actor_role="client",
        conversation_id="case-1",
        payload_text="Crucial preserved text",
        schema_version=1,
        correlation_id="corr-preserve",
    )
    db.add(event)
    db.commit()
    db.close()
    engine.dispose()

    with pytest.raises(MigrationError) as exc_info:
        runner.run_upgrade("head", _fail_after_backup=True)
    assert "Simulated failure inside migration runner" in str(exc_info.value)

    check_engine = create_db_engine(f"sqlite:///{test_db}")
    check_session = sessionmaker(bind=check_engine)()
    restored = check_session.query(CapturedEvent).filter_by(event_id="preserved-after-fail").first()
    assert restored is not None
    assert restored.payload_text == "Crucial preserved text"
    check_session.close()
    check_engine.dispose()

# 30. SQLite backup includes committed WAL data safely
def test_sqlite_backup_includes_committed_wal_data_safely(tmp_path):
    db_file = tmp_path / "wal_source.sqlite3"
    backup_file = tmp_path / "wal_backup.sqlite3"
    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("CREATE TABLE test_wal (id INT, value TEXT);")
    conn.execute("INSERT INTO test_wal VALUES (1, 'in_wal');")
    conn.commit()

    create_online_backup(str(db_file), str(backup_file))
    conn.close()

    backup_conn = sqlite3.connect(str(backup_file))
    cursor = backup_conn.cursor()
    cursor.execute("SELECT value FROM test_wal WHERE id = 1;")
    row = cursor.fetchone()
    backup_conn.close()
    assert row is not None
    assert row[0] == "in_wal"

# 31. Backups use unique names and are not overwritten
def test_backups_use_unique_names_and_are_not_overwritten(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    test_db = data_dir / "unique_backups.sqlite3"
    settings = Settings(data_dir=str(data_dir), db_path=f"sqlite:///{test_db}")
    runner = MigrationRunner(settings)
    runner.run_upgrade("head")

    b1 = runner.run_upgrade("head")
    b2 = runner.run_upgrade("head")
    assert b1 != b2
    assert os.path.exists(b1)
    assert os.path.exists(b2)

# 32. Provider gateway enforces timeout against a non-cooperative provider
def test_provider_gateway_enforces_timeout_against_non_cooperative_provider():
    uncooperative = FakeUncooperativeTimeoutAIProvider(sleep_seconds=10.0)
    gateway = AIProviderGateway(uncooperative)
    with pytest.raises(ProviderTimeoutError):
        asyncio.run(gateway.get_suggestion("Draft prompt", {}, timeout=0.05))

# 33. Provider unavailable produces the correct typed failure
def test_provider_unavailable_produces_correct_typed_failure():
    gateway = AIProviderGateway(FakeUnavailableAIProvider())
    with pytest.raises(ProviderUnavailableError):
        asyncio.run(gateway.get_suggestion("Draft prompt", {}))

# 34. Invalid provider output produces the correct typed failure
def test_invalid_provider_output_produces_correct_typed_failure():
    gateway = AIProviderGateway(FakeInvalidOutputAIProvider())
    with pytest.raises(InvalidProviderOutputError):
        asyncio.run(gateway.get_suggestion("Draft prompt", {}))

# 35. Unexpected provider failure is safely classified
def test_unexpected_provider_failure_is_safely_classified():
    gateway = AIProviderGateway(FakeUnexpectedExceptionAIProvider())
    with pytest.raises(InternalProviderError):
        asyncio.run(gateway.get_suggestion("Draft prompt", {}))

# 36. Secrets are absent from logs, errors, audit records, and health output
def test_secrets_absent_from_logs_errors_audit_and_health(app_and_db):
    client, session_factory = app_and_db
    secret_token = "secret-token-to-redact-9999"

    formatter = JSONFormatter()
    record = logger.makeRecord(
        name="support_copilot",
        level=20,
        fn="test",
        lno=1,
        msg=f"Auth Bearer {secret_token}",
        args=(),
        exc_info=None,
    )
    formatted = formatter.format(record)
    assert secret_token not in formatted
    assert "[REDACTED]" in formatted

    payload = create_valid_payload()
    res = client.post(
        "/v1/captures/manual-message",
        json=payload,
        headers={"Authorization": f"Bearer {VALID_TOKEN}"},
    )
    corr_id = res.json()["correlation_id"]
    db = session_factory()
    try:
        audit = db.query(AuditEvent).filter_by(correlation_id=corr_id).first()
        assert VALID_TOKEN not in audit.details_json
    finally:
        db.close()

    health_res = client.get("/v1/health")
    assert health_res.status_code == 200
    assert "token" not in health_res.text.lower()
    assert "db" not in health_res.text.lower()

# 37. Runtime factory smoke test succeeds after migration
def test_runtime_factory_smoke_test_succeeds_after_migration(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    test_db = data_dir / "smoke.sqlite3"
    monkeypatch.setenv("COPILOT_DATA_DIR", str(data_dir))
    monkeypatch.setenv("COPILOT_DB_PATH", f"sqlite:///{test_db}")
    monkeypatch.setenv(
        "COPILOT_API_TOKENS",
        json.dumps({"smoke-test-token-valid123456": ["capture:write"]}),
    )
    runner = MigrationRunner(Settings(data_dir=str(data_dir), db_path=f"sqlite:///{test_db}"))
    runner.run_upgrade("head")

    from support_copilot.main import create_production_app
    app = create_production_app()
    client = TestClient(app)
    res = client.get("/v1/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "schema_ready": True}

# 38. Runtime startup rejects an unmigrated database
def test_runtime_startup_rejects_unmigrated_database(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    test_db = data_dir / "unmigrated.sqlite3"
    monkeypatch.setenv("COPILOT_DATA_DIR", str(data_dir))
    monkeypatch.setenv("COPILOT_DB_PATH", f"sqlite:///{test_db}")
    monkeypatch.setenv(
        "COPILOT_API_TOKENS",
        json.dumps({"smoke-test-token-valid123456": ["capture:write"]}),
    )
    from support_copilot.main import create_production_app
    with pytest.raises(RuntimeError) as exc_info:
        create_production_app()
    assert "Database schema is not ready" in str(exc_info.value)

# 39. Robust root-file test using subprocess application execution
def test_focused_tests_create_no_new_database_file_in_repository_root(tmp_path):
    root_dir = Path(".")
    patterns = ["*.db", "*.sqlite", "*.sqlite3", "*-wal", "*-shm", "*.backup_*.db"]
    before_files = {p.name for pat in patterns for p in root_dir.glob(pat)}

    # Subprocess runs full app flow: import, migration, health check, authenticated manual capture
    data_dir = tmp_path / "isolated_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_file = data_dir / "subproc.sqlite3"

    script = f"""
import sys, json
from support_copilot.config import Settings
from support_copilot.migration_runner import MigrationRunner
from support_copilot.main import create_production_app
from fastapi.testclient import TestClient

settings = Settings(
    data_dir=r"{data_dir}",
    db_path=r"sqlite:///{db_file}",
    api_tokens={{"subprocess-valid-token-12345": ["capture:write"]}}
)
runner = MigrationRunner(settings)
runner.run_upgrade("head")

app = create_production_app()
client = TestClient(app)

h = client.get("/v1/health")
assert h.status_code == 200

payload = {{
    "event": {{
        "provider": "manual",
        "event_id": "subproc-evt-1",
        "event_type": "message.received",
        "occurred_at": "2026-09-20T12:00:00+00:00",
        "actor": {{"external_id": "c1", "role": "client"}},
        "conversation": {{"external_id": "conv1"}},
        "payload": {{"text": "Subprocess test"}},
        "schema_version": 1
    }}
}}
res = client.post("/v1/captures/manual-message", json=payload, headers={{"Authorization": "Bearer subprocess-valid-token-12345"}})
assert res.status_code == 200
"""

    env = os.environ.copy()
    env["PYTHONPATH"] = os.path.abspath(".")
    env["COPILOT_DATA_DIR"] = str(data_dir)
    env["COPILOT_DB_PATH"] = f"sqlite:///{db_file}"
    env["COPILOT_API_TOKENS"] = json.dumps({"subprocess-valid-token-12345": ["capture:write"]})

    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, f"Subprocess run failed: {proc.stderr}"

    after_files = {p.name for pat in patterns for p in root_dir.glob(pat)}
    new_files = after_files - before_files
    assert len(new_files) == 0, f"New database files created in repository root: {new_files}"

# 40. Database path isolation rejection tests
def test_database_path_isolation_rejections(tmp_path):
    valid_data_dir = tmp_path / "valid_data"
    valid_data_dir.mkdir(parents=True, exist_ok=True)

    # Rejects targeting Telegram assistant's work.sqlite3
    with pytest.raises(ValueError) as exc:
        resolve_and_validate_db_path("sqlite:///storage/work.sqlite3", str(valid_data_dir))
    assert "Telegram assistant" in str(exc.value)

    # Rejects parent traversal to Telegram assistant storage
    with pytest.raises(ValueError) as exc:
        resolve_and_validate_db_path("sqlite:///../storage/work.sqlite3", str(valid_data_dir))
    assert "data directory" in str(exc.value) or "Telegram assistant" in str(exc.value)

    # Rejects workspace root as target
    with pytest.raises(ValueError) as exc:
        resolve_and_validate_db_path("sqlite:///./copilot.sqlite3", str(valid_data_dir))
    assert "data directory" in str(exc.value) or "workspace root" in str(exc.value)

    # Rejects non-sqlite URL
    with pytest.raises(ValueError) as exc:
        resolve_and_validate_db_path("postgresql://user:pass@localhost/db", str(valid_data_dir))
    assert "sqlite:///" in str(exc.value)

    # Rejects empty path
    with pytest.raises(ValueError) as exc:
        resolve_and_validate_db_path("", str(valid_data_dir))
    assert "empty" in str(exc.value)

    # Rejects directory target
    with pytest.raises(ValueError) as exc:
        resolve_and_validate_db_path(f"sqlite:///{valid_data_dir}", str(valid_data_dir))
    assert "directory" in str(exc.value)
