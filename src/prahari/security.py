"""Local RBAC guardrails for API and collector access."""

from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from prahari.settings import Settings


@dataclass(frozen=True)
class Actor:
    actor_id: str
    role: str


def resolve_actor(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_actor_id: Annotated[str | None, Header()] = None,
    x_actor_role: Annotated[str | None, Header()] = None,
) -> Actor:
    """Resolve the caller without trusting identity headers in API-key mode."""

    settings: Settings = request.app.state.settings
    if settings.auth_mode == "disabled":
        return Actor(
            actor_id=x_actor_id or "local-operator",
            role=x_actor_role or "operator",
        )

    if authorization is None and settings.browser_auth_enabled:
        return request.app.state.browser_auth.resolve(request)
    token = _bearer_token(authorization)
    for expected_token, principal in settings.api_key_principals.items():
        if hmac.compare_digest(token, expected_token):
            return Actor(actor_id=principal.actor_id, role=principal.role)
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API credential")


def require_roles(*allowed_roles: str) -> Callable[[Actor], Actor]:
    """Create a FastAPI dependency that enforces a least-privilege role set."""

    def dependency(actor: Annotated[Actor, Depends(resolve_actor)]) -> Actor:
        if actor.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role {actor.role!r} cannot perform this operation",
            )
        return actor

    return dependency


def effective_actor_id(settings: Settings, actor: Actor, requested_actor_id: str) -> str:
    """Prevent a request body from impersonating an API-key principal."""

    if settings.auth_mode == "api_key" and requested_actor_id != actor.actor_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="request actor_id does not match the authenticated principal",
        )
    return actor.actor_id if settings.auth_mode == "api_key" else requested_actor_id


def _bearer_token(authorization: str | None) -> str:
    prefix = "Bearer "
    if authorization is None or not authorization.startswith(prefix):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer authentication is required",
        )
    token = authorization.removeprefix(prefix)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="empty API credential")
    return token
