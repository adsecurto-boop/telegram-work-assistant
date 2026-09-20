import base64

from fastapi.testclient import TestClient
from sqlalchemy import text

from support_copilot.ai_provider import (
    InvalidProviderOutputError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from support_copilot.config import Settings
from support_copilot.database import create_db_engine, create_session_factory
from support_copilot.knowledge_service import KnowledgeService
from support_copilot.main import create_app
from support_copilot.models import AuditEvent, Base
from support_copilot.screen_provider import ScreenAnalysisProvider, VisualAnalysis


TOKEN = "screen-analysis-token-123456"
PNG_DATA = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nminimal-redacted-image").decode()


class FakeVisualProvider(ScreenAnalysisProvider):
    def __init__(self, mode="normal"):
        self.calls = 0
        self.last_image = ""
        self.mode = mode

    async def analyze(self, image_base64, ocr_text, knowledge_context, allowed_article_ids, timeout):
        self.calls += 1
        self.last_image = image_base64
        if self.mode == "timeout":
            raise ProviderTimeoutError("Visual analysis timed out.")
        if self.mode == "unavailable":
            raise ProviderUnavailableError("Visual provider is unreachable.")
        if self.mode == "invalid_output":
            raise InvalidProviderOutputError("Visual provider returned invalid structured output.")
        if self.mode == "unauthorized_source":
            return VisualAnalysis(
                observations=["Observed interface."],
                recommended_steps=["Check configuration."],
                uncertainty="Low uncertainty.",
                source_article_ids=["unauthorized-article-id-999", allowed_article_ids[0]],
            )
        return VisualAnalysis(
            observations=["The support portal shows a missing-log warning."],
            recommended_steps=["Verify the attendance date range before retrying."],
            uncertainty="The screenshot does not prove whether ingestion completed.",
            source_article_ids=[allowed_article_ids[0]],
        )


def make_client(tmp_path, provider=None, seed_knowledge=True):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = f"sqlite:///{data_dir / 'screen.sqlite3'}"
    settings = Settings(
        data_dir=str(data_dir),
        db_path=db_path,
        api_tokens={TOKEN: ["screen:analyze"]},
    )
    engine = create_db_engine(db_path)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_articles_fts USING fts5("
            "version_id UNINDEXED, "
            "article_id UNINDEXED, "
            "title, "
            "content, "
            "product_scope, "
            "issue_type, "
            "client_scope)"
        ))
    sessions = create_session_factory(engine)
    if seed_knowledge:
        db = sessions()
        article = KnowledgeService.create_article(
            db,
            stable_key="screen-missing-logs",
            title="Missing attendance logs",
            product_scope="core",
            issue_type="missing_logs",
            initial_content="When attendance logs are missing, verify the date range and ingestion status.",
            created_by="tester",
        )
        KnowledgeService.approve_version(db, article.id, 1, "approver")
        db.close()
    app = create_app(
        settings,
        engine,
        sessions,
        screen_analysis_provider=provider,
    )
    return TestClient(app), sessions, engine


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def request_payload(ocr_text="attendance logs missing for selected date range", image_data_url=PNG_DATA):
    return {
        "image_data_url": image_data_url,
        "ocr_text": ocr_text,
        "ocr_confidence": 0.84,
        "product_scope": "core",
        "issue_type": "missing_logs",
    }


def test_redacted_screen_is_analyzed_with_approved_evidence_and_not_persisted(tmp_path):
    provider = FakeVisualProvider()
    client, sessions, engine = make_client(tmp_path, provider)
    response = client.post("/v1/screen/analyze", headers=auth(), json=request_payload())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "analyzed"
    assert body["recommended_steps"]
    assert body["sources"][0]["title"] == "Missing attendance logs"
    assert provider.calls == 1

    db = sessions()
    try:
        audit = db.query(AuditEvent).filter_by(action="screen.analyzed").one()
        assert "data:image" not in audit.details_json
        assert provider.last_image not in audit.details_json
        assert "redacted_screenshot_hash" in audit.details_json
    finally:
        db.close()
        engine.dispose()


def test_sensitive_ocr_is_blocked_before_visual_provider(tmp_path):
    provider = FakeVisualProvider()
    client, _, engine = make_client(tmp_path, provider)
    response = client.post(
        "/v1/screen/analyze",
        headers=auth(),
        json=request_payload("Enter your OTP, CVV, and credit card number"),
    )
    assert response.status_code == 400
    assert provider.calls == 0
    engine.dispose()


def test_disabled_visual_provider_returns_sources_without_fabricated_steps(tmp_path):
    client, _, engine = make_client(tmp_path, None)
    response = client.post("/v1/screen/analyze", headers=auth(), json=request_payload())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "provider_unavailable"
    assert body["recommended_steps"] == []
    assert body["sources"]
    engine.dispose()


def test_screen_analysis_requires_capability(tmp_path):
    client, _, engine = make_client(tmp_path, None)
    assert client.post("/v1/screen/analyze", json=request_payload()).status_code == 401
    engine.dispose()


def test_invalid_base64_returns_controlled_400(tmp_path):
    provider = FakeVisualProvider()
    client, _, engine = make_client(tmp_path, provider)
    response = client.post(
        "/v1/screen/analyze",
        headers=auth(),
        json=request_payload(image_data_url="data:image/png;base64,not-valid-base64!@#"),
    )
    assert response.status_code == 400
    assert "base64" in response.json()["detail"].lower()
    assert provider.calls == 0
    engine.dispose()


def test_non_png_bytes_returns_controlled_400(tmp_path):
    provider = FakeVisualProvider()
    client, _, engine = make_client(tmp_path, provider)
    non_png = "data:image/png;base64," + base64.b64encode(b"GIF89a-fake-gif-bytes").decode()
    response = client.post(
        "/v1/screen/analyze",
        headers=auth(),
        json=request_payload(image_data_url=non_png),
    )
    assert response.status_code == 400
    assert "png" in response.json()["detail"].lower()
    assert provider.calls == 0
    engine.dispose()


def test_malformed_data_url_returns_controlled_400(tmp_path):
    provider = FakeVisualProvider()
    client, _, engine = make_client(tmp_path, provider)
    response = client.post(
        "/v1/screen/analyze",
        headers=auth(),
        json=request_payload(image_data_url="not-a-valid-data-url"),
    )
    assert response.status_code == 400
    assert "malformed" in response.json()["detail"].lower()
    assert provider.calls == 0
    engine.dispose()


def test_invalid_structured_provider_output_returns_provider_unavailable(tmp_path):
    provider = FakeVisualProvider(mode="invalid_output")
    client, _, engine = make_client(tmp_path, provider)
    response = client.post("/v1/screen/analyze", headers=auth(), json=request_payload())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "provider_unavailable"
    assert body["recommended_steps"] == []
    assert len(body["sources"]) > 0
    assert provider.calls == 1
    engine.dispose()


def test_provider_timeout_returns_provider_timeout_with_sources(tmp_path):
    provider = FakeVisualProvider(mode="timeout")
    client, _, engine = make_client(tmp_path, provider)
    response = client.post("/v1/screen/analyze", headers=auth(), json=request_payload())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "provider_timeout"
    assert body["recommended_steps"] == []
    assert len(body["sources"]) > 0
    assert provider.calls == 1
    engine.dispose()


def test_provider_unavailability_returns_provider_unavailable_with_sources(tmp_path):
    provider = FakeVisualProvider(mode="unavailable")
    client, _, engine = make_client(tmp_path, provider)
    response = client.post("/v1/screen/analyze", headers=auth(), json=request_payload())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "provider_unavailable"
    assert body["recommended_steps"] == []
    assert len(body["sources"]) > 0
    assert provider.calls == 1
    engine.dispose()


def test_no_approved_knowledge_returns_knowledge_unavailable(tmp_path):
    provider = FakeVisualProvider()
    client, _, engine = make_client(tmp_path, provider, seed_knowledge=False)
    response = client.post("/v1/screen/analyze", headers=auth(), json=request_payload())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "knowledge_unavailable"
    assert provider.calls == 0
    engine.dispose()


def test_source_id_filtering_restricts_to_approved_retrieved_set(tmp_path):
    provider = FakeVisualProvider(mode="unauthorized_source")
    client, _, engine = make_client(tmp_path, provider)
    response = client.post("/v1/screen/analyze", headers=auth(), json=request_payload())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "analyzed"
    returned_ids = [s["article_id"] for s in body["sources"]]
    assert "unauthorized-article-id-999" not in returned_ids
    assert len(returned_ids) == 1
    engine.dispose()


def test_no_control_ipc_or_api_exposure():
    import pathlib
    security_file = pathlib.Path(__file__).resolve().parents[1] / "desktop" / "src" / "main" / "security.ts"
    content = security_file.read_text(encoding="utf-8")
    assert "contextIsolation: true" in content
    assert "nodeIntegration: false" in content
    assert "sandbox: true" in content
    # Ensure no channel allows keyboard, mouse, or arbitrary shell control
    for line in content.splitlines():
        if "copilot:" in line:
            assert "click" not in line
            assert "key" not in line
            assert "mouse" not in line
            assert "shell" not in line
            assert "exec" not in line

