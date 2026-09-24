#!/usr/bin/env python3
"""Rootless stdlib HTTP agent for one reversible PRAHARI lab action."""

from __future__ import annotations

import hmac
import json
import os
import re
import subprocess
import threading
import time
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Literal

INCIDENT_ID = "inc-branch-01-primary"
PLAYBOOK_ID = "backup-path-shift-v1"
PRIMARY_COST = "10"
SHIFT_COST = "500"
DRY_RUN_TTL_SECONDS = 300
MAX_BODY_BYTES = 4096
ACTOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,99}$")
DryRunKey = tuple[str, str, str, str]
ActionStatus = Literal["dry_run_passed", "applied", "rolled_back", "blocked", "validation_failed"]

dry_runs: dict[DryRunKey, float] = {}
state_lock = threading.Lock()
action_lock = threading.Lock()


def token() -> str:
    path = Path(os.environ.get("PRAHARI_ACTION_AGENT_TOKEN_FILE", ""))
    if not path.is_file():
        raise RuntimeError("PRAHARI_ACTION_AGENT_TOKEN_FILE must identify a token file")
    value = path.read_text(encoding="utf-8").strip()
    if len(value) < 32:
        raise RuntimeError("action-agent token must contain at least 32 characters")
    return value


AUTH_TOKEN = token()


class Handler(BaseHTTPRequestHandler):
    server_version = "PrahariActionAgent/1.0"

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        if self.path != "/health":
            self._send(HTTPStatus.NOT_FOUND, {"detail": "not found"})
            return
        self._send(HTTPStatus.OK, {"status": "healthy"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        request = self._request()
        if request is None:
            return
        try:
            if self.path == "/v1/actions/backup-path-shift/dry-run":
                response = dry_run(request)
            elif self.path == "/v1/actions/backup-path-shift/apply":
                response = apply(request)
            elif self.path == "/v1/actions/backup-path-shift/rollback":
                response = rollback(request)
            else:
                self._send(HTTPStatus.NOT_FOUND, {"detail": "not found"})
                return
        except RuntimeError:
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"detail": "action agent failed safely"})
            return
        self._send(HTTPStatus.OK, response)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(supplied, AUTH_TOKEN):
            self._send(HTTPStatus.UNAUTHORIZED, {"detail": "invalid token"})
            return False
        return True

    def _request(self) -> dict[str, str] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not 1 <= length <= MAX_BODY_BYTES:
            self._send(HTTPStatus.BAD_REQUEST, {"detail": "invalid request size"})
            return None
        try:
            value = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(HTTPStatus.BAD_REQUEST, {"detail": "invalid JSON"})
            return None
        expected = {
            "operation_id",
            "incident_id",
            "playbook_id",
            "actor_id",
            "prediction_id",
        }
        if not isinstance(value, dict) or set(value) != expected:
            self._send(HTTPStatus.UNPROCESSABLE_ENTITY, {"detail": "invalid fields"})
            return None
        if (
            value["incident_id"] != INCIDENT_ID
            or value["playbook_id"] != PLAYBOOK_ID
            or not isinstance(value["operation_id"], str)
            or ACTOR.fullmatch(value["operation_id"]) is None
            or not isinstance(value["actor_id"], str)
            or ACTOR.fullmatch(value["actor_id"]) is None
            or not isinstance(value["prediction_id"], str)
            or ACTOR.fullmatch(value["prediction_id"]) is None
        ):
            self._send(HTTPStatus.UNPROCESSABLE_ENTITY, {"detail": "unsupported action"})
            return None
        return value

    def _send(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def dry_run(request: dict[str, str]) -> dict[str, object]:
    with action_lock:
        return _dry_run(request)


def _dry_run(request: dict[str, str]) -> dict[str, object]:
    try:
        require_lab()
        if route() != "primary":
            return result(request, "blocked", "Primary path is not selected; no action taken.")
        require_backup_neighbors()
        ping()
    except RuntimeError as error:
        return result(request, "blocked", f"Dry-run prerequisite failed: {error}")
    with state_lock:
        dry_runs[key(request)] = time.monotonic() + DRY_RUN_TTL_SECONDS
    return result(
        request,
        "dry_run_passed",
        "Primary route, backup adjacencies, and end-to-end reachability are healthy.",
    )


def apply(request: dict[str, str]) -> dict[str, object]:
    with action_lock:
        return _apply(request)


def _apply(request: dict[str, str]) -> dict[str, object]:
    with state_lock:
        expires_at = dry_runs.pop(key(request), 0)
    if expires_at < time.monotonic():
        return result(
            request,
            "blocked",
            "A fresh successful dry-run by this operator is required.",
        )
    try:
        set_primary_cost(SHIFT_COST)
        wait_for_route("backup")
        ping()
    except RuntimeError as error:
        rollback_error = ""
        try:
            set_primary_cost(PRIMARY_COST)
            wait_for_route("primary")
        except RuntimeError as rollback_failure:
            rollback_error = f" Automatic rollback also failed: {rollback_failure}"
        return result(
            request,
            "validation_failed",
            f"Post-change validation failed: {error}.{rollback_error}",
        )
    return result(
        request,
        "applied",
        "Traffic shifted to backup and reachability validation passed.",
    )


def rollback(request: dict[str, str]) -> dict[str, object]:
    with action_lock:
        return _rollback(request)


def _rollback(request: dict[str, str]) -> dict[str, object]:
    try:
        require_lab()
        set_primary_cost(PRIMARY_COST)
        wait_for_route("primary")
        ping()
    except RuntimeError as error:
        return result(request, "validation_failed", f"Rollback validation failed: {error}")
    return result(request, "rolled_back", "Primary routing policy restored and validated.")


def key(request: dict[str, str]) -> DryRunKey:
    return (
        request["incident_id"],
        request["playbook_id"],
        request["actor_id"],
        request["prediction_id"],
    )


def run(*arguments: str, timeout: float = 10) -> str:
    try:
        completed = subprocess.run(
            list(arguments),
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"fixed command failed: {arguments[0]}") from error
    return completed.stdout


def require_lab() -> None:
    for node in ("branch-edge", "dc-edge", "pe-backup", "branch-client"):
        container = f"clab-prahari-{node}"
        label = run(
            "docker",
            "inspect",
            "--format",
            '{{index .Config.Labels "containerlab"}}',
            container,
        ).strip()
        running = run("docker", "inspect", "--format", "{{.State.Running}}", container).strip()
        if label != "prahari" or running != "true":
            raise RuntimeError(f"expected lab node is not healthy: {node}")


def require_backup_neighbors() -> None:
    branch = run(
        "docker", "exec", "clab-prahari-branch-edge", "vtysh", "-c", "show ip ospf neighbor"
    )
    dc = run("docker", "exec", "clab-prahari-dc-edge", "vtysh", "-c", "show ip ospf neighbor")
    if not _neighbor_is_full(branch, "10.20.0.2"):
        raise RuntimeError("branch backup OSPF adjacency is not Full")
    if not _neighbor_is_full(dc, "10.20.1.1"):
        raise RuntimeError("DC backup OSPF adjacency is not Full")


def _neighbor_is_full(output: str, neighbor_address: str) -> bool:
    # FRR reports router ID first; Address is five columns from the end,
    # both with and without the optional Up Time column.
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 9 and fields[2].startswith("Full/") and fields[-5] == neighbor_address:
            return True
    return False


def route() -> str:
    output = run(
        "docker", "exec", "clab-prahari-branch-edge", "vtysh", "-c", "show ip route 172.16.20.0/24"
    )
    if "via eth1" in output:
        return "primary"
    if "via eth2" in output:
        return "backup"
    return "unknown"


def set_primary_cost(cost: str) -> None:
    if cost not in {PRIMARY_COST, SHIFT_COST}:
        raise RuntimeError("refusing non-allow-listed OSPF cost")
    for container in ("clab-prahari-branch-edge", "clab-prahari-dc-edge"):
        run(
            "docker",
            "exec",
            container,
            "vtysh",
            "-c",
            "configure terminal",
            "-c",
            "interface eth1",
            "-c",
            f"ip ospf cost {cost}",
        )


def wait_for_route(expected: str) -> None:
    for _ in range(20):
        if route() == expected:
            return
        time.sleep(1)
    raise RuntimeError(f"route did not converge to {expected}")


def ping() -> None:
    output = run(
        "docker",
        "exec",
        "clab-prahari-branch-client",
        "ping",
        "-n",
        "-c",
        "3",
        "-W",
        "2",
        "172.16.20.2",
        timeout=10,
    )
    if "3 received, 0% packet loss" not in output:
        raise RuntimeError("end-to-end validation probe did not pass")


def result(
    request: dict[str, str], action_status: ActionStatus, message: str
) -> dict[str, object]:
    return {
        "operation_id": request["operation_id"],
        "status": action_status,
        "message": message,
        "observed_route": safe_route(),
        "checked_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


def safe_route() -> str:
    try:
        return route()
    except RuntimeError:
        return "unknown"


def main() -> None:
    address = os.environ.get("PRAHARI_ACTION_AGENT_BIND", "172.31.255.1")
    server = ThreadingHTTPServer((address, 8091), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
