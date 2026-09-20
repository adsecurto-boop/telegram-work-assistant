import os
from typing import Optional, Callable
from fastapi import FastAPI, Depends, Request, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.engine import Engine

from .config import Settings, get_settings, ALLOWED_LOOPBACK_HOSTS
from .logger import logger
from .schemas import CaptureRequest, CaptureResponse
from .database import create_db_engine, create_session_factory, verify_schema_readiness
from .auth import require_capability, AuthenticatedCaller
from .service import CaptureService, IdempotencyConflictError
from .ai_provider import AIProvider, GeminiAIProvider, ProviderTimeoutError, ProviderUnavailableError

def create_app(
    settings: Optional[Settings] = None,
    engine: Optional[Engine] = None,
    session_factory: Optional[sessionmaker] = None,
    ai_provider: Optional[AIProvider] = None,
    verify_schema: bool = True,
    verify_auth: bool = True,
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
    from .schemas import (
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
    )

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

    provider: Optional[AIProvider] = None
    if settings.ai_provider == "gemini":
        provider = GeminiAIProvider(
            api_key=settings.gemini_api_key.get_secret_value(),  # type: ignore[union-attr]
            model=settings.gemini_model,
        )

    return create_app(
        settings=settings,
        ai_provider=provider,
        verify_schema=True,
        verify_auth=True,
    )
