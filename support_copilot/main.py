import json
import os
from typing import Optional, Callable
from fastapi import FastAPI, Depends, Request, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.engine import Engine

from .config import Settings, get_settings, ALLOWED_LOOPBACK_HOSTS
from .schemas import (
    CaptureRequest,
    CaptureResponse,
    CreateKnowledgeArticleRequest,
    CreateKnowledgeVersionRequest,
    KnowledgeArticleResponse,
    KnowledgeVersionResponse,
    KnowledgeSearchResponse,
    KnowledgeSearchResultItem,
    CreateSuggestionRequest,
    SuggestionResponse,
    SuggestionSourceItem,
    SuggestionCandidateItem,
    SuggestionOutcomeResponse,
    ConfirmSentRequest,
    SentResponseModel,
    ActivityTodayResponse,
    ActivityEventItem,
    CreateActivityEventRequest,
    ReportPreviewRequest,
    ReportFinalizeRequest,
    ReportSnapshotResponse,
    N8nReportExportRequest,
    N8nReportExportResponse,
    CreateCaseRequest,
    UpdateCaseStatusRequest,
    CaseResponse,
    CaseListResponse,
    StartMeetingRequest,
    MeetingSessionResponse,
    TranscriptSegmentRequest,
    TranscriptSegmentResponse,
    MeetingProposalResponse,
    MeetingProposalListResponse,
    ReviewMeetingProposalRequest,
    RetentionPurgeResponse,
    MeetingDetailResponse,
    ScreenAnalysisRequest,
    ScreenAnalysisResponse,
    ScreenAnalysisSource,
    ProposeLearningCandidateRequest,
    ReviewLearningCandidateRequest,
    LearningCandidateResponse,
    LearningCandidateListResponse,
    DetailedHealthResponse,
)
from .logger import logger
from .database import create_db_engine, create_session_factory, verify_schema_readiness
from .auth import require_capability, AuthenticatedCaller
from .service import CaptureService, IdempotencyConflictError
from .ai_provider import AIProvider, GeminiAIProvider, ProviderTimeoutError, ProviderUnavailableError
from .screen_provider import GeminiScreenAnalysisProvider, ScreenAnalysisProvider

def create_app(
    settings: Optional[Settings] = None,
    engine: Optional[Engine] = None,
    session_factory: Optional[sessionmaker] = None,
    ai_provider: Optional[AIProvider] = None,
    screen_analysis_provider: Optional[ScreenAnalysisProvider] = None,
    verify_schema: bool = True,
    verify_auth: bool = True,
    dispose_engine_on_shutdown: bool = False,
) -> FastAPI:
    if settings is None:
        settings = get_settings()

    # Validate bind host
    if settings.api_host.strip().lower() not in ALLOWED_LOOPBACK_HOSTS:
        raise ValueError(
            f"Unsafe bind host '{settings.api_host}'. Only loopback addresses are permitted."
        )

    # Validate API token configuration
    if verify_auth:
        settings.validate_tokens_for_startup()

    # Database engine and session factory
    if engine is None:
        engine = create_db_engine(settings.db_path)
    if session_factory is None:
        session_factory = create_session_factory(engine)

    if verify_schema:
        verify_schema_readiness(engine)

    app = FastAPI(
        title="Support Copilot Core API",
        description="Local loopback API for Support Copilot",
        version="0.1.0",
    )

    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.ai_provider = ai_provider
    app.state.screen_analysis_provider = screen_analysis_provider

    if dispose_engine_on_shutdown:
        app.router.add_event_handler("shutdown", engine.dispose)

    def get_db() -> Session:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    def get_app_settings() -> Settings:
        return settings

    # Loopback network boundary middleware
    @app.middleware("http")
    async def loopback_only(request: Request, call_next):
        client_host = request.client.host if request.client else None
        if client_host not in ("127.0.0.1", "::1", "localhost", "testclient"):
            logger.warning(
                f"Unauthorized network access attempt from non-loopback address: {client_host}",
                extra={"failure_category": "network_boundary_violation"},
            )
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"detail": "Unauthorized. Loopback interface only."},
            )
        response = await call_next(request)
        return response

    # Exception handlers
    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(request: Request, exc: RequestValidationError):
        errors = []
        for err in exc.errors():
            loc = ".".join(str(l) for l in err.get("loc", []))
            errors.append({"field": loc, "message": err.get("msg", "Invalid field")})
        # Use 422 integer directly to avoid framework deprecation differences
        return JSONResponse(
            status_code=422,
            content={"detail": errors, "error_type": "validation_error"},
        )

    @app.exception_handler(IdempotencyConflictError)
    async def idempotency_conflict_handler(request: Request, exc: IdempotencyConflictError):
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(exc), "error_type": "idempotency_conflict"},
        )

    from .suggestion_service import ConflictError

    @app.exception_handler(ConflictError)
    async def conflict_handler(request: Request, exc: ConflictError):
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(exc), "error_type": "conflict"},
        )

    @app.exception_handler(ProviderTimeoutError)
    async def provider_timeout_handler(request: Request, exc: ProviderTimeoutError):
        return JSONResponse(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            content={"detail": str(exc), "error_type": "provider_timeout"},
        )

    @app.exception_handler(ProviderUnavailableError)
    async def provider_unavailable_handler(request: Request, exc: ProviderUnavailableError):
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": str(exc), "error_type": "provider_unavailable"},
        )

    # Routes
    @app.get("/v1/health")
    async def health_check():
        """Health endpoint that reports schema readiness without leaking database paths or secrets."""
        return {
            "status": "ok",
            "schema_ready": True,
        }

    @app.get("/v1/health/detailed", response_model=DetailedHealthResponse)
    async def detailed_health(
        db: Session = Depends(get_db),
        caller: AuthenticatedCaller = Depends(require_capability("operations:read")),
    ):
        from sqlalchemy import text
        from pathlib import Path
        from .database import CURRENT_SCHEMA_REVISION, REQUIRED_TABLES_CURRENT

        db_connected = True
        integrity_ok = True
        try:
            db.execute(text("SELECT 1"))
            res = db.execute(text("PRAGMA integrity_check;")).scalar()
            integrity_ok = (res == "ok")
        except Exception:
            db_connected = False
            integrity_ok = False

        schema_revision = "unknown"
        schema_ready = False
        try:
            row = db.execute(text("SELECT version_num FROM alembic_version;")).first()
            if row:
                schema_revision = str(row[0])
            verify_schema_readiness(engine, required_tables=REQUIRED_TABLES_CURRENT)
            schema_ready = schema_revision == CURRENT_SCHEMA_REVISION
        except Exception:
            pass

        data_dir = Path(settings.data_dir)
        writable = os.access(data_dir, os.W_OK) if data_dir.exists() else False

        # Provider mode without revealing keys
        ai_configured = settings.ai_provider == "disabled" or bool(settings.gemini_api_key)
        ai_mode = settings.ai_provider

        # Check latest backup
        backups_dir = data_dir / "backups"
        last_backup = None
        if backups_dir.exists():
            manifests = list(backups_dir.glob("*/manifest.json")) + list(backups_dir.glob("*_manifest.json"))
            if manifests:
                manifests.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                try:
                    m_data = json.loads(manifests[0].read_text(encoding="utf-8"))
                    last_backup = m_data.get("creation_timestamp") or m_data.get("created_at")
                except Exception:
                    pass

        # The Telegram adapter is a separate process and exposes no reliable
        # heartbeat yet.  Do not infer runtime state from this API process.
        telegram_status = "external_adapter_unreported"
        n8n_configured = bool(settings.n8n_shared_secret)

        # Status calculation
        if not db_connected or not integrity_ok or not writable or not schema_ready:
            overall = "unhealthy"
        elif not ai_configured:
            overall = "degraded"
        else:
            overall = "healthy"

        return DetailedHealthResponse(
            status=overall,
            api_status="healthy",
            database={
                "connected": db_connected,
                "schema_revision": schema_revision,
                "schema_ready": schema_ready,
                "integrity_check": "ok" if integrity_ok else "failed",
                "data_dir_writable": writable,
            },
            ai_provider={
                "configured": ai_configured,
                "mode": ai_mode,
            },
            backup={
                "last_backup_timestamp": last_backup,
            },
            retention={
                "status": "manual_cleanup_available",
            },
            telegram={
                "status": telegram_status,
            },
            n8n={
                "configured": n8n_configured,
            },
        )

    @app.post("/v1/captures/manual-message", response_model=CaptureResponse)
    async def capture_manual_message(
        capture: CaptureRequest,
        db: Session = Depends(get_db),
        caller: AuthenticatedCaller = Depends(require_capability("capture:write")),
    ):
        service = CaptureService(db)
        return service.capture_manual_message(capture.event, actor=caller.token_name)

    # ==============================================================================
    # Phase 1 Routes: Knowledge, Suggestions, Confirm-Sent, Activity
    # ==============================================================================
    from .knowledge_service import KnowledgeService
    from .suggestion_service import SuggestionService
    from .ai_gateway import AIProviderGateway
    from .models import ResponseSuggestion, SuggestionSource, ActivityEvent

    @app.post("/v1/knowledge/articles", response_model=KnowledgeArticleResponse)
    async def create_knowledge_article(
        payload: CreateKnowledgeArticleRequest,
        db: Session = Depends(get_db),
        caller: AuthenticatedCaller = Depends(require_capability("knowledge:write")),
    ):
        try:
            art = KnowledgeService.create_article(
                db=db,
                stable_key=payload.stable_key,
                title=payload.title,
                product_scope=payload.product_scope,
                issue_type=payload.issue_type,
                initial_content=payload.initial_content,
                client_scope=payload.client_scope,
                required_facts=payload.required_facts,
                prohibited_claims=payload.prohibited_claims,
                created_by=caller.token_name,
            )
            return KnowledgeArticleResponse(
                id=art.id,
                stable_key=art.stable_key,
                title=art.title,
                product_scope=art.product_scope,
                issue_type=art.issue_type,
                client_scope=art.client_scope,
                lifecycle_status=art.lifecycle_status,
                created_at=art.created_at,
                created_by=art.created_by,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    def case_response(case) -> CaseResponse:
        return CaseResponse(
            id=case.id,
            case_number=case.case_number,
            title=case.title,
            status=case.status,
            client_identifier=case.client_identifier,
            created_at=case.created_at,
            updated_at=case.updated_at,
        )

    @app.post("/v1/cases", response_model=CaseResponse)
    async def create_case(
        payload: CreateCaseRequest,
        db: Session = Depends(get_db),
        caller: AuthenticatedCaller = Depends(require_capability("case:write")),
    ):
        import uuid
        from datetime import datetime, timezone
        from sqlalchemy.exc import IntegrityError
        from .models import SupportCase
        case = SupportCase(
            id=str(uuid.uuid4()),
            case_number=payload.case_number.strip(),
            title=payload.title.strip(),
            status="open",
            client_identifier=payload.client_identifier.strip(),
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        db.add(case)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail="Case number already exists.") from exc
        db.refresh(case)
        return case_response(case)

    @app.get("/v1/cases", response_model=CaseListResponse)
    async def list_cases(
        status_filter: Optional[str] = None,
        client_identifier: Optional[str] = None,
        db: Session = Depends(get_db),
        caller: AuthenticatedCaller = Depends(require_capability("case:read")),
    ):
        from .models import SupportCase
        query = db.query(SupportCase)
        if status_filter:
            if status_filter not in {"open", "closed"}:
                raise HTTPException(status_code=400, detail="status_filter must be open or closed.")
            query = query.filter_by(status=status_filter)
        if client_identifier:
            query = query.filter_by(client_identifier=client_identifier)
        cases = query.order_by(SupportCase.updated_at.desc()).limit(100).all()
        return CaseListResponse(cases=[case_response(case) for case in cases])

    @app.patch("/v1/cases/{case_id}/status", response_model=CaseResponse)
    async def update_case_status(
        case_id: str,
        payload: UpdateCaseStatusRequest,
        db: Session = Depends(get_db),
        caller: AuthenticatedCaller = Depends(require_capability("case:write")),
    ):
        from datetime import datetime, timezone
        from .models import SupportCase
        case = db.query(SupportCase).filter(
            (SupportCase.id == case_id) | (SupportCase.case_number == case_id)
        ).first()
        if case is None:
            raise HTTPException(status_code=404, detail="Case not found.")
        case.status = payload.status
        case.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(case)
        return case_response(case)

    @app.post("/v1/knowledge/articles/{article_id}/versions", response_model=KnowledgeVersionResponse)
    async def create_knowledge_version(
        article_id: str,
        payload: CreateKnowledgeVersionRequest,
        db: Session = Depends(get_db),
        caller: AuthenticatedCaller = Depends(require_capability("knowledge:write")),
    ):
        try:
            v = KnowledgeService.create_version(
                db=db,
                article_id=article_id,
                content=payload.content,
                created_by=caller.token_name,
                required_facts=payload.required_facts,
                prohibited_claims=payload.prohibited_claims,
            )
            import json
            return KnowledgeVersionResponse(
                id=v.id,
                article_id=v.article_id,
                version=v.version,
                immutable_content=v.immutable_content,
                required_facts=json.loads(v.required_facts) if v.required_facts else [],
                prohibited_claims=json.loads(v.prohibited_claims) if v.prohibited_claims else [],
                content_hash=v.content_hash,
                approval_state=v.approval_state,
                approved_at=v.approved_at,
                approved_by=v.approved_by,
                retired_at=v.retired_at,
                created_at=v.created_at,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/v1/knowledge/articles/{article_id}/versions/{version}/approve", response_model=KnowledgeVersionResponse)
    async def approve_knowledge_version(
        article_id: str,
        version: int,
        db: Session = Depends(get_db),
        caller: AuthenticatedCaller = Depends(require_capability("knowledge:approve")),
    ):
        try:
            v = KnowledgeService.approve_version(
                db=db,
                article_id=article_id,
                version_num=version,
                approved_by=caller.token_name,
            )
            import json
            return KnowledgeVersionResponse(
                id=v.id,
                article_id=v.article_id,
                version=v.version,
                immutable_content=v.immutable_content,
                required_facts=json.loads(v.required_facts) if v.required_facts else [],
                prohibited_claims=json.loads(v.prohibited_claims) if v.prohibited_claims else [],
                content_hash=v.content_hash,
                approval_state=v.approval_state,
                approved_at=v.approved_at,
                approved_by=v.approved_by,
                retired_at=v.retired_at,
                created_at=v.created_at,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/v1/knowledge/articles/{article_id}/versions/{version}/retire", response_model=KnowledgeVersionResponse)
    async def retire_knowledge_version(
        article_id: str,
        version: int,
        db: Session = Depends(get_db),
        caller: AuthenticatedCaller = Depends(require_capability("knowledge:approve")),
    ):
        try:
            v = KnowledgeService.retire_version(
                db=db,
                article_id=article_id,
                version_num=version,
                retired_by=caller.token_name,
            )
            import json
            return KnowledgeVersionResponse(
                id=v.id,
                article_id=v.article_id,
                version=v.version,
                immutable_content=v.immutable_content,
                required_facts=json.loads(v.required_facts) if v.required_facts else [],
                prohibited_claims=json.loads(v.prohibited_claims) if v.prohibited_claims else [],
                content_hash=v.content_hash,
                approval_state=v.approval_state,
                approved_at=v.approved_at,
                approved_by=v.approved_by,
                retired_at=v.retired_at,
                created_at=v.created_at,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/v1/knowledge/search", response_model=KnowledgeSearchResponse)
    async def search_knowledge(
        q: str,
        product: Optional[str] = None,
        issue: Optional[str] = None,
        client: Optional[str] = None,
        limit: int = 5,
        db: Session = Depends(get_db),
        caller: AuthenticatedCaller = Depends(require_capability("knowledge:read")),
    ):
        results = KnowledgeService.search_approved_knowledge(
            db=db,
            query_text=q,
            product_scope=product,
            issue_type=issue,
            client_scope=client,
            limit=limit,
        )
        return KnowledgeSearchResponse(
            results=[
                KnowledgeSearchResultItem(
                    article_id=r.article_id,
                    version_id=r.version_id,
                    version_number=r.version_number,
                    title=r.title,
                    content_excerpt=r.content_excerpt,
                    full_content=r.full_content,
                    product_scope=r.product_scope,
                    issue_type=r.issue_type,
                    client_scope=r.client_scope,
                    required_facts=r.required_facts,
                    prohibited_claims=r.prohibited_claims,
                    retrieval_score=r.retrieval_score,
                    retrieval_rank=r.retrieval_rank,
                )
                for r in results
            ]
        )

    @app.post("/v1/suggestions", response_model=SuggestionOutcomeResponse)
    async def create_suggestion(
        payload: CreateSuggestionRequest,
        db: Session = Depends(get_db),
        caller: AuthenticatedCaller = Depends(require_capability("suggestion:write")),
    ):
        try:
            gw = AIProviderGateway(app.state.ai_provider) if app.state.ai_provider else None
            outcome = await SuggestionService.generate_suggestion(
                db=db,
                captured_event_id=payload.captured_event_id,
                actor=caller.token_name,
                explicit_case_id=payload.case_id,
                correlation_id=payload.correlation_id,
                product_scope=payload.product_scope,
                issue_type=payload.issue_type,
                gateway=gw,
            )

            sugg_model = None
            if outcome.suggestion:
                import json
                s = outcome.suggestion
                sugg_model = SuggestionResponse(
                    id=s.id,
                    captured_event_id=s.captured_event_id,
                    resolved_case_id=s.resolved_case_id,
                    lifecycle_status=s.lifecycle_status,
                    draft=s.draft,
                    missing_facts=json.loads(s.missing_facts) if s.missing_facts else [],
                    assumptions=json.loads(s.assumptions) if s.assumptions else [],
                    confidence=s.confidence,
                    recommended_action=s.recommended_action,
                    sources=[
                        SuggestionSourceItem(
                            article_id=src.article_id,
                            article_version_id=src.article_version_id,
                            version_number=src.version_number,
                            retrieval_rank=src.retrieval_rank,
                            retrieval_score=src.retrieval_score,
                        )
                        for src in outcome.sources
                    ],
                    correlation_id=s.correlation_id,
                    created_at=s.created_at,
                    copied_at=s.copied_at,
                    rejected_at=s.rejected_at,
                    sent_at=s.sent_at,
                )

            return SuggestionOutcomeResponse(
                status=outcome.status,
                suggestion=sugg_model,
                candidates=[
                    SuggestionCandidateItem(
                        id=c.id,
                        case_number=c.case_number,
                        title=c.title,
                        status=c.status,
                        client_identifier=c.client_identifier,
                    )
                    for c in outcome.candidates
                ],
                message=outcome.message,
                correlation_id=outcome.correlation_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/v1/suggestions/{suggestion_id}", response_model=SuggestionResponse)
    async def get_suggestion(
        suggestion_id: str,
        db: Session = Depends(get_db),
        caller: AuthenticatedCaller = Depends(require_capability("suggestion:read")),
    ):
        s = db.query(ResponseSuggestion).filter_by(id=suggestion_id).first()
        if not s:
            raise HTTPException(status_code=404, detail=f"Suggestion '{suggestion_id}' not found.")
        import json
        sources = db.query(SuggestionSource).filter_by(suggestion_id=suggestion_id).all()
        return SuggestionResponse(
            id=s.id,
            captured_event_id=s.captured_event_id,
            resolved_case_id=s.resolved_case_id,
            lifecycle_status=s.lifecycle_status,
            draft=s.draft,
            missing_facts=json.loads(s.missing_facts) if s.missing_facts else [],
            assumptions=json.loads(s.assumptions) if s.assumptions else [],
            confidence=s.confidence,
            recommended_action=s.recommended_action,
            sources=[
                SuggestionSourceItem(
                    article_id=src.article_id,
                    article_version_id=src.article_version_id,
                    version_number=src.version_number,
                    retrieval_rank=src.retrieval_rank,
                    retrieval_score=src.retrieval_score,
                )
                for src in sources
            ],
            correlation_id=s.correlation_id,
            created_at=s.created_at,
            copied_at=s.copied_at,
            rejected_at=s.rejected_at,
            sent_at=s.sent_at,
        )

    @app.post("/v1/suggestions/{suggestion_id}/copy", response_model=SuggestionResponse)
    async def copy_suggestion(
        suggestion_id: str,
        db: Session = Depends(get_db),
        caller: AuthenticatedCaller = Depends(require_capability("suggestion:write")),
    ):
        try:
            s = SuggestionService.copy_suggestion(
                db=db,
                suggestion_id=suggestion_id,
                actor=caller.token_name,
            )
            import json
            sources = db.query(SuggestionSource).filter_by(suggestion_id=suggestion_id).all()
            return SuggestionResponse(
                id=s.id,
                captured_event_id=s.captured_event_id,
                resolved_case_id=s.resolved_case_id,
                lifecycle_status=s.lifecycle_status,
                draft=s.draft,
                missing_facts=json.loads(s.missing_facts) if s.missing_facts else [],
                assumptions=json.loads(s.assumptions) if s.assumptions else [],
                confidence=s.confidence,
                recommended_action=s.recommended_action,
                sources=[
                    SuggestionSourceItem(
                        article_id=src.article_id,
                        article_version_id=src.article_version_id,
                        version_number=src.version_number,
                        retrieval_rank=src.retrieval_rank,
                        retrieval_score=src.retrieval_score,
                    )
                    for src in sources
                ],
                correlation_id=s.correlation_id,
                created_at=s.created_at,
                copied_at=s.copied_at,
                rejected_at=s.rejected_at,
                sent_at=s.sent_at,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/v1/suggestions/{suggestion_id}/reject", response_model=SuggestionResponse)
    async def reject_suggestion(
        suggestion_id: str,
        db: Session = Depends(get_db),
        caller: AuthenticatedCaller = Depends(require_capability("suggestion:write")),
    ):
        try:
            s = SuggestionService.reject_suggestion(
                db=db,
                suggestion_id=suggestion_id,
                actor=caller.token_name,
            )
            import json
            sources = db.query(SuggestionSource).filter_by(suggestion_id=suggestion_id).all()
            return SuggestionResponse(
                id=s.id,
                captured_event_id=s.captured_event_id,
                resolved_case_id=s.resolved_case_id,
                lifecycle_status=s.lifecycle_status,
                draft=s.draft,
                missing_facts=json.loads(s.missing_facts) if s.missing_facts else [],
                assumptions=json.loads(s.assumptions) if s.assumptions else [],
                confidence=s.confidence,
                recommended_action=s.recommended_action,
                sources=[
                    SuggestionSourceItem(
                        article_id=src.article_id,
                        article_version_id=src.article_version_id,
                        version_number=src.version_number,
                        retrieval_rank=src.retrieval_rank,
                        retrieval_score=src.retrieval_score,
                    )
                    for src in sources
                ],
                correlation_id=s.correlation_id,
                created_at=s.created_at,
                copied_at=s.copied_at,
                rejected_at=s.rejected_at,
                sent_at=s.sent_at,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/v1/suggestions/{suggestion_id}/confirm-sent", response_model=SentResponseModel)
    async def confirm_sent_response(
        suggestion_id: str,
        payload: ConfirmSentRequest,
        db: Session = Depends(get_db),
        caller: AuthenticatedCaller = Depends(require_capability("response:confirm_sent")),
    ):
        try:
            sent = SuggestionService.confirm_sent(
                db=db,
                suggestion_id=suggestion_id,
                exact_sent_text=payload.exact_sent_text,
                idempotency_key=payload.idempotency_key,
                actor=caller.token_name,
                correlation_id=payload.correlation_id,
            )
            return SentResponseModel(
                id=sent.id,
                suggestion_id=sent.suggestion_id,
                exact_sent_text=sent.exact_sent_text,
                final_text_hash=sent.final_text_hash,
                confirmed_by_actor=sent.confirmed_by_actor,
                confirmation_idempotency_key=sent.confirmation_idempotency_key,
                confirmed_at=sent.confirmed_at,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/v1/activity/today", response_model=ActivityTodayResponse)
    async def get_activity_today(
        timezone_name: str = "Asia/Kolkata",
        db: Session = Depends(get_db),
        caller: AuthenticatedCaller = Depends(require_capability("activity:read")),
    ):
        import json
        from datetime import datetime
        from .report_service import ReportService, resolve_timezone
        try:
            tz = resolve_timezone(timezone_name)
            local_date = datetime.now(tz).date()
            events = list(reversed(ReportService.events_for_day(db, local_date, timezone_name)))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return ActivityTodayResponse(
            events=[
                ActivityEventItem(
                    id=e.id,
                    event_type=e.event_type,
                    subject_type=e.subject_type,
                    subject_id=e.subject_id,
                    case_id=e.case_id,
                    actor=e.actor,
                    details=json.loads(e.details_json) if e.details_json else {},
                    occurred_at=e.occurred_at,
                )
                for e in events
            ]
        )

    @app.post("/v1/activity/events", response_model=ActivityEventItem)
    async def create_activity_event(
        payload: CreateActivityEventRequest,
        db: Session = Depends(get_db),
        caller: AuthenticatedCaller = Depends(require_capability("activity:write")),
    ):
        import json
        import uuid
        from datetime import datetime, timezone
        event = ActivityEvent(
            id=str(uuid.uuid4()),
            event_type=payload.event_type,
            subject_type=payload.subject_type,
            subject_id=payload.subject_id,
            case_id=payload.case_id,
            correlation_id=payload.correlation_id or str(uuid.uuid4()),
            actor=caller.token_name,
            details_json=json.dumps(payload.details, sort_keys=True, ensure_ascii=False),
            occurred_at=payload.occurred_at or datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        return ActivityEventItem(
            id=event.id,
            event_type=event.event_type,
            subject_type=event.subject_type,
            subject_id=event.subject_id,
            case_id=event.case_id,
            actor=event.actor,
            details=payload.details,
            occurred_at=event.occurred_at,
        )

    def report_response(snapshot) -> ReportSnapshotResponse:
        import json
        return ReportSnapshotResponse(
            id=snapshot.id,
            report_type=snapshot.report_type,
            report_date=snapshot.report_date,
            timezone_name=snapshot.timezone_name,
            lifecycle_status=snapshot.lifecycle_status,
            facts_hash=snapshot.facts_hash,
            content=json.loads(snapshot.content_json),
            created_at=snapshot.created_at,
            finalized_at=snapshot.finalized_at,
        )

    @app.post("/v1/reports/{report_type}/preview", response_model=ReportSnapshotResponse)
    async def preview_report(
        report_type: str,
        payload: ReportPreviewRequest,
        db: Session = Depends(get_db),
        caller: AuthenticatedCaller = Depends(require_capability("report:write")),
    ):
        from datetime import date, datetime
        from .report_service import ReportService, resolve_timezone
        try:
            tz = resolve_timezone(payload.timezone_name)
            report_date = date.fromisoformat(payload.report_date) if payload.report_date else datetime.now(tz).date()
            return report_response(
                ReportService.create_preview(
                    db, report_type, report_date, payload.timezone_name, caller.token_name
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/v1/reports/{report_type}/finalize", response_model=ReportSnapshotResponse)
    async def finalize_report(
        report_type: str,
        payload: ReportFinalizeRequest,
        db: Session = Depends(get_db),
        caller: AuthenticatedCaller = Depends(require_capability("report:write")),
    ):
        from .report_service import ReportService
        try:
            snapshot = ReportService.finalize(
                db,
                payload.preview_id,
                payload.expected_facts_hash,
                caller.token_name,
                report_type,
            )
            return report_response(snapshot)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/v1/integrations/n8n/report-export", response_model=N8nReportExportResponse)
    async def n8n_report_export(
        payload: N8nReportExportRequest,
        request: Request,
        db: Session = Depends(get_db),
    ):
        from .n8n_service import export_finalized_report, verify_request
        secret = settings.n8n_shared_secret
        if secret is None or not secret.get_secret_value().strip():
            raise HTTPException(status_code=503, detail="n8n integration is not configured.")
        timestamp = request.headers.get("X-Copilot-Timestamp", "")
        event_id = request.headers.get("X-Copilot-Event-ID", "")
        capability = request.headers.get("X-Copilot-Capability", "")
        signature = request.headers.get("X-Copilot-Signature", "")
        if not event_id or len(event_id) > 128:
            raise HTTPException(status_code=401, detail="Missing or invalid integration event ID.")
        payload_dict = payload.model_dump(mode="json")
        try:
            verify_request(
                secret.get_secret_value(),
                timestamp,
                event_id,
                capability,
                signature,
                payload_dict,
                settings.n8n_signature_max_age_seconds,
            )
            return N8nReportExportResponse.model_validate(
                export_finalized_report(db, event_id, payload_dict)
            )
        except PermissionError as exc:
            raise HTTPException(status_code=401, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    def meeting_session_response(session) -> MeetingSessionResponse:
        return MeetingSessionResponse(
            id=session.id,
            title=session.title,
            lifecycle_status=session.lifecycle_status,
            consent_acknowledged=bool(session.consent_acknowledged),
            consent_note=session.consent_note,
            retention_until=session.retention_until,
            started_at=session.started_at,
            stopped_at=session.stopped_at,
        )

    def transcript_response(segment) -> TranscriptSegmentResponse:
        return TranscriptSegmentResponse(
            id=segment.id,
            meeting_session_id=segment.meeting_session_id,
            speaker_label=segment.speaker_label,
            transcript_text=segment.transcript_text,
            confidence=segment.confidence,
            uncertainty_visible=segment.confidence < 0.75,
            occurred_at=segment.occurred_at,
        )

    def meeting_proposal_response(proposal) -> MeetingProposalResponse:
        import json
        return MeetingProposalResponse(
            id=proposal.id,
            meeting_session_id=proposal.meeting_session_id,
            proposal_type=proposal.proposal_type,
            proposal_text=proposal.proposal_text,
            evidence_segment_ids=json.loads(proposal.evidence_segment_ids_json),
            lifecycle_status=proposal.lifecycle_status,
            created_at=proposal.created_at,
            reviewed_at=proposal.reviewed_at,
        )

    @app.post("/v1/meetings/start", response_model=MeetingSessionResponse)
    async def start_meeting(
        payload: StartMeetingRequest,
        db: Session = Depends(get_db),
        caller: AuthenticatedCaller = Depends(require_capability("meeting:write")),
    ):
        from .meeting_service import MeetingService
        try:
            return meeting_session_response(
                MeetingService.start_session(
                    db,
                    payload.title,
                    payload.consent_note,
                    payload.transcript_retention_days,
                    caller.token_name,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/v1/meetings/active/current", response_model=Optional[MeetingSessionResponse])
    async def get_active_meeting(
        db: Session = Depends(get_db),
        caller: AuthenticatedCaller = Depends(require_capability("meeting:read")),
    ):
        from .models import MeetingSession
        session = (
            db.query(MeetingSession)
            .filter_by(lifecycle_status="active")
            .order_by(MeetingSession.started_at.desc())
            .first()
        )
        return meeting_session_response(session) if session else None

    @app.post("/v1/meetings/{session_id}/stop", response_model=MeetingSessionResponse)
    async def stop_meeting(
        session_id: str,
        db: Session = Depends(get_db),
        caller: AuthenticatedCaller = Depends(require_capability("meeting:write")),
    ):
        from .meeting_service import MeetingService
        try:
            return meeting_session_response(MeetingService.stop_session(db, session_id, caller.token_name))
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.post("/v1/meetings/{session_id}/transcript-segments", response_model=TranscriptSegmentResponse)
    async def add_meeting_transcript_segment(
        session_id: str,
        payload: TranscriptSegmentRequest,
        db: Session = Depends(get_db),
        caller: AuthenticatedCaller = Depends(require_capability("meeting:write")),
    ):
        from .meeting_service import MeetingService
        try:
            return transcript_response(
                MeetingService.add_segment(
                    db,
                    session_id,
                    payload.speaker_label,
                    payload.transcript_text,
                    payload.confidence,
                    payload.occurred_at,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/v1/meetings/{session_id}/proposals", response_model=MeetingProposalListResponse)
    async def generate_meeting_proposals(
        session_id: str,
        db: Session = Depends(get_db),
        caller: AuthenticatedCaller = Depends(require_capability("meeting:write")),
    ):
        from .meeting_service import MeetingService
        try:
            proposals = MeetingService.generate_proposals(db, session_id)
            return MeetingProposalListResponse(
                proposals=[meeting_proposal_response(proposal) for proposal in proposals]
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/v1/meetings/proposals/{proposal_id}/review", response_model=MeetingProposalResponse)
    async def review_meeting_proposal(
        proposal_id: str,
        payload: ReviewMeetingProposalRequest,
        db: Session = Depends(get_db),
        caller: AuthenticatedCaller = Depends(require_capability("meeting:approve")),
    ):
        from .meeting_service import MeetingService
        try:
            return meeting_proposal_response(
                MeetingService.review_proposal(db, proposal_id, payload.decision, caller.token_name)
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @app.get("/v1/meetings/{session_id}", response_model=MeetingDetailResponse)
    async def get_meeting(
        session_id: str,
        db: Session = Depends(get_db),
        caller: AuthenticatedCaller = Depends(require_capability("meeting:read")),
    ):
        from .models import MeetingProposal, MeetingSession, MeetingTranscriptSegment
        session = db.query(MeetingSession).filter_by(id=session_id).first()
        if session is None:
            raise HTTPException(status_code=404, detail="Meeting session not found.")
        segments = db.query(MeetingTranscriptSegment).filter_by(meeting_session_id=session_id).order_by(MeetingTranscriptSegment.occurred_at).all()
        proposals = db.query(MeetingProposal).filter_by(meeting_session_id=session_id).order_by(MeetingProposal.created_at).all()
        return MeetingDetailResponse(
            session=meeting_session_response(session),
            transcript_segments=[transcript_response(segment) for segment in segments],
            proposals=[meeting_proposal_response(proposal) for proposal in proposals],
        )

    @app.post("/v1/meetings/retention/purge", response_model=RetentionPurgeResponse)
    async def purge_meeting_transcripts(
        db: Session = Depends(get_db),
        caller: AuthenticatedCaller = Depends(require_capability("meeting:write")),
    ):
        from .meeting_service import MeetingService
        return RetentionPurgeResponse(
            purged_transcript_segments=MeetingService.purge_expired_transcripts(db)
        )

    @app.post("/v1/screen/analyze", response_model=ScreenAnalysisResponse)
    async def analyze_screen(
        payload: ScreenAnalysisRequest,
        db: Session = Depends(get_db),
        caller: AuthenticatedCaller = Depends(require_capability("screen:analyze")),
    ):
        import hashlib
        import json
        import uuid
        from datetime import datetime, timezone
        from .models import AuditEvent
        from .screen_provider import SENSITIVE_SCREEN_TEXT, extract_png_base64
        from .ai_provider import InvalidProviderOutputError, ProviderTimeoutError, ProviderUnavailableError

        if SENSITIVE_SCREEN_TEXT.search(payload.ocr_text):
            raise HTTPException(status_code=400, detail="Sensitive screen content is blocked from visual analysis.")
        try:
            encoded_png = extract_png_base64(payload.image_data_url)
        except (ValueError, IndexError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        results = KnowledgeService.search_approved_knowledge(
            db,
            query_text=payload.ocr_text or "screen troubleshooting",
            product_scope=payload.product_scope,
            issue_type=payload.issue_type,
            limit=5,
        )
        if not results:
            return ScreenAnalysisResponse(
                status="knowledge_unavailable",
                uncertainty="No approved knowledge matched the visible screen.",
                message="Visual-model analysis was skipped because no approved evidence was available.",
            )
        provider = app.state.screen_analysis_provider
        if provider is None:
            return ScreenAnalysisResponse(
                status="provider_unavailable",
                uncertainty="The visual provider is disabled.",
                message="Approved sources remain available for manual troubleshooting.",
                sources=[
                    ScreenAnalysisSource(
                        article_id=item.article_id,
                        article_version_id=item.version_id,
                        version_number=item.version_number,
                        title=item.title,
                    )
                    for item in results
                ],
            )
        knowledge_context = "\n\n".join(
            f"[article_id={item.article_id}, version={item.version_number}, title={item.title}]\n{item.full_content}"
            for item in results
        )
        try:
            analysis = await provider.analyze(
                encoded_png,
                payload.ocr_text,
                knowledge_context,
                [item.article_id for item in results],
                settings.ai_timeout_seconds,
            )
        except ProviderTimeoutError:
            return ScreenAnalysisResponse(
                status="provider_timeout",
                uncertainty="Visual analysis timed out.",
                message="No action was taken. Review the approved sources manually.",
                sources=[
                    ScreenAnalysisSource(
                        article_id=item.article_id,
                        article_version_id=item.version_id,
                        version_number=item.version_number,
                        title=item.title,
                    )
                    for item in results
                ],
            )
        except (ProviderUnavailableError, InvalidProviderOutputError):
            return ScreenAnalysisResponse(
                status="provider_unavailable",
                uncertainty="Visual analysis is temporarily unavailable.",
                message="No action was taken. Review the approved sources manually.",
                sources=[
                    ScreenAnalysisSource(
                        article_id=item.article_id,
                        article_version_id=item.version_id,
                        version_number=item.version_number,
                        title=item.title,
                    )
                    for item in results
                ],
            )
        selected = [item for item in results if item.article_id in set(analysis.source_article_ids)]
        if not selected:
            selected = results[:1]
        screenshot_hash = hashlib.sha256(encoded_png.encode("ascii")).hexdigest()
        db.add(
            AuditEvent(
                actor=caller.token_name,
                action="screen.analyzed",
                resource=f"screen_analysis/{uuid.uuid4()}",
                correlation_id=str(uuid.uuid4()),
                timestamp=datetime.now(timezone.utc),
                details_json=json.dumps(
                    {
                        "redacted_screenshot_hash": screenshot_hash,
                        "ocr_confidence": payload.ocr_confidence,
                        "source_count": len(selected),
                    }
                ),
            )
        )
        db.commit()
        return ScreenAnalysisResponse(
            status="analyzed",
            observations=analysis.observations,
            recommended_steps=analysis.recommended_steps,
            uncertainty=analysis.uncertainty,
            sources=[
                ScreenAnalysisSource(
                    article_id=item.article_id,
                    article_version_id=item.version_id,
                    version_number=item.version_number,
                    title=item.title,
                )
                for item in selected
            ],
        )

    def learning_candidate_response(c) -> LearningCandidateResponse:
        return LearningCandidateResponse(
            id=c.id,
            suggestion_id=c.suggestion_id,
            sent_response_id=c.sent_response_id,
            candidate_title=c.candidate_title,
            candidate_content=c.candidate_content,
            product_scope=c.product_scope,
            issue_type=c.issue_type,
            client_scope=c.client_scope,
            target_stable_key=c.target_stable_key,
            target_article_id=c.target_article_id,
            lifecycle_status=c.lifecycle_status,
            created_by=c.created_by,
            created_at=c.created_at,
            reviewed_by=c.reviewed_by,
            reviewed_at=c.reviewed_at,
            resulting_article_version_id=c.resulting_article_version_id,
        )

    @app.post("/v1/suggestions/{suggestion_id}/learning-candidate", response_model=LearningCandidateResponse)
    async def propose_learning_candidate(
        suggestion_id: str,
        payload: ProposeLearningCandidateRequest,
        db: Session = Depends(get_db),
        caller: AuthenticatedCaller = Depends(require_capability("knowledge:write")),
    ):
        from .learning_service import LearningService
        try:
            candidate = LearningService.propose_candidate(
                db=db,
                suggestion_id=suggestion_id,
                candidate_title=payload.candidate_title,
                candidate_content=payload.candidate_content,
                content_reviewed_for_sensitive_data=payload.content_reviewed_for_sensitive_data,
                actor=caller.token_name,
                target_stable_key=payload.target_stable_key,
                target_article_id=payload.target_article_id,
                notes=payload.notes,
            )
            return learning_candidate_response(candidate)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/v1/learning/candidates/{candidate_id}/review", response_model=LearningCandidateResponse)
    async def review_learning_candidate(
        candidate_id: str,
        payload: ReviewLearningCandidateRequest,
        db: Session = Depends(get_db),
        caller: AuthenticatedCaller = Depends(require_capability("knowledge:approve")),
    ):
        from .learning_service import LearningService
        try:
            candidate = LearningService.review_candidate(
                db=db,
                candidate_id=candidate_id,
                decision=payload.decision,
                actor=caller.token_name,
            )
            return learning_candidate_response(candidate)
        except ValueError as exc:
            raise HTTPException(status_code=409 if "already been reviewed" in str(exc) else 400, detail=str(exc))

    @app.get("/v1/learning/candidates", response_model=LearningCandidateListResponse)
    async def list_learning_candidates(
        status_filter: Optional[str] = None,
        db: Session = Depends(get_db),
        caller: AuthenticatedCaller = Depends(require_capability("knowledge:read")),
    ):
        from .learning_service import LearningService
        candidates = LearningService.list_candidates(db=db, status=status_filter)
        return LearningCandidateListResponse(
            candidates=[learning_candidate_response(c) for c in candidates]
        )

    app.dependency_overrides[get_settings] = get_app_settings

    return app

def create_production_app() -> FastAPI:
    """Production runtime factory entry point."""
    settings = get_settings()
    settings.validate_ai_provider_for_startup()
    # Ensure writable data directory
    db_raw = settings.db_path.replace("sqlite:///", "")
    db_dir = os.path.dirname(os.path.abspath(db_raw)) or "."
    if not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
    if not os.access(db_dir, os.W_OK):
        raise RuntimeError(f"Database directory '{db_dir}' is not writable.")

    # The public factory is also used by unit tests with intentionally minimal
    # schemas. Production startup must independently prove the complete current
    # migrated schema, including the FTS5 virtual table.
    production_engine = create_db_engine(settings.db_path)
    from .database import REQUIRED_TABLES_CURRENT
    try:
        verify_schema_readiness(production_engine, required_tables=REQUIRED_TABLES_CURRENT)
    except Exception:
        production_engine.dispose()
        raise

    provider: Optional[AIProvider] = None
    screen_provider: Optional[ScreenAnalysisProvider] = None
    if settings.ai_provider == "gemini":
        provider = GeminiAIProvider(
            api_key=settings.gemini_api_key.get_secret_value(),  # type: ignore[union-attr]
            model=settings.gemini_model,
        )
        screen_provider = GeminiScreenAnalysisProvider(
            api_key=settings.gemini_api_key.get_secret_value(),  # type: ignore[union-attr]
            model=settings.gemini_model,
        )

    try:
        return create_app(
            settings=settings,
            engine=production_engine,
            ai_provider=provider,
            screen_analysis_provider=screen_provider,
            verify_schema=True,
            verify_auth=True,
            dispose_engine_on_shutdown=True,
        )
    except Exception:
        production_engine.dispose()
        raise
