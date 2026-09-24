from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from prahari.traffic_demo.protocol import ReceiverLimits
from prahari.traffic_demo.telemetry import CollectorPublishResult
from prahari.traffic_demo.user_service import UserDemoConfig, create_user_demo_app


class RecordingPublisher:
    def __init__(self) -> None:
        self.app_probes: list[dict[str, object]] = []
        self.events: list[dict[str, object]] = []
        self.run_id = ""

    async def publish(self, *, app_probes, events, run_id):  # type: ignore[no-untyped-def]
        self.app_probes = app_probes
        self.events = events
        self.run_id = run_id
        return CollectorPublishResult(app_probes_accepted=2, events_accepted=2)


def _assets(tmp_path: Path) -> Path:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.html").write_text("<h1>User Link</h1>", encoding="utf-8")
    (assets / "app.js").write_text("void 0;", encoding="utf-8")
    (assets / "styles.css").write_text("body {}", encoding="utf-8")
    return assets


def _chat_sample(client: TestClient, headers: dict[str, str], message: str = "private text"):
    echoed = client.post("/api/chat", headers=headers, json={"message": message})
    assert echoed.status_code == 200
    ack = client.post(
        "/api/chat/ack",
        headers=headers,
        json={"message_id": echoed.json()["message_id"], "rtt_ms": 12.5},
    )
    assert ack.status_code == 204
    return echoed


def test_browser_chat_and_upload_publish_measurements_without_payloads(tmp_path: Path) -> None:
    publisher = RecordingPublisher()
    token = "private-demo-token-123"
    app = create_user_demo_app(
        UserDemoConfig(
            assets_directory=_assets(tmp_path),
            access_token=token,
            publisher=publisher,
        )
    )
    client = TestClient(app)
    headers = {"X-Prahari-Demo-Token": token}

    home = client.get("/")
    echoed = _chat_sample(client, headers)
    uploaded = client.post(
        "/api/upload",
        content=b"secret file bytes",
        headers={
            **headers,
            "Content-Type": "application/octet-stream",
            "X-Prahari-File-Name": "sample.bin",
        },
    )

    assert home.status_code == 200
    assert home.headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in home.headers["Content-Security-Policy"]
    assert echoed.json()["echo"] == "private text"
    assert echoed.json()["stored"] is False
    assert uploaded.status_code == 200
    assert uploaded.json()["bytes_received"] == len(b"secret file bytes")
    assert uploaded.json()["evidence_saved"] is True
    assert uploaded.json()["stored"] is False
    evidence = repr((publisher.app_probes, publisher.events))
    assert "private text" not in evidence
    assert "secret file bytes" not in evidence
    assert len(publisher.app_probes) == 2
    assert len(publisher.events) == 2
    assert publisher.run_id.startswith("user-demo-")


def test_access_code_and_chat_measurement_are_required(tmp_path: Path) -> None:
    token = "private-demo-token-123"
    client = TestClient(
        create_user_demo_app(
            UserDemoConfig(assets_directory=_assets(tmp_path), access_token=token)
        )
    )

    missing = client.post("/api/chat", json={"message": "hello"})
    wrong = client.post(
        "/api/chat",
        headers={"X-Prahari-Demo-Token": "wrong-demo-token-123"},
        json={"message": "hello"},
    )
    upload_without_chat = client.post(
        "/api/upload",
        content=b"x",
        headers={
            "X-Prahari-Demo-Token": token,
            "Content-Type": "application/octet-stream",
            "X-Prahari-File-Name": "sample.bin",
        },
    )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert upload_without_chat.status_code == 422
    assert "chat" in upload_without_chat.json()["detail"]


def test_upload_enforces_media_type_filename_and_actual_size(tmp_path: Path) -> None:
    token = "private-demo-token-123"
    client = TestClient(
        create_user_demo_app(
            UserDemoConfig(
                assets_directory=_assets(tmp_path),
                access_token=token,
                limits=ReceiverLimits(max_file_bytes=4, max_chunk_bytes=4),
            )
        )
    )
    headers = {"X-Prahari-Demo-Token": token}
    _chat_sample(client, headers)

    wrong_media = client.post(
        "/api/upload",
        content=b"x",
        headers={**headers, "Content-Type": "text/plain", "X-Prahari-File-Name": "x.bin"},
    )
    unsafe_name = client.post(
        "/api/upload",
        content=b"x",
        headers={
            **headers,
            "Content-Type": "application/octet-stream",
            "X-Prahari-File-Name": "../x.bin",
        },
    )
    oversized = client.post(
        "/api/upload",
        content=b"12345",
        headers={
            **headers,
            "Content-Type": "application/octet-stream",
            "X-Prahari-File-Name": "x.bin",
        },
    )

    assert wrong_media.status_code == 415
    assert unsafe_name.status_code == 422
    assert oversized.status_code == 413
