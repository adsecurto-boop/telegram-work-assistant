import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import List

from sqlalchemy.orm import Session

from .models import (
    ActivityEvent,
    AuditEvent,
    MeetingProposal,
    MeetingSession,
    MeetingTranscriptSegment,
)


ACTION_PATTERN = re.compile(r"\b(action|follow[ -]?up|will|need to|todo|to-do)\b", re.IGNORECASE)
PROMISE_PATTERN = re.compile(r"\b(promise|commit|guarantee)\b", re.IGNORECASE)
RESOLUTION_PATTERN = re.compile(r"\b(resolved|fixed|closed)\b", re.IGNORECASE)


class MeetingService:
    @staticmethod
    def start_session(db: Session, title: str, consent_note: str, retention_days: int, actor: str) -> MeetingSession:
        active = db.query(MeetingSession).filter_by(lifecycle_status="active").first()
        if active is not None:
            raise ValueError("Another meeting is currently active. Stop it before starting a new one.")
        now = datetime.now(timezone.utc)
        session = MeetingSession(
            id=str(uuid.uuid4()),
            title=title.strip(),
            lifecycle_status="active",
            consent_acknowledged=1,
            consent_note=consent_note.strip(),
            retention_until=now + timedelta(days=retention_days),
            started_at=now,
            created_by=actor,
            created_at=now,
        )
        db.add(session)
        db.add(
            AuditEvent(
                actor=actor,
                action="meeting.started",
                resource=f"meeting_session/{session.id}",
                correlation_id=str(uuid.uuid4()),
                details_json=json.dumps({"consent_acknowledged": True, "retention_days": retention_days}),
            )
        )
        db.commit()
        db.refresh(session)
        return session

    @staticmethod
    def stop_session(db: Session, session_id: str, actor: str) -> MeetingSession:
        session = db.query(MeetingSession).filter_by(id=session_id).first()
        if session is None:
            raise ValueError("Meeting session not found.")
        if session.lifecycle_status == "stopped":
            return session
        session.lifecycle_status = "stopped"
        session.stopped_at = datetime.now(timezone.utc)
        db.add(
            AuditEvent(
                actor=actor,
                action="meeting.stopped",
                resource=f"meeting_session/{session.id}",
                correlation_id=str(uuid.uuid4()),
                details_json="{}",
            )
        )
        db.commit()
        db.refresh(session)
        return session

    @staticmethod
    def add_segment(
        db: Session,
        session_id: str,
        speaker_label: str | None,
        transcript_text: str,
        confidence: float,
        occurred_at: datetime | None,
    ) -> MeetingTranscriptSegment:
        session = db.query(MeetingSession).filter_by(id=session_id).first()
        if session is None:
            raise ValueError("Meeting session not found.")
        if session.lifecycle_status != "active":
            raise ValueError("Transcript segments can be added only while the meeting is active.")
        segment = MeetingTranscriptSegment(
            id=str(uuid.uuid4()),
            meeting_session_id=session_id,
            speaker_label=speaker_label.strip() if speaker_label else None,
            transcript_text=transcript_text.strip(),
            confidence=confidence,
            occurred_at=occurred_at or datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        db.add(segment)
        db.commit()
        db.refresh(segment)
        return segment

    @staticmethod
    def generate_proposals(db: Session, session_id: str) -> List[MeetingProposal]:
        session = db.query(MeetingSession).filter_by(id=session_id).first()
        if session is None:
            raise ValueError("Meeting session not found.")
        if session.lifecycle_status != "stopped":
            raise ValueError("Stop the meeting before generating proposals.")
        existing = db.query(MeetingProposal).filter_by(meeting_session_id=session_id).all()
        if existing:
            return existing
        segments = (
            db.query(MeetingTranscriptSegment)
            .filter_by(meeting_session_id=session_id)
            .order_by(MeetingTranscriptSegment.occurred_at.asc())
            .all()
        )
        if not segments:
            raise ValueError("No transcript segments are available.")
        evidence_ids = [segment.id for segment in segments]
        summary_parts = []
        for segment in segments:
            speaker = segment.speaker_label or "Unknown speaker"
            uncertainty = " [uncertain]" if segment.confidence < 0.75 else ""
            summary_parts.append(f"{speaker}{uncertainty}: {segment.transcript_text}")
        proposals = [
            MeetingProposal(
                id=str(uuid.uuid4()),
                meeting_session_id=session_id,
                proposal_type="summary",
                proposal_text="\n".join(summary_parts),
                evidence_segment_ids_json=json.dumps(evidence_ids),
                lifecycle_status="proposed",
                created_at=datetime.now(timezone.utc),
            )
        ]
        for segment in segments:
            proposal_type = None
            if RESOLUTION_PATTERN.search(segment.transcript_text):
                proposal_type = "resolution"
            elif PROMISE_PATTERN.search(segment.transcript_text):
                proposal_type = "promise"
            elif ACTION_PATTERN.search(segment.transcript_text):
                proposal_type = "action"
            if proposal_type:
                proposals.append(
                    MeetingProposal(
                        id=str(uuid.uuid4()),
                        meeting_session_id=session_id,
                        proposal_type=proposal_type,
                        proposal_text=segment.transcript_text,
                        evidence_segment_ids_json=json.dumps([segment.id]),
                        lifecycle_status="proposed",
                        created_at=datetime.now(timezone.utc),
                    )
                )
        db.add_all(proposals)
        db.commit()
        for proposal in proposals:
            db.refresh(proposal)
        return proposals

    @staticmethod
    def review_proposal(db: Session, proposal_id: str, decision: str, actor: str) -> MeetingProposal:
        proposal = db.query(MeetingProposal).filter_by(id=proposal_id).first()
        if proposal is None:
            raise ValueError("Meeting proposal not found.")
        if proposal.lifecycle_status != "proposed":
            if proposal.lifecycle_status == ("approved" if decision == "approve" else "rejected"):
                return proposal
            raise ValueError("Meeting proposal has already been reviewed.")
        now = datetime.now(timezone.utc)
        proposal.lifecycle_status = "approved" if decision == "approve" else "rejected"
        proposal.reviewed_at = now
        proposal.reviewed_by = actor
        if decision == "approve" and proposal.proposal_type != "summary":
            event_type = "followup.created" if proposal.proposal_type == "action" else "outcome.verified"
            db.add(
                ActivityEvent(
                    id=str(uuid.uuid4()),
                    event_type=event_type,
                    subject_type="followup" if proposal.proposal_type == "action" else "outcome",
                    subject_id=proposal.id,
                    case_id=None,
                    correlation_id=str(uuid.uuid4()),
                    actor=actor,
                    details_json=json.dumps(
                        {
                            "meeting_session_id": proposal.meeting_session_id,
                            "proposal_type": proposal.proposal_type,
                            "approved_text": proposal.proposal_text,
                        }
                    ),
                    occurred_at=now,
                    created_at=now,
                )
            )
        db.add(
            AuditEvent(
                actor=actor,
                action=f"meeting.proposal_{proposal.lifecycle_status}",
                resource=f"meeting_proposal/{proposal.id}",
                correlation_id=str(uuid.uuid4()),
                details_json=json.dumps({"proposal_type": proposal.proposal_type}),
            )
        )
        db.commit()
        db.refresh(proposal)
        return proposal

    @staticmethod
    def purge_expired_transcripts(db: Session, now: datetime | None = None) -> int:
        cutoff = now or datetime.now(timezone.utc)
        expired_ids = [
            row[0]
            for row in db.query(MeetingSession.id)
            .filter(MeetingSession.retention_until <= cutoff, MeetingSession.lifecycle_status == "stopped")
            .all()
        ]
        if not expired_ids:
            return 0
        count = (
            db.query(MeetingTranscriptSegment)
            .filter(MeetingTranscriptSegment.meeting_session_id.in_(expired_ids))
            .delete(synchronize_session=False)
        )
        db.query(MeetingProposal).filter(MeetingProposal.meeting_session_id.in_(expired_ids)).update(
            {MeetingProposal.evidence_segment_ids_json: "[]"}, synchronize_session=False
        )
        db.commit()
        return count
