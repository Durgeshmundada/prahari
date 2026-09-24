"""Regression and loopback smoke tests for the bounded two-server traffic demo."""

from __future__ import annotations

import asyncio
import json
import random
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import prahari.api as api_module
from prahari.api import create_app
from prahari.contracts import AppProbeRecord
from prahari.persistence import PersistenceStore
from prahari.services.demo import PrahariService
from prahari.settings import Settings
from prahari.traffic_demo.protocol import (
    ProbeMeasurements,
    ReceiverLimits,
    TrafficDemoError,
    UploadMeasurement,
)
from prahari.traffic_demo.receiver import (
    ReceiverConfig,
    create_receiver_app,
    validate_bind_options,
)
from prahari.traffic_demo.sender import (
    TrafficDemoResult,
    TrafficProbeConfig,
    prepare_uploaded_payload,
    run_periodic_traffic_demo,
    run_traffic_demo,
)
from prahari.traffic_demo.telemetry import (
    CollectorPublishError,
    InProcessTelemetryPublisher,
    TelemetryPublisher,
    TrafficTelemetryContext,
    build_app_probe_records,
    build_event_records,
)


def test_measurement_jitter_uses_consecutive_rtt_samples() -> None:
    measurements = ProbeMeasurements(attempted=3, received=3, rtts_ms=(2.0, 4.0, 7.0))

    assert measurements.packet_loss_ratio == 0
    assert measurements.p50_rtt_ms == 4.0
    assert measurements.p95_rtt_ms == 7.0
    assert measurements.jitter_ms == 2.5


def test_receiver_hashes_in_memory_and_returns_a_verified_receipt() -> None:
    client = TestClient(
        create_receiver_app(
            ReceiverConfig(limits=ReceiverLimits(max_file_bytes=1_024, max_chunk_bytes=1_024))
        )
    )
    payload = b"bounded-demo-payload"
    checksum = "a69e47565acc7dd60fe85bf3343574405f6db18ad9eb77146e97bf97c247274a"

    with client.websocket_connect("/ws/traffic") as websocket:
        websocket.send_json({"type": "hello", "protocol": "prahari-traffic-demo/v1"})
        hello = websocket.receive_json()
        assert hello["type"] == "hello_ack"
        assert hello["stores_uploaded_files"] is False

        websocket.send_json(
            {
                "type": "chat",
                "message_id": "chat-1",
                "message": "hello from branch",
            }
        )
        assert websocket.receive_json()["type"] == "chat_echo"

        websocket.send_json(
            {
                "type": "file_start",
                "transfer_id": "transfer-1",
                "file_name": "demo.bin",
                "bytes_expected": len(payload),
                "checksum_sha256": checksum,
            }
        )
        assert websocket.receive_json()["type"] == "file_ready"
        websocket.send_bytes(payload)
        websocket.send_json({"type": "file_end", "transfer_id": "transfer-1"})
        receipt = websocket.receive_json()

    assert receipt == {
        "type": "file_received",
        "transfer_id": "transfer-1",
        "bytes_received": len(payload),
        "checksum_sha256": checksum,
        "integrity": "verified",
        "status": "completed",
        "error": None,
    }


def test_receiver_rejects_browser_websocket_origins() -> None:
    client = TestClient(create_receiver_app())

    with pytest.raises(WebSocketDisconnect) as denied:
        with client.websocket_connect(
            "/ws/traffic",
            headers={"Origin": "http://127.0.0.1:5173"},
        ):
            pass

    assert denied.value.code == 1008
    assert denied.value.reason == "browser origins are not accepted"


def test_uploaded_payload_is_bounded_and_never_needs_a_path() -> None:
    payload = prepare_uploaded_payload("demo.jpg", b"camera-bytes", max_file_bytes=32)

    assert payload.file_name == "demo.jpg"
    assert payload.data == b"camera-bytes"
    assert payload.kind == "photo"
    with pytest.raises(TrafficDemoError, match="bounded demo limit"):
        prepare_uploaded_payload("large.bin", b"12345", max_file_bytes=4)
    with pytest.raises(TrafficDemoError, match="plain"):
        prepare_uploaded_payload("../escape.bin", b"x", max_file_bytes=4)


def test_transfer_payload_validates_against_the_live_contract_and_dashboard_api(
    project_root: Path,
) -> None:
    now = datetime.now(UTC)
    chat = ProbeMeasurements(attempted=5, received=4, rtts_ms=(4.0, 5.0, 6.0, 7.0))
    upload = UploadMeasurement(
        transfer_id="transfer-contract-test",
        file_name="demo.mp4",
        bytes_expected=1_024,
        bytes_transferred=1_024,
        bytes_received=1_024,
        elapsed_seconds=0.25,
        checksum_sha256="a" * 64,
        integrity="verified",
        status="completed",
    )
    context = TrafficTelemetryContext(run_id="traffic-demo-contract-test")
    app_probes = build_app_probe_records(
        context=context,
        observed_at=now,
        chat=chat,
        upload=upload,
        kind="video",
    )
    events = build_event_records(
        context=context,
        started_at=now,
        finished_at=now,
        chat=chat,
        upload=upload,
    )

    file_probe = AppProbeRecord.model_validate(app_probes[1])
    assert file_probe.transfer is not None
    assert file_probe.transfer.chat_measurements is not None
    assert file_probe.transfer.chat_measurements.messages_received == 4

    settings = Settings(
        environment="test",
        data_mode="live",
        database_url="sqlite://",
        auth_mode="disabled",
    )
    store = PersistenceStore(settings.database_url)
    store.create_schema()
    service = PrahariService(root=project_root, data_mode="live", store=store)
    client = TestClient(create_app(service, settings=settings, store=store))

    app_response = client.post(
        "/api/telemetry/app-probes",
        headers={"X-Actor-Role": "collector"},
        json={"run_id": context.run_id, "records": app_probes},
    )
    event_response = client.post(
        "/api/telemetry/events",
        headers={"X-Actor-Role": "collector"},
        json={"run_id": context.run_id, "records": events},
    )
    dashboard_response = client.get("/api/demo/traffic")

    assert app_response.status_code == 202
    assert event_response.status_code == 202
    assert dashboard_response.status_code == 200
    status = dashboard_response.json()
    assert status["status"] == "failed"
    assert status["latency_ms"]["p95"] == 7.0
    assert status["loss_percent"] == 20.0
    assert status["throughput_mbps"] == pytest.approx(round(upload.throughput_mbps, 3))


def test_in_process_publisher_requires_two_plus_two_and_writes_events_first() -> None:
    observed_at = datetime.now(UTC)
    context = TrafficTelemetryContext(run_id="traffic-demo-publisher-order")
    chat = ProbeMeasurements(attempted=1, received=1, rtts_ms=(1.0,))
    upload = UploadMeasurement(
        transfer_id="transfer-publisher-order",
        file_name="demo.bin",
        bytes_expected=4,
        bytes_transferred=4,
        bytes_received=4,
        elapsed_seconds=0.01,
        checksum_sha256="a" * 64,
        integrity="verified",
        status="completed",
    )
    app_probes = build_app_probe_records(
        context=context,
        observed_at=observed_at,
        chat=chat,
        upload=upload,
        kind="file",
    )
    events = build_event_records(
        context=context,
        started_at=observed_at,
        finished_at=observed_at,
        chat=chat,
        upload=upload,
    )
    calls: list[tuple[str, str | None, int]] = []

    class RecordingService:
        def ingest_events(self, records, *, run_id=None):
            calls.append(("events", run_id, len(records)))
            return len(records)

        def ingest_app_probes(self, records, *, run_id=None):
            calls.append(("app_probes", run_id, len(records)))
            return len(records)

    publisher = InProcessTelemetryPublisher(RecordingService())  # type: ignore[arg-type]
    result = asyncio.run(
        publisher.publish(app_probes=app_probes, events=events, run_id=context.run_id)
    )

    assert result.events_accepted == 2
    assert result.app_probes_accepted == 2
    assert calls == [
        ("events", context.run_id, 2),
        ("app_probes", context.run_id, 2),
    ]
    with pytest.raises(CollectorPublishError, match="exactly two"):
        asyncio.run(
            publisher.publish(
                app_probes=app_probes[:1],
                events=events,
                run_id=context.run_id,
            )
        )


def test_loopback_sender_receiver_smoke_posts_collector_evidence() -> None:
    posted: dict[str, list[dict[str, object]]] = {"app-probes": [], "events": []}

    def collector_handler(request: httpx.Request) -> httpx.Response:
        key = request.url.path.rsplit("/", maxsplit=1)[-1]
        payload = json.loads(request.content)
        posted[key].append(payload)
        return httpx.Response(
            202,
            json={
                "accepted": len(payload["records"]),
                "record_type": key.rstrip("s"),
                "run_id": "x",
            },
        )

    publisher = TelemetryPublisher(
        "http://collector.test",
        transport=httpx.MockTransport(collector_handler),
    )
    with _live_loopback_receiver() as receiver_url:
        config = TrafficProbeConfig(
            receiver_url=receiver_url,
            collector_url=None,
            chat_messages=3,
            message_interval_seconds=0,
            timeout_seconds=2.0,
            generated_file_bytes=1_024,
        )
        result = asyncio.run(run_traffic_demo(config, publisher=publisher))

    assert result.passed
    assert result.chat.received == 3
    assert result.upload.bytes_received == 1_024
    assert result.upload.integrity == "verified"
    assert len(posted["app-probes"]) == 1
    assert len(posted["events"]) == 1
    posted_file_probe = posted["app-probes"][0]["records"][1]
    assert isinstance(posted_file_probe, dict)
    assert AppProbeRecord.model_validate(posted_file_probe).transfer is not None


def test_development_api_runs_uploaded_bytes_and_returns_correlated_evidence(
    project_root: Path,
) -> None:
    # An incompressible payload larger than one frame guards against WebSocket
    # compression overhead breaking the negotiated 64 KiB chunk boundary.
    payload = random.Random(2026).randbytes(128 * 1024)
    with _live_loopback_receiver() as receiver_url:
        settings = Settings(
            environment="development",
            data_mode="replay",
            database_url="sqlite://",
            auth_mode="disabled",
            traffic_demo_run_enabled=True,
            traffic_demo_receiver_url=receiver_url,
            traffic_demo_max_file_bytes=256 * 1024,
        )
        store = PersistenceStore(settings.database_url)
        store.create_schema()
        service = PrahariService(root=project_root, data_mode="replay", store=store)
        client = TestClient(create_app(service, settings=settings, store=store))
        before_events = len(service.event_records(limit=5_000))
        before_probes = len(service.app_probe_records(limit=5_000))

        response = client.post(
            "/api/demo/traffic/run",
            content=payload,
            headers={
                "Content-Type": "application/octet-stream",
                "Origin": "http://127.0.0.1:5173",
                "X-Prahari-Demo-Intent": "run-traffic-test",
                "X-Prahari-File-Name": "selected.bin",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"].startswith("traffic-demo-")
    assert body["transfer_id"].startswith("transfer-")
    assert body["evidence_saved"] is True
    assert body["traffic"]["status"] == "passed"
    assert body["traffic"]["can_run"] is True
    assert body["traffic"]["max_file_bytes"] == 256 * 1024
    assert body["traffic"]["payload"]["name"] == "selected.bin"
    assert body["traffic"]["payload"]["size_bytes"] == len(payload)
    assert body["traffic"]["payload"]["bytes_sent"] == len(payload)
    assert body["traffic"]["payload"]["bytes_received"] == len(payload)
    assert len(service.event_records(limit=5_000)) == before_events + 2
    assert len(service.app_probe_records(limit=5_000)) == before_probes + 2

    latest = client.get("/api/demo/traffic")
    assert latest.status_code == 200
    assert latest.json()["can_run"] is True
    assert latest.json()["max_file_bytes"] == 256 * 1024
    assert latest.json()["payload"]["sha256"] == body["traffic"]["payload"]["sha256"]


def test_development_traffic_control_is_absent_until_explicitly_enabled(
    project_root: Path,
) -> None:
    settings = Settings(
        environment="development",
        data_mode="replay",
        database_url="sqlite://",
        auth_mode="disabled",
    )
    store = PersistenceStore(settings.database_url)
    store.create_schema()
    client = TestClient(
        create_app(
            PrahariService(root=project_root, data_mode="replay", store=store),
            settings=settings,
            store=store,
        )
    )

    assert client.post("/api/demo/traffic/run", content=b"x").status_code == 404
    status_response = client.get("/api/demo/traffic")
    assert status_response.status_code == 200
    assert status_response.json()["can_run"] is False
    assert status_response.json()["max_file_bytes"] == 5 * 1024 * 1024


def test_development_upload_rejects_wrong_origin_intent_media_name_and_size(
    project_root: Path,
) -> None:
    settings = Settings(
        environment="development",
        data_mode="replay",
        database_url="sqlite://",
        auth_mode="disabled",
        traffic_demo_run_enabled=True,
        traffic_demo_max_file_bytes=4,
    )
    store = PersistenceStore(settings.database_url)
    store.create_schema()
    client = TestClient(
        create_app(
            PrahariService(root=project_root, data_mode="replay", store=store),
            settings=settings,
            store=store,
        )
    )
    valid_headers = {
        "Content-Type": "application/octet-stream",
        "Origin": "http://127.0.0.1:5173",
        "X-Prahari-Demo-Intent": "run-traffic-test",
        "X-Prahari-File-Name": "x.bin",
    }

    wrong_origin = client.post(
        "/api/demo/traffic/run",
        content=b"x",
        headers={**valid_headers, "Origin": "http://127.0.0.1:5173.evil.test"},
    )
    missing_intent_headers = dict(valid_headers)
    missing_intent_headers.pop("X-Prahari-Demo-Intent")
    missing_intent = client.post(
        "/api/demo/traffic/run",
        content=b"x",
        headers=missing_intent_headers,
    )
    wrong_media = client.post(
        "/api/demo/traffic/run",
        content=b"x",
        headers={**valid_headers, "Content-Type": "text/plain"},
    )
    unsafe_name = client.post(
        "/api/demo/traffic/run",
        content=b"x",
        headers={**valid_headers, "X-Prahari-File-Name": "../x.bin"},
    )
    oversized = client.post(
        "/api/demo/traffic/run",
        content=b"12345",
        headers=valid_headers,
    )
    empty = client.post(
        "/api/demo/traffic/run",
        content=b"",
        headers=valid_headers,
    )

    assert wrong_origin.status_code == 403
    assert missing_intent.status_code == 403
    assert wrong_media.status_code == 415
    assert unsafe_name.status_code == 422
    assert oversized.status_code == 413
    assert empty.status_code == 422


def test_development_upload_requires_operator_role(project_root: Path) -> None:
    settings = Settings(
        environment="development",
        data_mode="replay",
        database_url="sqlite://",
        auth_mode="api_key",
        api_keys_json=json.dumps(
            {
                "viewer-token": {"actor_id": "viewer-01", "role": "viewer"},
                "operator-token": {"actor_id": "operator-01", "role": "operator"},
            }
        ),
        traffic_demo_run_enabled=True,
    )
    store = PersistenceStore(settings.database_url)
    store.create_schema()
    client = TestClient(
        create_app(
            PrahariService(root=project_root, data_mode="replay", store=store),
            settings=settings,
            store=store,
        )
    )
    headers = {
        "Authorization": "Bearer viewer-token",
        "Content-Type": "application/octet-stream",
        "X-Prahari-Demo-Intent": "run-traffic-test",
        "X-Prahari-File-Name": "x.bin",
    }

    unauthenticated = client.post("/api/demo/traffic/run", content=b"x", headers={})
    viewer = client.post("/api/demo/traffic/run", content=b"x", headers=headers)

    assert unauthenticated.status_code == 401
    assert viewer.status_code == 403


def test_chunked_upload_is_bounded_without_content_length(project_root: Path) -> None:
    settings = Settings(
        environment="development",
        data_mode="replay",
        database_url="sqlite://",
        auth_mode="disabled",
        traffic_demo_run_enabled=True,
        traffic_demo_max_file_bytes=4,
    )
    store = PersistenceStore(settings.database_url)
    store.create_schema()
    app = create_app(
        PrahariService(root=project_root, data_mode="replay", store=store),
        settings=settings,
        store=store,
    )

    async def exercise() -> httpx.Response:
        async def chunks():
            yield b"123"
            yield b"45"

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            return await client.post(
                "/api/demo/traffic/run",
                content=chunks(),
                headers={
                    "Content-Type": "application/octet-stream",
                    "Origin": "http://127.0.0.1:5173",
                    "X-Prahari-Demo-Intent": "run-traffic-test",
                    "X-Prahari-File-Name": "x.bin",
                },
            )

    response = asyncio.run(exercise())

    assert response.request.headers.get("Content-Length") is None
    assert response.status_code == 413


def test_dashboard_upload_allows_only_one_active_run(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        environment="development",
        data_mode="replay",
        database_url="sqlite://",
        auth_mode="disabled",
        traffic_demo_run_enabled=True,
        traffic_demo_run_timeout_seconds=2,
    )
    store = PersistenceStore(settings.database_url)
    store.create_schema()
    app = create_app(
        PrahariService(root=project_root, data_mode="replay", store=store),
        settings=settings,
        store=store,
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def held_run(config, **_kwargs):
        started.set()
        await release.wait()
        return _unsaved_result(config)

    monkeypatch.setattr(api_module, "run_traffic_demo", held_run)

    async def exercise() -> tuple[httpx.Response, httpx.Response]:
        headers = {
            "Content-Type": "application/octet-stream",
            "Origin": "http://127.0.0.1:5173",
            "X-Prahari-Demo-Intent": "run-traffic-test",
            "X-Prahari-File-Name": "x.bin",
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            first_task = asyncio.create_task(
                client.post("/api/demo/traffic/run", content=b"x", headers=headers)
            )
            await started.wait()
            second = await client.post(
                "/api/demo/traffic/run",
                content=b"y",
                headers={**headers, "X-Prahari-File-Name": "y.bin"},
            )
            release.set()
            return await first_task, second

    first, second = asyncio.run(exercise())

    assert first.status_code == 200
    assert first.json()["evidence_saved"] is False
    assert second.status_code == 409


def test_whole_run_timeout_releases_the_dashboard_slot(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        environment="development",
        data_mode="replay",
        database_url="sqlite://",
        auth_mode="disabled",
        traffic_demo_run_enabled=True,
        traffic_demo_run_timeout_seconds=1,
    )
    store = PersistenceStore(settings.database_url)
    store.create_schema()
    app = create_app(
        PrahariService(root=project_root, data_mode="replay", store=store),
        settings=settings,
        store=store,
    )

    async def never_finishes(_config, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(api_module, "run_traffic_demo", never_finishes)

    async def exercise() -> tuple[httpx.Response, httpx.Response]:
        headers = {
            "Content-Type": "application/octet-stream",
            "Origin": "http://127.0.0.1:5173",
            "X-Prahari-Demo-Intent": "run-traffic-test",
            "X-Prahari-File-Name": "x.bin",
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            timed_out = await client.post(
                "/api/demo/traffic/run",
                content=b"x",
                headers=headers,
            )

            async def finishes(config, **_kwargs):
                return _unsaved_result(config)

            monkeypatch.setattr(api_module, "run_traffic_demo", finishes)
            after_timeout = await client.post(
                "/api/demo/traffic/run",
                content=b"x",
                headers=headers,
            )
            return timed_out, after_timeout

    timed_out, after_timeout = asyncio.run(exercise())

    assert timed_out.status_code == 504
    assert after_timeout.status_code == 200


def _unsaved_result(config: TrafficProbeConfig) -> TrafficDemoResult:
    now = datetime.now(UTC)
    return TrafficDemoResult(
        run_id=config.run_id,
        session_id=None,
        started_at=now,
        finished_at=now,
        chat=ProbeMeasurements(attempted=1, received=0, rtts_ms=()),
        upload=UploadMeasurement(
            transfer_id="transfer-unsaved-test",
            file_name="x.bin",
            bytes_expected=1,
            bytes_transferred=0,
            bytes_received=None,
            elapsed_seconds=0,
            checksum_sha256="a" * 64,
            integrity="failed",
            status="failed",
            error="test stub",
        ),
    )


def test_periodic_runner_can_stream_without_retaining_results() -> None:
    observations: list[str] = []

    # This test deliberately uses a one-run loopback server instead of mocking
    # the internal runner, proving the callback path remains compatible.
    with _live_loopback_receiver() as receiver_url:
        run_config = TrafficProbeConfig(
            receiver_url=receiver_url,
            collector_url=None,
            generated_file_bytes=16,
            chat_messages=1,
            timeout_seconds=2,
        )
        results = asyncio.run(
            run_periodic_traffic_demo(
                run_config,
                repetitions=2,
                cadence_seconds=1,
                on_result=lambda result: observations.append(result.run_id),
                retain_results=False,
            )
        )

    assert results == []
    assert len(observations) == 2


def test_non_loopback_receiver_requires_private_address_flag_and_token() -> None:
    with pytest.raises(TrafficDemoError):
        validate_bind_options("0.0.0.0", allow_non_loopback=True, shared_token="x" * 16)
    with pytest.raises(TrafficDemoError):
        validate_bind_options("192.168.1.20", allow_non_loopback=False, shared_token="x" * 16)
    assert validate_bind_options(
        "192.168.1.20",
        allow_non_loopback=True,
        shared_token="x" * 16,
    ) == "x" * 16


def test_collector_api_key_is_published_from_memory_and_plain_http_is_gated() -> None:
    authorizations: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authorizations.append(request.headers.get("Authorization"))
        return httpx.Response(202, json={"accepted": 1, "record_type": "event", "run_id": "x"})

    publisher = TelemetryPublisher(
        "http://127.0.0.1:8000",
        api_key="collector-test-token",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(
        publisher.publish(app_probes=[{"test": "probe"}], events=[{"test": "event"}], run_id="x")
    )

    assert result.app_probes_accepted == 1
    assert authorizations == ["Bearer collector-test-token", "Bearer collector-test-token"]
    with pytest.raises(TrafficDemoError):
        TelemetryPublisher("http://192.168.1.20:8000", api_key="collector-test-token")
    TelemetryPublisher(
        "http://192.168.1.20:8000",
        api_key="collector-test-token",
        allow_insecure_private_http=True,
    )


@contextmanager
def _live_loopback_receiver() -> Iterator[str]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            create_receiver_app(),
            host="127.0.0.1",
            port=port,
            log_level="critical",
            access_log=False,
            lifespan="off",
        )
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2)
        listener.close()
        raise AssertionError("loopback receiver did not start")
    try:
        yield f"ws://127.0.0.1:{port}/ws/traffic"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
