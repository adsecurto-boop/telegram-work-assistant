import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import List, Optional
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from .models import KnowledgeArticle, KnowledgeArticleVersion, AuditEvent
from .logger import logger


class KnowledgeSearchResult(BaseModel):
    article_id: str
    version_id: str
    version_number: int
    title: str
    content_excerpt: str
    full_content: str
    product_scope: str
    issue_type: str
    client_scope: Optional[str] = None
    required_facts: List[str] = Field(default_factory=list)
    prohibited_claims: List[str] = Field(default_factory=list)
    retrieval_score: float = 1.0
    retrieval_rank: int = 1


def compute_content_hash(content: str, required_facts: List[str], prohibited_claims: List[str]) -> str:
    """Deterministic SHA256 hash of version content and policies."""
    payload = {
        "content": content.strip(),
        "required_facts": sorted(required_facts),
        "prohibited_claims": sorted(prohibited_claims),
    }
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


STOP_WORDS = {
    "a", "an", "the", "and", "or", "in", "on", "at", "to", "for", "with",
    "is", "are", "was", "were", "be", "been", "being", "have", "has", "had",
    "do", "does", "did", "of", "from", "by", "about", "into", "through",
    "during", "before", "after", "above", "below", "up", "down", "out",
    "off", "over", "under", "again", "further", "then", "once", "here", "there",
    "when", "where", "why", "how", "all", "any", "both", "each", "few", "more",
    "most", "other", "some", "such", "no", "nor", "not", "only", "own", "same",
    "so", "than", "too", "very", "can", "will", "just", "don", "should", "now",
    "my", "your", "our", "their", "his", "her", "its", "i", "you", "we", "they",
    "me", "him", "us", "them", "what", "which", "who", "whom", "this", "that",
    "these", "those", "am"
}

def sanitize_fts_query(query: str) -> str:
    """
    Sanitizes user input for SQLite FTS5 MATCH queries.
    Removes FTS control characters and operators to prevent syntax errors.
    Filters out common stop words and joins terms with OR so that documents
    matching more query keywords are ranked higher by FTS5 BM25.
    """
    cleaned = re.sub(r'[^\w\s]', ' ', query, flags=re.UNICODE)
    raw_tokens = [t.strip().lower() for t in cleaned.split() if t.strip()]
    tokens = [t for t in raw_tokens if t not in STOP_WORDS]
    if not tokens:
        tokens = raw_tokens
    if not tokens:
        return ""
    quoted_tokens = [f'"{t}"' for t in tokens]
    return " OR ".join(quoted_tokens)


class KnowledgeService:
    @staticmethod
    def create_article(
        db: Session,
        stable_key: str,
        title: str,
        product_scope: str,
        issue_type: str,
        initial_content: str,
        created_by: str,
        client_scope: Optional[str] = None,
        required_facts: Optional[List[str]] = None,
        prohibited_claims: Optional[List[str]] = None,
        correlation_id: Optional[str] = None,
    ) -> KnowledgeArticle:
        """Creates a new knowledge article and its initial draft version (version 1)."""
        existing = db.query(KnowledgeArticle).filter_by(stable_key=stable_key).first()
        if existing:
            raise ValueError(f"Knowledge article with stable_key '{stable_key}' already exists.")

        req_facts = required_facts or []
        proh_claims = prohibited_claims or []
        content_hash = compute_content_hash(initial_content, req_facts, proh_claims)

        article_id = str(uuid.uuid4())
        article = KnowledgeArticle(
            id=article_id,
            stable_key=stable_key,
            title=title,
            product_scope=product_scope,
            issue_type=issue_type,
            client_scope=client_scope,
            lifecycle_status="draft",
            created_at=datetime.now(timezone.utc),
            created_by=created_by,
        )
        db.add(article)

        version_id = str(uuid.uuid4())
        version = KnowledgeArticleVersion(
            id=version_id,
            article_id=article_id,
            version=1,
            immutable_content=initial_content,
            required_facts=json.dumps(req_facts),
            prohibited_claims=json.dumps(proh_claims),
            content_hash=content_hash,
            approval_state="draft",
            created_at=datetime.now(timezone.utc),
        )
        db.add(version)

        # Audit event
        audit = AuditEvent(
            actor=created_by,
            action="knowledge.article.created",
            resource=f"knowledge_article/{article_id}",
            correlation_id=correlation_id or str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc),
            details_json=json.dumps({
                "stable_key": stable_key,
                "version": 1,
                "product_scope": product_scope,
                "issue_type": issue_type,
            }),
        )
        db.add(audit)
        db.commit()
        db.refresh(article)
        return article

    @staticmethod
    def create_version(
        db: Session,
        article_id: str,
        content: str,
        created_by: str,
        required_facts: Optional[List[str]] = None,
        prohibited_claims: Optional[List[str]] = None,
        correlation_id: Optional[str] = None,
    ) -> KnowledgeArticleVersion:
        """Creates a new immutable draft version for an existing article."""
        article = db.query(KnowledgeArticle).filter_by(id=article_id).first()
        if not article:
            raise ValueError(f"Knowledge article with id '{article_id}' not found.")

        versions = db.query(KnowledgeArticleVersion).filter_by(article_id=article_id).all()
        next_version = max((v.version for v in versions), default=0) + 1

        req_facts = required_facts or []
        proh_claims = prohibited_claims or []
        content_hash = compute_content_hash(content, req_facts, proh_claims)

        version_id = str(uuid.uuid4())
        new_version = KnowledgeArticleVersion(
            id=version_id,
            article_id=article_id,
            version=next_version,
            immutable_content=content,
            required_facts=json.dumps(req_facts),
            prohibited_claims=json.dumps(proh_claims),
            content_hash=content_hash,
            approval_state="draft",
            created_at=datetime.now(timezone.utc),
        )
        db.add(new_version)

        audit = AuditEvent(
            actor=created_by,
            action="knowledge.version.created",
            resource=f"knowledge_article_version/{version_id}",
            correlation_id=correlation_id or str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc),
            details_json=json.dumps({
                "article_id": article_id,
                "version": next_version,
                "content_hash": content_hash,
            }),
        )
        db.add(audit)
        db.commit()
        db.refresh(new_version)
        return new_version

    @staticmethod
    def approve_version(
        db: Session,
        article_id: str,
        version_num: int,
        approved_by: str,
        correlation_id: Optional[str] = None,
    ) -> KnowledgeArticleVersion:
        """
        Approves a draft knowledge article version.
        Requires knowledge:approve capability.
        Only approved versions enter FTS5 retrieval.
        Previous approved versions of this article are retired from retrieval.
        """
        version = (
            db.query(KnowledgeArticleVersion)
            .filter_by(article_id=article_id, version=version_num)
            .first()
        )
        if not version:
            raise ValueError(f"Version {version_num} for article '{article_id}' not found.")
        if version.approval_state == "approved":
            raise ValueError(f"Version {version_num} is already approved.")
        if version.approval_state == "retired":
            raise ValueError(f"Cannot approve a retired version.")

        article = db.query(KnowledgeArticle).filter_by(id=article_id).first()
        now = datetime.now(timezone.utc)

        # Retire previously approved versions for this article
        previous_approved = (
            db.query(KnowledgeArticleVersion)
            .filter(
                KnowledgeArticleVersion.article_id == article_id,
                KnowledgeArticleVersion.approval_state == "approved",
                KnowledgeArticleVersion.id != version.id,
            )
            .all()
        )
        for prev in previous_approved:
            prev.approval_state = "retired"
            prev.retired_at = now
            # Remove previous version from FTS
            db.execute(
                text("DELETE FROM knowledge_articles_fts WHERE version_id = :vid"),
                {"vid": prev.id},
            )

        version.approval_state = "approved"
        version.approved_at = now
        version.approved_by = approved_by
        article.lifecycle_status = "approved"

        # Index in FTS5
        db.execute(
            text(
                "INSERT INTO knowledge_articles_fts ("
                "version_id, article_id, title, content, product_scope, issue_type, client_scope"
                ") VALUES (:vid, :aid, :title, :content, :product, :issue, :client)"
            ),
            {
                "vid": version.id,
                "aid": article.id,
                "title": article.title,
                "content": version.immutable_content,
                "product": article.product_scope,
                "issue": article.issue_type,
                "client": article.client_scope or "",
            },
        )

        audit = AuditEvent(
            actor=approved_by,
            action="knowledge.version.approved",
            resource=f"knowledge_article_version/{version.id}",
            correlation_id=correlation_id or str(uuid.uuid4()),
            timestamp=now,
            details_json=json.dumps({
                "article_id": article_id,
                "version": version_num,
                "approved_by": approved_by,
            }),
        )
        db.add(audit)
        db.commit()
        db.refresh(version)
        return version

    @staticmethod
    def retire_version(
        db: Session,
        article_id: str,
        version_num: int,
        retired_by: str,
        correlation_id: Optional[str] = None,
    ) -> KnowledgeArticleVersion:
        """
        Retires an approved or draft knowledge article version.
        Removes the version from FTS5 index.
        Historical references to this version are preserved.
        """
        version = (
            db.query(KnowledgeArticleVersion)
            .filter_by(article_id=article_id, version=version_num)
            .first()
        )
        if not version:
            raise ValueError(f"Version {version_num} for article '{article_id}' not found.")
        if version.approval_state == "retired":
            raise ValueError(f"Version {version_num} is already retired.")

        now = datetime.now(timezone.utc)
        version.approval_state = "retired"
        version.retired_at = now

        # Remove from FTS5
        db.execute(
            text("DELETE FROM knowledge_articles_fts WHERE version_id = :vid"),
            {"vid": version.id},
        )

        # Check if article has any other approved version
        other_approved = (
            db.query(KnowledgeArticleVersion)
            .filter(
                KnowledgeArticleVersion.article_id == article_id,
                KnowledgeArticleVersion.approval_state == "approved",
                KnowledgeArticleVersion.id != version.id,
            )
            .count()
        )
        article = db.query(KnowledgeArticle).filter_by(id=article_id).first()
        if other_approved == 0:
            article.lifecycle_status = "retired"

        audit = AuditEvent(
            actor=retired_by,
            action="knowledge.version.retired",
            resource=f"knowledge_article_version/{version.id}",
            correlation_id=correlation_id or str(uuid.uuid4()),
            timestamp=now,
            details_json=json.dumps({
                "article_id": article_id,
                "version": version_num,
                "retired_by": retired_by,
            }),
        )
        db.add(audit)
        db.commit()
        db.refresh(version)
        return version

    @staticmethod
    def search_approved_knowledge(
        db: Session,
        query_text: str,
        product_scope: Optional[str] = None,
        issue_type: Optional[str] = None,
        client_scope: Optional[str] = None,
        limit: int = 5,
    ) -> List[KnowledgeSearchResult]:
        """
        FTS5 search over approved, non-retired knowledge article versions.
        Applies metadata filters and stable tie-breaking: rank ASC, version DESC, article_id ASC.
        Draft and retired versions are NEVER returned.
        """
        match_query = sanitize_fts_query(query_text)
        if not match_query:
            return []

        # Execute FTS query
        sql = """
            SELECT
                fts.version_id,
                fts.article_id,
                fts.title,
                fts.content,
                fts.product_scope,
                fts.issue_type,
                fts.client_scope,
                fts.rank
            FROM knowledge_articles_fts fts
            WHERE knowledge_articles_fts MATCH :match_query
            ORDER BY fts.rank ASC
        """
        rows = db.execute(text(sql), {"match_query": match_query}).fetchall()

        results: List[KnowledgeSearchResult] = []
        rank_counter = 1

        for row in rows:
            vid, aid, title, content, prod, issue, client, score = row

            # Verify in database that this version is approved and not retired
            v_record = (
                db.query(KnowledgeArticleVersion)
                .filter_by(id=vid, approval_state="approved")
                .first()
            )
            if not v_record or v_record.retired_at is not None:
                continue

            # Apply metadata filters
            if product_scope and prod != product_scope:
                continue
            if issue_type and issue != issue_type:
                continue
            if client_scope and client and client != client_scope:
                continue

            req_facts = json.loads(v_record.required_facts) if v_record.required_facts else []
            proh_claims = json.loads(v_record.prohibited_claims) if v_record.prohibited_claims else []

            results.append(
                KnowledgeSearchResult(
                    article_id=aid,
                    version_id=vid,
                    version_number=v_record.version,
                    title=title,
                    content_excerpt=content[:500] if content else "",
                    full_content=content,
                    product_scope=prod,
                    issue_type=issue,
                    client_scope=client if client else None,
                    required_facts=req_facts,
                    prohibited_claims=proh_claims,
                    retrieval_score=float(score) if score is not None else 1.0,
                    retrieval_rank=rank_counter,
                )
            )
            rank_counter += 1
            if len(results) >= limit:
                break

        # Stable tie-breaking: retrieval_rank ASC, version_number DESC, article_id ASC
        results.sort(key=lambda r: (r.retrieval_rank, -r.version_number, r.article_id))
        return results

    @staticmethod
    def rebuild_fts_index(db: Session) -> int:
        """
        Rebuilds the FTS5 index from all active approved, non-retired versions.
        Idempotent operation.
        """
        db.execute(text("DELETE FROM knowledge_articles_fts;"))
        approved_versions = (
            db.query(KnowledgeArticleVersion, KnowledgeArticle)
            .join(KnowledgeArticle, KnowledgeArticleVersion.article_id == KnowledgeArticle.id)
            .filter(
                KnowledgeArticleVersion.approval_state == "approved",
                KnowledgeArticleVersion.retired_at.is_(None),
            )
            .all()
        )

        for version, article in approved_versions:
            db.execute(
                text(
                    "INSERT INTO knowledge_articles_fts ("
                    "version_id, article_id, title, content, product_scope, issue_type, client_scope"
                    ") VALUES (:vid, :aid, :title, :content, :product, :issue, :client)"
                ),
                {
                    "vid": version.id,
                    "aid": article.id,
                    "title": article.title,
                    "content": version.immutable_content,
                    "product": article.product_scope,
                    "issue": article.issue_type,
                    "client": article.client_scope or "",
                },
            )

        db.commit()
        logger.info(f"Rebuilt knowledge FTS5 index with {len(approved_versions)} approved versions.")
        return len(approved_versions)


def main():
    """CLI helper for knowledge service, e.g. rebuilding FTS index."""
    import sys
    from .config import get_settings
    from .database import create_db_engine, create_session_factory

    if len(sys.argv) > 1 and sys.argv[1] == "rebuild-fts":
        settings = get_settings()
        engine = create_db_engine(settings.db_path)
        session_factory = create_session_factory(engine)
        db = session_factory()
        try:
            count = KnowledgeService.rebuild_fts_index(db)
            print(f"FTS5 index rebuilt successfully with {count} approved articles.")
        finally:
            db.close()
            engine.dispose()
    else:
        print("Usage: python -m support_copilot.knowledge_service rebuild-fts")


if __name__ == "__main__":
    main()
