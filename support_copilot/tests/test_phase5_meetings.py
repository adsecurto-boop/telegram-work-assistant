from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from support_copilot.config import Settings
from support_copilot.database import create_db_engine, create_session_factory
from support_copilot.main import create_app
from support_copilot.meeting_service import MeetingService
from support_copilot.models import ActivityEvent, Base, MeetingProposal, MeetingSession, MeetingTranscriptSegment


TOKEN = "meeting-capability-token-123456"


def make_client(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = f"sqlite:///{data_dir / 'meetings.sqlite3'}"
    settings = Settings(
        data_dir=str(data_dir),
        db_path=db_path,
        api_tokens={TOKEN: ["meeting:read", "meeting:write", "meeting:approve"]},
    )
    engine = create_db_engine(db_path)
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)
    return TestClient(create_app(settings, engine, sessions)), sessions, engine


def auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def start(client):
    response = client.post(
        "/v1/meetings/start",
        headers=auth(),
        json={
            "title": "Client incident review",
            "consent_acknowledged": True,
            "consent_note": "All participants consented to transcription.",
            "transcript_retention_days": 7,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_session_cannot_start_without_explicit_consent(tmp_path):
    client, _, engine = make_client(tmp_path)
    response = client.post(
        "/v1/meetings/start",
        headers=auth(),
        json={
            "title": "No consent",
            "consent_acknowledged": False,
            "consent_note": "not confirmed",
            "transcript_retention_days": 7,
        },
    )
    assert response.status_code == 422
    engine.dispose()


def test_transcript_uncertainty_and_proposals_require_human_approval(tmp_path):
    client, sessions, engine = make_client(tmp_path)
    meeting = start(client)
    segment = client.post(
        f"/v1/meetings/{meeting['id']}/transcript-segments",
        headers=auth(),
        json={
            "speaker_label": "Client",
            "transcript_text": "The issue is resolved and we will follow up tomorrow.",
            "confidence": 0.55,
        },
    )
    assert segment.status_code == 200
    assert segment.json()["uncertainty_visible"] is True
    assert client.post(f"/v1/meetings/{meeting['id']}/stop", headers=auth()).status_code == 200

    proposed = client.post(f"/v1/meetings/{meeting['id']}/proposals", headers=auth())
    assert proposed.status_code == 200, proposed.text
    proposals = proposed.json()["proposals"]
    assert all(item["lifecycle_status"] == "proposed" for item in proposals)
    summary = next(item for item in proposals if item["proposal_type"] == "summary")
    resolution = next(item for item in proposals if item["proposal_type"] == "resolution")
    assert "[uncertain]" in summary["proposal_text"]

    db = sessions()
    try:
        assert db.query(ActivityEvent).count() == 0
    finally:
        db.close()

    approved = client.post(
        f"/v1/meetings/proposals/{resolution['id']}/review",
        headers=auth(),
        json={"decision": "approve"},
    )
    assert approved.status_code == 200
    assert approved.json()["lifecycle_status"] == "approved"
    db = sessions()
    try:
        events = db.query(ActivityEvent).all()
        assert len(events) == 1
        assert events[0].event_type == "outcome.verified"
    finally:
        db.close()
        engine.dispose()


def test_stopped_session_rejects_more_transcript(tmp_path):
    client, _, engine = make_client(tmp_path)
    meeting = start(client)
    client.post(f"/v1/meetings/{meeting['id']}/stop", headers=auth())
    response = client.post(
        f"/v1/meetings/{meeting['id']}/transcript-segments",
        headers=auth(),
        json={"transcript_text": "late capture", "confidence": 1.0},
    )
    assert response.status_code == 400
    assert "only while" in response.json()["detail"]
    engine.dispose()


def test_retention_purge_removes_raw_transcript_and_keeps_approved_proposal(tmp_path):
    client, sessions, engine = make_client(tmp_path)
    meeting = start(client)
    client.post(
        f"/v1/meetings/{meeting['id']}/transcript-segments",
        headers=auth(),
        json={"speaker_label": "Agent", "transcript_text": "We will follow up.", "confidence": 0.9},
    )
    client.post(f"/v1/meetings/{meeting['id']}/stop", headers=auth())
    proposals = client.post(f"/v1/meetings/{meeting['id']}/proposals", headers=auth()).json()["proposals"]
    action = next(item for item in proposals if item["proposal_type"] == "action")
    client.post(
        f"/v1/meetings/proposals/{action['id']}/review",
        headers=auth(),
        json={"decision": "approve"},
    )

    db = sessions()
    try:
        stored_session = db.query(MeetingSession).filter_by(id=meeting["id"]).one()
        stored_session.retention_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        assert MeetingService.purge_expired_transcripts(db) == 1
        assert db.query(MeetingTranscriptSegment).count() == 0
        retained = db.query(MeetingProposal).filter_by(id=action["id"]).one()
        assert retained.lifecycle_status == "approved"
        assert retained.evidence_segment_ids_json == "[]"
    finally:
        db.close()
        engine.dispose()


def test_only_one_active_meeting_allowed(tmp_path):
    client, _, engine = make_client(tmp_path)
    meeting = start(client)
    # Attempting to start another meeting while the first is active must fail
    response = client.post(
        "/v1/meetings/start",
        headers=auth(),
        json={
            "title": "Second meeting attempt",
            "consent_acknowledged": True,
            "consent_note": "Consented.",
            "transcript_retention_days": 7,
        },
    )
    assert response.status_code == 400
    assert "currently active" in response.json()["detail"].lower()

    # Once stopped, a new meeting can be started
    client.post(f"/v1/meetings/{meeting['id']}/stop", headers=auth())
    meeting2 = start(client)
    assert meeting2["id"] != meeting["id"]
    engine.dispose()


def test_proposals_can_only_be_generated_after_stopping(tmp_path):
    client, _, engine = make_client(tmp_path)
    meeting = start(client)
    client.post(
        f"/v1/meetings/{meeting['id']}/transcript-segments",
        headers=auth(),
        json={"speaker_label": "Agent", "transcript_text": "We will check.", "confidence": 0.9},
    )
    # While active, proposal generation fails
    response = client.post(f"/v1/meetings/{meeting['id']}/proposals", headers=auth())
    assert response.status_code == 400
    assert "stop the meeting" in response.json()["detail"].lower()
    engine.dispose()


def test_proposal_rejection_creates_no_verified_activity(tmp_path):
    client, sessions, engine = make_client(tmp_path)
    meeting = start(client)
    client.post(
        f"/v1/meetings/{meeting['id']}/transcript-segments",
        headers=auth(),
        json={"speaker_label": "Agent", "transcript_text": "We promise to deliver by Friday.", "confidence": 0.95},
    )
    client.post(f"/v1/meetings/{meeting['id']}/stop", headers=auth())
    proposals = client.post(f"/v1/meetings/{meeting['id']}/proposals", headers=auth()).json()["proposals"]
    promise = next(item for item in proposals if item["proposal_type"] == "promise")

    response = client.post(
        f"/v1/meetings/proposals/{promise['id']}/review",
        headers=auth(),
        json={"decision": "reject"},
    )
    assert response.status_code == 200
    assert response.json()["lifecycle_status"] == "rejected"

    db = sessions()
    try:
        assert db.query(ActivityEvent).count() == 0
    finally:
        db.close()
        engine.dispose()


def test_approving_summary_creates_no_activity_event(tmp_path):
    client, sessions, engine = make_client(tmp_path)
    meeting = start(client)
    client.post(
        f"/v1/meetings/{meeting['id']}/transcript-segments",
        headers=auth(),
        json={"speaker_label": "Agent", "transcript_text": "Discussion on architecture.", "confidence": 0.95},
    )
    client.post(f"/v1/meetings/{meeting['id']}/stop", headers=auth())
    proposals = client.post(f"/v1/meetings/{meeting['id']}/proposals", headers=auth()).json()["proposals"]
    summary = next(item for item in proposals if item["proposal_type"] == "summary")

    response = client.post(
        f"/v1/meetings/proposals/{summary['id']}/review",
        headers=auth(),
        json={"decision": "approve"},
    )
    assert response.status_code == 200
    assert response.json()["lifecycle_status"] == "approved"

    db = sessions()
    try:
        # Approving summary must NOT create a completed-work activity event
        assert db.query(ActivityEvent).count() == 0
    finally:
        db.close()
        engine.dispose()


def test_approving_action_creates_followup_event(tmp_path):
    client, sessions, engine = make_client(tmp_path)
    meeting = start(client)
    client.post(
        f"/v1/meetings/{meeting['id']}/transcript-segments",
        headers=auth(),
        json={"speaker_label": "Agent", "transcript_text": "We will follow up with client logs.", "confidence": 0.9},
    )
    client.post(f"/v1/meetings/{meeting['id']}/stop", headers=auth())
    proposals = client.post(f"/v1/meetings/{meeting['id']}/proposals", headers=auth()).json()["proposals"]
    action = next(item for item in proposals if item["proposal_type"] == "action")

    response = client.post(
        f"/v1/meetings/proposals/{action['id']}/review",
        headers=auth(),
        json={"decision": "approve"},
    )
    assert response.status_code == 200

    db = sessions()
    try:
        events = db.query(ActivityEvent).all()
        assert len(events) == 1
        assert events[0].event_type == "followup.created"
        assert events[0].subject_type == "followup"
    finally:
        db.close()
        engine.dispose()


def test_review_proposal_is_idempotent_and_conflict_fails_with_409(tmp_path):
    client, sessions, engine = make_client(tmp_path)
    meeting = start(client)
    client.post(
        f"/v1/meetings/{meeting['id']}/transcript-segments",
        headers=auth(),
        json={"speaker_label": "Agent", "transcript_text": "Issue is resolved.", "confidence": 0.95},
    )
    client.post(f"/v1/meetings/{meeting['id']}/stop", headers=auth())
    proposals = client.post(f"/v1/meetings/{meeting['id']}/proposals", headers=auth()).json()["proposals"]
    resolution = next(item for item in proposals if item["proposal_type"] == "resolution")

    # First approve
    r1 = client.post(f"/v1/meetings/proposals/{resolution['id']}/review", headers=auth(), json={"decision": "approve"})
    assert r1.status_code == 200

    # Idempotent second approve
    r2 = client.post(f"/v1/meetings/proposals/{resolution['id']}/review", headers=auth(), json={"decision": "approve"})
    assert r2.status_code == 200

    db = sessions()
    try:
        # Still exactly 1 event created, not 2
        assert db.query(ActivityEvent).count() == 1
    finally:
        db.close()

    # Conflicting decision must return 409
    r3 = client.post(f"/v1/meetings/proposals/{resolution['id']}/review", headers=auth(), json={"decision": "reject"})
    assert r3.status_code == 409
    engine.dispose()


def test_no_raw_audio_claimed_or_stored():
    from sqlalchemy import inspect
    from support_copilot.models import MeetingSession, MeetingTranscriptSegment, MeetingProposal
    # Inspect columns
    session_cols = {c.name for c in MeetingSession.__table__.columns}
    segment_cols = {c.name for c in MeetingTranscriptSegment.__table__.columns}
    proposal_cols = {c.name for c in MeetingProposal.__table__.columns}
    for cols in [session_cols, segment_cols, proposal_cols]:
        assert "audio" not in cols
        assert "audio_bytes" not in cols
        assert "audio_url" not in cols
        assert "recording" not in cols

