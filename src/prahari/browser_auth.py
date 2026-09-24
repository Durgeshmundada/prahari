"""Same-origin browser authentication; API keys remain server-to-server credentials."""

from __future__ import annotations

import hmac
import re
import secrets
import threading
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import Field, SecretStr

from prahari.contracts import ContractModel
from prahari.identity import DUMMY_PASSWORD_HASH, BrowserUser, verify_password
from prahari.persistence import BrowserSessionRow, PersistenceStore
from prahari.security import Actor, resolve_actor
from prahari.settings import Settings

_SESSION_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")


class LoginRequest(ContractModel):
    username: str = Field(pattern=r"^[a-z0-9][a-z0-9_.@-]{0,99}$")
    password: SecretStr = Field(min_length=1, max_length=256)


class BrowserAuth:
    def __init__(self, settings: Settings, store: PersistenceStore) -> None:
        self.settings = settings
        self.store = store
        self._password_slots = threading.BoundedSemaphore(2)

    def require_origin(self, request: Request) -> None:
        if request.headers.get("origin") != self.settings.browser_origin:
            raise HTTPException(403, "request origin does not match the configured dashboard")

    def lookup(self, request: Request) -> BrowserSessionRow:
        token = request.cookies.get(self.settings.browser_cookie_name, "")
        if not _SESSION_TOKEN.fullmatch(token):
            raise HTTPException(401, "sign in to continue")
        token_hash = sha256(token.encode()).hexdigest()
        row = self.store.browser_session(
            token_hash, idle_seconds=self.settings.browser_idle_seconds
        )
        if row is None:
            raise HTTPException(401, "session expired; sign in again")
        self.user_for_session(row)
        return row

    def user_for_session(self, row: BrowserSessionRow) -> BrowserUser:
        user = self.settings.browser_users.get(row.username)
        if (
            user is None
            or user.disabled
            or not hmac.compare_digest(row.credential_fingerprint, user.credential_fingerprint)
        ):
            self.store.revoke_browser_session(row.token_hash, row.actor_id)
            raise HTTPException(401, "session revoked; sign in again")
        return user

    def resolve(self, request: Request) -> Actor:
        row = self.lookup(request)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            self.require_origin(request)
            if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), row.csrf_token):
                raise HTTPException(403, "CSRF token is missing or invalid")
        user = self.user_for_session(row)
        return Actor(actor_id=user.actor_id, role=user.role)

    def login(
        self, request: Request, response: Response, payload: LoginRequest
    ) -> dict[str, object]:
        self.require_origin(request)
        # Fixed buckets bound persistent limiter storage even for invented usernames.
        bucket = sha256(payload.username.encode()).hexdigest()[:2]
        global_attempts = self.store.record_login_attempt("global", window_seconds=60)
        attempts = self.store.record_login_attempt(f"user-{bucket}", window_seconds=300)
        if global_attempts > 60 or attempts > 10:
            raise HTTPException(
                429, "too many sign-in attempts; try again later", headers={"Retry-After": "300"}
            )
        if not self._password_slots.acquire(blocking=False):
            raise HTTPException(
                429, "sign-in is busy; try again shortly", headers={"Retry-After": "5"}
            )
        try:
            user = self.settings.browser_users.get(payload.username)
            encoded = user.password_hash.get_secret_value() if user else DUMMY_PASSWORD_HASH
            valid = verify_password(payload.password.get_secret_value(), encoded)
        finally:
            self._password_slots.release()
        if not valid or user is None or user.disabled:
            self.store.append_audit(
                incident_id=None,
                actor_id="anonymous",
                event_type="browser_login_failed",
                detail="Browser sign-in failed.",
            )
            raise HTTPException(401, "invalid username or password")
        now = datetime.now(UTC)
        token = secrets.token_urlsafe(32)
        old_token = request.cookies.get(self.settings.browser_cookie_name)
        row = BrowserSessionRow(
            token_hash=sha256(token.encode()).hexdigest(),
            username=payload.username,
            actor_id=user.actor_id,
            credential_fingerprint=user.credential_fingerprint,
            csrf_token=secrets.token_urlsafe(32),
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(seconds=self.settings.browser_session_seconds),
        )
        self.store.create_browser_session(
            row, old_token_hash=sha256(old_token.encode()).hexdigest() if old_token else None
        )
        response.set_cookie(
            self.settings.browser_cookie_name,
            token,
            max_age=self.settings.browser_session_seconds,
            httponly=True,
            secure=self.settings.browser_cookie_secure,
            samesite="strict",
            path="/",
        )
        return self.session_payload(row)

    def session_payload(self, row: BrowserSessionRow) -> dict[str, object]:
        user = self.user_for_session(row)
        return {
            "authenticated": True,
            "mode": "local",
            "actor": {"actor_id": user.actor_id, "role": user.role},
            "csrf_token": row.csrf_token,
            "expires_at": row.expires_at.replace(tzinfo=UTC),
        }


def install_browser_auth(app: FastAPI, settings: Settings, store: PersistenceStore | None) -> None:
    if settings.browser_auth_enabled and store is None:
        raise RuntimeError("browser authentication requires a persistent session store")
    manager = BrowserAuth(settings, store) if settings.browser_auth_enabled and store else None
    app.state.browser_auth = manager

    @app.get("/api/auth/session")
    def session_status(request: Request) -> dict[str, object]:
        if manager is not None:
            try:
                return manager.session_payload(manager.lookup(request))
            except HTTPException as error:
                if error.status_code != 401:
                    raise
            return {
                "authenticated": False,
                "mode": "local",
                "actor": None,
                "csrf_token": None,
                "expires_at": None,
            }
        if settings.auth_mode == "disabled":
            actor = resolve_actor(
                request,
                x_actor_id=request.headers.get("x-actor-id"),
                x_actor_role=request.headers.get("x-actor-role"),
            )
            return {
                "authenticated": True,
                "mode": "disabled",
                "actor": {"actor_id": actor.actor_id, "role": actor.role},
                "csrf_token": None,
                "expires_at": None,
            }
        return {
            "authenticated": False,
            "mode": "api_key",
            "actor": None,
            "csrf_token": None,
            "expires_at": None,
        }

    @app.post("/api/auth/login")
    def login(request: Request, response: Response, payload: LoginRequest) -> dict[str, object]:
        if manager is None:
            raise HTTPException(404, "browser sign-in is not configured")
        return manager.login(request, response, payload)

    @app.post("/api/auth/logout", status_code=204)
    def logout(request: Request, response: Response) -> None:
        if manager is None:
            raise HTTPException(404, "browser sign-in is not configured")
        actor = manager.resolve(request)
        token = request.cookies[settings.browser_cookie_name]
        manager.store.revoke_browser_session(sha256(token.encode()).hexdigest(), actor.actor_id)
        response.delete_cookie(
            settings.browser_cookie_name,
            path="/",
            httponly=True,
            secure=settings.browser_cookie_secure,
            samesite="strict",
        )
