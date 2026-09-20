from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, field_validator

class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

class Actor(StrictBaseModel):
    external_id: str = Field(..., min_length=1, max_length=128)
    role: Literal["client", "agent"]

class ConversationInfo(StrictBaseModel):
    external_id: str = Field(..., min_length=1, max_length=128)
    case_hint: Optional[str] = Field(None, min_length=1, max_length=128)

class Payload(StrictBaseModel):
    text: str = Field(..., min_length=1, max_length=10000)

    @field_validator("text")
    @classmethod
    def validate_non_empty_text(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Message text cannot be empty or whitespace only.")
        return v

class EventEnvelope(StrictBaseModel):
    provider: Literal["manual"]
    event_id: str = Field(..., min_length=1, max_length=128)
    event_type: Literal["message.received", "message.sent"]
    occurred_at: datetime
    actor: Actor
    conversation: ConversationInfo
    payload: Payload
    schema_version: Literal[1] = 1

    @field_validator("occurred_at")
    @classmethod
    def validate_timezone_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
            raise ValueError("occurred_at must be a timezone-aware datetime.")
        return v

class SourceRef(StrictBaseModel):
    article_id: str = Field(..., min_length=1, max_length=128)
    version: int = Field(..., gt=0)

    @field_validator("article_id", mode="before")
    @classmethod
    def coerce_article_id(cls, v: Any) -> str:
        return str(v)

class SuggestedResponse(StrictBaseModel):
    draft: str = Field(..., min_length=1, max_length=10000)
    source_refs: List[SourceRef] = Field(default_factory=list, max_length=50)
    missing_facts: List[str] = Field(default_factory=list, max_length=50)
    assumptions: List[str] = Field(default_factory=list, max_length=50)
    prohibited_claims_detected: List[str] = Field(default_factory=list, max_length=50)
    confidence: float = Field(..., ge=0.0, le=1.0)
    recommended_action: Literal["ask_clarification", "reply", "escalate"]

class CaptureRequest(StrictBaseModel):
    event: EventEnvelope

class CaptureResponse(StrictBaseModel):
    status: Literal["accepted"]
    correlation_id: str = Field(..., min_length=1, max_length=128)
    captured_event_id: int = Field(..., ge=1)

# ==============================================================================
# Phase 1 Schemas
# ==============================================================================

class CreateKnowledgeArticleRequest(StrictBaseModel):
    stable_key: str = Field(..., min_length=1, max_length=128)
    title: str = Field(..., min_length=1, max_length=256)
    product_scope: str = Field(..., min_length=1, max_length=64)
    issue_type: str = Field(..., min_length=1, max_length=64)
    initial_content: str = Field(..., min_length=1, max_length=20000)
    client_scope: Optional[str] = Field(None, max_length=64)
    required_facts: List[str] = Field(default_factory=list, max_length=50)
    prohibited_claims: List[str] = Field(default_factory=list, max_length=50)

class CreateKnowledgeVersionRequest(StrictBaseModel):
    content: str = Field(..., min_length=1, max_length=20000)
    required_facts: List[str] = Field(default_factory=list, max_length=50)
    prohibited_claims: List[str] = Field(default_factory=list, max_length=50)

class KnowledgeArticleResponse(StrictBaseModel):
    id: str
    stable_key: str
    title: str
    product_scope: str
    issue_type: str
    client_scope: Optional[str] = None
    lifecycle_status: str
    created_at: datetime
    created_by: str

class KnowledgeVersionResponse(StrictBaseModel):
    id: str
    article_id: str
    version: int
    immutable_content: str
    required_facts: List[str]
    prohibited_claims: List[str]
    content_hash: str
    approval_state: str
    approved_at: Optional[datetime] = None
    approved_by: Optional[str] = None
    retired_at: Optional[datetime] = None
    created_at: datetime

class KnowledgeSearchResultItem(StrictBaseModel):
    article_id: str
    version_id: str
    version_number: int
    title: str
    content_excerpt: str
    full_content: str
    product_scope: str
    issue_type: str
    client_scope: Optional[str] = None
    required_facts: List[str]
    prohibited_claims: List[str]
    retrieval_score: float
    retrieval_rank: int

class KnowledgeSearchResponse(StrictBaseModel):
    results: List[KnowledgeSearchResultItem]

class CreateSuggestionRequest(StrictBaseModel):
    captured_event_id: int
    case_id: Optional[str] = None
    correlation_id: Optional[str] = None
    product_scope: Optional[str] = Field(default=None, min_length=1, max_length=64)
    issue_type: Optional[str] = Field(default=None, min_length=1, max_length=64)

class SuggestionSourceItem(StrictBaseModel):
    article_id: str
    article_version_id: str
    version_number: int
    retrieval_rank: int
    retrieval_score: float

class SuggestionCandidateItem(StrictBaseModel):
    id: str
    case_number: str
    title: str
    status: str
    client_identifier: str

class SuggestionResponse(StrictBaseModel):
    id: str
    captured_event_id: int
    resolved_case_id: Optional[str] = None
    lifecycle_status: str
    draft: str
    missing_facts: List[str] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)
    confidence: float
    recommended_action: str
    sources: List[SuggestionSourceItem] = Field(default_factory=list)
    correlation_id: str
    created_at: datetime
    copied_at: Optional[datetime] = None
    rejected_at: Optional[datetime] = None
    sent_at: Optional[datetime] = None

class SuggestionOutcomeResponse(StrictBaseModel):
    status: Literal["suggested", "resolution_required", "knowledge_unavailable", "provider_timeout", "provider_unavailable", "invalid_provider_output"]
    suggestion: Optional[SuggestionResponse] = None
    candidates: List[SuggestionCandidateItem] = Field(default_factory=list)
    message: Optional[str] = None
    correlation_id: str

class ConfirmSentRequest(StrictBaseModel):
    exact_sent_text: str = Field(..., min_length=1, max_length=10000)
    idempotency_key: str = Field(..., min_length=1, max_length=128)
    correlation_id: Optional[str] = Field(None, min_length=1, max_length=128)

    @field_validator("exact_sent_text")
    @classmethod
    def validate_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("exact_sent_text cannot be empty or whitespace only.")
        return v

class SentResponseModel(StrictBaseModel):
    id: str
    suggestion_id: str
    exact_sent_text: str
    final_text_hash: str
    confirmed_by_actor: str
    confirmation_idempotency_key: str
    confirmed_at: datetime

class ActivityEventItem(StrictBaseModel):
    id: str
    event_type: str
    subject_type: str
    subject_id: str
    case_id: Optional[str] = None
    actor: str
    details: Dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime

class ActivityTodayResponse(StrictBaseModel):
    events: List[ActivityEventItem]


class CreateActivityEventRequest(StrictBaseModel):
    event_type: Literal[
        "support.completed",
        "test.completed",
        "escalation.created",
        "followup.created",
        "outcome.verified",
    ]
    subject_type: Literal["support", "test", "escalation", "followup", "outcome"]
    subject_id: str = Field(..., min_length=1, max_length=128)
    case_id: Optional[str] = Field(default=None, max_length=36)
    correlation_id: Optional[str] = Field(default=None, max_length=128)
    details: Dict[str, Any] = Field(default_factory=dict)
    occurred_at: Optional[datetime] = None


class ReportPreviewRequest(StrictBaseModel):
    report_date: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    timezone_name: str = Field(default="Asia/Kolkata", min_length=1, max_length=64)


class ReportFinalizeRequest(StrictBaseModel):
    preview_id: str = Field(..., min_length=1, max_length=36)
    expected_facts_hash: str = Field(..., min_length=64, max_length=64)


class ReportSnapshotResponse(StrictBaseModel):
    id: str
    report_type: Literal["tod", "eod"]
    report_date: str
    timezone_name: str
    lifecycle_status: Literal["preview", "finalized"]
    facts_hash: str
    content: Dict[str, Any]
    created_at: datetime
    finalized_at: Optional[datetime] = None


class N8nReportExportRequest(StrictBaseModel):
    report_type: Literal["tod", "eod"] = "eod"
    report_date: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class N8nReportExportResponse(StrictBaseModel):
    event_id: str
    report: ReportSnapshotResponse


class CreateCaseRequest(StrictBaseModel):
    case_number: str = Field(..., min_length=1, max_length=64)
    title: str = Field(..., min_length=1, max_length=256)
    client_identifier: str = Field(..., min_length=1, max_length=128)


class UpdateCaseStatusRequest(StrictBaseModel):
    status: Literal["open", "closed"]


class CaseResponse(StrictBaseModel):
    id: str
    case_number: str
    title: str
    status: Literal["open", "closed"]
    client_identifier: str
    created_at: datetime
    updated_at: datetime


class CaseListResponse(StrictBaseModel):
    cases: List[CaseResponse]
