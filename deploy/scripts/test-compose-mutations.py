#!/usr/bin/env python3
"""Mutation-test the production Compose policy against security regressions."""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any


@dataclass(frozen=True)
class Mutation:
    name: str
    expected_error: str
    apply: Callable[[dict[str, Any]], None]


def fail(message: str) -> None:
    raise SystemExit(f"ERROR: {message}")


def load_validator() -> ModuleType:
    path = Path(__file__).with_name("assert-compose-model.py")
    spec = importlib.util.spec_from_file_location("prahari_compose_assertions", path)
    if spec is None or spec.loader is None:
        fail("could not load the rendered Compose validator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def set_value(path: tuple[str, ...], value: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(model: dict[str, Any]) -> None:
        target: dict[str, Any] = model
        for component in path[:-1]:
            target = target[component]
        target[path[-1]] = value

    return mutate


def append_value(path: tuple[str, ...], value: Any) -> Callable[[dict[str, Any]], None]:
    def mutate(model: dict[str, Any]) -> None:
        target: Any = model
        for component in path:
            target = target[component]
        target.append(value)

    return mutate


def add_api_network(model: dict[str, Any]) -> None:
    networks = model["services"]["api"]["networks"]
    if isinstance(networks, dict):
        networks["unexpected"] = None
    else:
        networks.append("unexpected")


def change_api_model_mount(model: dict[str, Any], *, create: bool = False) -> None:
    mount = next(
        item
        for item in model["services"]["api"]["volumes"]
        if item.get("target") == "/opt/prahari/models"
    )
    if create:
        mount["bind"]["create_host_path"] = True
    else:
        mount["read_only"] = False


def production_mutations() -> tuple[Mutation, ...]:
    return (
        Mutation(
            "internal-only-gateway",
            "web ingress must support host publishing",
            set_value(("networks", "web_ingress", "internal"), True),
        ),
        Mutation(
            "gateway-egress-nat",
            "web ingress must disable outbound masquerading",
            set_value(
                (
                    "networks",
                    "web_ingress",
                    "driver_opts",
                    "com.docker.network.bridge.enable_ip_masquerade",
                ),
                "true",
            ),
        ),
        Mutation(
            "gateway-default-public-bind",
            "web ingress default publishing must bind loopback",
            set_value(
                (
                    "networks",
                    "web_ingress",
                    "driver_opts",
                    "com.docker.network.bridge.host_binding_ipv4",
                ),
                "0.0.0.0",
            ),
        ),
        Mutation(
            "gateway-missing-ingress",
            "dashboard network changed",
            set_value(("services", "dashboard", "networks"), {"app_internal": {}}),
        ),
        Mutation(
            "database-on-ingress",
            "postgres may not join web ingress",
            lambda model: model["services"]["postgres"]["networks"].update(web_ingress={}),
        ),
        Mutation(
            "unsupported-multiple-workers",
            "production API requires --workers=1",
            lambda model: model["services"]["api"]["command"].__setitem__(
                model["services"]["api"]["command"].index("--workers") + 1, "2"
            ),
        ),
        Mutation(
            "unbounded-api-concurrency",
            "production API concurrency must be between 1 and 256",
            lambda model: model["services"]["api"]["command"].__setitem__(
                model["services"]["api"]["command"].index("--limit-concurrency") + 1, "0"
            ),
        ),
        Mutation(
            "production-lab-adapter",
            "production API requires PRAHARI_ACTION_ADAPTER_ENABLED=false",
            set_value(("services", "api", "environment", "PRAHARI_ACTION_ADAPTER_ENABLED"), "true"),
        ),
        Mutation(
            "production-lab-profile-default",
            "production API requires PRAHARI_NETWORK_PROFILE_PATH=",
            set_value(("services", "api", "environment", "PRAHARI_NETWORK_PROFILE_PATH"), ""),
        ),
        Mutation(
            "writable-network-profile",
            "production requires an existing read-only network profile bind mount",
            lambda model: next(
                volume
                for volume in model["services"]["api"]["volumes"]
                if volume["target"] == "/opt/prahari/config/network-profile.json"
            ).update(read_only=False),
        ),
        Mutation(
            "writable-knowledge",
            "production requires an existing read-only knowledge bind mount",
            lambda model: next(
                volume
                for volume in model["services"]["api"]["volumes"]
                if volume["target"] == "/opt/prahari/knowledge"
            ).update(read_only=False),
        ),
        Mutation(
            "privileged-api",
            "api must not be privileged",
            set_value(("services", "api", "privileged"), True),
        ),
        Mutation(
            "added-capability",
            "api must not add Linux capabilities",
            set_value(("services", "api", "cap_add"), ["NET_ADMIN"]),
        ),
        Mutation(
            "host-device",
            "api must not expose host devices",
            set_value(("services", "api", "devices"), ["/dev/kvm:/dev/kvm"]),
        ),
        Mutation(
            "host-network",
            "api must not use host network_mode",
            set_value(("services", "api", "network_mode"), "host"),
        ),
        Mutation(
            "docker-socket",
            "api must not mount the Docker control socket",
            append_value(
                ("services", "api", "volumes"),
                {
                    "type": "bind",
                    "source": "/var/run/docker.sock",
                    "target": "/var/run/docker.sock",
                },
            ),
        ),
        Mutation(
            "floating-image",
            "ollama image must be pinned by sha256 digest",
            set_value(("services", "ollama", "image"), "ollama/ollama:latest"),
        ),
        Mutation(
            "writable-root",
            "ollama root filesystem must be read-only",
            set_value(("services", "ollama", "read_only"), False),
        ),
        Mutation(
            "root-ollama",
            "ollama must run as 10001:10001",
            set_value(("services", "ollama", "user"), "0:0"),
        ),
        Mutation(
            "external-network",
            "network copilot_internal must be internal",
            set_value(("networks", "copilot_internal", "internal"), False),
        ),
        Mutation(
            "api-host-port",
            "only dashboard may publish a host port",
            set_value(
                ("services", "api", "ports"),
                [{"target": 8080, "published": "18080", "host_ip": "127.0.0.1"}],
            ),
        ),
        Mutation(
            "direct-secret",
            "api exposes secret-like value PRAHARI_API_TOKEN directly",
            set_value(
                ("services", "api", "environment", "PRAHARI_API_TOKEN"),
                "not-a-secret-but-still-forbidden",
            ),
        ),
        Mutation(
            "extra-api-network",
            "production API may only join app and copilot internal networks",
            add_api_network,
        ),
        Mutation(
            "published-ollama",
            "only dashboard may publish a host port",
            set_value(
                ("services", "ollama", "ports"),
                [{"target": 11434, "published": "11434", "host_ip": "127.0.0.1"}],
            ),
        ),
        Mutation(
            "ollama-cloud-enabled",
            "Ollama cloud features must be disabled",
            set_value(("services", "ollama", "environment", "OLLAMA_NO_CLOUD"), "false"),
        ),
        Mutation(
            "ollama-pruning-enabled",
            "Ollama must not mutate the read-only offline model store",
            set_value(("services", "ollama", "environment", "OLLAMA_NOPRUNE"), "false"),
        ),
        Mutation(
            "ollama-address-mismatch",
            "Ollama static address must match the API allowlist",
            set_value(
                ("services", "ollama", "networks", "copilot_internal", "ipv4_address"),
                "172.30.250.11",
            ),
        ),
        Mutation(
            "model-path-traversal",
            "PRAHARI_PREDICTIVE_MODEL_MANIFEST_PATH must remain inside",
            set_value(
                (
                    "services",
                    "api",
                    "environment",
                    "PRAHARI_PREDICTIVE_MODEL_MANIFEST_PATH",
                ),
                "/opt/prahari/models/../unapproved/manifest.json",
            ),
        ),
        Mutation(
            "writable-model-mount",
            "model mount must be read-only",
            change_api_model_mount,
        ),
        Mutation(
            "auto-created-model-directory",
            "missing model directory must fail rather than be auto-created",
            lambda model: change_api_model_mount(model, create=True),
        ),
        Mutation(
            "wrong-model-preflight",
            "Ollama model preflight must check the exact configured model",
            set_value(
                ("services", "ollama-model-check", "command"),
                ["show", "different:latest"],
            ),
        ),
        Mutation(
            "api-does-not-wait-for-model",
            "API must wait for the exact local model preflight",
            set_value(
                ("services", "api", "depends_on", "ollama-model-check", "condition"),
                "service_started",
            ),
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("model", type=Path)
    args = parser.parse_args()
    try:
        baseline = json.loads(args.model.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(f"cannot read rendered production Compose JSON: {error}")
    validator = load_validator()
    try:
        validator.validate(copy.deepcopy(baseline), production=True)
    except SystemExit as error:
        fail(f"baseline production model is invalid: {error}")

    mutations = production_mutations()
    for mutation in mutations:
        altered = copy.deepcopy(baseline)
        mutation.apply(altered)
        try:
            validator.validate(altered, production=True)
        except SystemExit as error:
            if mutation.expected_error not in str(error):
                fail(
                    f"mutation {mutation.name} failed for the wrong reason: {error}; "
                    f"expected {mutation.expected_error!r}"
                )
        else:
            fail(f"security mutation was accepted: {mutation.name}")
    print(f"Production Compose mutation suite passed ({len(mutations)} regressions rejected).")


if __name__ == "__main__":
    main()
