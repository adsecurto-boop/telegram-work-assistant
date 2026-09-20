from fastapi.testclient import TestClient

from support_copilot.config import Settings
from support_copilot.database import create_db_engine, create_session_factory
from support_copilot.main import create_app
from support_copilot.models import Base


TOKEN = "case-management-token-123456"


def make_client(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = f"sqlite:///{data_dir / 'cases.sqlite3'}"
    settings = Settings(
        data_dir=str(data_dir),
        db_path=db_path,
        api_tokens={TOKEN: ["case:read", "case:write"]},
    )
    engine = create_db_engine(db_path)
    Base.metadata.create_all(engine)
    app = create_app(settings, engine, create_session_factory(engine))
    return TestClient(app), engine


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_case_create_list_close_lifecycle(tmp_path):
    client, engine = make_client(tmp_path)
    created = client.post(
        "/v1/cases",
        headers=auth(),
        json={"case_number": "CASE-100", "title": "Login failure", "client_identifier": "client-1"},
    )
    assert created.status_code == 200, created.text
    case_id = created.json()["id"]

    listed = client.get("/v1/cases?status_filter=open", headers=auth())
    assert [case["id"] for case in listed.json()["cases"]] == [case_id]

    closed = client.patch(
        f"/v1/cases/{case_id}/status", headers=auth(), json={"status": "closed"}
    )
    assert closed.status_code == 200
    assert closed.json()["status"] == "closed"
    assert client.get("/v1/cases?status_filter=open", headers=auth()).json()["cases"] == []
    engine.dispose()


def test_duplicate_case_number_conflicts_and_auth_is_required(tmp_path):
    client, engine = make_client(tmp_path)
    payload = {"case_number": "CASE-101", "title": "Issue", "client_identifier": "client-1"}
    assert client.post("/v1/cases", headers=auth(), json=payload).status_code == 200
    assert client.post("/v1/cases", headers=auth(), json=payload).status_code == 409
    assert client.get("/v1/cases").status_code == 401
    engine.dispose()
