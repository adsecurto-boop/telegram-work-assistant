import json
import hashlib
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from .knowledge_service import KnowledgeService
from .models import (
    AuditEvent,
    KnowledgeArticle,
    KnowledgeArticleVersion,
    ResponseLearningCandidate,
    ResponseSuggestion,
    SentResponse,
    SuggestionSource,
)


class LearningService:
    SENSITIVE_KNOWLEDGE_PATTERN = re.compile(
        r"(?:\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b|"
        r"\b(?:\d[ -]?){10,16}\b|"
        r"\b(?:bearer|sk|api[_ -]?key|token)[_ :=-]*[A-Z0-9_-]{16,}\b)",
        re.IGNORECASE,
    )

    @staticmethod
    def propose_candidate(
        db: Session,
        suggestion_id: str,
        candidate_title: str,
        candidate_content: str,
        content_reviewed_for_sensitive_data: bool,
        actor: str,
        target_stable_key: Optional[str] = None,
        target_article_id: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> ResponseLearningCandidate:
        suggestion = db.query(ResponseSuggestion).filter_by(id=suggestion_id).first()
        if suggestion is None:
            raise ValueError("Response suggestion not found.")

        # Candidate can originate ONLY from a human-confirmed sent response
        sent_response = db.query(SentResponse).filter_by(suggestion_id=suggestion_id).first()
        if sent_response is None:
            raise ValueError("Cannot create a learning candidate from an unconfirmed suggestion. Human must confirm sending first.")

        safe_title = candidate_title.strip()
        safe_content = candidate_content.strip()
        if not safe_title or not safe_content:
            raise ValueError("Candidate title and reviewed knowledge content are required.")
        if not content_reviewed_for_sensitive_data:
            raise ValueError("Confirm that the proposed knowledge content was reviewed for client-specific data.")
        if LearningService.SENSITIVE_KNOWLEDGE_PATTERN.search(safe_content):
            raise ValueError(
                "Proposed knowledge appears to contain an email, phone/account number, or credential. "
                "Generalize or redact client-specific data before proposing it."
            )

        target_article = None
        # If target_article_id is provided, verify it exists
        if target_article_id:
            target_article = db.query(KnowledgeArticle).filter_by(id=target_article_id).first()
            if not target_article:
                raise ValueError("Target knowledge article not found.")
            target_stable_key = target_article.stable_key

        # Collect source versions used in original suggestion
        sources = db.query(SuggestionSource).filter_by(suggestion_id=suggestion_id).all()
        source_versions = [
            {
                "article_id": s.article_id,
                "article_version_id": s.article_version_id,
                "version_number": s.version_number,
                "retrieval_rank": s.retrieval_rank,
            }
            for s in sources
        ]

        metadata = {
            "notes": notes or "",
            "confirmed_sent_at": sent_response.confirmed_at.isoformat(),
            "sent_text_hash": sent_response.final_text_hash,
            "candidate_content_hash": hashlib.sha256(safe_content.encode("utf-8")).hexdigest(),
            "content_differs_from_sent_response": safe_content != sent_response.exact_sent_text.strip(),
            "sensitive_data_review_confirmed": True,
        }

        # Repeated submissions of the same reviewed content are idempotent
        # while the earlier candidate remains proposed or has been approved.
        existing = (
            db.query(ResponseLearningCandidate)
            .filter(
                ResponseLearningCandidate.sent_response_id == sent_response.id,
                ResponseLearningCandidate.candidate_content == safe_content,
                ResponseLearningCandidate.lifecycle_status.in_(["proposed", "approved"]),
            )
            .first()
        )
        if existing is not None:
            return existing

        # Resolve product_scope and issue_type from target article, sources, or defaults
        product_scope = "core"
        issue_type = "general"
        client_scope = None
        if target_article:
            product_scope = target_article.product_scope
            issue_type = target_article.issue_type
            client_scope = target_article.client_scope
        elif sources:
            first_article = db.query(KnowledgeArticle).filter_by(id=sources[0].article_id).first()
            if first_article:
                product_scope = first_article.product_scope
                issue_type = first_article.issue_type
                client_scope = first_article.client_scope

        now = datetime.now(timezone.utc)
        candidate = ResponseLearningCandidate(
            id=str(uuid.uuid4()),
            suggestion_id=suggestion_id,
            sent_response_id=sent_response.id,
            candidate_title=safe_title,
            candidate_content=safe_content,
            product_scope=product_scope,
            issue_type=issue_type,
            client_scope=client_scope,
            target_stable_key=target_stable_key,
            target_article_id=target_article_id,
            source_versions_json=json.dumps(source_versions),
            metadata_json=json.dumps(metadata),
            lifecycle_status="proposed",
            created_by=actor,
            created_at=now,
        )
        db.add(candidate)
        db.add(
            AuditEvent(
                actor=actor,
                action="learning.candidate_proposed",
                resource=f"learning_candidate/{candidate.id}",
                correlation_id=str(uuid.uuid4()),
                timestamp=now,
                details_json=json.dumps(
                    {
                        "suggestion_id": suggestion_id,
                        "sent_response_id": sent_response.id,
                        "candidate_title": candidate.candidate_title,
                        "target_article_id": target_article_id,
                    }
                ),
            )
        )
        db.commit()
        db.refresh(candidate)
        return candidate

    @staticmethod
    def review_candidate(
        db: Session,
        candidate_id: str,
        decision: str,
        actor: str,
    ) -> ResponseLearningCandidate:
        candidate = db.query(ResponseLearningCandidate).filter_by(id=candidate_id).first()
        if candidate is None:
            raise ValueError("Learning candidate not found.")

        # Never allow AI to approve its own candidate or perform candidate reviews
        if actor.strip().lower() in {"ai", "gemini", "model", "copilot", "copilot-ai", "assistant", "system"}:
            raise ValueError("AI agents and automated systems are not authorized to approve learning candidates. Human approval is required.")

        # Idempotency check
        if candidate.lifecycle_status != "proposed":
            if candidate.lifecycle_status == ("approved" if decision == "approve" else "rejected"):
                return candidate
            raise ValueError(f"Learning candidate has already been reviewed (status: {candidate.lifecycle_status}).")

        now = datetime.now(timezone.utc)

        if decision == "reject":
            candidate.lifecycle_status = "rejected"
            candidate.reviewed_by = actor
            candidate.reviewed_at = now
            db.add(
                AuditEvent(
                    actor=actor,
                    action="learning.candidate_rejected",
                    resource=f"learning_candidate/{candidate.id}",
                    correlation_id=str(uuid.uuid4()),
                    timestamp=now,
                    details_json=json.dumps({"candidate_id": candidate.id}),
                )
            )
            db.commit()
            db.refresh(candidate)
            return candidate

        # Decision is "approve"
        # Find existing target article or create a new one
        target_article = None
        if candidate.target_article_id:
            target_article = db.query(KnowledgeArticle).filter_by(id=candidate.target_article_id).first()
        elif candidate.target_stable_key:
            target_article = db.query(KnowledgeArticle).filter_by(stable_key=candidate.target_stable_key).first()

        if target_article:
            # Create a new version for existing article
            new_version = KnowledgeService.create_version(
                db=db,
                article_id=target_article.id,
                content=candidate.candidate_content,
                created_by=actor,
                required_facts=[],
                prohibited_claims=[],
            )
            # Approve new version
            approved_ver = KnowledgeService.approve_version(
                db=db,
                article_id=target_article.id,
                version_num=new_version.version,
                approved_by=actor,
            )
            resulting_version_id = approved_ver.id
        else:
            # Create new knowledge article
            stable_key = candidate.target_stable_key or f"learned-{candidate.id[:8]}"
            new_article = KnowledgeService.create_article(
                db=db,
                stable_key=stable_key,
                title=candidate.candidate_title,
                product_scope=candidate.product_scope,
                issue_type=candidate.issue_type,
                initial_content=candidate.candidate_content,
                created_by=actor,
                client_scope=candidate.client_scope,
            )
            # Approve version 1
            approved_ver = KnowledgeService.approve_version(
                db=db,
                article_id=new_article.id,
                version_num=1,
                approved_by=actor,
            )
            resulting_version_id = approved_ver.id
            candidate.target_article_id = new_article.id
            candidate.target_stable_key = stable_key

        candidate.lifecycle_status = "approved"
        candidate.reviewed_by = actor
        candidate.reviewed_at = now
        candidate.resulting_article_version_id = resulting_version_id

        db.add(
            AuditEvent(
                actor=actor,
                action="learning.candidate_approved",
                resource=f"learning_candidate/{candidate.id}",
                correlation_id=str(uuid.uuid4()),
                timestamp=now,
                details_json=json.dumps(
                    {
                        "candidate_id": candidate.id,
                        "resulting_article_version_id": resulting_version_id,
                        "target_article_id": candidate.target_article_id,
                    }
                ),
            )
        )
        db.commit()
        db.refresh(candidate)
        return candidate

    @staticmethod
    def list_candidates(
        db: Session,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[ResponseLearningCandidate]:
        query = db.query(ResponseLearningCandidate)
        if status:
            query = query.filter_by(lifecycle_status=status)
        return query.order_by(ResponseLearningCandidate.created_at.desc()).limit(limit).all()
