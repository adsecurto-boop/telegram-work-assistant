from datetime import datetime, timezone
import uuid
from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    DateTime,
    Float,
    ForeignKey,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class CapturedEvent(Base):
    __tablename__ = "captured_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider = Column(String(64), nullable=False, index=True)
    event_id = Column(String(128), nullable=False, index=True)
    event_type = Column(String(64), nullable=False)
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    actor_id = Column(String(128), nullable=False)
    actor_role = Column(String(32), nullable=False)
    conversation_id = Column(String(128), nullable=False, index=True)
    case_hint = Column(String(128), nullable=True)
    payload_text = Column(Text, nullable=False)
    schema_version = Column(Integer, nullable=False, default=1)
    correlation_id = Column(String(128), nullable=False, index=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("provider", "event_id", name="uq_captured_provider_event"),
    )


class IntegrationIdempotency(Base):
    __tablename__ = "integration_idempotency"

    id = Column(Integer, primary_key=True, autoincrement=True)
    provider = Column(String(64), nullable=False, index=True)
    event_id = Column(String(128), nullable=False, index=True)
    payload_hash = Column(String(64), nullable=False)
    correlation_id = Column(String(128), nullable=False, index=True)
    status = Column(String(32), nullable=False)
    response_json = Column(Text, nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint("provider", "event_id", name="uq_idempotency_provider_event"),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    actor = Column(String(128), nullable=False, index=True)
    action = Column(String(64), nullable=False)
    resource = Column(String(128), nullable=False)
    correlation_id = Column(String(128), nullable=False, index=True)
    timestamp = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    details_json = Column(Text, nullable=False)


# ==============================================================================
# Phase 1 Models: Knowledge, Cases, Suggestions, Sent Responses, Activity
# ==============================================================================


class KnowledgeArticle(Base):
    __tablename__ = "knowledge_articles"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    stable_key = Column(String(128), nullable=False, unique=True, index=True)
    title = Column(String(256), nullable=False)
    product_scope = Column(String(64), nullable=False, index=True)
    issue_type = Column(String(64), nullable=False, index=True)
    client_scope = Column(String(64), nullable=True, index=True)
    lifecycle_status = Column(String(32), nullable=False, default="draft")  # draft, approved, retired
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    created_by = Column(String(128), nullable=False)

    versions = relationship("KnowledgeArticleVersion", back_populates="article", cascade="all, delete-orphan")


class KnowledgeArticleVersion(Base):
    __tablename__ = "knowledge_article_versions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    article_id = Column(String(36), ForeignKey("knowledge_articles.id"), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    immutable_content = Column(Text, nullable=False)
    required_facts = Column(Text, nullable=False, default="[]")  # JSON list
    prohibited_claims = Column(Text, nullable=False, default="[]")  # JSON list
    content_hash = Column(String(64), nullable=False)
    approval_state = Column(String(32), nullable=False, default="draft")  # draft, approved, retired
    approved_at = Column(DateTime(timezone=True), nullable=True)
    approved_by = Column(String(128), nullable=True)
    retired_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    article = relationship("KnowledgeArticle", back_populates="versions")

    __table_args__ = (
        UniqueConstraint("article_id", "version", name="uq_article_version"),
    )


class SupportCase(Base):
    __tablename__ = "support_cases"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    case_number = Column(String(64), nullable=False, unique=True, index=True)
    title = Column(String(256), nullable=False)
    status = Column(String(32), nullable=False, default="open", index=True)  # open, closed
    client_identifier = Column(String(128), nullable=False, index=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    provider = Column(String(64), nullable=False)
    external_conversation_id = Column(String(128), nullable=False, index=True)
    case_id = Column(String(36), ForeignKey("support_cases.id"), nullable=True, index=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    case = relationship("SupportCase")

    __table_args__ = (
        UniqueConstraint("provider", "external_conversation_id", name="uq_conv_provider_ext_id"),
    )


class ResponseSuggestion(Base):
    __tablename__ = "response_suggestions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    captured_event_id = Column(Integer, ForeignKey("captured_events.id"), nullable=False, index=True)
    resolved_case_id = Column(String(36), ForeignKey("support_cases.id"), nullable=True, index=True)
    lifecycle_status = Column(String(32), nullable=False, default="suggested")  # suggested, copied, rejected, sent
    draft = Column(Text, nullable=False)
    missing_facts = Column(Text, nullable=False, default="[]")  # JSON list
    assumptions = Column(Text, nullable=False, default="[]")  # JSON list
    prohibited_claim_evaluation = Column(Text, nullable=False, default="{}")  # JSON dict
    confidence = Column(Float, nullable=False, default=1.0)
    recommended_action = Column(String(128), nullable=False, default="review")
    provider_identifier = Column(String(64), nullable=False)
    model_identifier = Column(String(64), nullable=True)
    correlation_id = Column(String(128), nullable=False, index=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    copied_at = Column(DateTime(timezone=True), nullable=True)
    rejected_at = Column(DateTime(timezone=True), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    edited_text_hash = Column(String(64), nullable=True)

    sources = relationship("SuggestionSource", back_populates="suggestion", cascade="all, delete-orphan")
    sent_response = relationship("SentResponse", back_populates="suggestion", uselist=False)


class SuggestionSource(Base):
    __tablename__ = "suggestion_sources"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    suggestion_id = Column(String(36), ForeignKey("response_suggestions.id"), nullable=False, index=True)
    article_id = Column(String(36), ForeignKey("knowledge_articles.id"), nullable=False, index=True)
    article_version_id = Column(String(36), ForeignKey("knowledge_article_versions.id"), nullable=False, index=True)
    version_number = Column(Integer, nullable=False)
    retrieval_rank = Column(Integer, nullable=False)
    retrieval_score = Column(Float, nullable=False)

    suggestion = relationship("ResponseSuggestion", back_populates="sources")

    __table_args__ = (
        UniqueConstraint("suggestion_id", "article_version_id", name="uq_sugg_src_version"),
    )


class SentResponse(Base):
    __tablename__ = "sent_responses"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    suggestion_id = Column(String(36), ForeignKey("response_suggestions.id"), nullable=False, unique=True, index=True)
    exact_sent_text = Column(Text, nullable=False)
    final_text_hash = Column(String(64), nullable=False)
    confirmed_by_actor = Column(String(128), nullable=False)
    confirmation_idempotency_key = Column(String(128), nullable=False, unique=True, index=True)
    confirmed_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    suggestion = relationship("ResponseSuggestion", back_populates="sent_response")


class ActivityEvent(Base):
    __tablename__ = "activity_events"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    event_type = Column(String(64), nullable=False, index=True)  # suggestion.generated, suggestion.copied, suggestion.rejected, response.sent
    subject_type = Column(String(32), nullable=False)  # suggestion, response
    subject_id = Column(String(36), nullable=False, index=True)
    case_id = Column(String(36), nullable=True, index=True)
    correlation_id = Column(String(128), nullable=False, index=True)
    actor = Column(String(128), nullable=False, index=True)
    details_json = Column(Text, nullable=False)
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class ReportSnapshot(Base):
    __tablename__ = "report_snapshots"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    report_type = Column(String(16), nullable=False, index=True)
    report_date = Column(String(10), nullable=False, index=True)
    timezone_name = Column(String(64), nullable=False)
    lifecycle_status = Column(String(16), nullable=False, default="preview", index=True)
    facts_hash = Column(String(64), nullable=False)
    content_json = Column(Text, nullable=False)
    created_by = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    finalized_at = Column(DateTime(timezone=True), nullable=True)
