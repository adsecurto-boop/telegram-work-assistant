from typing import List, Optional
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .models import SupportCase, Conversation, CapturedEvent
from .logger import logger


class CaseCandidate(BaseModel):
    id: str
    case_number: str
    title: str
    status: str
    client_identifier: str


class CaseResolutionOutcome(BaseModel):
    status: str  # "resolved", "resolution_required", "unlinked"
    resolved_case: Optional[CaseCandidate] = None
    candidates: List[CaseCandidate] = []
    resolution_method: str = "none"  # "explicit_id", "conversation_link", "single_candidate", "none"


class CaseResolutionError(Exception):
    """Raised when case resolution encounters an invalid or nonexistent explicit case ID."""
    pass


class CaseResolutionService:
    @staticmethod
    def resolve_case(
        db: Session,
        captured_event: Optional[CapturedEvent] = None,
        explicit_case_id: Optional[str] = None,
        client_identifier: Optional[str] = None,
    ) -> CaseResolutionOutcome:
        """
        Deterministically resolves a support case.
        Order:
        1. Explicit validated case ID supplied by human.
        2. Verified conversation-to-case link.
        3. Exactly one bounded open candidate based on verified client metadata.
        4. Otherwise returns resolution_required with bounded candidates.
        Never guesses or mutates cases.
        """
        # 1. Explicit validated case ID
        if explicit_case_id:
            case = (
                db.query(SupportCase)
                .filter(
                    (SupportCase.id == explicit_case_id)
                    | (SupportCase.case_number == explicit_case_id)
                )
                .first()
            )
            if not case:
                raise CaseResolutionError(f"Explicit case '{explicit_case_id}' does not exist.")
            candidate = CaseCandidate(
                id=case.id,
                case_number=case.case_number,
                title=case.title,
                status=case.status,
                client_identifier=case.client_identifier,
            )
            return CaseResolutionOutcome(
                status="resolved",
                resolved_case=candidate,
                resolution_method="explicit_id",
            )

        # 2. Verified conversation-to-case link
        if captured_event and captured_event.conversation_id:
            conv = (
                db.query(Conversation)
                .filter_by(
                    provider=captured_event.provider,
                    external_conversation_id=captured_event.conversation_id,
                )
                .first()
            )
            if conv and conv.case_id:
                case = db.query(SupportCase).filter_by(id=conv.case_id).first()
                if case:
                    candidate = CaseCandidate(
                        id=case.id,
                        case_number=case.case_number,
                        title=case.title,
                        status=case.status,
                        client_identifier=case.client_identifier,
                    )
                    return CaseResolutionOutcome(
                        status="resolved",
                        resolved_case=candidate,
                        resolution_method="conversation_link",
                    )

        # 3. Single bounded open candidate for client
        effective_client = client_identifier
        if not effective_client and captured_event:
            effective_client = captured_event.actor_id

        if effective_client:
            open_cases = (
                db.query(SupportCase)
                .filter(
                    SupportCase.client_identifier == effective_client,
                    SupportCase.status == "open",
                )
                .all()
            )
            candidates = [
                CaseCandidate(
                    id=c.id,
                    case_number=c.case_number,
                    title=c.title,
                    status=c.status,
                    client_identifier=c.client_identifier,
                )
                for c in open_cases
            ]

            if len(candidates) == 1:
                return CaseResolutionOutcome(
                    status="resolved",
                    resolved_case=candidates[0],
                    resolution_method="single_candidate",
                )
            elif len(candidates) > 1:
                return CaseResolutionOutcome(
                    status="resolution_required",
                    candidates=candidates,
                    resolution_method="ambiguous_candidates",
                )

        # 4. No candidate found: unlinked
        return CaseResolutionOutcome(
            status="unlinked",
            resolution_method="none",
        )
