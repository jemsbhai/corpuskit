"""FastAPI application factory and control-plane health endpoints."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Protocol
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from corpuskit import __version__
from corpuskit.adapters.corpusgen import (
    CorpusgenAdapter,
    CorpusgenAnalysisAdapter,
    CorpusgenCapabilityProbe,
    CorpusgenGenerationAdapter,
    CorpusgenInventoryAdapter,
    CorpusgenLabAdapter,
    CorpusgenScoringAdapter,
)
from corpuskit.adapters.corpusgen.datg import CorpusgenDatgAdapter
from corpuskit.adapters.corpusgen.phon_rl import CorpusgenPhonRlAdapter
from corpuskit.api.advanced_capabilities import (
    AdvancedCapabilityCatalog,
    advanced_capabilities,
    advanced_capabilities_router,
)
from corpuskit.api.artifacts import ArtifactApiService, artifact_router
from corpuskit.api.cli_parity import CliPreviewService, cli_parity_router
from corpuskit.api.coverage_weighting_lab import (
    LabHttpService,
    coverage_weighting_lab_router,
)
from corpuskit.api.datg_lab import (
    DatgInspectionHttpService,
    DatgValidationHttpPolicy,
    datg_lab_router,
)
from corpuskit.api.exploration_analysis import (
    AnalysisHttpService,
    InventoryHttpService,
    exploration_analysis_router,
)
from corpuskit.api.generation_scoring import (
    GenerationPreviewHttpService,
    ScoringHttpService,
    generation_scoring_router,
)
from corpuskit.api.jobs import JobApiService, job_router
from corpuskit.api.model_runtime import ModelRuntimeHttpPolicy, model_runtime_router
from corpuskit.api.multilingual_demo import (
    MultilingualDemoRunner,
    multilingual_demo_router,
)
from corpuskit.api.phon_rl_lab import (
    PhonRlLabHttpService,
    PhonRlTrainingHttpPolicy,
    phon_rl_lab_router,
)
from corpuskit.api.platform import PlatformApiService, platform_router
from corpuskit.api.projects import ProjectWorkspaceApi, project_workspace_router
from corpuskit.api.reproducibility import ReproducibilityApiService, reproducibility_router
from corpuskit.api.telemetry import metrics_router
from corpuskit.api.workflows import WorkflowService, workflow_router
from corpuskit.auth import (
    AuthBoundaryError,
    Authenticator,
    AuthRole,
    Principal,
    build_authenticator,
    require_principal,
    require_roles,
)
from corpuskit.auth.models import DEMO_PRINCIPAL
from corpuskit.config import Settings, get_settings
from corpuskit.domain.capabilities import CapabilityReport
from corpuskit.domain.errors import ApplicationError, ApplicationErrorCode
from corpuskit.persistence import Database
from corpuskit.persistence.artifact_store import build_object_store
from corpuskit.persistence.datg_cache import (
    ReadOnlyFilesystemDatgIndexCache,
    UnavailableDatgIndexCache,
    read_only_datg_cache_available,
)
from corpuskit.services import (
    AnalysisService,
    CorpusWorkflowService,
    CoverageWeightingLabService,
    GenerationCoordinator,
    GenerationPreviewService,
    InventoryExplorationService,
    ScoringService,
)
from corpuskit.services.artifacts import ArtifactService
from corpuskit.services.cli_parity import CliParityService
from corpuskit.services.datg_catalog import DatgIndexCatalogService
from corpuskit.services.jobs import JobActor, JobControlPlane
from corpuskit.services.multilingual_demo import MultilingualDemoService
from corpuskit.services.phon_rl import PhonRlLabService
from corpuskit.services.platform import PlatformService
from corpuskit.services.project_workspaces import ProjectWorkspaceService
from corpuskit.services.rate_limits import (
    AuthenticatedRateLimiter,
    DatabaseRateLimiter,
    DisabledRateLimiter,
)
from corpuskit.services.reproducibility import RunManifestService
from corpuskit.services.run_admission import ConfiguredRunAdmission, RunAdmissionPolicy
from corpuskit.telemetry import ApiMetrics, ApiMetricsMiddleware, StructuredAccessLogMiddleware


class CapabilityReporter(Protocol):
    """Narrow interface used by health routes and test doubles."""

    def report(self, *, force: bool = False) -> CapabilityReport: ...


class ServiceVersion(BaseModel):
    """Build and engine versions safe to expose publicly."""

    model_config = ConfigDict(frozen=True)

    service: str = "corpuskit"
    version: str
    corpusgen_contract: str = "0.1.7"


class Liveness(BaseModel):
    """Minimal liveness result that does not touch dependencies."""

    status: str = "ok"


class ReadinessFailure(BaseModel):
    """Stable body returned when required runtime capabilities are missing."""

    status: str = "not_ready"
    report: CapabilityReport


class ErrorResponse(BaseModel):
    """Stable public error envelope without engine or filesystem details."""

    code: ApplicationErrorCode
    message: str
    operation: str
    request_id: str


class AuthenticationErrorResponse(BaseModel):
    """Stable authentication error that never reflects token or provider details."""

    code: str
    message: str
    request_id: str


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach conservative browser security headers to every response."""

    def __init__(self, app: ASGIApp, *, docs_enabled: bool, hsts_enabled: bool) -> None:
        super().__init__(app)
        self._docs_enabled = docs_enabled
        self._hsts_enabled = hsts_enabled

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if self._hsts_enabled:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if self._docs_enabled and request.url.path in {"/docs", "/docs/oauth2-redirect"}:
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; "
                "script-src https://cdn.jsdelivr.net 'unsafe-inline'; "
                "style-src https://cdn.jsdelivr.net 'unsafe-inline'; "
                "img-src data: https://fastapi.tiangolo.com; "
                "connect-src 'self'; frame-ancestors 'none'"
            )
        else:
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; frame-ancestors 'none'"
            )
        return response


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Propagate a caller request ID or generate a non-secret correlation ID."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = _validated_request_id(request.headers.get("X-Request-ID"))
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


def _validated_request_id(candidate: str | None) -> str:
    """Return a bounded safe correlation token, replacing untrusted values with a UUID."""

    if candidate is not None and re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", candidate, flags=re.ASCII
    ):
        return candidate
    return str(uuid4())


class RequestSizeLimitMiddleware:
    """Buffer at most one bounded request body before endpoint deserialization."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in {
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        }:
            await self._app(scope, receive, send)
            return

        content_lengths = [
            value for name, value in scope.get("headers", ()) if name.lower() == b"content-length"
        ]
        transfer_encodings = [
            value
            for name, value in scope.get("headers", ())
            if name.lower() == b"transfer-encoding"
        ]
        if len(content_lengths) > 1 or (content_lengths and transfer_encodings):
            await self._rejection(scope, receive, send)
            return
        if content_lengths:
            try:
                declared_length = int(content_lengths[0])
            except ValueError:
                await self._rejection(scope, receive, send)
                return
            if declared_length < 0 or declared_length > self._max_bytes:
                await self._rejection(scope, receive, send)
                return

        buffered: list[Message] = []
        received_bytes = 0
        while True:
            message = await receive()
            buffered.append(message)
            if message["type"] == "http.disconnect":
                break
            if message["type"] != "http.request":
                continue
            received_bytes += len(message.get("body", b""))
            if received_bytes > self._max_bytes:
                await self._rejection(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        position = 0

        async def replay() -> Message:
            nonlocal position
            if position < len(buffered):
                message = buffered[position]
                position += 1
                return message
            return {"type": "http.disconnect"}

        await self._app(scope, replay, send)

    async def _rejection(self, scope: Scope, receive: Receive, send: Send) -> None:
        request_id = scope.get("state", {}).get("request_id", "unavailable")
        response = JSONResponse(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            content={
                "code": "request_too_large",
                "message": "The request body exceeds the configured limit.",
                "operation": "http.request",
                "request_id": request_id,
            },
        )
        await response(scope, receive, send)


class RequestTargetLimitMiddleware:
    """Reject oversized request paths and queries before routing or parsing."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_path_bytes: int = 2_048,
        max_query_bytes: int = 4_096,
    ) -> None:
        self._app = app
        self._max_path_bytes = max_path_bytes
        self._max_query_bytes = max_query_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        raw_path = scope.get("raw_path") or scope.get("path", "").encode("utf-8")
        query = scope.get("query_string", b"")
        if len(raw_path) <= self._max_path_bytes and len(query) <= self._max_query_bytes:
            await self._app(scope, receive, send)
            return
        request_id = scope.get("state", {}).get("request_id", "unavailable")
        response = JSONResponse(
            status_code=status.HTTP_414_URI_TOO_LONG,
            content={
                "code": "request_uri_too_long",
                "message": "The request target exceeds the configured limit.",
                "operation": "http.request_target",
                "request_id": request_id,
            },
        )
        await response(scope, receive, send)


def _control_router(reporter: CapabilityReporter) -> APIRouter:
    router = APIRouter()

    @router.get("/version", response_model=ServiceVersion)
    async def service_version() -> ServiceVersion:
        return ServiceVersion(version=__version__)

    @router.get("/health/live", response_model=Liveness)
    async def live() -> Liveness:
        return Liveness()

    @router.get(
        "/health/ready",
        response_model=CapabilityReport,
        responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadinessFailure}},
    )
    async def ready() -> CapabilityReport | JSONResponse:
        report = await run_in_threadpool(reporter.report)
        if not report.ready:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content=ReadinessFailure(report=report).model_dump(mode="json"),
            )
        return report

    @router.get("/capabilities", response_model=CapabilityReport)
    async def capabilities() -> CapabilityReport:
        return await run_in_threadpool(reporter.report)

    return router


def _identity_router() -> APIRouter:
    router = APIRouter()

    @router.get("/auth/me", response_model=Principal)
    async def current_principal(
        principal: Annotated[Principal, Depends(require_principal)],
    ) -> Principal:
        return principal

    return router


def create_app(
    settings: Settings | None = None,
    *,
    reporter_factory: Callable[[Settings], CapabilityReporter] = CorpusgenCapabilityProbe,
    workflow_service_factory: Callable[[Settings], WorkflowService] | None = None,
    inventory_service_factory: Callable[[Settings], InventoryHttpService] | None = None,
    analysis_service_factory: Callable[[Settings], AnalysisHttpService] | None = None,
    lab_service_factory: Callable[[Settings], LabHttpService] | None = None,
    job_service_factory: Callable[[Settings], JobApiService] | None = None,
    run_admission_factory: Callable[[Settings], RunAdmissionPolicy] | None = None,
    model_runtime_policy_factory: Callable[[Settings], ModelRuntimeHttpPolicy] | None = None,
    datg_inspection_service_factory: Callable[[Settings], DatgInspectionHttpService] | None = None,
    datg_validation_policy_factory: Callable[[Settings], DatgValidationHttpPolicy] | None = None,
    phon_rl_lab_service_factory: Callable[[Settings], PhonRlLabHttpService] | None = None,
    phon_rl_training_policy_factory: Callable[[Settings], PhonRlTrainingHttpPolicy] | None = None,
    advanced_catalog_factory: Callable[[Settings], AdvancedCapabilityCatalog] = (
        advanced_capabilities
    ),
    workspace_service_factory: Callable[[Settings], ProjectWorkspaceApi] | None = None,
    generation_service_factory: Callable[[Settings], GenerationPreviewHttpService] | None = None,
    scoring_service_factory: Callable[[Settings], ScoringHttpService] | None = None,
    artifact_service_factory: Callable[[Settings], ArtifactApiService] | None = None,
    reproducibility_service_factory: Callable[[Settings], ReproducibilityApiService] | None = None,
    platform_service_factory: Callable[[Settings], PlatformApiService] | None = None,
    cli_preview_service_factory: Callable[[Settings], CliPreviewService] | None = None,
    multilingual_demo_service_factory: Callable[[Settings], MultilingualDemoRunner] | None = None,
    authenticator_factory: Callable[[Settings], Authenticator] = build_authenticator,
    rate_limiter_factory: Callable[[Settings], AuthenticatedRateLimiter] | None = None,
) -> FastAPI:
    """Create an isolated application instance for production or tests."""

    resolved = settings or get_settings()
    configured_admission = ConfiguredRunAdmission.from_settings(resolved)
    run_admission = (
        run_admission_factory(resolved)
        if run_admission_factory is not None
        else configured_admission
    )
    model_runtime_policy = (
        model_runtime_policy_factory(resolved)
        if model_runtime_policy_factory is not None
        else configured_admission.model_runtime
    )
    datg_validation_policy = (
        datg_validation_policy_factory(resolved)
        if datg_validation_policy_factory is not None
        else configured_admission.datg
    )
    datg_inspection_service = (
        datg_inspection_service_factory(resolved)
        if datg_inspection_service_factory is not None
        else None
    )
    phon_rl_lab_service = (
        phon_rl_lab_service_factory(resolved)
        if phon_rl_lab_service_factory is not None
        else PhonRlLabService(CorpusgenPhonRlAdapter())
    )
    phon_rl_training_policy = (
        phon_rl_training_policy_factory(resolved)
        if phon_rl_training_policy_factory is not None
        else configured_admission.phon_rl
    )
    advanced_catalog = advanced_catalog_factory(resolved)
    reporter = reporter_factory(resolved)
    authenticator = authenticator_factory(resolved)
    workflow_service = (
        workflow_service_factory(resolved)
        if workflow_service_factory is not None
        else CorpusWorkflowService(CorpusgenAdapter(), resolved)
    )
    inventory_service = (
        inventory_service_factory(resolved)
        if inventory_service_factory is not None
        else InventoryExplorationService(CorpusgenInventoryAdapter())
    )
    analysis_service = (
        analysis_service_factory(resolved)
        if analysis_service_factory is not None
        else AnalysisService(CorpusgenAnalysisAdapter(), resolved)
    )
    lab_service = (
        lab_service_factory(resolved)
        if lab_service_factory is not None
        else CoverageWeightingLabService(CorpusgenLabAdapter(), reporter, resolved)
    )
    cli_preview_service = (
        cli_preview_service_factory(resolved)
        if cli_preview_service_factory is not None
        else CliParityService()
    )
    multilingual_demo_service = (
        multilingual_demo_service_factory(resolved)
        if multilingual_demo_service_factory is not None
        else MultilingualDemoService(CorpusgenAdapter())
    )
    generation_service = (
        generation_service_factory(resolved)
        if generation_service_factory is not None
        else GenerationPreviewService(
            GenerationCoordinator(
                CorpusgenGenerationAdapter(),
                allowed_huggingface_sources=resolved.worker_huggingface_repository_policies,
            )
        )
    )
    scoring_service = (
        scoring_service_factory(resolved)
        if scoring_service_factory is not None
        else ScoringService(CorpusgenScoringAdapter())
    )
    database = (
        Database(resolved.database_url)
        if job_service_factory is None
        or workspace_service_factory is None
        or artifact_service_factory is None
        or reproducibility_service_factory is None
        or platform_service_factory is None
        or datg_inspection_service_factory is None
        or (resolved.api_rate_limit_enabled and rate_limiter_factory is None)
        else None
    )
    object_store = (
        build_object_store(resolved)
        if artifact_service_factory is None or reproducibility_service_factory is None
        else None
    )
    if job_service_factory is not None:
        job_service = job_service_factory(resolved)
    else:
        assert database is not None
        job_service = JobControlPlane(database, run_admission)
    if datg_inspection_service is None:
        assert database is not None
        datg_root = resolved.worker_datg_index_cache_root
        datg_cache = (
            ReadOnlyFilesystemDatgIndexCache(datg_root)
            if datg_root is not None
            and read_only_datg_cache_available(
                datg_root,
                declared_read_only=resolved.worker_datg_cache_mount_read_only,
            )
            else UnavailableDatgIndexCache()
        )
        datg_inspection_service = DatgIndexCatalogService(
            database,
            datg_cache,
            CorpusgenDatgAdapter(),
        )
    if workspace_service_factory is not None:
        workspace_service = workspace_service_factory(resolved)
    else:
        assert database is not None
        workspace_service = ProjectWorkspaceService(database, resolved)
    if artifact_service_factory is not None:
        artifact_service = artifact_service_factory(resolved)
    else:
        assert database is not None
        assert object_store is not None
        artifact_service = ArtifactService(database, object_store, resolved)
    if reproducibility_service_factory is not None:
        reproducibility_service = reproducibility_service_factory(resolved)
    else:
        assert database is not None
        assert object_store is not None
        reproducibility_service = RunManifestService(
            database,
            object_store,
            resolved,
            admission_policy=run_admission,
        )
    if platform_service_factory is not None:
        platform_service = platform_service_factory(resolved)
    else:
        assert database is not None
        platform_service = PlatformService(database)
    if rate_limiter_factory is not None:
        rate_limiter = rate_limiter_factory(resolved)
    elif resolved.api_rate_limit_enabled:
        assert database is not None
        rate_limiter = DatabaseRateLimiter(
            database,
            window_seconds=resolved.api_rate_limit_window_seconds,
            read_requests=resolved.api_rate_limit_read_requests,
            write_requests=resolved.api_rate_limit_write_requests,
            retention_windows=resolved.api_rate_limit_retention_windows,
        )
    else:
        rate_limiter = DisabledRateLimiter()
    docs_url = "/docs" if resolved.api_docs_enabled else None
    openapi_url = "/openapi.json" if resolved.api_docs_enabled else None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            if isinstance(job_service, JobControlPlane) and resolved.auth_mode == "demo":
                await job_service.bootstrap_demo(
                    JobActor(
                        subject=DEMO_PRINCIPAL.subject,
                        organization_id=DEMO_PRINCIPAL.organization_id,
                    ),
                    environment=resolved.environment,
                )
            yield
        finally:
            if database is not None:
                await database.dispose()

    app = FastAPI(
        title="CorpusKit API",
        summary="Production corpus-design control plane powered by CorpusGen",
        version=__version__,
        docs_url=docs_url,
        redoc_url=None,
        openapi_url=openapi_url,
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.capability_reporter = reporter
    app.state.workflow_service = workflow_service
    app.state.inventory_service = inventory_service
    app.state.analysis_service = analysis_service
    app.state.lab_service = lab_service
    app.state.cli_preview_service = cli_preview_service
    app.state.multilingual_demo_service = multilingual_demo_service
    app.state.generation_service = generation_service
    app.state.scoring_service = scoring_service
    app.state.job_service = job_service
    app.state.run_admission = run_admission
    app.state.model_runtime_policy = model_runtime_policy
    app.state.datg_inspection_service = datg_inspection_service
    app.state.datg_validation_policy = datg_validation_policy
    app.state.phon_rl_lab_service = phon_rl_lab_service
    app.state.phon_rl_training_policy = phon_rl_training_policy
    app.state.advanced_catalog = advanced_catalog
    app.state.workspace_service = workspace_service
    app.state.artifact_service = artifact_service
    app.state.reproducibility_service = reproducibility_service
    app.state.platform_service = platform_service
    app.state.authenticator = authenticator
    app.state.rate_limiter = rate_limiter
    metrics = ApiMetrics() if resolved.metrics_enabled else None
    app.state.metrics = metrics
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.allowed_origins,
        allow_credentials=resolved.auth_mode == "oidc",
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
    )
    app.add_middleware(RequestSizeLimitMiddleware, max_bytes=resolved.max_upload_bytes)
    app.add_middleware(RequestTargetLimitMiddleware)
    app.add_middleware(
        SecurityHeadersMiddleware,
        docs_enabled=resolved.api_docs_enabled,
        hsts_enabled=resolved.environment == "production",
    )
    app.add_middleware(RequestIdMiddleware)
    if metrics is not None:
        app.add_middleware(ApiMetricsMiddleware, metrics=metrics)
    app.add_middleware(StructuredAccessLogMiddleware)
    error_statuses = {
        ApplicationErrorCode.INVALID_REQUEST: status.HTTP_422_UNPROCESSABLE_CONTENT,
        ApplicationErrorCode.LANGUAGE_NOT_SUPPORTED: status.HTTP_422_UNPROCESSABLE_CONTENT,
        ApplicationErrorCode.INVENTORY_NOT_FOUND: status.HTTP_404_NOT_FOUND,
        ApplicationErrorCode.INVENTORY_DATA_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
        ApplicationErrorCode.DEPENDENCY_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
        ApplicationErrorCode.ENGINE_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
        ApplicationErrorCode.ENGINE_CONTRACT_VIOLATION: status.HTTP_502_BAD_GATEWAY,
        ApplicationErrorCode.RESOURCE_NOT_FOUND: status.HTTP_404_NOT_FOUND,
        ApplicationErrorCode.RESOURCE_CONFLICT: status.HTTP_409_CONFLICT,
        ApplicationErrorCode.INVALID_STATE_TRANSITION: status.HTTP_409_CONFLICT,
        ApplicationErrorCode.QUOTA_EXCEEDED: status.HTTP_429_TOO_MANY_REQUESTS,
        ApplicationErrorCode.RATE_LIMITED: status.HTTP_429_TOO_MANY_REQUESTS,
    }

    @app.exception_handler(ApplicationError)
    async def application_error_handler(request: Request, error: ApplicationError) -> JSONResponse:
        body = ErrorResponse(
            code=error.code,
            message=error.public_message,
            operation=error.operation,
            request_id=getattr(request.state, "request_id", "unavailable"),
        )
        headers: dict[str, str] | None = None
        retry_after = getattr(error, "retry_after_seconds", None)
        if (
            isinstance(retry_after, int)
            and not isinstance(retry_after, bool)
            and 1 <= retry_after <= 86_400
        ):
            headers = {"Retry-After": str(retry_after)}
        return JSONResponse(
            status_code=error_statuses[error.code],
            content=body.model_dump(mode="json"),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        details: list[dict[str, object]] = []
        for issue in error.errors():
            details.append(
                {
                    "type": issue.get("type", "validation_error"),
                    "location": issue.get("loc", ()),
                    "message": issue.get("msg", "Invalid value."),
                }
            )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "code": "validation_error",
                "message": "The request payload did not match the API contract.",
                "operation": "http.validation",
                "request_id": getattr(request.state, "request_id", "unavailable"),
                "details": details,
            },
        )

    @app.exception_handler(AuthBoundaryError)
    async def authentication_error_handler(
        request: Request,
        error: AuthBoundaryError,
    ) -> JSONResponse:
        body = AuthenticationErrorResponse(
            code=error.code,
            message=error.public_message,
            request_id=getattr(request.state, "request_id", "unavailable"),
        )
        headers = {"WWW-Authenticate": "Bearer"} if error.status_code == 401 else None
        return JSONResponse(
            status_code=error.status_code,
            content=body.model_dump(mode="json"),
            headers=headers,
        )

    app.include_router(_control_router(reporter), prefix="/api/v1", tags=["control-plane"])
    app.include_router(_identity_router(), prefix="/api/v1", tags=["identity"])
    app.include_router(
        workflow_router(workflow_service),
        prefix="/api/v1",
        tags=["workflows"],
        dependencies=[
            Depends(require_roles(AuthRole.OWNER, AuthRole.ADMIN, AuthRole.EDITOR)),
        ],
    )
    app.include_router(
        exploration_analysis_router(
            inventory_service,
            analysis_service,
            load_dependencies=(
                Depends(require_roles(AuthRole.OWNER, AuthRole.ADMIN, AuthRole.EDITOR)),
            ),
        ),
        prefix="/api/v1",
        tags=["exploration-analysis"],
        dependencies=[
            Depends(
                require_roles(
                    AuthRole.OWNER,
                    AuthRole.ADMIN,
                    AuthRole.EDITOR,
                    AuthRole.VIEWER,
                )
            ),
        ],
    )
    app.include_router(
        coverage_weighting_lab_router(lab_service),
        prefix="/api/v1",
        tags=["coverage-weighting-lab"],
        dependencies=[
            Depends(
                require_roles(
                    AuthRole.OWNER,
                    AuthRole.ADMIN,
                    AuthRole.EDITOR,
                    AuthRole.VIEWER,
                )
            ),
        ],
    )
    app.include_router(
        cli_parity_router(cli_preview_service),
        prefix="/api/v1",
        tags=["cli-parity-lab"],
        dependencies=[
            Depends(
                require_roles(
                    AuthRole.OWNER,
                    AuthRole.ADMIN,
                    AuthRole.EDITOR,
                    AuthRole.VIEWER,
                )
            ),
        ],
    )
    app.include_router(
        multilingual_demo_router(multilingual_demo_service),
        prefix="/api/v1",
        tags=["multilingual-demo"],
        dependencies=[
            Depends(
                require_roles(
                    AuthRole.OWNER,
                    AuthRole.ADMIN,
                    AuthRole.EDITOR,
                    AuthRole.VIEWER,
                )
            ),
        ],
    )
    app.include_router(job_router(job_service), prefix="/api/v1", tags=["jobs"])
    advanced_read_dependencies = (
        Depends(
            require_roles(
                AuthRole.OWNER,
                AuthRole.ADMIN,
                AuthRole.EDITOR,
                AuthRole.VIEWER,
            )
        ),
    )
    advanced_write_dependencies = (
        Depends(require_roles(AuthRole.OWNER, AuthRole.ADMIN, AuthRole.EDITOR)),
    )
    app.include_router(
        advanced_capabilities_router(advanced_catalog),
        prefix="/api/v1",
        tags=["advanced-capabilities"],
        dependencies=advanced_read_dependencies,
    )
    app.include_router(
        model_runtime_router(model_runtime_policy),
        prefix="/api/v1",
        tags=["model-runtime-validation"],
        dependencies=advanced_write_dependencies,
    )
    app.include_router(
        datg_lab_router(
            datg_inspection_service,
            datg_validation_policy,
            inspection_dependencies=advanced_read_dependencies,
            validation_dependencies=advanced_write_dependencies,
        ),
        prefix="/api/v1",
        tags=["datg-lab"],
    )
    app.include_router(
        phon_rl_lab_router(
            phon_rl_lab_service,
            phon_rl_training_policy,
            lab_dependencies=advanced_read_dependencies,
            validation_dependencies=advanced_write_dependencies,
        ),
        prefix="/api/v1",
        tags=["phon-rl-lab"],
    )
    app.include_router(
        platform_router(platform_service),
        prefix="/api/v1",
        tags=["platform"],
    )
    app.include_router(
        generation_scoring_router(generation_service, scoring_service),
        prefix="/api/v1",
        tags=["generation-scoring"],
        dependencies=[
            Depends(require_roles(AuthRole.OWNER, AuthRole.ADMIN, AuthRole.EDITOR)),
        ],
    )
    app.include_router(
        project_workspace_router(
            workspace_service,
            max_upload_bytes=resolved.max_upload_bytes,
        ),
        prefix="/api/v1",
        tags=["project-workspaces"],
    )
    app.include_router(
        artifact_router(
            artifact_service,
            max_upload_bytes=resolved.artifact_max_bytes,
            default_presign_seconds=resolved.artifact_presign_seconds,
        ),
        prefix="/api/v1",
        tags=["artifacts"],
    )
    app.include_router(
        reproducibility_router(reproducibility_service),
        prefix="/api/v1",
        tags=["reproducibility"],
    )
    if metrics is not None:
        app.include_router(metrics_router(metrics, resolved.metrics_bearer_token))
    return app


app = create_app()
