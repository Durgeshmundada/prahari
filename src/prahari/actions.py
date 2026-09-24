"""Narrow client and contracts for the isolated PRAHARI lab action agent."""

from __future__ import annotations

import ipaddress
from pathlib import Path
from urllib.parse import urlparse

import httpx
from pydantic import Field

from prahari.contracts import ActionAgentReceipt, ContractModel


class ActionAgentError(RuntimeError):
    pass


class AgentActionRequest(ContractModel):
    operation_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:@-]+$")
    incident_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:@-]+$")
    playbook_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:@-]+$")
    actor_id: str = Field(min_length=1, max_length=100)
    prediction_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:@-]+$")


class AgentActionResponse(ActionAgentReceipt):
    """Validated original reply from the controlled-lab adapter."""


class LabActionAgentClient:
    """Authenticated client restricted to one exact private action-agent IP."""

    def __init__(
        self,
        base_url: str,
        token_file: Path,
        *,
        allowed_private_ip: str,
        timeout_seconds: float = 30,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        parsed = urlparse(base_url)
        try:
            address = ipaddress.ip_address(parsed.hostname or "")
        except ValueError as error:
            raise ValueError("action-agent URL must use an explicit IP address") from error
        if (
            parsed.scheme != "http"
            or str(address) != allowed_private_ip
            or not address.is_private
            or parsed.path not in {"", "/"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("action-agent URL must use the exact allowed private HTTP origin")
        token = token_file.read_text(encoding="utf-8").strip()
        if len(token) < 32:
            raise ValueError("action-agent token file must contain at least 32 characters")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout_seconds,
            transport=transport,
            trust_env=False,
        )

    def dry_run(self, request: AgentActionRequest) -> AgentActionResponse:
        return self._request("/v1/actions/backup-path-shift/dry-run", request)

    def close(self) -> None:
        self._client.close()

    def apply(self, request: AgentActionRequest) -> AgentActionResponse:
        return self._request("/v1/actions/backup-path-shift/apply", request)

    def rollback(self, request: AgentActionRequest) -> AgentActionResponse:
        return self._request("/v1/actions/backup-path-shift/rollback", request)

    def _request(self, path: str, request: AgentActionRequest) -> AgentActionResponse:
        try:
            response = self._client.post(path, json=request.model_dump())
            response.raise_for_status()
            receipt = AgentActionResponse.model_validate(response.json())
            if receipt.operation_id != request.operation_id:
                raise ActionAgentError(
                    "action agent receipt does not match the requested operation"
                )
            return receipt
        except (httpx.HTTPError, ValueError) as error:
            raise ActionAgentError(f"action agent request failed: {error}") from error
