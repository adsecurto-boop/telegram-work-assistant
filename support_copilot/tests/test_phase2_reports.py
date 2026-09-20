from datetime import datetime, timezone

from fastapi.testclient import TestClient

from support_copilot.config import Settings
from support_copilot.database import create_db_engine, create_session_factory
from support_copilot.main import create_app
from support_copilot.models import Base, ReportSnapshot


TOKEN = "phase-two-secret-token-123456"


def make_client(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = f"sqlite:///{data_dir / 'phase2.sqlite3'}"
    settings = Settings(
        data_dir=str(data_dir),
        db_path=db_path,
        api_tokens={TOKEN: ["activity:read", "activity:write", "report:read", "report:write"]},
    )
    engine = create_db_engine(db_path)
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)
    app = create_app(settings, engine, sessions, verify_schema=True, verify_auth=True)
    return TestClient(app), sessions, engine


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def add_event(client, event_type, subject_type, subject_id):
    response = client.post(
        "/v1/activity/events",
        headers=auth(),
        json={
            "event_type": event_type,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "details": {"summary": f"verified {subject_id}"},
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_report_contains_only_verified_work_and_source_event_links(tmp_path):
    client, sessions, engine = make_client(tmp_path)
    completed = add_event(client, "support.completed", "support", "support-1")

    preview = client.post(
        "/v1/reports/eod/preview",
        headers=auth(),
        json={"timezone_name": "Asia/Kolkata"},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["lifecycle_status"] == "preview"
    assert body["content"]["summary"]["verified_work_items"] == 1
    assert body["content"]["source_event_ids"] == [completed["id"]]

    finalized = client.post(
        "/v1/reports/eod/finalize",
        headers=auth(),
        json={"preview_id": body["id"], "expected_facts_hash": body["facts_hash"]},
    )
    assert finalized.status_code == 200, finalized.text
    assert finalized.json()["lifecycle_status"] == "finalized"

    db = sessions()
    try:
        stored = db.query(ReportSnapshot).filter_by(id=body["id"]).one()
        assert stored.lifecycle_status == "finalized"
    finally:
        db.close()
        engine.dispose()


def test_late_activity_makes_report_preview_stale(tmp_path):
    client, _, engine = make_client(tmp_path)
    add_event(client, "test.completed", "test", "test-1")
    preview = client.post(
        "/v1/reports/eod/preview", headers=auth(), json={"timezone_name": "UTC"}
    ).json()
    add_event(client, "followup.created", "followup", "followup-1")

    finalized = client.post(
        "/v1/reports/eod/finalize",
        headers=auth(),
        json={"preview_id": preview["id"], "expected_facts_hash": preview["facts_hash"]},
    )
    assert finalized.status_code == 409
    assert finalized.json()["error_type"] == "conflict"
    engine.dispose()


def test_activity_and_reports_require_scoped_authorization(tmp_path):
    client, _, engine = make_client(tmp_path)
    assert client.post("/v1/reports/eod/preview", json={}).status_code == 401
    assert client.post("/v1/activity/events", json={}).status_code == 401
    engine.dispose()


def test_unknown_timezone_fails_with_typed_client_error(tmp_path):
    client, _, engine = make_client(tmp_path)
    response = client.post(
        "/v1/reports/eod/preview",
        headers=auth(),
        json={"timezone_name": "Mars/Olympus"},
    )
    assert response.status_code == 400
    assert "Unknown timezone" in response.json()["detail"]
    engine.dispose()
