import json
import time
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from support_copilot.config import Settings
from support_copilot.database import create_db_engine, create_session_factory
from support_copilot.main import create_app
from support_copilot.models import Base, IntegrationIdempotency, ReportSnapshot
from support_copilot.n8n_service import signature_for


SECRET = "n8n-private-signing-secret"


def setup_app(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = f"sqlite:///{data_dir / 'n8n.sqlite3'}"
    settings = Settings(
        data_dir=str(data_dir),
        db_path=db_path,
        api_tokens={"unused-valid-token-12345": ["capture:write"]},
        n8n_shared_secret=SECRET,
    )
    engine = create_db_engine(db_path)
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)
    db = sessions()
    report = ReportSnapshot(
        id="report-final-1",
        report_type="eod",
        report_date="2026-09-21",
        timezone_name="Asia/Kolkata",
        lifecycle_status="finalized",
        facts_hash="a" * 64,
        content_json=json.dumps({"summary": {"verified_work_items": 1}}),
        created_by="tester",
        created_at=datetime.now(timezone.utc),
        finalized_at=datetime.now(timezone.utc),
    )
    db.add(report)
    db.commit()
    db.close()
    return TestClient(create_app(settings, engine, sessions)), sessions, engine


def signed_headers(payload, event_id="n8n-event-1", capability="report:export", timestamp=None):
    timestamp = timestamp or str(int(time.time()))
    return {
        "X-Copilot-Timestamp": timestamp,
        "X-Copilot-Event-ID": event_id,
        "X-Copilot-Capability": capability,
        "X-Copilot-Signature": signature_for(SECRET, timestamp, event_id, capability, payload),
    }


def test_signed_export_is_idempotent_across_retry(tmp_path):
    client, sessions, engine = setup_app(tmp_path)
    payload = {"report_type": "eod", "report_date": None}
    headers = signed_headers(payload)
    first = client.post("/v1/integrations/n8n/report-export", json=payload, headers=headers)
    retry = client.post("/v1/integrations/n8n/report-export", json=payload, headers=headers)
    assert first.status_code == retry.status_code == 200
    assert first.json() == retry.json()
    db = sessions()
    try:
        assert db.query(IntegrationIdempotency).filter_by(provider="n8n").count() == 1
    finally:
        db.close()
        engine.dispose()


def test_signature_failure_and_excess_capability_fail_closed(tmp_path):
    client, _, engine = setup_app(tmp_path)
    payload = {"report_type": "eod", "report_date": None}
    bad = signed_headers(payload)
    bad["X-Copilot-Signature"] = "0" * 64
    assert client.post("/v1/integrations/n8n/report-export", json=payload, headers=bad).status_code == 401
    excessive = signed_headers(payload, capability="database:write")
    response = client.post("/v1/integrations/n8n/report-export", json=payload, headers=excessive)
    assert response.status_code == 401
    assert "not allowed" in response.json()["detail"]
    engine.dispose()


def test_expired_signature_and_changed_replay_are_rejected(tmp_path):
    client, _, engine = setup_app(tmp_path)
    payload = {"report_type": "eod", "report_date": None}
    expired = signed_headers(payload, timestamp=str(int(time.time()) - 1000))
    assert client.post("/v1/integrations/n8n/report-export", json=payload, headers=expired).status_code == 401

    headers = signed_headers(payload, event_id="reused")
    assert client.post("/v1/integrations/n8n/report-export", json=payload, headers=headers).status_code == 200
    changed = {"report_type": "tod", "report_date": None}
    changed_headers = signed_headers(changed, event_id="reused")
    assert client.post("/v1/integrations/n8n/report-export", json=changed, headers=changed_headers).status_code == 409
    engine.dispose()


def test_workflow_exports_contain_no_sqlite_access():
    from pathlib import Path

    workflow_dir = Path("support_copilot/n8n")
    text = "\n".join(path.read_text(encoding="utf-8") for path in workflow_dir.glob("*.json"))
    assert "sqlite" not in text.lower()
    assert "report-export" in text


def test_eod_workflow_code_node_and_signing_construction():
    from pathlib import Path

    workflow_file = Path("support_copilot/n8n/eod-distribution.workflow.json")
    data = json.loads(workflow_file.read_text(encoding="utf-8"))
    nodes = {n["name"]: n for n in data["nodes"]}

    code_node = nodes["Build Signed Export Request"]
    js = code_node["parameters"]["jsCode"]

    # Code node is not a placeholder
    assert "return items;" not in js
    assert "crypto.createHmac('sha256'" in js
    assert "X-Copilot-Signature" in js
    assert "X-Copilot-Timestamp" in js
    assert "X-Copilot-Event-ID" in js
    assert "report:export" in js

    # Only report:export capability requested
    assert "const capability = 'report:export';" in js

    # HTTP request node configuration
    http_node = nodes["Fetch Finalized EOD"]
    params = http_node["parameters"]

    # Headers connected to generated values
    header_params = {p["name"]: p["value"] for p in params["headerParameters"]["parameters"]}
    assert header_params["X-Copilot-Timestamp"] == "={{ $json.headers['X-Copilot-Timestamp'] }}"
    assert header_params["X-Copilot-Event-ID"] == "={{ $json.headers['X-Copilot-Event-ID'] }}"
    assert header_params["X-Copilot-Capability"] == "={{ $json.headers['X-Copilot-Capability'] }}"
    assert header_params["X-Copilot-Signature"] == "={{ $json.headers['X-Copilot-Signature'] }}"

    # Signed body matches transmitted body
    assert params["body"] == "={{ $json.body }}"

    # Bounded retries and no redirects
    assert http_node["retryOnFail"] is True
    assert 1 <= http_node["maxTries"] <= 5
    assert params["options"]["redirect"]["redirect"]["followRedirects"] is False

    # Distribution target is no-op and inactive
    target_node = nodes["Approved Distribution Target"]
    assert target_node["type"] == "n8n-nodes-base.noOp"
    assert data["active"] is False


def test_operational_error_workflow_redacts_output():
    from pathlib import Path

    workflow_file = Path("support_copilot/n8n/operational-error.workflow.json")
    data = json.loads(workflow_file.read_text(encoding="utf-8"))
    nodes = {n["name"]: n for n in data["nodes"]}

    redact_node = nodes["Redact Error"]
    js = redact_node["parameters"]["jsCode"]

    # Error message is sanitized and does not leak stack traces or payloads
    assert "Support Copilot workflow failed; inspect n8n execution logs." in js
    assert data["active"] is False
