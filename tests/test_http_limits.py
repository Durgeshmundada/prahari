import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from prahari.api import create_app
from prahari.http_limits import BodyLimitMiddleware
from prahari.services.demo import DemoService
from prahari.settings import Settings


def run_body(messages, headers=(), *, timeout=False):
    called = []
    output = []

    async def downstream(scope, receive, send):
        called.append(await receive())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        if timeout:
            await asyncio.sleep(1)
        return messages.pop(0)

    async def send(message):
        output.append(message)

    asyncio.run(
        BodyLimitMiddleware(downstream, max_bytes=64, timeout_seconds=0.02)(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/telemetry/metrics",
                "headers": headers,
            },
            receive,
            send,
        )
    )
    return called, output


def test_chunked_body_cannot_bypass_limit_with_a_false_content_length():
    called, output = run_body(
        [
            {"type": "http.request", "body": b"a" * 40, "more_body": True},
            {"type": "http.request", "body": b"b" * 40, "more_body": False},
        ],
        [(b"content-length", b"10")],
    )
    assert called == []
    assert output[0]["status"] == 413


def test_bounded_chunked_body_is_forwarded_exactly():
    called, output = run_body(
        [
            {"type": "http.request", "body": b"first", "more_body": True},
            {"type": "http.request", "body": b"second", "more_body": False},
        ]
    )
    assert called == [{"type": "http.request", "body": b"firstsecond", "more_body": False}]
    assert output[0]["status"] == 200


@pytest.mark.parametrize("length", [b"-1", b"invalid", b"999999"])
def test_declared_length_rejected_before_receiving_a_body(length):
    called, output = run_body([], [(b"content-length", length)])
    assert called == []
    assert output[0]["status"] == (413 if length == b"999999" else 400)


def test_slow_body_has_a_total_receipt_deadline():
    called, output = run_body([], timeout=True)
    assert called == []
    assert output[0]["status"] == 408


def test_oversized_review_does_not_mutate_workflow_and_retains_security_headers(project_root):
    service = DemoService(root=project_root)
    client = TestClient(create_app(service=service, settings=Settings(http_max_body_bytes=65536)))
    before = service.incident_detail(service.incident_id)["audit_trail"]
    response = client.post(
        f"/api/incidents/{service.incident_id}/review",
        content=json.dumps({"actor_id": "test", "note": "a" * 70000}),
        headers={"X-Request-Id": "hostile value " * 20, "Origin": "http://127.0.0.1:5173"},
    )
    assert response.status_code == 413
    assert response.headers["Cache-Control"] == "no-store"
    assert len(response.headers["X-Request-Id"]) == 36
    assert response.headers["Access-Control-Allow-Origin"] == "http://127.0.0.1:5173"
    assert service.incident_detail(service.incident_id)["audit_trail"] == before
