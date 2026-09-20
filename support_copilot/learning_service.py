import json
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
    @staticmethod
    def propose_candidate(
        db: Session,
        suggestion_id: str,
        candidate_title: str,
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
            "original_draft": suggestion.draft,
            "confirmed_sent_at": sent_response.confirmed_at.isoformat(),
            "confirmed_by": sent_response.confirmed_by_actor,
        }

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
            candidate_title=candidate_title.strip(),
            candidate_content=sent_response.exact_sent_text.strip(),
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
