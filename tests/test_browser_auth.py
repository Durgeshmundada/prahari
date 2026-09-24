import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from prahari.api import create_app
from prahari.identity import hash_password, verify_password
from prahari.persistence import BrowserSessionRow, LoginWindowRow, PersistenceStore
from prahari.services.demo import DemoService
from prahari.settings import Settings

PASSWORD = "correct horse battery staple"
ORIGIN = "https://prahari.example"


@pytest.fixture(scope="module")
def password_hash() -> str:
    return hash_password(PASSWORD)


@pytest.fixture
def auth_app(project_root: Path, tmp_path: Path, password_hash: str):
    users = tmp_path / "users.json"
    users.write_text(
        json.dumps(
            {
                "operator": {
                    "actor_id": "operator-01",
                    "role": "operator",
                    "password_hash": password_hash,
                },
                "reviewer": {
                    "actor_id": "reviewer-01",
                    "role": "reviewer",
                    "password_hash": password_hash,
                },
            }
        ),
        encoding="utf-8",
    )
    url = f"sqlite:///{(tmp_path / 'sessions.db').as_posix()}"
    settings = Settings(
        environment="test",
        database_url=url,
        auth_mode="api_key",
        api_keys_json=json.dumps(
            {"service-token": {"actor_id": "collector-01", "role": "collector"}}
        ),
        browser_auth_enabled=True,
        browser_origin=ORIGIN,
        browser_users_file=users,
    )
    store = PersistenceStore(url)
    store.create_schema()
    app = create_app(DemoService(root=project_root, store=store), settings=settings, store=store)
    with TestClient(app, base_url=ORIGIN) as client:
        yield client, store, settings, users
    store.dispose()


def login(client: TestClient, username: str = "operator"):
    return client.post(
        "/api/auth/login",
        headers={"Origin": ORIGIN},
        json={
            "username": username,
            "password": PASSWORD,
        },
    )


def test_password_hashes_are_salted_and_verified(password_hash: str) -> None:
    assert hash_password(PASSWORD) != password_hash
    assert verify_password(PASSWORD, password_hash)
    assert not verify_password("incorrect password", password_hash)


def test_browser_session_uses_secure_cookie_and_enforces_csrf_and_identity(auth_app) -> None:
    client, store, settings, _ = auth_app
    assert client.get("/api/incidents").status_code == 401
    response = login(client)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert all(
        flag in cookie
        for flag in ("__Host-prahari_session", "HttpOnly", "Secure", "SameSite=strict")
    )
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["actor"] == {"actor_id": "operator-01", "role": "operator"}
    assert "password" not in response.text and "service-token" not in response.text
    assert client.get("/api/incidents").status_code == 200
    path = f"/api/incidents/{DemoService.incident_id}/review"
    payload = {"actor_id": "operator-01", "note": "Reviewed current evidence."}
    assert client.post(path, json=payload).status_code == 403
    assert (
        client.post(
            path,
            json=payload,
            headers={"Origin": "https://evil.example", "X-CSRF-Token": body["csrf_token"]},
        ).status_code
        == 403
    )
    headers = {"Origin": ORIGIN, "X-CSRF-Token": body["csrf_token"]}
    assert (
        client.post(path, json={**payload, "actor_id": "reviewer-01"}, headers=headers).status_code
        == 403
    )
    assert client.post(path, json=payload, headers=headers).status_code == 200
    token = client.cookies.get(settings.browser_cookie_name)
    with store._session_factory() as session:
        row = session.scalar(select(BrowserSessionRow))
        assert row is not None and row.token_hash == sha256(token.encode()).hexdigest()
        assert row.token_hash != token
    assert client.post("/api/auth/logout", headers=headers).status_code == 204
    assert client.get("/api/incidents").status_code == 401


def test_login_rejects_other_origins_and_revokes_existing_cookie(auth_app) -> None:
    client, _, settings, _ = auth_app
    rejected = client.post("/api/auth/login", json={"username": "operator", "password": PASSWORD})
    assert rejected.status_code == 403
    assert login(client).status_code == 200
    old_token = client.cookies.get(settings.browser_cookie_name)
    assert login(client).status_code == 200
    assert client.cookies.get(settings.browser_cookie_name) != old_token
    client.cookies.clear()
    client.cookies.set(settings.browser_cookie_name, old_token)
    assert client.get("/api/incidents").status_code == 401


@pytest.mark.parametrize("change", ["disabled", "role", "password"])
def test_account_changes_revoke_existing_sessions(auth_app, change: str) -> None:
    client, _, _, users = auth_app
    assert login(client).status_code == 200
    raw = json.loads(users.read_text(encoding="utf-8"))
    if change == "disabled":
        raw["operator"]["disabled"] = True
    elif change == "role":
        raw["operator"]["role"] = "admin"
    else:
        raw["operator"]["password_hash"] = hash_password("a completely different passphrase")
    users.write_text(json.dumps(raw), encoding="utf-8")
    assert client.get("/api/incidents").status_code == 401
    assert client.get("/api/auth/session").json()["authenticated"] is False


@pytest.mark.parametrize("expired_column", ["expires_at", "last_seen_at"])
def test_expired_sessions_cannot_be_reused(auth_app, expired_column: str) -> None:
    client, store, _, _ = auth_app
    assert login(client).status_code == 200
    with store._session_factory.begin() as session:
        session.execute(
            update(BrowserSessionRow).values(
                {expired_column: datetime.now(UTC) - timedelta(days=1)}
            )
        )
    assert client.get("/api/incidents").status_code == 401


def test_login_throttling_is_shared_between_requests(auth_app) -> None:
    client, store, _, _ = auth_app
    bucket = sha256(b"operator").hexdigest()[:2]
    for _ in range(10):
        store.record_login_attempt(f"user-{bucket}", window_seconds=300)
    assert login(client).status_code == 429
    with store._session_factory() as session:
        assert len(session.scalars(select(LoginWindowRow)).all()) == 2


def test_reviewer_cookie_cannot_perform_operator_actions(auth_app) -> None:
    client, _, _, _ = auth_app
    response = login(client, "reviewer")
    assert response.status_code == 200
    response = client.post(
        f"/api/incidents/{DemoService.incident_id}/review",
        headers={"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]},
        json={"actor_id": "reviewer-01", "note": "Attempt operator mutation."},
    )
    assert response.status_code == 403


def test_plain_http_remote_browser_auth_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        Settings(
            auth_mode="api_key", browser_auth_enabled=True, browser_origin="http://remote.example"
        )


def test_login_audit_failure_cannot_leave_an_authenticated_session(auth_app, monkeypatch) -> None:
    client, store, _, _ = auth_app

    def fail_audit(*_args):
        raise RuntimeError("simulated audit storage failure")

    monkeypatch.setattr(store, "_add_audit_event", fail_audit)
    with TestClient(client.app, base_url=ORIGIN, raise_server_exceptions=False) as failing:
        response = login(failing)
        assert response.status_code in {500, 503}
        assert "set-cookie" not in response.headers
        assert failing.get("/api/incidents").status_code == 401
    with store._session_factory() as session:
        assert session.scalar(select(BrowserSessionRow)) is None
