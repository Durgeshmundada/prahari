"""Authenticated FastAPI surface for local replay and live PRAHARI modes."""

from __future__ import annotations

import asyncio
import hmac
import re
import threading
import time
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest

from prahari.actions import ActionAgentError
from prahari.analytics.baseline import PredictionWithheldError
from prahari.browser_auth import install_browser_auth
from prahari.config import project_root
from prahari.contracts import (
    ActionOperation,
    ActionOperationsPage,
    ActionReconciliationRequest,
    ActionResult,
    AppProbeIngestRequest,
    AppProbeRecord,
    ClosureCase,
    ClosureRequest,
    ClosureReviewRequest,
    EventIngestRequest,
    EventRecord,
    IncidentCard,
    IngestionResult,
    MetricIngestRequest,
    MetricRecord,
    ReviewRequest,
    TrafficDemoRunResponse,
    TrafficDemoStatus,
)
from prahari.http_limits import BodyLimitMiddleware
from prahari.operational_metrics import (
    COPILOT_BRIEFS,
    DATABASE_AVAILABLE,
    HTTP_REQUEST_DURATION,
    PREDICTION_READY,
    UNCONFIRMED_ACTIONS,
    metric_method,
)
from prahari.persistence import PersistenceStore
from prahari.security import Actor, effective_actor_id, require_roles
from prahari.services.demo import DemoService, PrahariService
from prahari.settings import Settings
from prahari.traffic_demo.protocol import (
    DEFAULT_MAX_CHUNK_BYTES,
    TrafficDemoError,
    validate_filename,
)
from prahari.traffic_demo.sender import (
    ReceiverArtifactReceipt,
    TrafficDemoResult,
    TrafficProbeConfig,
    prepare_uploaded_payload,
    run_traffic_demo,
)
from prahari.traffic_demo.telemetry import InProcessTelemetryPublisher

HTTP_REQUESTS = Counter(
    "prahari_http_requests_total",
    "HTTP requests handled by PRAHARI",
    ("method", "path", "status"),
)

Viewer = Depends(require_roles("viewer", "operator", "reviewer", "admin"))
Operator = Depends(require_roles("operator", "admin"))
Reviewer = Depends(require_roles("reviewer", "admin"))
Collector = Depends(require_roles("collector", "admin"))

TRAFFIC_DEMO_FILE_NAME_HEADER = "X-Prahari-File-Name"
TRAFFIC_DEMO_INTENT_HEADER = "X-Prahari-Demo-Intent"
TRAFFIC_DEMO_INTENT = "run-traffic-test"
TRAFFIC_DEMO_DOWNLOAD_INTENT = "download-verified-file"
_PUBLIC_FILE_ID = re.compile(r"^file-[0-9a-f]{32}$")


class _DownloadRegistryFullError(RuntimeError):
    pass


class _DownloadNotFoundError(RuntimeError):
    pass


class _DownloadExpiredError(RuntimeError):
    pass


class _ReceiverArtifactFetchError(RuntimeError):
    def __init__(
        self,
        detail: str,
        *,
        status_code: int = status.HTTP_502_BAD_GATEWAY,
        revoke_grant: bool = False,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.revoke_grant = revoke_grant


@dataclass(frozen=True)
class _DownloadGrant:
    file_id: str
    run_id: str
    transfer_id: str
    actor_id: str = field(repr=False)
    receiver_handle: str = field(repr=False)
    file_name: str
    size_bytes: int
    checksum_sha256: str
    expires_at: datetime

    @property
    def download_url(self) -> str:
        return f"/api/demo/traffic/files/{self.file_id}/download"


class _DownloadRegistry:
    """Process-local map from browser-safe IDs to private receiver handles."""

    def __init__(self, *, max_items: int) -> None:
        self._max_items = max_items
        self._grants: dict[str, _DownloadGrant] = {}
        self._lock = threading.RLock()

    def register(
        self,
        *,
        result: TrafficDemoResult,
        artifact: ReceiverArtifactReceipt,
        actor_id: str,
    ) -> _DownloadGrant:
        now = datetime.now(UTC)
        if artifact.expires_at <= now:
            raise _DownloadExpiredError("receiver artifact has already expired")
        with self._lock:
            self._cleanup_locked(now)
            if len(self._grants) >= self._max_items:
                raise _DownloadRegistryFullError("download registry is at its item limit")
            grant = _DownloadGrant(
                file_id=f"file-{uuid4().hex}",
                run_id=result.run_id,
                transfer_id=result.upload.transfer_id,
                actor_id=actor_id,
                receiver_handle=artifact.handle,
                file_name=result.upload.file_name,
                size_bytes=result.upload.bytes_expected,
                checksum_sha256=result.upload.checksum_sha256,
                expires_at=artifact.expires_at,
            )
            self._grants[grant.file_id] = grant
            return grant

    def get(self, file_id: str, *, actor_id: str) -> _DownloadGrant:
        if not _PUBLIC_FILE_ID.fullmatch(file_id):
            raise _DownloadNotFoundError("download is unknown")
        with self._lock:
            grant = self._grants.get(file_id)
            if grant is None:
                self._cleanup_locked(datetime.now(UTC))
                raise _DownloadNotFoundError("download is unknown")
            if grant.expires_at <= datetime.now(UTC):
                self._grants.pop(file_id, None)
                raise _DownloadExpiredError("download has expired")
            if grant.actor_id != actor_id:
                # An opaque ID is not authorization. Return the same result as
                # an unknown ID so callers cannot enumerate another actor's grant.
                raise _DownloadNotFoundError("download is unknown")
            return grant

    def delete(self, file_id: str) -> None:
        with self._lock:
            self._grants.pop(file_id, None)

    def _cleanup_locked(self, now: datetime) -> None:
        expired = [file_id for file_id, grant in self._grants.items() if grant.expires_at <= now]
        for file_id in expired:
            self._grants.pop(file_id, None)


def create_app(
    service: PrahariService | None = None,
    *,
    settings: Settings | None = None,
    store: PersistenceStore | None = None,
) -> FastAPI:
    """Create an app that fails closed for unsafe production configuration."""

    runtime_settings = settings or Settings()
    owns_store = service is None and store is None
    copilot = None
    action_agent = None
    runtime_store = store
    if runtime_settings.environment == "production" and service is not None:
        raise RuntimeError("production services must be created by the runtime factory")
    if service is None:
        runtime_store = runtime_store or PersistenceStore(runtime_settings.database_url)
        if runtime_settings.auto_create_schema:
            runtime_store.create_schema()
        elif runtime_settings.environment == "production" and not runtime_store.schema_is_current():
            raise RuntimeError("database migrations must be applied before production startup")
        from prahari.network_profile import load_network_profile

        if (
            runtime_settings.environment == "production"
            and runtime_settings.network_profile_path is None
        ):
            raise RuntimeError("production requires PRAHARI_NETWORK_PROFILE_PATH")
        network_profile = (
            load_network_profile(runtime_settings.network_profile_path)
            if runtime_settings.network_profile_path is not None
            else None
        )
        if (
            runtime_settings.environment == "production"
            and network_profile is not None
            and network_profile.deployment_scope != "production"
        ):
            raise RuntimeError("production requires a production-scoped network profile")
        copilot = None
        if runtime_settings.copilot_enabled:
            from prahari.copilot.ollama import OllamaClient
            from prahari.copilot.service import IncidentCopilot

            if network_profile is not None and (
                runtime_settings.knowledge_root is None or not network_profile.approved_runbooks
            ):
                raise RuntimeError(
                    "external copilot requires approved runbooks and a knowledge root"
                )

            copilot = IncidentCopilot(
                runtime_settings.knowledge_root if network_profile else project_root(),
                OllamaClient(
                    runtime_settings.ollama_url,
                    runtime_settings.ollama_model,
                    timeout_seconds=runtime_settings.ollama_timeout_seconds,
                    allowed_private_ip=runtime_settings.ollama_allowed_private_ip,
                ),
                allowed_playbooks={
                    network_profile.playbook.id if network_profile else PrahariService.playbook_id
                },
                case_provider=lambda: runtime_store.list_closure_cases(
                    status="approved", limit=100
                ),
                document_checksums=(
                    {book.path: book.sha256 for book in network_profile.approved_runbooks}
                    if network_profile
                    else None
                ),
                playbook_sources=(
                    {network_profile.playbook.id: network_profile.playbook.source}
                    if network_profile
                    else None
                ),
            )
        action_agent = None
        if runtime_settings.action_adapter_enabled:
            from prahari.actions import LabActionAgentClient

            assert runtime_settings.action_agent_token_file is not None
            action_agent = LabActionAgentClient(
                runtime_settings.action_agent_url,
                runtime_settings.action_agent_token_file,
                allowed_private_ip=runtime_settings.action_agent_allowed_private_ip,
            )
        service = PrahariService(
            data_mode=runtime_settings.data_mode,
            network_profile=network_profile,
            store=runtime_store,
            model_artifact_path=runtime_settings.model_artifact_path,
            model_manifest_path=runtime_settings.model_manifest_path,
            predictive_model_manifest_path=runtime_settings.predictive_model_manifest_path,
            copilot=copilot,
            action_agent=action_agent,
        )
        if runtime_settings.environment == "production" and (
            service._horizon_classifier is None or not service._horizon_classifier.activated
        ):
            raise RuntimeError("production requires an activated predictive horizon model")
    if runtime_settings.environment == "production" and (
        service.fixture_only
        or service.model_info()["training_status"] != "registered and evaluated"
    ):
        raise RuntimeError("production requires a registered, evaluated prediction model")

    traffic_demo_can_run = (
        runtime_settings.environment == "development" and runtime_settings.traffic_demo_run_enabled
    )
    if traffic_demo_can_run:
        # Fail startup before advertising the control if its server-owned
        # receiver destination or token policy is invalid.
        _traffic_probe_config(runtime_settings, run_id="traffic-demo-startup-check")

    @asynccontextmanager
    async def lifespan(_app):
        with ExitStack() as resources:
            if owns_store and runtime_store is not None:
                resources.callback(runtime_store.dispose)
            if copilot is not None:
                resources.callback(copilot.close)
            if action_agent is not None:
                resources.callback(action_agent.close)
            yield

    app = FastAPI(
        lifespan=lifespan,
        title="PRAHARI API",
        version="0.2.0",
        description=(
            "Local-first predictive NOC workflow. Replay mode is fixture-only; no endpoint can "
            "execute a network change without a reviewed allow-listed adapter."
        ),
    )
    app.state.service = service
    app.state.settings = runtime_settings
    app.state.store = runtime_store
    install_browser_auth(app, runtime_settings, runtime_store)
    app.state.traffic_demo_run_lock = asyncio.Lock() if traffic_demo_can_run else None
    app.state.traffic_demo_download_registry = (
        _DownloadRegistry(max_items=runtime_settings.traffic_demo_download_registry_max_items)
        if traffic_demo_can_run
        else None
    )
    app.state.traffic_demo_download_slots = asyncio.Semaphore(2) if traffic_demo_can_run else None
    app.add_middleware(
        BodyLimitMiddleware,
        max_bytes=runtime_settings.http_max_body_bytes,
        timeout_seconds=runtime_settings.http_body_timeout_seconds,
        upload_max_bytes=(
            runtime_settings.traffic_demo_max_file_bytes + 65536
            if runtime_settings.traffic_demo_run_enabled
            else None
        ),
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=runtime_settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-Actor-Id",
            "X-Actor-Role",
            TRAFFIC_DEMO_FILE_NAME_HEADER,
            TRAFFIC_DEMO_INTENT_HEADER,
            "X-Request-Id",
        ],
    )

    @app.middleware("http")
    async def add_request_id_and_metrics(request: Request, call_next):
        candidate = request.headers.get("X-Request-Id", "")
        request_id = (
            candidate if re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", candidate) else str(uuid4())
        )
        started = time.perf_counter()
        method = metric_method(request.method)
        try:
            response = await call_next(request)
        except Exception:
            HTTP_REQUESTS.labels(method, _metric_path_label(request), "500").inc()
            HTTP_REQUEST_DURATION.labels(method, _metric_path_label(request), "500").observe(
                time.perf_counter() - started
            )
            raise
        response.headers["X-Request-Id"] = request_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        HTTP_REQUESTS.labels(
            method,
            _metric_path_label(request),
            str(response.status_code),
        ).inc()
        HTTP_REQUEST_DURATION.labels(
            method, _metric_path_label(request), str(response.status_code)
        ).observe(time.perf_counter() - started)
        return response

    def current_service() -> PrahariService:
        return app.state.service

    @app.get("/health")
    def health() -> dict[str, str | bool]:
        is_ready, reason = current_service().readiness()
        return {
            "status": "ok" if is_ready else "degraded",
            "fixture_only": current_service().fixture_only,
            "environment": runtime_settings.environment,
            "readiness": reason,
        }

    @app.get("/readyz")
    def ready() -> dict[str, str | bool]:
        if runtime_store is not None:
            try:
                runtime_store.healthcheck()
            except Exception as error:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="database is not ready",
                ) from error
        is_ready, reason = current_service().readiness()
        if not is_ready:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"live telemetry is not ready: {reason}",
            )
        return {"status": "ready", "fixture_only": current_service().fixture_only}

    @app.get("/metrics", include_in_schema=False)
    def prometheus_metrics(_: Actor = Viewer) -> Response:
        service = current_service()
        try:
            available, unconfirmed = service.operational_storage_health()
        except Exception:
            available, unconfirmed = False, float("nan")
        DATABASE_AVAILABLE.set(int(available))
        UNCONFIRMED_ACTIONS.set(unconfirmed)
        try:
            PREDICTION_READY.set(int(service.readiness()[0]))
        except Exception:
            PREDICTION_READY.set(0)
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/api/topology")
    def topology(_: Actor = Viewer) -> dict[str, object]:
        return current_service().topology()

    @app.get("/api/metrics", response_model=list[MetricRecord])
    def metrics(
        entity_id: str | None = Query(default=None),
        metric: str | None = Query(default=None),
        limit: int = Query(default=1_000, ge=1, le=5_000),
        _: Actor = Viewer,
    ) -> list[MetricRecord]:
        return current_service().metric_records(entity_id=entity_id, metric=metric, limit=limit)

    @app.get("/api/events", response_model=list[EventRecord])
    def events(
        entity_id: str | None = Query(default=None),
        before: datetime | None = Query(default=None),
        limit: int = Query(default=1_000, ge=1, le=5_000),
        _: Actor = Viewer,
    ) -> list[EventRecord]:
        return current_service().event_records(
            entity_id=entity_id,
            before=_parse_before(before),
            limit=limit,
        )

    @app.get("/api/app-probes", response_model=list[AppProbeRecord])
    def app_probes(
        entity_id: str | None = Query(default=None),
        before: datetime | None = Query(default=None),
        limit: int = Query(default=1_000, ge=1, le=5_000),
        _: Actor = Viewer,
    ) -> list[AppProbeRecord]:
        return current_service().app_probe_records(
            entity_id=entity_id,
            before=_parse_before(before),
            limit=limit,
        )

    @app.get("/api/demo/traffic", response_model=TrafficDemoStatus)
    def traffic_demo(
        path_id: str | None = Query(default=None, min_length=1, max_length=255),
        limit: int = Query(default=50, ge=1, le=100),
        _: Actor = Viewer,
    ) -> TrafficDemoStatus:
        """Show only safe metadata for the newest two-server transfer demo."""

        try:
            summary = current_service().traffic_demo_status(path_id=path_id, limit=limit)
            return _with_traffic_demo_control(
                summary,
                can_run=traffic_demo_can_run,
                max_file_bytes=runtime_settings.traffic_demo_max_file_bytes,
            )
        except KeyError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(error),
            ) from error

    if traffic_demo_can_run:

        @app.post("/api/demo/traffic/run", response_model=TrafficDemoRunResponse)
        async def run_traffic_demo_from_dashboard(
            request: Request,
            actor: Actor = Operator,
        ) -> TrafficDemoRunResponse:
            """Run one bounded, server-directed demo and persist only its evidence."""

            _require_traffic_demo_request_headers(request, runtime_settings)
            file_name = request.headers.get(TRAFFIC_DEMO_FILE_NAME_HEADER)
            try:
                validated_file_name = validate_filename(file_name or "")
            except TrafficDemoError as error:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=str(error),
                ) from error
            _validate_content_length(
                request,
                max_file_bytes=runtime_settings.traffic_demo_max_file_bytes,
            )

            run_lock: asyncio.Lock = app.state.traffic_demo_run_lock
            try:
                await asyncio.wait_for(run_lock.acquire(), timeout=0.01)
            except TimeoutError as error:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="another dashboard traffic test is already running",
                ) from error

            try:
                try:
                    async with asyncio.timeout(runtime_settings.traffic_demo_run_timeout_seconds):
                        data = await _read_bounded_body(
                            request,
                            max_file_bytes=runtime_settings.traffic_demo_max_file_bytes,
                        )
                        prepared = prepare_uploaded_payload(
                            validated_file_name,
                            data,
                            max_file_bytes=runtime_settings.traffic_demo_max_file_bytes,
                        )
                        config = _traffic_probe_config(runtime_settings)
                        result = await run_traffic_demo(
                            config,
                            publisher=InProcessTelemetryPublisher(current_service()),
                            prepared_payload=prepared,
                        )
                except TimeoutError as error:
                    raise HTTPException(
                        status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                        detail="the traffic demo exceeded its whole-run timeout",
                    ) from error
                except TrafficDemoError as error:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                        detail=str(error),
                    ) from error

                evidence_saved = _traffic_evidence_saved(result)
                if evidence_saved:
                    traffic = current_service().traffic_demo_status(
                        path_id=config.path_id,
                        transfer_id=result.upload.transfer_id,
                    )
                    if traffic.status == "idle":
                        evidence_saved = False
                if not evidence_saved:
                    traffic = TrafficDemoStatus(
                        status="unavailable",
                        path_id=config.path_id,
                        message=(
                            "The traffic test finished, but its exact evidence bundle "
                            "could not be saved."
                        ),
                    )
                grant = None
                if _receiver_artifact_is_downloadable(
                    result,
                    traffic=traffic,
                    settings=runtime_settings,
                ):
                    try:
                        registry: _DownloadRegistry = app.state.traffic_demo_download_registry
                        assert result.receiver_artifact is not None
                        grant = registry.register(
                            result=result,
                            artifact=result.receiver_artifact,
                            actor_id=actor.actor_id,
                        )
                    except (_DownloadExpiredError, _DownloadRegistryFullError):
                        grant = None
                if result.receiver_artifact is not None and grant is None:
                    await _delete_receiver_artifact_best_effort(
                        runtime_settings,
                        result.receiver_artifact.handle,
                    )
                return TrafficDemoRunResponse(
                    run_id=result.run_id,
                    transfer_id=result.upload.transfer_id,
                    evidence_saved=evidence_saved,
                    traffic=_with_traffic_demo_control(
                        traffic,
                        can_run=True,
                        max_file_bytes=runtime_settings.traffic_demo_max_file_bytes,
                    ),
                    downloadable=grant is not None,
                    download_url=grant.download_url if grant is not None else None,
                    download_expires_at=grant.expires_at if grant is not None else None,
                )
            finally:
                run_lock.release()

        @app.post("/api/demo/traffic/files/{file_id}/download")
        async def download_verified_traffic_demo_file(
            file_id: str,
            request: Request,
            actor: Actor = Operator,
        ) -> Response:
            """Redeem an actor-bound grant for the receiver's verified byte copy."""

            _require_traffic_demo_download_headers(request, runtime_settings)
            await _require_empty_body(request)
            registry: _DownloadRegistry = app.state.traffic_demo_download_registry
            try:
                grant = registry.get(file_id, actor_id=actor.actor_id)
            except _DownloadExpiredError as error:
                raise HTTPException(
                    status_code=status.HTTP_410_GONE,
                    detail="the verified demo file has expired",
                ) from error
            except _DownloadNotFoundError as error:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="the verified demo file is unavailable",
                ) from error

            download_slots: asyncio.Semaphore = app.state.traffic_demo_download_slots
            acquired_slot = False
            try:
                try:
                    await asyncio.wait_for(download_slots.acquire(), timeout=0.01)
                    acquired_slot = True
                except TimeoutError as error:
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail="verified demo downloads are busy",
                        headers={"Retry-After": "1"},
                    ) from error
                try:
                    data = await _fetch_receiver_artifact(runtime_settings, grant)
                except _ReceiverArtifactFetchError as error:
                    if error.revoke_grant:
                        registry.delete(file_id)
                    raise HTTPException(
                        status_code=error.status_code,
                        detail=str(error),
                    ) from error
                return Response(
                    content=data,
                    media_type="application/octet-stream",
                    headers={
                        "Cache-Control": "no-store",
                        "Content-Disposition": _download_content_disposition(grant.file_name),
                        "X-Content-Type-Options": "nosniff",
                        "X-Prahari-Run-Id": grant.run_id,
                        "X-Prahari-Transfer-Id": grant.transfer_id,
                    },
                )
            finally:
                if acquired_slot:
                    download_slots.release()

    @app.post(
        "/api/telemetry/metrics",
        response_model=IngestionResult,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def ingest_metrics(request_body: MetricIngestRequest, _: Actor = Collector) -> IngestionResult:
        accepted = _ingest_or_422(
            lambda: current_service().ingest_metrics(
                request_body.records,
                run_id=request_body.run_id,
            )
        )
        return IngestionResult(accepted=accepted, record_type="metric", run_id=request_body.run_id)

    @app.post(
        "/api/telemetry/app-probes",
        response_model=IngestionResult,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def ingest_app_probes(
        request_body: AppProbeIngestRequest, _: Actor = Collector
    ) -> IngestionResult:
        accepted = _ingest_or_422(
            lambda: current_service().ingest_app_probes(
                request_body.records,
                run_id=request_body.run_id,
            )
        )
        return IngestionResult(
            accepted=accepted,
            record_type="app_probe",
            run_id=request_body.run_id,
        )

    @app.post(
        "/api/telemetry/events",
        response_model=IngestionResult,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def ingest_events(request_body: EventIngestRequest, _: Actor = Collector) -> IngestionResult:
        accepted = _ingest_or_422(
            lambda: current_service().ingest_events(
                request_body.records,
                run_id=request_body.run_id,
            )
        )
        return IngestionResult(accepted=accepted, record_type="event", run_id=request_body.run_id)

    @app.get("/api/incidents", response_model=list[IncidentCard])
    def incidents(_: Actor = Viewer) -> list[IncidentCard]:
        try:
            return current_service().list_incidents()
        except (PredictionWithheldError, ValueError) as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(error),
            ) from error

    @app.get("/api/incidents/{incident_id}")
    def incident_detail(incident_id: str, _: Actor = Viewer) -> dict[str, object]:
        try:
            return current_service().incident_detail(incident_id)
        except KeyError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except (PredictionWithheldError, ValueError) as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(error),
            ) from error

    @app.post("/api/incidents/{incident_id}/brief")
    def incident_brief(
        incident_id: str,
        prediction_id: str | None = Query(default=None, max_length=100),
        actor: Actor = Operator,
    ) -> dict[str, object]:
        from prahari.copilot.service import CopilotUnavailableError

        try:
            result = (
                current_service()
                .incident_brief(incident_id, actor.actor_id, prediction_id=prediction_id)
                .model_dump(mode="json")
            )
            COPILOT_BRIEFS.labels("verified").inc()
            return result
        except KeyError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except (CopilotUnavailableError, RuntimeError) as error:
            COPILOT_BRIEFS.labels("unavailable").inc()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(error),
            ) from error
        except PredictionWithheldError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(error),
            ) from error
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error

    def require_evidence_precondition(prediction_id: str | None) -> None:
        if runtime_settings.environment == "production" and prediction_id is None:
            raise HTTPException(428, "prediction_id is required; refresh the incident evidence")

    @app.post("/api/incidents/{incident_id}/review", response_model=IncidentCard)
    def review_incident(
        incident_id: str,
        request_body: ReviewRequest,
        actor: Actor = Operator,
    ) -> IncidentCard:
        actor_id = effective_actor_id(runtime_settings, actor, request_body.actor_id)
        require_evidence_precondition(request_body.prediction_id)
        try:
            return current_service().review(
                incident_id, actor_id, request_body.note, prediction_id=request_body.prediction_id
            )
        except KeyError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except PredictionWithheldError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(error),
            ) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.post(
        "/api/incidents/{incident_id}/actions/{playbook_id}/dry-run",
        response_model=ActionResult,
    )
    def dry_run_action(
        incident_id: str,
        playbook_id: str,
        request_body: ReviewRequest,
        actor: Actor = Operator,
    ) -> ActionResult:
        actor_id = effective_actor_id(runtime_settings, actor, request_body.actor_id)
        require_evidence_precondition(request_body.prediction_id)
        try:
            return current_service().dry_run_action(
                incident_id, playbook_id, actor_id, prediction_id=request_body.prediction_id
            )
        except KeyError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except (PredictionWithheldError, ActionAgentError) as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(error),
            ) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.post(
        "/api/incidents/{incident_id}/actions/{playbook_id}/approve",
        response_model=ActionResult,
    )
    def approve_action(
        incident_id: str,
        playbook_id: str,
        request_body: ReviewRequest,
        actor: Actor = Operator,
    ) -> Response | ActionResult:
        actor_id = effective_actor_id(runtime_settings, actor, request_body.actor_id)
        require_evidence_precondition(request_body.prediction_id)
        try:
            result = current_service().approve_action(
                incident_id,
                playbook_id,
                actor_id,
                request_body.note,
                prediction_id=request_body.prediction_id,
            )
        except KeyError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except (PredictionWithheldError, ActionAgentError) as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(error),
            ) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        if result.status in {"blocked", "validation_failed"}:
            return Response(
                content=result.model_dump_json(),
                media_type="application/json",
                status_code=status.HTTP_409_CONFLICT,
            )
        return result

    @app.post(
        "/api/incidents/{incident_id}/actions/{playbook_id}/rollback",
        response_model=ActionResult,
    )
    def rollback_action(
        incident_id: str,
        playbook_id: str,
        request_body: ReviewRequest,
        actor: Actor = Operator,
    ) -> Response | ActionResult:
        actor_id = effective_actor_id(runtime_settings, actor, request_body.actor_id)
        try:
            result = current_service().rollback_action(
                incident_id,
                playbook_id,
                actor_id,
                request_body.note,
            )
        except KeyError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except (PredictionWithheldError, ActionAgentError) as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(error),
            ) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        if result.status in {"blocked", "validation_failed"}:
            return Response(
                content=result.model_dump_json(),
                media_type="application/json",
                status_code=status.HTTP_409_CONFLICT,
            )
        return result

    @app.get("/api/action-operations", response_model=ActionOperationsPage)
    def action_operations(_: Actor = Viewer) -> ActionOperationsPage:
        # Action outcomes remain readable even when incident telemetry is unavailable.
        return ActionOperationsPage(
            operations=current_service().list_action_operations(),
            dispatch_enabled=current_service().device_dispatch_enabled,
        )

    @app.post("/api/action-operations/{operation_id}/reconcile", response_model=ActionOperation)
    def reconcile_action_operation(
        operation_id: str,
        request_body: ActionReconciliationRequest,
        actor: Actor = Reviewer,
    ) -> ActionOperation:
        actor_id = effective_actor_id(runtime_settings, actor, request_body.actor_id)
        try:
            return current_service().reconcile_action(
                operation_id,
                request_body.model_copy(update={"actor_id": actor_id}),
            )
        except KeyError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/incidents/{incident_id}/closure", response_model=IncidentCard)
    def close_incident(
        incident_id: str,
        request_body: ClosureRequest,
        actor: Actor = Operator,
    ) -> IncidentCard:
        actor_id = effective_actor_id(runtime_settings, actor, request_body.actor_id)
        require_evidence_precondition(request_body.prediction_id)
        request_with_actor = request_body.model_copy(update={"actor_id": actor_id})
        try:
            return current_service().close(incident_id, request_with_actor)
        except KeyError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except PredictionWithheldError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(error),
            ) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.post("/api/cases/{incident_id}/review", response_model=IncidentCard)
    def review_closure_case(
        incident_id: str,
        request_body: ClosureReviewRequest,
        actor: Actor = Reviewer,
    ) -> IncidentCard:
        actor_id = effective_actor_id(runtime_settings, actor, request_body.actor_id)
        request_with_actor = request_body.model_copy(update={"actor_id": actor_id})
        try:
            return current_service().review_closure(incident_id, request_with_actor)
        except KeyError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except (PredictionWithheldError, ValueError) as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(error),
            ) from error

    @app.get("/api/models")
    def models(_: Actor = Viewer) -> dict[str, object]:
        return current_service().model_info()

    @app.get("/api/cases", response_model=list[ClosureCase])
    def closure_cases(
        status_filter: Literal["pending", "approved", "rejected"] | None = Query(
            default=None, alias="status"
        ),
        outcome: Literal[
            "resolved", "partially_resolved", "failed", "false_positive", "uncertain"
        ]
        | None = None,
        incident_type: Literal["normal", "tunnel_degradation"] | None = None,
        entity_id: str | None = Query(
            default=None,
            min_length=1,
            max_length=200,
            pattern=r"^[A-Za-z0-9_.:@/+:-]+$",
        ),
        network_profile_sha256: str | None = Query(
            default=None, pattern=r"^[a-f0-9]{64}$"
        ),
        limit: int = Query(default=100, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        _: Actor = Viewer,
    ) -> list[ClosureCase]:
        return current_service().list_closure_cases(
            status=status_filter,
            outcome=outcome,
            incident_type=incident_type,
            entity_id=entity_id,
            network_profile_sha256=network_profile_sha256,
            limit=limit,
            offset=offset,
        )

    @app.get("/api/cases/{case_id}", response_model=ClosureCase)
    def closure_case(case_id: str, _: Actor = Viewer) -> ClosureCase:
        try:
            return current_service().closure_case(case_id)
        except KeyError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    @app.post("/api/cases/{case_id}/decision", response_model=ClosureCase)
    def decide_closure_case(
        case_id: str, request_body: ClosureReviewRequest, actor: Actor = Reviewer
    ) -> ClosureCase:
        actor_id = effective_actor_id(runtime_settings, actor, request_body.actor_id)
        try:
            return current_service().decide_closure_case(
                case_id, request_body.model_copy(update={"actor_id": actor_id})
            )
        except KeyError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error

    @app.get("/api/audit/{incident_id}")
    def audit_trail(
        incident_id: str,
        limit: int = Query(default=200, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
        _: Actor = Reviewer,
    ) -> list[dict[str, object]]:
        try:
            events = current_service().audit_trail(incident_id, limit=limit, offset=offset)
        except KeyError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        return [event.model_dump(mode="json") for event in events]

    return app


def app_for_root(root: Path) -> FastAPI:
    """Test helper bound to an isolated in-memory database and replay fixture."""

    settings = Settings(
        environment="test",
        data_mode="replay",
        database_url="sqlite://",
        auth_mode="disabled",
    )
    store = PersistenceStore(settings.database_url)
    store.create_schema()
    return create_app(
        DemoService(root=root, store=store),
        settings=settings,
        store=store,
    )


def _ingest_or_422(operation) -> int:
    try:
        return operation()
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(error),
        ) from error


def _metric_path_label(request: Request) -> str:
    """Use route templates so untrusted path IDs cannot grow metric cardinality."""

    route = request.scope.get("route")
    path_format = getattr(route, "path_format", None)
    return str(path_format) if isinstance(path_format, str) else "unmatched"


def _parse_before(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="before must be an ISO-8601 timestamp with UTC offset",
        )
    return value.astimezone(UTC)


def _with_traffic_demo_control(
    summary: TrafficDemoStatus,
    *,
    can_run: bool,
    max_file_bytes: int,
) -> TrafficDemoStatus:
    return summary.model_copy(update={"can_run": can_run, "max_file_bytes": max_file_bytes})


def _require_traffic_demo_request_headers(request: Request, settings: Settings) -> None:
    intent = request.headers.get(TRAFFIC_DEMO_INTENT_HEADER)
    if intent != TRAFFIC_DEMO_INTENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"{TRAFFIC_DEMO_INTENT_HEADER} must explicitly request a traffic test",
        )
    content_type = request.headers.get("Content-Type", "").partition(";")[0].strip().lower()
    if content_type != "application/octet-stream":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="traffic-demo uploads require application/octet-stream",
        )
    content_encoding = request.headers.get("Content-Encoding")
    if content_encoding is not None and content_encoding.strip().lower() != "identity":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="encoded traffic-demo request bodies are not accepted",
        )
    if settings.auth_mode == "disabled":
        origin = request.headers.get("Origin")
        if origin is None or origin not in settings.cors_origin_list:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="an exact configured dashboard Origin is required",
            )


def _require_traffic_demo_download_headers(request: Request, settings: Settings) -> None:
    intent = request.headers.get(TRAFFIC_DEMO_INTENT_HEADER)
    if intent != TRAFFIC_DEMO_DOWNLOAD_INTENT:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"{TRAFFIC_DEMO_INTENT_HEADER} must explicitly request a verified download",
        )
    if request.headers.get("Range") is not None:
        raise HTTPException(
            status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
            detail="verified demo downloads do not support ranges",
        )
    origin = request.headers.get("Origin")
    if origin is not None and origin not in settings.cors_origin_list:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="the dashboard Origin is not configured",
        )
    if settings.auth_mode == "disabled" and origin is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="an exact configured dashboard Origin is required",
        )


async def _require_empty_body(request: Request) -> None:
    raw_length = request.headers.get("Content-Length")
    if raw_length not in {None, "0"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="verified demo download requests must have an empty body",
        )
    async for chunk in request.stream():
        if chunk:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="verified demo download requests must have an empty body",
            )


def _validate_content_length(request: Request, *, max_file_bytes: int) -> None:
    raw_length = request.headers.get("Content-Length")
    if raw_length is None:
        return
    try:
        content_length = int(raw_length)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Content-Length must be a non-negative integer",
        ) from error
    if content_length < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Content-Length must be a non-negative integer",
        )
    if content_length > max_file_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"traffic-demo files cannot exceed {max_file_bytes} bytes",
        )


async def _read_bounded_body(request: Request, *, max_file_bytes: int) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_file_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"traffic-demo files cannot exceed {max_file_bytes} bytes",
            )
        body.extend(chunk)
    if not body:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="traffic-demo file must contain at least one byte",
        )
    return bytes(body)


def _traffic_probe_config(
    settings: Settings,
    *,
    run_id: str | None = None,
) -> TrafficProbeConfig:
    token = None
    if settings.traffic_demo_token is not None:
        token_value = settings.traffic_demo_token.get_secret_value().strip()
        token = token_value or None
    values: dict[str, object] = {}
    if run_id is not None:
        values["run_id"] = run_id
    return TrafficProbeConfig(
        receiver_url=settings.traffic_demo_receiver_url,
        collector_url=None,
        shared_token=token,
        allow_non_loopback_receiver=settings.traffic_demo_allow_non_loopback_receiver,
        allow_insecure_remote_ws=settings.traffic_demo_allow_insecure_remote_ws,
        generated_file_bytes=1,
        max_file_bytes=settings.traffic_demo_max_file_bytes,
        chunk_bytes=min(DEFAULT_MAX_CHUNK_BYTES, settings.traffic_demo_max_file_bytes),
        **values,
    )


def _traffic_evidence_saved(result: TrafficDemoResult) -> bool:
    return result.evidence_saved


def _receiver_artifact_is_downloadable(
    result: TrafficDemoResult,
    *,
    traffic: TrafficDemoStatus,
    settings: Settings,
) -> bool:
    artifact = result.receiver_artifact
    upload = result.upload
    return bool(
        settings.traffic_demo_receiver_artifact_token is not None
        and artifact is not None
        and artifact.expires_at > datetime.now(UTC)
        and result.evidence_saved
        and traffic.status == "passed"
        and upload.status == "completed"
        and upload.integrity == "verified"
        and upload.bytes_expected > 0
        and upload.bytes_transferred == upload.bytes_expected
        and upload.bytes_received == upload.bytes_expected
    )


def _receiver_artifact_url(settings: Settings) -> str:
    receiver = urlsplit(settings.traffic_demo_receiver_url)
    http_scheme = "https" if receiver.scheme == "wss" else "http"
    return urlunsplit((http_scheme, receiver.netloc, "/api/traffic/artifact", "", ""))


def _receiver_artifact_headers(settings: Settings, handle: str) -> dict[str, str] | None:
    token_setting = settings.traffic_demo_receiver_artifact_token
    if token_setting is None:
        return None
    token = token_setting.get_secret_value()
    if not token:
        return None
    return {
        "Authorization": f"Bearer {token}",
        "X-Prahari-Artifact-Handle": handle,
        "Accept": "application/octet-stream",
        "Accept-Encoding": "identity",
    }


async def _fetch_receiver_artifact(
    settings: Settings,
    grant: _DownloadGrant,
) -> bytes:
    headers = _receiver_artifact_headers(settings, grant.receiver_handle)
    if headers is None:
        raise _ReceiverArtifactFetchError(
            "verified demo downloads are not configured",
            status_code=status.HTTP_404_NOT_FOUND,
            revoke_grant=True,
        )
    if not 1 <= grant.size_bytes <= settings.traffic_demo_max_file_bytes:
        raise _ReceiverArtifactFetchError(
            "the verified demo file failed its bounded integrity check",
            revoke_grant=True,
        )
    try:
        timeout = httpx.Timeout(settings.traffic_demo_artifact_fetch_timeout_seconds)
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with client.stream(
                "GET",
                _receiver_artifact_url(settings),
                headers=headers,
            ) as response:
                if response.status_code == status.HTTP_404_NOT_FOUND:
                    raise _ReceiverArtifactFetchError(
                        "the verified demo file is unavailable",
                        status_code=status.HTTP_404_NOT_FOUND,
                        revoke_grant=True,
                    )
                if response.status_code == status.HTTP_410_GONE:
                    raise _ReceiverArtifactFetchError(
                        "the verified demo file has expired",
                        status_code=status.HTTP_410_GONE,
                        revoke_grant=True,
                    )
                if response.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
                    raise _ReceiverArtifactFetchError(
                        "the receiver is busy; retry the verified download",
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    )
                if response.status_code != status.HTTP_200_OK:
                    raise _ReceiverArtifactFetchError(
                        "the receiver could not provide the verified demo file"
                    )
                _validate_receiver_artifact_response(response, grant)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > grant.size_bytes:
                        raise _ReceiverArtifactFetchError(
                            "the verified demo file failed its bounded integrity check",
                            revoke_grant=True,
                        )
                    body.extend(chunk)
    except _ReceiverArtifactFetchError:
        raise
    except httpx.TimeoutException as error:
        raise _ReceiverArtifactFetchError(
            "the receiver timed out while providing the verified demo file",
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
        ) from error
    except httpx.HTTPError as error:
        raise _ReceiverArtifactFetchError(
            "the receiver could not provide the verified demo file"
        ) from error
    data = bytes(body)
    if len(data) != grant.size_bytes or not hmac.compare_digest(
        sha256(data).hexdigest(),
        grant.checksum_sha256,
    ):
        raise _ReceiverArtifactFetchError(
            "the verified demo file failed its bounded integrity check",
            revoke_grant=True,
        )
    return data


def _validate_receiver_artifact_response(
    response: httpx.Response,
    grant: _DownloadGrant,
) -> None:
    content_type = response.headers.get("Content-Type", "").partition(";")[0].strip().lower()
    content_encoding = response.headers.get("Content-Encoding")
    content_range = response.headers.get("Content-Range")
    raw_length = response.headers.get("Content-Length")
    if content_type != "application/octet-stream":
        raise _ReceiverArtifactFetchError(
            "the verified demo file failed its bounded integrity check",
            revoke_grant=True,
        )
    if content_encoding is not None and content_encoding.strip().lower() != "identity":
        raise _ReceiverArtifactFetchError(
            "the verified demo file failed its bounded integrity check",
            revoke_grant=True,
        )
    if content_range is not None:
        raise _ReceiverArtifactFetchError(
            "the verified demo file failed its bounded integrity check",
            revoke_grant=True,
        )
    if raw_length is not None:
        try:
            content_length = int(raw_length)
        except ValueError as error:
            raise _ReceiverArtifactFetchError(
                "the verified demo file failed its bounded integrity check",
                revoke_grant=True,
            ) from error
        if content_length != grant.size_bytes:
            raise _ReceiverArtifactFetchError(
                "the verified demo file failed its bounded integrity check",
                revoke_grant=True,
            )
    expected_headers = {
        "X-Prahari-Artifact-Run-Id": grant.run_id,
        "X-Prahari-Artifact-Transfer-Id": grant.transfer_id,
        "X-Prahari-Artifact-SHA256": grant.checksum_sha256,
    }
    if any(
        not hmac.compare_digest(response.headers.get(name, ""), expected)
        for name, expected in expected_headers.items()
    ):
        raise _ReceiverArtifactFetchError(
            "the verified demo file failed its correlation check",
            revoke_grant=True,
        )
    raw_expiry = response.headers.get("X-Prahari-Artifact-Expires-At", "")
    try:
        expiry = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
    except ValueError as error:
        raise _ReceiverArtifactFetchError(
            "the verified demo file failed its correlation check",
            revoke_grant=True,
        ) from error
    if expiry.tzinfo is None or expiry.utcoffset() is None:
        raise _ReceiverArtifactFetchError(
            "the verified demo file failed its correlation check",
            revoke_grant=True,
        )
    if expiry.astimezone(UTC) != grant.expires_at or expiry <= datetime.now(UTC):
        raise _ReceiverArtifactFetchError(
            "the verified demo file has expired",
            status_code=status.HTTP_410_GONE,
            revoke_grant=True,
        )


async def _delete_receiver_artifact_best_effort(
    settings: Settings,
    handle: str,
) -> None:
    headers = _receiver_artifact_headers(settings, handle)
    if headers is None:
        return
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(settings.traffic_demo_artifact_fetch_timeout_seconds),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            await client.delete(_receiver_artifact_url(settings), headers=headers)
    except httpx.HTTPError:
        return


def _download_content_disposition(file_name: str) -> str:
    return f'attachment; filename="{validate_filename(file_name)}"'
