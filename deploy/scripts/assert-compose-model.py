#!/usr/bin/env python3
"""Fail closed when the rendered Compose model loses a security invariant."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

DIGEST_IMAGE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
EXPECTED_BASE_SERVICES = {
    "postgres",
    "migrate",
    "grant-app-role",
    "api",
    "dashboard",
}


def fail(message: str) -> None:
    raise SystemExit(f"ERROR: {message}")


def require(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def _networks(service: dict[str, Any]) -> set[str]:
    value = service.get("networks", {})
    return set(value if isinstance(value, dict) else value)


def _positive_number(value: Any) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _secret_names(service: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for secret in service.get("secrets", []):
        if isinstance(secret, str):
            names.add(secret)
        elif isinstance(secret, dict) and isinstance(secret.get("source"), str):
            names.add(secret["source"])
    return names


def _require_locked_down(name: str, service: dict[str, Any], user: str) -> None:
    require(service.get("read_only") is True, f"{name} root filesystem must be read-only")
    require(service.get("user") == user, f"{name} must run as {user}")
    require("ALL" in service.get("cap_drop", []), f"{name} must drop all capabilities")
    require(
        "no-new-privileges:true" in service.get("security_opt", []),
        f"{name} must set no-new-privileges",
    )


def _require_model_path(environment: dict[str, Any], key: str) -> None:
    raw = environment.get(key)
    require(isinstance(raw, str), f"production API requires {key}")
    prefix = "/opt/prahari/models/"
    require(raw.startswith(prefix), f"{key} must use the read-only model mount")
    relative = raw.removeprefix(prefix)
    require(
        bool(relative) and not relative.startswith("/") and ".." not in relative.split("/"),
        f"{key} must remain inside the read-only model mount",
    )


def _security_invariants(name: str, service: dict[str, Any]) -> None:
    require(service.get("pull_policy") == "never", f"{name} must set pull_policy=never")
    image = service.get("image", "")
    require(bool(DIGEST_IMAGE.fullmatch(image)), f"{name} image must be pinned by sha256 digest")
    require(_positive_number(service.get("mem_limit")), f"{name} must have a memory limit")
    require(_positive_number(service.get("cpus")), f"{name} must have a CPU limit")
    require(_positive_number(service.get("pids_limit")), f"{name} must have a PID limit")
    require(service.get("privileged") is not True, f"{name} must not be privileged")
    require(not service.get("cap_add"), f"{name} must not add Linux capabilities")
    require(not service.get("devices"), f"{name} must not expose host devices")
    for host_mode in ("network_mode", "pid", "ipc", "uts", "cgroup"):
        require(service.get(host_mode) != "host", f"{name} must not use host {host_mode}")
    for volume in service.get("volumes", []):
        if isinstance(volume, str):
            source, _, target = volume.partition(":")
        else:
            source = str(volume.get("source", ""))
            target = str(volume.get("target", ""))
        require(
            "docker.sock" not in source and "docker.sock" not in target,
            f"{name} must not mount the Docker control socket",
        )

    environment = service.get("environment", {})
    require(isinstance(environment, dict), f"{name} environment must render as a mapping")
    for key in environment:
        upper = key.upper()
        if any(marker in upper for marker in ("PASSWORD", "TOKEN", "API_KEY", "DATABASE_URL")):
            require(upper.endswith("_FILE"), f"{name} exposes secret-like value {key} directly")


def validate(model: dict[str, Any], *, production: bool) -> None:
    services = model.get("services", {})
    require(isinstance(services, dict), "Compose services must be a mapping")
    require(EXPECTED_BASE_SERVICES <= set(services), "required base services are missing")
    for name, service in services.items():
        _security_invariants(name, service)

    for network_name, network in model.get("networks", {}).items():
        if network_name == "web_ingress":
            require(network.get("internal") is not True, "web ingress must support host publishing")
            require(network.get("driver") == "bridge", "web ingress must use bridge networking")
            options = network.get("driver_opts", {})
            require(
                options.get("com.docker.network.bridge.enable_ip_masquerade") == "false",
                "web ingress must disable outbound masquerading",
            )
            require(
                options.get("com.docker.network.bridge.host_binding_ipv4") == "127.0.0.1",
                "web ingress default publishing must bind loopback",
            )
            continue
        require(network.get("internal") is True, f"network {network_name} must be internal")
    require("web_ingress" in model.get("networks", {}), "web ingress network is required")
    require(
        _networks(services["dashboard"]) == {"app_internal", "web_ingress"},
        "dashboard network changed",
    )
    for name, service in services.items():
        if name != "dashboard":
            require("web_ingress" not in _networks(service), f"{name} may not join web ingress")

    port_services = {name for name, service in services.items() if service.get("ports")}
    require(port_services == {"dashboard"}, "only dashboard may publish a host port")
    dashboard_ports = services["dashboard"]["ports"]
    require(len(dashboard_ports) == 1, "dashboard must publish exactly one port")
    require(
        dashboard_ports[0].get("host_ip") == "127.0.0.1",
        "dashboard must remain bound to IPv4 loopback",
    )

    for name in ("api", "dashboard"):
        service = services[name]
        require(service.get("read_only") is True, f"{name} root filesystem must be read-only")
        require("ALL" in service.get("cap_drop", []), f"{name} must drop all capabilities")
        require(
            "no-new-privileges:true" in service.get("security_opt", []),
            f"{name} must set no-new-privileges",
        )
        require("healthcheck" in service, f"{name} must define a healthcheck")
    require(services["api"].get("user") == "10001:10001", "API must run as UID/GID 10001")
    require("healthcheck" in services["postgres"], "PostgreSQL must define a healthcheck")
    _require_locked_down("migrate", services["migrate"], "10001:10001")
    _require_locked_down("grant-app-role", services["grant-app-role"], "postgres")
    _require_locked_down("api", services["api"], "10001:10001")
    _require_locked_down("dashboard", services["dashboard"], "101:101")

    expected_service_secrets = {
        "postgres": {
            "postgres_bootstrap_password",
            "prahari_migrator_password",
            "prahari_app_password",
        },
        "migrate": {"prahari_migrator_database_url"},
        "grant-app-role": {"postgres_bootstrap_password"},
        "api": {"prahari_app_database_url", "prahari_api_keys_json"},
        "dashboard": set(),
    }
    if production:
        expected_service_secrets["api"].add("prahari_browser_users_json")
    for name, expected_secrets in expected_service_secrets.items():
        require(
            _secret_names(services[name]) == expected_secrets,
            f"{name} secret exposure does not match least privilege",
        )

    secrets = model.get("secrets", {})
    require(secrets, "Compose must declare file-backed secrets")
    for name, secret in secrets.items():
        require(set(secret) <= {"name", "file"}, f"secret {name} must be file-backed")
        require(bool(secret.get("file")), f"secret {name} is missing its file path")

    if not production:
        require(_networks(services["api"]) == {"app_internal"}, "base API network changed")
        return

    require({"ollama", "ollama-model-check"} <= set(services), "copilot services are missing")
    api_command = services["api"].get("command", [])
    require(isinstance(api_command, list), "production API command must be explicit")
    for option, expected_value in (("--workers", "1"), ("--timeout-keep-alive", "5")):
        require(api_command.count(option) == 1, f"production API requires {option}")
        index = api_command.index(option) + 1
        require(
            index < len(api_command) and str(api_command[index]) == expected_value,
            f"production API requires {option}={expected_value}",
        )
    require(
        api_command.count("--limit-concurrency") == 1, "production API requires bounded concurrency"
    )
    concurrency_index = api_command.index("--limit-concurrency") + 1
    concurrency = (
        str(api_command[concurrency_index]) if concurrency_index < len(api_command) else ""
    )
    require(
        concurrency.isdigit() and 1 <= int(concurrency) <= 256,
        "production API concurrency must be between 1 and 256",
    )
    api_environment = services["api"]["environment"]
    expected = {
        "PRAHARI_ENVIRONMENT": "production",
        "PRAHARI_NETWORK_PROFILE_PATH": "/opt/prahari/config/network-profile.json",
        "PRAHARI_KNOWLEDGE_ROOT": "/opt/prahari/knowledge",
        "PRAHARI_ACTION_ADAPTER_ENABLED": "false",
        "PRAHARI_DATA_MODE": "live",
        "PRAHARI_AUTH_MODE": "api_key",
        "PRAHARI_BROWSER_AUTH_ENABLED": "true",
        "PRAHARI_BROWSER_USERS_FILE": "/run/secrets/prahari_browser_users_json",
        "PRAHARI_MODEL_APPROVAL_STATUS": "approved",
        "PRAHARI_COPILOT_ENABLED": "true",
        "PRAHARI_AUTO_CREATE_SCHEMA": "false",
        "PRAHARI_TRAFFIC_DEMO_RUN_ENABLED": "false",
    }
    for key, value in expected.items():
        require(api_environment.get(key) == value, f"production API requires {key}={value}")
    profile_mounts = [
        mount
        for mount in services["api"].get("volumes", [])
        if mount.get("target") == "/opt/prahari/config/network-profile.json"
    ]
    require(
        len(profile_mounts) == 1
        and profile_mounts[0].get("read_only") is True
        and profile_mounts[0].get("type") == "bind"
        and profile_mounts[0].get("bind", {}).get("create_host_path") is False,
        "production requires an existing read-only network profile bind mount",
    )
    knowledge_mounts = [
        mount
        for mount in services["api"].get("volumes", [])
        if mount.get("target") == "/opt/prahari/knowledge"
    ]
    require(
        len(knowledge_mounts) == 1
        and knowledge_mounts[0].get("read_only") is True
        and knowledge_mounts[0].get("type") == "bind"
        and knowledge_mounts[0].get("bind", {}).get("create_host_path") is False,
        "production requires an existing read-only knowledge bind mount",
    )
    require(
        str(api_environment.get("PRAHARI_BROWSER_ORIGIN", "")).startswith("https://"),
        "production browser sessions require an HTTPS origin",
    )
    require(
        api_environment.get("PRAHARI_OLLAMA_URL")
        == f"http://{api_environment.get('PRAHARI_OLLAMA_ALLOWED_PRIVATE_IP')}:11434",
        "Ollama URL must match the exact allow-listed private IP",
    )
    require(
        _networks(services["api"]) == {"app_internal", "copilot_internal"},
        "production API may only join app and copilot internal networks",
    )
    require(_networks(services["ollama"]) == {"copilot_internal"}, "Ollama network changed")
    require(_networks(services["postgres"]) == {"app_internal"}, "database network changed")
    require(
        _networks(services["dashboard"]) == {"app_internal", "web_ingress"},
        "dashboard network changed",
    )
    require(not services["ollama"].get("ports"), "Ollama must not publish a host port")
    require(
        not services["ollama-model-check"].get("ports"),
        "Ollama preflight must not publish a host port",
    )
    ollama_network = services["ollama"].get("networks", {}).get("copilot_internal", {})
    require(
        ollama_network.get("ipv4_address")
        == api_environment.get("PRAHARI_OLLAMA_ALLOWED_PRIVATE_IP"),
        "Ollama static address must match the API allowlist",
    )

    for name in ("ollama", "ollama-model-check"):
        _require_locked_down(name, services[name], "10001:10001")
        require(not _secret_names(services[name]), f"{name} must not receive any secrets")
    require("healthcheck" in services["ollama"], "Ollama must define a healthcheck")
    ollama_environment = services["ollama"]["environment"]
    require(
        ollama_environment.get("HOME") == "/home/ollama",
        "Ollama must use its non-root writable HOME tmpfs",
    )
    require(
        services["ollama-model-check"]["environment"].get("HOME") == "/home/ollama",
        "Ollama model preflight must use a non-root HOME tmpfs",
    )
    require(
        any(
            str(entry).startswith("/home/ollama:") for entry in services["ollama"].get("tmpfs", [])
        ),
        "Ollama requires a bounded writable HOME tmpfs",
    )
    require(
        not any("/root" in str(entry) for entry in services["ollama"].get("tmpfs", [])),
        "Ollama must not provision a root home",
    )
    require(services["ollama"].get("init") is True, "Ollama must use an init process")
    require(
        ollama_environment.get("OLLAMA_NO_CLOUD") == "true",
        "Ollama cloud features must be disabled",
    )
    require(
        ollama_environment.get("OLLAMA_NOPRUNE") == "true",
        "Ollama must not mutate the read-only offline model store during startup",
    )
    require(
        services["ollama-model-check"]["environment"].get("OLLAMA_NO_CLOUD") == "true",
        "Ollama model preflight must keep cloud features disabled",
    )
    require(
        services["ollama-model-check"].get("entrypoint") == ["/bin/ollama"],
        "Ollama model preflight entrypoint changed",
    )
    require(
        services["ollama-model-check"].get("command")
        == ["show", api_environment.get("PRAHARI_OLLAMA_MODEL")],
        "Ollama model preflight must check the exact configured model",
    )

    for key in (
        "PRAHARI_MODEL_ARTIFACT_PATH",
        "PRAHARI_MODEL_MANIFEST_PATH",
        "PRAHARI_PREDICTIVE_MODEL_MANIFEST_PATH",
    ):
        _require_model_path(api_environment, key)

    api_model_mounts = [
        volume
        for volume in services["api"].get("volumes", [])
        if volume.get("target") == "/opt/prahari/models"
    ]
    require(len(api_model_mounts) == 1, "API requires one model-artifact mount")
    require(api_model_mounts[0].get("read_only") is True, "model mount must be read-only")
    require(
        api_model_mounts[0].get("bind", {}).get("create_host_path") is False,
        "missing model directory must fail rather than be auto-created",
    )

    ollama_mounts = [
        volume
        for volume in services["ollama"].get("volumes", [])
        if volume.get("target") == "/opt/ollama/models"
    ]
    require(len(ollama_mounts) == 1, "Ollama requires one offline model mount")
    require(ollama_mounts[0].get("read_only") is True, "Ollama model mount must be read-only")
    require(
        ollama_mounts[0].get("bind", {}).get("create_host_path") is False,
        "missing Ollama model directory must fail rather than be auto-created",
    )
    dependency = services["api"].get("depends_on", {}).get("ollama-model-check", {})
    require(
        dependency.get("condition") == "service_completed_successfully",
        "API must wait for the exact local model preflight",
    )
    require(
        services["ollama-model-check"].get("depends_on", {}).get("ollama", {}).get("condition")
        == "service_healthy",
        "Ollama model preflight must wait for the local server healthcheck",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("model", type=Path)
    parser.add_argument("--production", action="store_true")
    args = parser.parse_args()
    try:
        model = json.loads(args.model.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(f"cannot read rendered Compose JSON: {error}")
    validate(model, production=args.production)
    print("Rendered Compose security invariants are valid.")


if __name__ == "__main__":
    main()
