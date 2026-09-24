#!/usr/bin/env python3
"""Enforce and attest PRAHARI's exact WSL2/Containerlab egress boundary.

The default command is deliberately non-mutating. Firewall changes require
root *and* an exact confirmation phrase. The policy is data, but all nftables
identifiers and rule shapes are fixed in this program so a modified policy
cannot inject nft syntax.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SECURITY_DIR = Path(__file__).resolve().parent
TRUSTED_TOOL_PATH = Path("/usr/local/libexec/prahari/zero_egress.py")
TRUSTED_POLICY_PATH = Path("/etc/prahari/prahari-lab-zero-egress-v1.json")
TRUSTED_STATE_DIR = Path("/var/lib/prahari/security")
TRUSTED_EVIDENCE_DIR = Path("/var/lib/prahari/security/evidence")
SYSTEMD_SYSTEM_DIR = Path("/etc/systemd/system")
SYSTEMD_USER_DIR = Path("/etc/systemd/user")
SYSCTL_PATH = Path("/etc/sysctl.d/90-prahari-zero-egress.conf")
MODULES_LOAD_PATH = Path("/etc/modules-load.d/90-prahari-zero-egress.conf")
EARLY_UNIT_PATH = SYSTEMD_SYSTEM_DIR / "prahari-zero-egress-early.service"
REPAIR_UNIT_PATH = SYSTEMD_SYSTEM_DIR / "prahari-zero-egress.service"
TIMER_UNIT_PATH = SYSTEMD_SYSTEM_DIR / "prahari-zero-egress.timer"
SERVICE_USER_PATH = Path("/etc/prahari/zero-egress-service-user")
INSTALL_MANIFEST_PATH = TRUSTED_STATE_DIR / "install-manifest.json"
ATTESTATION_KEY_PATH = Path("/etc/prahari/zero-egress-attestation-ed25519.pem")
ATTESTATION_PUBLIC_KEY_PATH = TRUSTED_STATE_DIR / "zero-egress-attestation-ed25519.pub.pem"
IS_TRUSTED_INSTALL = Path(__file__).resolve() == TRUSTED_TOOL_PATH
DEFAULT_POLICY = (
    TRUSTED_POLICY_PATH
    if IS_TRUSTED_INSTALL
    else SECURITY_DIR / "prahari-lab-zero-egress-v1.json"
)
DEFAULT_EVIDENCE_DIR = TRUSTED_EVIDENCE_DIR if IS_TRUSTED_INSTALL else SECURITY_DIR / "evidence"
USER_DROPIN_NAME = "50-prahari-zero-egress.conf"
DOCKER_DROPIN_SUFFIX = f"docker.service.d/{USER_DROPIN_NAME}"
EXPECTED_SCHEMA = "prahari-zero-egress-policy-v1"
EXPECTED_POLICY_ID = "prahari-lab-zero-egress"
TABLE_NAME = "prahari_zero_egress"
APPLY_CONFIRMATION = "APPLY-PRAHARI-ZERO-EGRESS"
REPAIR_CONFIRMATION = "REPAIR-PRAHARI-ZERO-EGRESS"
REMOVE_CONFIRMATION = "REMOVE-PRAHARI-ZERO-EGRESS"
INSTALL_CONFIRMATION = "INSTALL-PRAHARI-ZERO-EGRESS"
MIGRATE_CONFIRMATION = "MIGRATE-PRAHARI-ZERO-EGRESS"
UNINSTALL_CONFIRMATION = "UNINSTALL-PRAHARI-ZERO-EGRESS"
SAFE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
SAFE_INTERFACE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,14}$")
SHA256_ID = re.compile(r"^sha256:[a-f0-9]{64}$")
PINNED_IMAGE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9./_-]*(?::[a-zA-Z0-9_.-]+)?@sha256:[a-f0-9]{64}$")
EXPECTED_CONTAINER_ADDRESSES = {
    "clab-prahari-branch-client": "172.31.255.10",
    "clab-prahari-branch-edge": "172.31.255.11",
    "clab-prahari-pe-primary": "172.31.255.12",
    "clab-prahari-core": "172.31.255.13",
    "clab-prahari-pe-secondary": "172.31.255.14",
    "clab-prahari-pe-backup": "172.31.255.15",
    "clab-prahari-dc-edge": "172.31.255.16",
    "clab-prahari-app-server": "172.31.255.17",
    "clab-prahari-noc-api": "172.31.255.18",
    "prahari-ollama": "172.31.255.19",
}
EXPECTED_CONTAINER_INTERFACES = {
    "clab-prahari-branch-client": {
        "eth0": ["172.31.255.10/24"],
        "eth1": ["172.16.10.2/24"],
    },
    "clab-prahari-branch-edge": {
        "eth0": ["172.31.255.11/24"],
        "eth1": ["10.10.0.1/30"],
        "eth2": ["10.20.0.1/30"],
        "eth3": ["172.16.10.1/24"],
    },
    "clab-prahari-pe-primary": {
        "eth0": ["172.31.255.12/24"],
        "eth1": ["10.10.0.2/30"],
        "eth2": ["10.10.1.1/30"],
    },
    "clab-prahari-core": {
        "eth0": ["172.31.255.13/24"],
        "eth1": ["10.10.1.2/30"],
        "eth2": ["10.10.2.1/30"],
    },
    "clab-prahari-pe-secondary": {
        "eth0": ["172.31.255.14/24"],
        "eth1": ["10.10.2.2/30"],
        "eth2": ["10.10.3.1/30"],
    },
    "clab-prahari-pe-backup": {
        "eth0": ["172.31.255.15/24"],
        "eth1": ["10.20.0.2/30"],
        "eth2": ["10.20.1.1/30"],
    },
    "clab-prahari-dc-edge": {
        "eth0": ["172.31.255.16/24"],
        "eth1": ["10.10.3.2/30"],
        "eth2": ["10.20.1.2/30"],
        "eth3": ["172.16.20.1/24"],
    },
    "clab-prahari-app-server": {
        "eth0": ["172.31.255.17/24"],
        "eth1": ["172.16.20.2/24"],
    },
    "clab-prahari-noc-api": {"eth0": ["172.31.255.18/24"]},
    "prahari-ollama": {"eth0": ["172.31.255.19/24"]},
}
EXPECTED_DEFAULT_GATEWAYS = {
    "clab-prahari-branch-client": "172.16.10.1",
    "clab-prahari-app-server": "172.16.20.1",
    **{
        name: "172.31.255.1"
        for name in EXPECTED_CONTAINER_ADDRESSES
        if name not in {"clab-prahari-branch-client", "clab-prahari-app-server"}
    },
}
EXPECTED_PRIVILEGE = {
    name: name != "prahari-ollama" for name in EXPECTED_CONTAINER_ADDRESSES
}
FRR_IMAGE = (
    "quay.io/frrouting/frr@sha256:"
    "e47e67bd612030cb1bedee2bde85b73913f2ea021573b749deb21d94940f03c1"
)
OLLAMA_IMAGE = (
    "ollama/ollama@sha256:"
    "020e4134285e2ef4d8fd801234176de3b4faadc992a3eb06c8e66a2f9d4c4ba2"
)
EXPECTED_LAB_CIDRS = {
    "10.10.0.0/30",
    "10.10.1.0/30",
    "10.10.2.0/30",
    "10.10.3.0/30",
    "10.20.0.0/30",
    "10.20.1.0/30",
    "172.16.10.0/24",
    "172.16.20.0/24",
}
EXPECTED_USER_SERVICES = {
    "prahari-action-agent.service",
    "prahari-lab-recovery.service",
    "prahari-path-collector.service",
    "prahari-router-collector.service",
}
EXPECTED_PROXY_VARIABLES = {
    "ALL_PROXY",
    "FTP_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "all_proxy",
    "ftp_proxy",
    "http_proxy",
    "https_proxy",
}
EXPECTED_NETWORK_OPTIONS = {
    "com.docker.network.bridge.name": "prahari-mgmt",
    "com.docker.network.bridge.enable_ip_masquerade": "false",
    "com.docker.network.bridge.gateway_mode_ipv4": "nat-unprotected",
    "com.docker.network.bridge.gateway_mode_ipv6": "nat-unprotected",
    "com.docker.network.driver.mtu": "0",
}
EXPECTED_ALLOW_TUPLES = {
    ("clab-prahari-branch-client", "clab-prahari-noc-api", 8080),
    ("clab-prahari-noc-api", "prahari-ollama", 11434),
}
EXPECTED_HOST_ALLOW_TUPLES = {("clab-prahari-noc-api", "172.31.255.1", 8091)}
EXPECTED_DROPIN_TEXT = """[Service]
# PRAHARI host helpers may use Docker's Unix socket and the fixed lab-local
# IPv4 management plane. They cannot open public IPv4 or IPv6 connections.
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
IPAddressDeny=any
IPAddressAllow=127.0.0.0/8
IPAddressAllow=::1/128
IPAddressAllow=172.31.255.0/24
"""
EXPECTED_DOCKER_DROPIN_TEXT = """[Unit]
Requires=prahari-zero-egress-early.service
After=prahari-zero-egress-early.service

[Service]
# Dedicated offline PRAHARI lab host: prevent daemon-side DNS and registry
# access from bypassing the per-container forwarding boundary.
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
IPAddressDeny=any
IPAddressAllow=127.0.0.0/8
IPAddressAllow=::1/128
IPAddressAllow=172.31.255.0/24
"""
EXPECTED_SYSCTL_TEXT = """# Required for inet-family filtering of Docker bridge traffic.
net.bridge.bridge-nf-call-iptables = 1
net.bridge.bridge-nf-call-ip6tables = 1
"""
EXPECTED_MODULES_LOAD_TEXT = """# Required before Docker starts so bridge traffic reaches nftables.
br_netfilter
"""
EARLY_UNIT_TEXT = f"""[Unit]
Description=PRAHARI fail-closed early zero-egress firewall
DefaultDependencies=no
Requires=systemd-modules-load.service systemd-sysctl.service
After=local-fs.target systemd-modules-load.service systemd-sysctl.service
Before=docker.service network-online.target

[Service]
Type=oneshot
ExecStart={TRUSTED_TOOL_PATH} enforce --confirm {REPAIR_CONFIRMATION}
RemainAfterExit=yes
User=root
Group=root
UMask=0077
NoNewPrivileges=yes
ProtectHome=yes
ProtectSystem=strict
ReadWritePaths={TRUSTED_STATE_DIR}
ProtectKernelTunables=yes
ProtectControlGroups=yes
ProtectKernelModules=yes
PrivateTmp=yes
RestrictAddressFamilies=AF_UNIX AF_NETLINK
CapabilityBoundingSet=CAP_NET_ADMIN
AmbientCapabilities=CAP_NET_ADMIN

[Install]
WantedBy=sysinit.target
"""
TIMER_UNIT_TEXT = """[Unit]
Description=Periodically repair and attest PRAHARI zero-egress controls

[Timer]
OnBootSec=45s
OnUnitActiveSec=60s
AccuracySec=5s
Persistent=true
Unit=prahari-zero-egress.service

[Install]
WantedBy=timers.target
"""
REPAIR_UNIT_TEMPLATE = f"""[Unit]
Description=Repair and attest PRAHARI zero-egress controls
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
ExecStart={TRUSTED_TOOL_PATH} repair --service-user {{service_user}} --confirm {REPAIR_CONFIRMATION}
User=root
Group=root
UMask=0077
NoNewPrivileges=yes
ProtectHome=yes
ProtectSystem=strict
ReadWritePaths=/etc/systemd/system /etc/systemd/user /etc/sysctl.d /etc/modules-load.d
ReadWritePaths=/etc/prahari {TRUSTED_STATE_DIR}
ProtectKernelTunables=no
ProtectControlGroups=yes
PrivateTmp=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
IPAddressDeny=any
IPAddressAllow=127.0.0.0/8
IPAddressAllow=::1/128
IPAddressAllow=172.31.255.0/24
CapabilityBoundingSet=CAP_NET_ADMIN CAP_SYS_MODULE CAP_CHOWN CAP_DAC_OVERRIDE CAP_FOWNER
AmbientCapabilities=CAP_NET_ADMIN CAP_SYS_MODULE
"""
OWNER_COMMENT_PREFIX = "managed-by:prahari-zero-egress-v1;policy-sha256:"
PASS = "pass"
FAIL = "fail"
WARN = "warning"
NOT_CHECKED = "not_checked"


class ZeroEgressError(RuntimeError):
    """A safe, operator-facing failure."""


def run(
    arguments: Sequence[str],
    *,
    input_text: str | None = None,
    check: bool = True,
    timeout: float = 20,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(arguments),
            input=input_text,
            text=True,
            capture_output=True,
            check=check,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as error:
        command = Path(arguments[0]).name if arguments else "command"
        raise ZeroEgressError(f"{command} failed safely") from error


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ZeroEgressError(f"duplicate JSON key is forbidden: {key}")
        result[key] = value
    return result


def parse_strict_json(raw: bytes, description: str) -> Any:
    if len(raw) > 2_000_000:
        raise ZeroEgressError(f"{description} exceeds the 2 MB safety limit")

    def reject_constant(value: str) -> None:
        raise ZeroEgressError(f"non-finite JSON value is forbidden: {value}")

    try:
        return json.loads(
            raw,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ZeroEgressError(f"cannot parse strict {description} JSON") from error


def load_policy_bytes(raw: bytes, *, source: str) -> dict[str, Any]:
    policy = parse_strict_json(raw, "policy")
    validate_policy(policy)
    policy["_policy_sha256"] = sha256_bytes(canonical_json(policy))
    policy["_source_sha256"] = sha256_bytes(raw)
    policy["_policy_path"] = source
    return policy


def load_policy(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ZeroEgressError(f"cannot read valid policy JSON: {path}") from error
    # The enforcement identity is semantic: whitespace-only changes do not
    # create false drift, while every validated policy value remains bound.
    return load_policy_bytes(raw, source=str(path.absolute()))


def require_exact_keys(value: Any, expected: set[str], description: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ZeroEgressError(
            f"{description} must contain exactly: {', '.join(sorted(expected))}"
        )
    return value


def parse_ipv4(value: Any, description: str) -> ipaddress.IPv4Address:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise ZeroEgressError(f"{description} must be a valid IPv4 address") from error
    if not isinstance(address, ipaddress.IPv4Address):
        raise ZeroEgressError(f"{description} must be IPv4")
    return address


def parse_ipv4_network(value: Any, description: str) -> ipaddress.IPv4Network:
    try:
        network = ipaddress.ip_network(value, strict=True)
    except ValueError as error:
        raise ZeroEgressError(f"{description} must be a canonical IPv4 network") from error
    if not isinstance(network, ipaddress.IPv4Network):
        raise ZeroEgressError(f"{description} must be IPv4")
    return network


def validate_policy(policy: Any) -> None:
    policy = require_exact_keys(
        policy,
        {
            "schema_version",
            "policy_id",
            "policy_version",
            "description",
            "boundary",
            "containers",
            "allowed_new_tcp_flows",
            "allowed_new_host_tcp_flows",
            "forbidden_proxy_environment_variables",
            "enforcement",
            "required_user_services",
        },
        "policy",
    )
    if policy.get("schema_version") != EXPECTED_SCHEMA:
        raise ZeroEgressError(f"policy schema must be {EXPECTED_SCHEMA}")
    if policy.get("policy_id") != EXPECTED_POLICY_ID:
        raise ZeroEgressError(f"policy ID must be {EXPECTED_POLICY_ID}")
    if policy.get("policy_version") != "1.0.0":
        raise ZeroEgressError("policy version must be 1.0.0")
    if not isinstance(policy.get("description"), str) or not policy["description"].strip():
        raise ZeroEgressError("policy description must be non-empty")

    boundary = require_exact_keys(
        policy.get("boundary"), {"docker_network", "non_egress_lab_cidrs"}, "boundary"
    )
    network = require_exact_keys(
        boundary.get("docker_network"),
        {
            "name",
            "driver",
            "ipv4_subnet",
            "gateway",
            "internal",
            "attachable",
            "ipv6_enabled",
            "options",
            "labels",
        },
        "Docker network policy",
    )
    if network.get("name") != "prahari_mgmt" or network.get("driver") != "bridge":
        raise ZeroEgressError("only the exact prahari_mgmt bridge is accepted")
    subnet = parse_ipv4_network(network.get("ipv4_subnet"), "management subnet")
    gateway = parse_ipv4(network.get("gateway"), "management gateway")
    if str(subnet) != "172.31.255.0/24" or str(gateway) != "172.31.255.1":
        raise ZeroEgressError("management subnet/gateway must be the reserved Track A range")
    if not subnet.is_private or gateway not in subnet:
        raise ZeroEgressError("management subnet/gateway must be private IPv4")
    if (
        network.get("internal") is not False
        or network.get("attachable") is not False
        or network.get("ipv6_enabled") is not False
    ):
        raise ZeroEgressError("unexpected Docker network mode")
    if network.get("options") != EXPECTED_NETWORK_OPTIONS:
        raise ZeroEgressError("Docker bridge options must match the fixed no-masquerade profile")
    if network.get("labels") != {"containerlab": ""}:
        raise ZeroEgressError("Docker network labels must identify the Containerlab network")
    lab_cidrs = boundary.get("non_egress_lab_cidrs")
    if (
        not isinstance(lab_cidrs, list)
        or len(lab_cidrs) != len(set(lab_cidrs))
        or set(lab_cidrs) != EXPECTED_LAB_CIDRS
    ):
        raise ZeroEgressError("data-plane CIDRs must be the exact Track A inventory")
    allowed_networks = [subnet, *(parse_ipv4_network(item, "lab CIDR") for item in lab_cidrs)]

    enforcement = require_exact_keys(
        policy.get("enforcement"),
        {"nft_family", "nft_table", "forward_priority", "input_priority", "blocked_probes"},
        "enforcement policy",
    )
    if (
        enforcement.get("nft_family") != "inet"
        or enforcement.get("nft_table") != TABLE_NAME
        or enforcement.get("forward_priority") != -10
        or enforcement.get("input_priority") != -10
    ):
        raise ZeroEgressError("unexpected enforcement table or hook priorities")

    containers = policy.get("containers")
    if not isinstance(containers, list) or len(containers) != 10:
        raise ZeroEgressError("the exact ten-container runtime inventory is required")
    names: set[str] = set()
    addresses: set[str] = set()
    for item in containers:
        item = require_exact_keys(
            item,
            {
                "name",
                "ipv4",
                "image",
                "image_id",
                "privileged",
                "containerlab_label",
                "interfaces",
                "default_ipv4_gateway",
            },
            "container policy",
        )
        if not SAFE_NAME.fullmatch(str(item.get("name", ""))):
            raise ZeroEgressError("invalid container name in policy")
        name = item["name"]
        address = parse_ipv4(item.get("ipv4"), f"management address for {name}")
        if address not in subnet or address == gateway:
            raise ZeroEgressError(f"invalid management address for {name}")
        if EXPECTED_CONTAINER_ADDRESSES.get(name) != str(address):
            raise ZeroEgressError(f"unexpected container identity or management address: {name}")
        if name in names or str(address) in addresses:
            raise ZeroEgressError("container names and addresses must be unique")
        image = item.get("image")
        image_id = item.get("image_id")
        if not isinstance(image, str) or not PINNED_IMAGE.fullmatch(image):
            raise ZeroEgressError(f"image for {name} must use an immutable sha256 reference")
        if not isinstance(image_id, str) or not SHA256_ID.fullmatch(image_id):
            raise ZeroEgressError(f"image ID for {name} must be an immutable sha256 identity")
        if image.rsplit("@", 1)[1] != image_id:
            raise ZeroEgressError(f"image reference and local image ID differ for {name}")
        if item.get("privileged") is not EXPECTED_PRIVILEGE[name]:
            raise ZeroEgressError(f"unexpected privilege expectation for {name}")
        if name == "prahari-ollama" and image != OLLAMA_IMAGE:
            raise ZeroEgressError("Ollama image must match the reviewed immutable release")
        if name.startswith("clab-prahari-") and name not in {
            "clab-prahari-branch-client",
            "clab-prahari-app-server",
            "clab-prahari-noc-api",
        } and image != FRR_IMAGE:
            raise ZeroEgressError(f"FRR image for {name} must match the reviewed release")
        if name in {
            "clab-prahari-branch-client",
            "clab-prahari-app-server",
            "clab-prahari-noc-api",
        } and not image.startswith("prahari-lab-app@sha256:"):
            raise ZeroEgressError(f"application image for {name} must use the local release name")
        label = item.get("containerlab_label")
        if label not in {None, "prahari"}:
            raise ZeroEgressError(f"invalid Containerlab label for {name}")
        if (name == "prahari-ollama") != (label is None):
            raise ZeroEgressError(f"unexpected Containerlab ownership label for {name}")
        interfaces = item.get("interfaces")
        if not isinstance(interfaces, dict) or not interfaces or "eth0" not in interfaces:
            raise ZeroEgressError(f"missing interface inventory for {name}")
        for interface, configured_addresses in interfaces.items():
            if not isinstance(interface, str) or not SAFE_INTERFACE.fullmatch(interface):
                raise ZeroEgressError(f"invalid interface name for {name}")
            if (
                not isinstance(configured_addresses, list)
                or len(configured_addresses) != 1
                or not isinstance(configured_addresses[0], str)
            ):
                raise ZeroEgressError(f"each interface for {name} must have one IPv4 address")
            try:
                configured = ipaddress.ip_interface(configured_addresses[0])
            except ValueError as error:
                raise ZeroEgressError(f"invalid interface address for {name}") from error
            if not isinstance(configured, ipaddress.IPv4Interface) or not any(
                configured.ip in allowed for allowed in allowed_networks
            ):
                raise ZeroEgressError(f"interface for {name} is outside the lab inventory")
        if interfaces != EXPECTED_CONTAINER_INTERFACES[name]:
            raise ZeroEgressError(f"interface inventory for {name} is not the exact Track A layout")
        default_gateway = parse_ipv4(
            item.get("default_ipv4_gateway"), f"default gateway for {name}"
        )
        if str(default_gateway) != EXPECTED_DEFAULT_GATEWAYS[name]:
            raise ZeroEgressError(f"default gateway for {name} is not the exact Track A next hop")
        interface_networks = {
            ipaddress.ip_interface(value).network
            for values in interfaces.values()
            for value in values
        }
        if not any(
            default_gateway in configured_network for configured_network in interface_networks
        ):
            raise ZeroEgressError(f"default gateway for {name} is not directly connected")
        names.add(name)
        addresses.add(str(address))

    if names != set(EXPECTED_CONTAINER_ADDRESSES):
        raise ZeroEgressError("container names must match the exact Track A inventory")
    application_pins = {
        (item["image"], item["image_id"])
        for item in containers
        if item["name"]
        in {
            "clab-prahari-branch-client",
            "clab-prahari-app-server",
            "clab-prahari-noc-api",
        }
    }
    if len(application_pins) != 1:
        raise ZeroEgressError("all application containers must use one exact immutable image")

    allowed = validate_flows(policy.get("allowed_new_tcp_flows"), names, policy)
    host_allowed = validate_flows(
        policy.get("allowed_new_host_tcp_flows"), names, policy, host=True
    )
    if allowed != EXPECTED_ALLOW_TUPLES or host_allowed != EXPECTED_HOST_ALLOW_TUPLES:
        raise ZeroEgressError("TCP allowlists must match the exact Track A local flows")
    forbidden = policy.get("forbidden_proxy_environment_variables")
    if not isinstance(forbidden, list) or set(forbidden) != EXPECTED_PROXY_VARIABLES:
        raise ZeroEgressError("forbidden proxy variables must match the exact denylist")
    if len(forbidden) != len(EXPECTED_PROXY_VARIABLES):
        raise ZeroEgressError("forbidden proxy variables must be unique")

    blocked = enforcement["blocked_probes"]
    if not isinstance(blocked, list) or len(blocked) != 3:
        raise ZeroEgressError("three exact negative probes are required")
    probe_keys = {"container", "destination", "port", "timeout_seconds", "family"}
    expected_probes = {
        ("clab-prahari-noc-api", "192.0.2.1", 443, 1.0, "ipv4"),
        ("clab-prahari-noc-api", "2001:db8::1", 443, 1.0, "ipv6"),
        ("clab-prahari-branch-client", "172.31.255.19", 11434, 1.0, "ipv4"),
    }
    observed_probes: set[tuple[str, str, int, float, str]] = set()
    for probe in blocked:
        probe = require_exact_keys(probe, probe_keys, "blocked probe")
        if probe["container"] not in names or probe["family"] not in {"ipv4", "ipv6"}:
            raise ZeroEgressError("invalid blocked probe source or family")
        try:
            destination = ipaddress.ip_address(probe["destination"])
        except ValueError as error:
            raise ZeroEgressError("blocked probe destination must be an IP literal") from error
        if destination.version != (4 if probe["family"] == "ipv4" else 6):
            raise ZeroEgressError("blocked probe family does not match its destination")
        if type(probe["port"]) is not int or not 1 <= probe["port"] <= 65535:
            raise ZeroEgressError("blocked probe port is invalid")
        if type(probe["timeout_seconds"]) not in {int, float} or not (
            0.1 <= probe["timeout_seconds"] <= 5.0
        ):
            raise ZeroEgressError("blocked probe timeout is invalid")
        observed_probes.add(
            (
                probe["container"],
                probe["destination"],
                probe["port"],
                float(probe["timeout_seconds"]),
                probe["family"],
            )
        )
    if observed_probes != expected_probes:
        raise ZeroEgressError(
            "blocked probes must cover public IPv4, public IPv6, and denied local TCP"
        )

    required_services = policy.get("required_user_services")
    if (
        not isinstance(required_services, list)
        or len(required_services) != len(EXPECTED_USER_SERVICES)
        or set(required_services) != EXPECTED_USER_SERVICES
    ):
        raise ZeroEgressError("required user services must match the exact PRAHARI inventory")


def validate_flows(
    flows: Any,
    names: set[str],
    policy: dict[str, Any],
    *,
    host: bool = False,
) -> set[tuple[str, str, int]]:
    if not isinstance(flows, list) or not flows:
        raise ZeroEgressError("each allowlist must contain at least one flow")
    seen: set[tuple[str, str, int]] = set()
    gateway = policy["boundary"]["docker_network"]["gateway"]
    for flow in flows:
        flow = require_exact_keys(flow, {"source", "destination", "port", "purpose"}, "TCP flow")
        if flow.get("source") not in names:
            raise ZeroEgressError("flow source is not in the container inventory")
        destination = flow.get("destination")
        if (host and destination != gateway) or (not host and destination not in names):
            raise ZeroEgressError("flow destination is outside the local inventory")
        port = flow.get("port")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ZeroEgressError("flow port must be an integer in 1..65535")
        if not isinstance(flow.get("purpose"), str) or not flow["purpose"].strip():
            raise ZeroEgressError("flow purpose must be non-empty")
        key = flow["source"], destination, port
        if key in seen:
            raise ZeroEgressError("duplicate allow-listed flow")
        seen.add(key)
    return seen


def policy_without_internal_fields(policy: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in policy.items() if not key.startswith("_")}


def container_map(policy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["name"]: item for item in policy["containers"]}


def check(identifier: str, status: str, detail: str, **evidence: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"id": identifier, "status": status, "detail": detail}
    if evidence:
        result["evidence"] = evidence
    return result


def require_command(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise ZeroEgressError(f"required command is unavailable: {name}")
    return path


def secure_path_metadata(path: Path, *, regular: bool) -> dict[str, Any]:
    """Reject symlinks and any root trust path writable outside root."""
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ZeroEgressError(f"required trusted path is unavailable: {path}") from error
    if stat.S_ISLNK(metadata.st_mode):
        raise ZeroEgressError(f"trusted path must not be a symlink: {path}")
    if regular and not stat.S_ISREG(metadata.st_mode):
        raise ZeroEgressError(f"trusted path must be a regular file: {path}")
    if not regular and not stat.S_ISDIR(metadata.st_mode):
        raise ZeroEgressError(f"trusted path must be a directory: {path}")
    if metadata.st_uid != 0 or metadata.st_gid != 0 or metadata.st_mode & 0o022:
        raise ZeroEgressError(
            f"trusted path must be root-owned and not group/world writable: {path}"
        )
    return {
        "path": str(path),
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
        "mode": oct(stat.S_IMODE(metadata.st_mode)),
    }


def nearest_existing_path(path: Path) -> Path:
    candidate = path.absolute()
    while not candidate.exists():
        if candidate == candidate.parent:
            raise ZeroEgressError(f"cannot resolve a filesystem for {path}")
        candidate = candidate.parent
    return candidate


def require_ext4_path(path: Path) -> str:
    """Require a WSL ext4 trust root, never a Windows/9p workspace mount."""
    candidate = nearest_existing_path(path)
    completed = run(
        [require_command("findmnt"), "-T", str(candidate), "-n", "-o", "FSTYPE"]
    )
    filesystem = completed.stdout.strip()
    if filesystem != "ext4":
        raise ZeroEgressError(
            "trusted path must be on the WSL ext4 filesystem, not "
            f"{filesystem or 'unknown'}: {path}"
        )
    return filesystem


def secure_root_parents(path: Path) -> list[dict[str, Any]]:
    """Validate every existing parent below /; none may be symlinked or writable."""
    evidence: list[dict[str, Any]] = []
    parent = path.absolute().parent
    while parent != parent.parent:
        if parent.exists():
            evidence.append(secure_path_metadata(parent, regular=False))
        parent = parent.parent
    return evidence


def require_trusted_tool() -> dict[str, Any]:
    require_root()
    tool_path = Path(__file__).absolute()
    if tool_path.resolve() != TRUSTED_TOOL_PATH:
        raise ZeroEgressError(
            "root operation must execute /usr/local/libexec/prahari/zero_egress.py; "
            "never execute the workspace copy with sudo"
        )
    require_ext4_path(tool_path)
    tool_metadata = secure_path_metadata(tool_path, regular=True)
    if stat.S_IMODE(tool_path.lstat().st_mode) != 0o755:
        raise ZeroEgressError("trusted tool must have exact mode 0755")
    return {
        "tool": tool_metadata,
        "parents": secure_root_parents(tool_path),
        "filesystem": "ext4",
    }


def require_trusted_runtime_files(policy_path: Path) -> dict[str, Any]:
    evidence = require_trusted_tool()
    if policy_path.absolute() != TRUSTED_POLICY_PATH:
        raise ZeroEgressError(
            "root operation requires the installed tool and policy under /usr/local and /etc"
        )
    require_ext4_path(policy_path)
    evidence["policy"] = secure_path_metadata(policy_path, regular=True)
    if stat.S_IMODE(policy_path.lstat().st_mode) != 0o644:
        raise ZeroEgressError("trusted policy must have exact mode 0644")
    evidence["policy_parents"] = secure_root_parents(policy_path)
    return evidence


def ensure_root_directory(path: Path, mode: int) -> None:
    """Create a fixed root trust directory without traversing symlinks."""
    require_root()
    require_ext4_path(path)
    missing: list[Path] = []
    candidate = path.absolute()
    while not candidate.exists():
        missing.append(candidate)
        candidate = candidate.parent
    secure_path_metadata(candidate, regular=False)
    for directory in reversed(missing):
        directory.mkdir(mode=mode)
        os.chown(directory, 0, 0)
        os.chmod(directory, mode)
    secure_path_metadata(path, regular=False)


def atomic_install_root_file(path: Path, content: bytes, mode: int) -> bool:
    """Atomically install exact root-owned bytes under an already trusted parent."""
    require_root()
    require_ext4_path(path)
    ensure_root_directory(path.parent, 0o755)
    if path.exists() or path.is_symlink():
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ZeroEgressError(f"refusing to replace unsafe trusted file: {path}")
        if (
            metadata.st_uid == 0
            and metadata.st_gid == 0
            and stat.S_IMODE(metadata.st_mode) == mode
            and path.read_bytes() == content
        ):
            return False
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        os.fchown(descriptor, 0, 0)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
    installed = path.lstat()
    if (
        not stat.S_ISREG(installed.st_mode)
        or stat.S_ISLNK(installed.st_mode)
        or installed.st_uid != 0
        or installed.st_gid != 0
        or stat.S_IMODE(installed.st_mode) != mode
        or path.read_bytes() != content
    ):
        raise ZeroEgressError(f"post-install verification failed for {path}")
    return True


def read_candidate_file(path: Path, *, limit: int = 2_000_000) -> bytes:
    """Read a migration candidate once, rejecting links and special files."""
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ZeroEgressError(f"migration candidate is unavailable: {path}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ZeroEgressError(f"migration candidate must be a non-symlink regular file: {path}")
    if metadata.st_size <= 0 or metadata.st_size > limit:
        raise ZeroEgressError(f"migration candidate has an unsafe size: {path}")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ZeroEgressError(f"cannot read migration candidate: {path}") from error
    if len(raw) != metadata.st_size:
        raise ZeroEgressError(f"migration candidate changed while being read: {path}")
    return raw


def validate_tool_candidate(raw: bytes) -> None:
    if len(raw) > 2_000_000 or not raw.startswith(b"#!/usr/bin/env python3\n"):
        raise ZeroEgressError("tool candidate is not the expected bounded Python executable")
    try:
        source = raw.decode("utf-8")
        compile(source, "zero_egress.py", "exec")
    except (UnicodeDecodeError, SyntaxError) as error:
        raise ZeroEgressError("tool candidate is not valid UTF-8 Python") from error
    required_markers = (
        'EXPECTED_SCHEMA = "prahari-zero-egress-policy-v1"',
        'EXPECTED_POLICY_ID = "prahari-lab-zero-egress"',
        "def main(",
        "def validate_policy(",
    )
    if any(marker not in source for marker in required_markers):
        raise ZeroEgressError("tool candidate lacks a required PRAHARI safety marker")


def repair_unit_text(service_user: str) -> str:
    if not SAFE_NAME.fullmatch(service_user) or service_user == "root":
        raise ZeroEgressError("invalid service user for repair unit")
    return REPAIR_UNIT_TEMPLATE.format(service_user=service_user)


def expected_managed_files(service_user: str) -> dict[Path, tuple[bytes, int]]:
    files: dict[Path, tuple[bytes, int]] = {
        SYSCTL_PATH: (EXPECTED_SYSCTL_TEXT.encode(), 0o644),
        MODULES_LOAD_PATH: (EXPECTED_MODULES_LOAD_TEXT.encode(), 0o644),
        EARLY_UNIT_PATH: (EARLY_UNIT_TEXT.encode(), 0o644),
        REPAIR_UNIT_PATH: (repair_unit_text(service_user).encode(), 0o644),
        TIMER_UNIT_PATH: (TIMER_UNIT_TEXT.encode(), 0o644),
        SYSTEMD_SYSTEM_DIR / DOCKER_DROPIN_SUFFIX: (
            EXPECTED_DOCKER_DROPIN_TEXT.encode(),
            0o644,
        ),
        SERVICE_USER_PATH: (f"{service_user}\n".encode(), 0o644),
    }
    for unit in EXPECTED_USER_SERVICES:
        files[SYSTEMD_USER_DIR / f"{unit}.d" / USER_DROPIN_NAME] = (
            EXPECTED_DROPIN_TEXT.encode(),
            0o644,
        )
    return files


def signing_key_pair(*, create: bool) -> dict[str, Any]:
    """Create once or validate the root-owned Ed25519 attestation key pair."""
    require_root()
    openssl = require_command("openssl")
    ensure_root_directory(ATTESTATION_KEY_PATH.parent, 0o755)
    ensure_root_directory(ATTESTATION_PUBLIC_KEY_PATH.parent, 0o700)
    private_present = ATTESTATION_KEY_PATH.exists() or ATTESTATION_KEY_PATH.is_symlink()
    public_present = (
        ATTESTATION_PUBLIC_KEY_PATH.exists() or ATTESTATION_PUBLIC_KEY_PATH.is_symlink()
    )
    if private_present != public_present:
        raise ZeroEgressError("attestation key pair is incomplete; refusing identity rotation")
    created = False
    if not private_present:
        if not create:
            raise ZeroEgressError("attestation signing key is absent")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".zero-egress-key.", dir=ATTESTATION_KEY_PATH.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        public_temporary = temporary.with_suffix(".pub")
        try:
            run([openssl, "genpkey", "-algorithm", "ED25519", "-out", str(temporary)])
            os.chown(temporary, 0, 0)
            os.chmod(temporary, 0o600)
            run(
                [
                    openssl,
                    "pkey",
                    "-in",
                    str(temporary),
                    "-pubout",
                    "-out",
                    str(public_temporary),
                ]
            )
            public_bytes = public_temporary.read_bytes()
            atomic_install_root_file(ATTESTATION_KEY_PATH, temporary.read_bytes(), 0o600)
            atomic_install_root_file(ATTESTATION_PUBLIC_KEY_PATH, public_bytes, 0o644)
            created = True
        finally:
            temporary.unlink(missing_ok=True)
            public_temporary.unlink(missing_ok=True)
    private_meta = secure_path_metadata(ATTESTATION_KEY_PATH, regular=True)
    public_meta = secure_path_metadata(ATTESTATION_PUBLIC_KEY_PATH, regular=True)
    if stat.S_IMODE(ATTESTATION_KEY_PATH.lstat().st_mode) != 0o600:
        raise ZeroEgressError("attestation private key must have mode 0600")
    if stat.S_IMODE(ATTESTATION_PUBLIC_KEY_PATH.lstat().st_mode) != 0o644:
        raise ZeroEgressError("attestation public key must have mode 0644")
    with tempfile.TemporaryDirectory(prefix="prahari-key-check-") as directory:
        derived = Path(directory) / "derived.pem"
        run(
            [
                openssl,
                "pkey",
                "-in",
                str(ATTESTATION_KEY_PATH),
                "-pubout",
                "-out",
                str(derived),
            ]
        )
        if derived.read_bytes() != ATTESTATION_PUBLIC_KEY_PATH.read_bytes():
            raise ZeroEgressError("attestation public key does not match its private key")
    public_sha = sha256_bytes(ATTESTATION_PUBLIC_KEY_PATH.read_bytes())
    return {
        "created": created,
        "algorithm": "Ed25519",
        "key_id": f"sha256:{public_sha}",
        "private_key": private_meta,
        "public_key": public_meta,
    }


def sign_payload(payload: bytes) -> dict[str, str]:
    key = signing_key_pair(create=False)
    with tempfile.TemporaryDirectory(prefix="prahari-attestation-") as directory:
        payload_path = Path(directory) / "payload"
        signature_path = Path(directory) / "signature"
        payload_path.write_bytes(payload)
        run(
            [
                require_command("openssl"),
                "pkeyutl",
                "-sign",
                "-rawin",
                "-inkey",
                str(ATTESTATION_KEY_PATH),
                "-in",
                str(payload_path),
                "-out",
                str(signature_path),
            ]
        )
        signature = signature_path.read_bytes()
    return {
        "algorithm": "Ed25519",
        "key_id": key["key_id"],
        "public_key_pem": ATTESTATION_PUBLIC_KEY_PATH.read_text(encoding="ascii"),
        "payload_sha256": sha256_bytes(payload),
        "signature_base64": base64.b64encode(signature).decode("ascii"),
    }


def prepare_evidence_directory(path: Path) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        if path.absolute() != TRUSTED_EVIDENCE_DIR:
            raise ZeroEgressError(
                "root evidence must be written only under /var/lib/prahari/security"
            )
        require_ext4_path(path)
        current = Path("/")
        for component in TRUSTED_EVIDENCE_DIR.parts[1:]:
            current /= component
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise ZeroEgressError(f"evidence path component is unsafe: {current}")
            if metadata.st_uid != 0 or metadata.st_gid != 0 or metadata.st_mode & 0o022:
                raise ZeroEgressError(f"evidence path component is not root-controlled: {current}")
        os.chmod(TRUSTED_EVIDENCE_DIR, 0o700)
    else:
        path.mkdir(parents=True, exist_ok=True)


def resolve_service_user(name: str) -> tuple[str, int]:
    if not SAFE_NAME.fullmatch(name) or name == "root":
        raise ZeroEgressError("--service-user must name the non-root PRAHARI service owner")
    try:
        import pwd

        account = pwd.getpwnam(name)
    except (ImportError, KeyError) as error:
        raise ZeroEgressError("cannot resolve the PRAHARI service owner") from error
    if account.pw_uid == 0 or account.pw_dir in {"", "/root"}:
        raise ZeroEgressError("PRAHARI service owner must be non-root")
    return name, account.pw_uid


def docker_network_snapshot(policy: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    checks: list[dict[str, Any]] = []
    expected = policy["boundary"]["docker_network"]
    completed = run([require_command("docker"), "network", "inspect", expected["name"]])
    try:
        networks = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ZeroEgressError("Docker returned invalid network JSON") from error
    if not isinstance(networks, list) or len(networks) != 1:
        raise ZeroEgressError("Docker did not return the exact management network")
    actual = networks[0]
    ipam = actual.get("IPAM", {}).get("Config", [])
    actual_network = {
        "name": actual.get("Name"),
        "id": actual.get("Id"),
        "driver": actual.get("Driver"),
        "scope": actual.get("Scope"),
        "internal": actual.get("Internal"),
        "attachable": actual.get("Attachable"),
        "ipv6_enabled": actual.get("EnableIPv6"),
        "ipam": ipam,
        "options": actual.get("Options") or {},
        "labels": actual.get("Labels") or {},
    }
    expected_shape = {
        "name": expected["name"],
        "driver": expected["driver"],
        "internal": expected["internal"],
        "attachable": expected["attachable"],
        "ipv6_enabled": expected["ipv6_enabled"],
        "ipam": [{"Subnet": expected["ipv4_subnet"], "Gateway": expected["gateway"]}],
        "options": expected["options"],
        "labels": expected["labels"],
    }
    comparable = {key: actual_network.get(key) for key in expected_shape}
    status = PASS if comparable == expected_shape else FAIL
    checks.append(
        check(
            "docker_network_exact",
            status,
            (
                "Docker management network matches policy"
                if status == PASS
                else "Docker network drift detected"
            ),
            expected=expected_shape,
            actual=comparable,
        )
    )
    network_id = actual.get("Id")
    option_bridge = (actual.get("Options") or {}).get("com.docker.network.bridge.name")
    if option_bridge:
        bridge = option_bridge
    elif isinstance(network_id, str) and re.fullmatch(r"[a-f0-9]{64}", network_id):
        bridge = f"br-{network_id[:12]}"
    else:
        raise ZeroEgressError("cannot derive a safe bridge name from Docker network ID")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", bridge):
        raise ZeroEgressError("Docker bridge name is invalid")
    link = run([require_command("ip"), "-j", "link", "show", "dev", bridge], check=False)
    bridge_ok = link.returncode == 0
    checks.append(
        check(
            "docker_bridge_present",
            PASS if bridge_ok else FAIL,
            (
                f"resolved management bridge {bridge}"
                if bridge_ok
                else f"management bridge {bridge} is absent"
            ),
            bridge=bridge,
        )
    )
    actual_network["bridge"] = bridge
    actual_network["connected_containers"] = sorted(
        endpoint.get("Name") for endpoint in (actual.get("Containers") or {}).values()
    )
    return actual_network, checks


def parse_json_array(payload: str, description: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ZeroEgressError(f"container returned invalid {description} JSON") from error
    if not isinstance(parsed, list) or any(not isinstance(item, dict) for item in parsed):
        raise ZeroEgressError(f"container returned an invalid {description} inventory")
    return parsed


def parse_proc_ipv4_routes(payload: str) -> list[dict[str, Any]]:
    routes: list[dict[str, Any]] = []
    lines = payload.splitlines()
    for line in lines[1:]:
        fields = line.split()
        if len(fields) < 8:
            continue
        try:
            destination = ipaddress.IPv4Address(bytes.fromhex(fields[1])[::-1])
            gateway = ipaddress.IPv4Address(bytes.fromhex(fields[2])[::-1])
            flags = int(fields[3], 16)
            mask = ipaddress.IPv4Address(bytes.fromhex(fields[7])[::-1])
            network = ipaddress.IPv4Network(f"{destination}/{mask}", strict=False)
        except (ValueError, ipaddress.AddressValueError, ipaddress.NetmaskValueError) as error:
            raise ZeroEgressError("container returned an invalid kernel IPv4 route") from error
        if not flags & 0x1:
            continue
        item: dict[str, Any] = {
            "dst": "default" if network.prefixlen == 0 else str(network),
            "dev": fields[0],
        }
        if int(gateway):
            item["gateway"] = str(gateway)
        routes.append(item)
    return routes


def namespace_network_snapshot(
    container: str, target: dict[str, Any], policy: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    docker = require_command("docker")
    interface_result = run(
        [docker, "exec", container, "ls", "-1", "/sys/class/net"], check=False
    )
    interface_names = sorted(item for item in interface_result.stdout.splitlines() if item)
    expected_names = sorted({"lo", *target["interfaces"]})
    interfaces_exact = interface_result.returncode == 0 and interface_names == expected_names

    address_result = run([docker, "exec", container, "ip", "-j", "address", "show"], check=False)
    route_result = run([docker, "exec", container, "ip", "-j", "-4", "route"], check=False)
    route6_result = run([docker, "exec", container, "ip", "-j", "-6", "route"], check=False)
    address_source = "ip-json"
    if address_result.returncode == 0 and route_result.returncode == 0:
        addresses = parse_json_array(address_result.stdout, "address")
        routes = parse_json_array(route_result.stdout, "IPv4 route")
        routes6 = (
            parse_json_array(route6_result.stdout, "IPv6 route")
            if route6_result.returncode == 0
            else []
        )
        observed_ipv4: dict[str, list[str]] = {}
        disallowed_ipv6: list[str] = []
        for interface in addresses:
            name = interface.get("ifname")
            if not isinstance(name, str):
                continue
            for address in interface.get("addr_info", []):
                if not isinstance(address, dict):
                    continue
                family = address.get("family")
                local = address.get("local")
                prefix = address.get("prefixlen")
                if family == "inet" and name != "lo" and isinstance(local, str):
                    observed_ipv4.setdefault(name, []).append(f"{local}/{prefix}")
                elif family == "inet6" and isinstance(local, str) and name != "lo":
                    try:
                        parsed = ipaddress.IPv6Address(local)
                    except ipaddress.AddressValueError:
                        disallowed_ipv6.append(local)
                    else:
                        if not parsed.is_link_local:
                            disallowed_ipv6.append(local)
    else:
        # The pinned Ollama image intentionally has no iproute2 binary. Its
        # namespace still exposes authoritative interfaces and kernel routes.
        if set(target["interfaces"]) != {"eth0"}:
            raise ZeroEgressError(
                f"iproute2 is unavailable in multi-interface container {container}"
            )
        proc4 = run([docker, "exec", container, "cat", "/proc/net/route"], check=False)
        proc6 = run([docker, "exec", container, "cat", "/proc/net/ipv6_route"], check=False)
        if proc4.returncode != 0 or proc6.returncode != 0:
            raise ZeroEgressError(f"cannot inspect routes inside {container}")
        routes = parse_proc_ipv4_routes(proc4.stdout)
        routes6 = [
            {"raw": line, "dev": line.split()[-1]}
            for line in proc6.stdout.splitlines()
            if line.split() and line.split()[-1] != "lo"
        ]
        observed_ipv4 = {"eth0": list(target["interfaces"]["eth0"])}
        disallowed_ipv6 = []
        address_source = "docker-endpoint-plus-procfs"

    expected_ipv4 = {name: sorted(values) for name, values in target["interfaces"].items()}
    observed_ipv4 = {name: sorted(values) for name, values in observed_ipv4.items()}
    addresses_exact = observed_ipv4 == expected_ipv4 and not disallowed_ipv6

    allowed_networks = [
        parse_ipv4_network(
            policy["boundary"]["docker_network"]["ipv4_subnet"], "management subnet"
        ),
        *(
            parse_ipv4_network(item, "lab CIDR")
            for item in policy["boundary"]["non_egress_lab_cidrs"]
        ),
    ]
    default_routes = [item for item in routes if item.get("dst") == "default"]
    default_exact = (
        len(default_routes) == 1
        and default_routes[0].get("gateway") == target["default_ipv4_gateway"]
        and default_routes[0].get("dev") in target["interfaces"]
    )
    observed_destinations: set[str] = set()
    observed_route_keys: set[tuple[str, str | None, str | None]] = set()
    routes_allowed = True
    directly_connected = {
        interface: ipaddress.ip_interface(addresses[0]).network
        for interface, addresses in target["interfaces"].items()
    }
    for route in routes:
        destination = route.get("dst")
        device = route.get("dev")
        if device not in target["interfaces"]:
            routes_allowed = False
        if destination == "default":
            continue
        try:
            parsed_destination = ipaddress.ip_network(destination, strict=False)
        except ValueError:
            routes_allowed = False
            continue
        if not isinstance(parsed_destination, ipaddress.IPv4Network) or not any(
            parsed_destination.subnet_of(allowed) for allowed in allowed_networks
        ):
            routes_allowed = False
        observed_destinations.add(str(parsed_destination))
        gateway = route.get("gateway")
        if gateway is not None:
            try:
                parsed_gateway = ipaddress.ip_address(gateway)
            except ValueError:
                routes_allowed = False
            else:
                if (
                    not isinstance(parsed_gateway, ipaddress.IPv4Address)
                    or device not in directly_connected
                    or parsed_gateway not in directly_connected[device]
                ):
                    routes_allowed = False
        elif device not in directly_connected or parsed_destination != directly_connected[device]:
            routes_allowed = False
        route_key = (str(parsed_destination), str(gateway) if gateway else None, device)
        if route_key in observed_route_keys:
            routes_allowed = False
        observed_route_keys.add(route_key)
    expected_connected = {
        str(ipaddress.ip_interface(address).network)
        for values in target["interfaces"].values()
        for address in values
    }
    connected_routes_present = expected_connected.issubset(observed_destinations)
    is_router = container not in {
        "clab-prahari-branch-client",
        "clab-prahari-app-server",
        "clab-prahari-noc-api",
        "prahari-ollama",
    }
    expected_destinations = (
        {str(network) for network in allowed_networks} if is_router else expected_connected
    )
    route_destinations_exact = observed_destinations == expected_destinations
    ipv6_routes_allowed = True
    for item in routes6:
        if item.get("raw") is not None:
            # Procfs fallback is used only by the single-interface Ollama
            # image. Any non-loopback IPv6 route is unexpected there.
            ipv6_routes_allowed = False
            continue
        destination = item.get("dst")
        if destination == "default" or item.get("dev") not in target["interfaces"]:
            ipv6_routes_allowed = False
            continue
        try:
            parsed_v6 = ipaddress.ip_network(destination, strict=False)
        except ValueError:
            ipv6_routes_allowed = False
            continue
        if not isinstance(parsed_v6, ipaddress.IPv6Network) or not parsed_v6.is_link_local:
            ipv6_routes_allowed = False
    exact = all(
        (
            interfaces_exact,
            addresses_exact,
            default_exact,
            routes_allowed,
            connected_routes_present,
            route_destinations_exact,
            ipv6_routes_allowed,
        )
    )
    return {
        "interfaces": interface_names,
        "expected_interfaces": expected_names,
        "ipv4_addresses": observed_ipv4,
        "expected_ipv4_addresses": expected_ipv4,
        "address_source": address_source,
        "disallowed_ipv6_addresses": disallowed_ipv6,
        "ipv4_routes": routes,
        "ipv6_routes": routes6,
        "default_route_exact": default_exact,
        "connected_routes_present": connected_routes_present,
        "expected_ipv4_route_destinations": sorted(expected_destinations),
        "ipv4_route_destinations_exact": route_destinations_exact,
        "routes_within_lab_inventory": routes_allowed and ipv6_routes_allowed,
    }, exact


def docker_container_snapshot(
    policy: dict[str, Any], network_snapshot: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    checks: list[dict[str, Any]] = []
    expected = container_map(policy)
    names = sorted(expected)
    completed = run([require_command("docker"), "inspect", *names], check=False)
    if completed.returncode != 0:
        checks.append(
            check(
                "container_inventory_exact",
                FAIL,
                "one or more expected containers are absent",
            )
        )
        return [], checks
    try:
        inspected = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ZeroEgressError("Docker returned invalid container JSON") from error
    snapshots: list[dict[str, Any]] = []
    all_ok = True
    forbidden = set(policy["forbidden_proxy_environment_variables"])
    for item in inspected:
        name = str(item.get("Name", "")).removeprefix("/")
        target = expected.get(name)
        if target is None:
            all_ok = False
            continue
        endpoint = (
            item.get("NetworkSettings", {})
            .get("Networks", {})
            .get(policy["boundary"]["docker_network"]["name"], {})
        )
        docker_networks = item.get("NetworkSettings", {}).get("Networks", {})
        ip_with_prefix = endpoint.get("IPAddress", "")
        labels = item.get("Config", {}).get("Labels") or {}
        proxy_names = sorted(
            entry.split("=", 1)[0]
            for entry in (item.get("Config", {}).get("Env") or [])
            if entry.split("=", 1)[0] in forbidden
        )
        actual = {
            "name": name,
            "running": item.get("State", {}).get("Running") is True,
            "ipv4": ip_with_prefix,
            "image_reference": item.get("Config", {}).get("Image"),
            "image_id": item.get("Image"),
            "privileged": item.get("HostConfig", {}).get("Privileged"),
            "containerlab_label": labels.get("containerlab"),
            "forbidden_proxy_variables_present": proxy_names,
            "docker_networks": sorted(docker_networks),
        }
        namespace, namespace_exact = namespace_network_snapshot(name, target, policy)
        actual["network_namespace"] = namespace
        label_ok = (
            actual["containerlab_label"] == target["containerlab_label"]
            if target["containerlab_label"] is not None
            else "containerlab" not in labels
        )
        item_ok = all(
            (
                actual["running"],
                actual["ipv4"] == target["ipv4"],
                actual["image_reference"] == target["image"],
                actual["image_id"] == target["image_id"],
                actual["privileged"] == target["privileged"],
                label_ok,
                not proxy_names,
                actual["docker_networks"] == [policy["boundary"]["docker_network"]["name"]],
                namespace_exact,
            )
        )
        actual["matches_policy"] = item_ok
        snapshots.append(actual)
        all_ok = all_ok and item_ok
    connected = set(network_snapshot.get("connected_containers", []))
    all_ok = all_ok and set(names) == connected and len(snapshots) == len(names)
    checks.append(
        check(
            "container_inventory_exact",
            PASS if all_ok else FAIL,
            "runtime identities, addresses, images, privileges, labels, and proxies match"
            if all_ok
            else "container inventory or runtime identity drift detected",
            expected_names=names,
            connected_names=sorted(connected),
        )
    )
    namespaces_exact = bool(snapshots) and all(
        item["network_namespace"]["interfaces"]
        == item["network_namespace"]["expected_interfaces"]
        and item["network_namespace"]["ipv4_addresses"]
        == item["network_namespace"]["expected_ipv4_addresses"]
        and not item["network_namespace"]["disallowed_ipv6_addresses"]
        and item["network_namespace"]["default_route_exact"]
        and item["network_namespace"]["connected_routes_present"]
        and item["network_namespace"]["ipv4_route_destinations_exact"]
        and item["network_namespace"]["routes_within_lab_inventory"]
        for item in snapshots
    )
    checks.append(
        check(
            "container_network_namespaces_exact",
            PASS if namespaces_exact else FAIL,
            "interfaces, addresses, IPv4 routes, and absence of routed IPv6 match policy"
            if namespaces_exact
            else "unexpected container interface, address, route, or routed IPv6 state detected",
        )
    )
    return sorted(snapshots, key=lambda value: value["name"]), checks


def kernel_snapshot() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    checks: list[dict[str, Any]] = []
    values: dict[str, str | None] = {}
    paths = {
        "ipv4_forward": Path("/proc/sys/net/ipv4/ip_forward"),
        "bridge_nf_call_iptables": Path("/proc/sys/net/bridge/bridge-nf-call-iptables"),
        "bridge_nf_call_ip6tables": Path("/proc/sys/net/bridge/bridge-nf-call-ip6tables"),
        "ipv6_all_forwarding": Path("/proc/sys/net/ipv6/conf/all/forwarding"),
    }
    for name, path in paths.items():
        try:
            values[name] = path.read_text(encoding="ascii").strip()
        except OSError:
            values[name] = None
    bridge_ok = values["bridge_nf_call_iptables"] == "1"
    checks.append(
        check(
            "bridge_netfilter_enabled",
            PASS if bridge_ok else FAIL,
            "bridged Docker packets enter the host filtering hooks"
            if bridge_ok
            else "bridge-nf-call-iptables must equal 1",
            observed=values["bridge_nf_call_iptables"],
        )
    )
    bridge6_ok = values["bridge_nf_call_ip6tables"] == "1"
    checks.append(
        check(
            "bridge_ipv6_netfilter_enabled",
            PASS if bridge6_ok else FAIL,
            "bridged IPv6 packets enter the host filtering hooks"
            if bridge6_ok
            else "bridge-nf-call-ip6tables must equal 1",
            observed=values["bridge_nf_call_ip6tables"],
        )
    )
    return values, checks


def address_for(policy: dict[str, Any], name: str) -> str:
    try:
        return container_map(policy)[name]["ipv4"]
    except KeyError as error:
        raise ZeroEgressError(f"flow references unknown container: {name}") from error


def nft_config(policy: dict[str, Any], bridge: str) -> str:
    network = policy["boundary"]["docker_network"]
    subnet = str(ipaddress.ip_network(network["ipv4_subnet"], strict=True))
    gateway = str(ipaddress.ip_address(network["gateway"]))
    policy_digest = policy["_policy_sha256"]
    lab_sources = sorted(
        {subnet, *policy["boundary"]["non_egress_lab_cidrs"]},
        key=lambda value: (int(ipaddress.ip_network(value).network_address), value),
    )
    source_set = "{ " + ", ".join(lab_sources) + " }"
    lines = [
        f"table inet {TABLE_NAME} {{",
        f'  comment "{OWNER_COMMENT_PREFIX}{policy_digest}"',
        "  chain forward {",
        "    type filter hook forward priority -10; policy accept;",
        (
            f'    iifname "{bridge}" ip saddr {subnet} ip daddr {subnet} '
            'ct state established,related counter accept comment "allow-established-local-forward"'
        ),
    ]
    for index, flow in enumerate(policy["allowed_new_tcp_flows"], start=1):
        source = address_for(policy, flow["source"])
        destination = address_for(policy, flow["destination"])
        lines.append(
            f'    iifname "{bridge}" ip saddr {source} ip daddr {destination} '
            f"tcp dport {flow['port']} ct state new counter accept "
            f'comment "allow-local-tcp-{index}"'
        )
    lines.extend(
        [
            (
                f'    iifname "{bridge}" meta nfproto ipv6 counter drop '
                'comment "deny-bridge-ipv6-forward"'
            ),
            f'    iifname "{bridge}" counter drop comment "deny-bridge-forward-default"',
            (
                f'    ip saddr {source_set} counter drop '
                'comment "deny-lab-source-forward-fallback"'
            ),
            "  }",
            "  chain input {",
            "    type filter hook input priority -10; policy accept;",
            (
                f'    iifname "{bridge}" ip saddr {subnet} ip daddr {gateway} '
                'ct state established,related counter accept comment "allow-established-host-input"'
            ),
        ]
    )
    for index, flow in enumerate(policy["allowed_new_host_tcp_flows"], start=1):
        source = address_for(policy, flow["source"])
        destination = str(ipaddress.ip_address(flow["destination"]))
        lines.append(
            f'    iifname "{bridge}" ip saddr {source} ip daddr {destination} '
            f"tcp dport {flow['port']} ct state new counter accept "
            f'comment "allow-host-tcp-{index}"'
        )
    lines.extend(
        [
            (
                f'    iifname "{bridge}" meta nfproto ipv6 counter drop '
                'comment "deny-bridge-ipv6-host-input"'
            ),
            f'    iifname "{bridge}" counter drop comment "deny-bridge-host-input-default"',
            (
                f'    ip saddr {source_set} counter drop '
                'comment "deny-lab-source-host-input-fallback"'
            ),
            "  }",
            "}",
            "",
        ]
    )
    return "\n".join(lines)


def normalized_nft_json(payload: str) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ZeroEgressError("nft returned invalid JSON") from error
    objects = parsed.get("nftables")
    if not isinstance(objects, list):
        raise ZeroEgressError("nft JSON has no object list")

    def scrub(value: Any) -> Any:
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if isinstance(value, dict):
            return {
                key: scrub(item)
                for key, item in value.items()
                if key not in {"handle", "packets", "bytes"}
            }
        return value

    return [scrub(item) for item in objects if "metainfo" not in item]


def isolated_nft_json(config: str) -> list[dict[str, Any]]:
    require_command("unshare")
    require_command("nft")
    arguments = ["unshare"]
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        arguments.extend(["--user", "--map-root-user"])
    arguments.extend(
        [
            "--net",
            "--fork",
            "sh",
            "-c",
            f"nft -f - && nft -j -s list table inet {TABLE_NAME}",
        ]
    )
    completed = run(arguments, input_text=config)
    return normalized_nft_json(completed.stdout)


def live_nft_json() -> list[dict[str, Any]] | None:
    completed = run(
        [require_command("nft"), "-j", "-s", "list", "table", "inet", TABLE_NAME],
        check=False,
    )
    if completed.returncode != 0:
        return None
    return normalized_nft_json(completed.stdout)


def nft_firewall_check(
    policy: dict[str, Any], bridge: str, expected: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return (
            check(
                "nftables_policy_exact",
                NOT_CHECKED,
                "authoritative nftables verification requires root",
            ),
            {"readable": False},
        )
    actual = live_nft_json()
    if actual is None:
        return (
            check("nftables_policy_exact", FAIL, "PRAHARI nftables table is absent"),
            {"readable": True, "table_present": False},
        )
    exact = actual == expected
    return (
        check(
            "nftables_policy_exact",
            PASS if exact else FAIL,
            "live nftables table exactly matches the rendered policy"
            if exact
            else "live nftables table differs from the rendered policy",
            bridge=bridge,
        ),
        {"readable": True, "table_present": True, "canonical_rules": actual},
    )


def parse_systemd_properties(output: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in output.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            result[key] = value
    return result


def sudo_user_systemctl_prefix(service_user: str) -> tuple[list[str], dict[str, str] | None]:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return [require_command("systemctl"), "--user"], None
    user, uid = resolve_service_user(service_user)
    import pwd

    account = pwd.getpwnam(user)
    environment = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "XDG_RUNTIME_DIR": f"/run/user/{uid}",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus",
        "HOME": account.pw_dir,
        "USER": user,
        "LOGNAME": user,
    }
    return [require_command("runuser"), "-u", user, "--", "systemctl", "--user"], environment


def file_content_exact(path: Path, expected: str, *, owner_uid: int) -> tuple[bool, dict[str, Any]]:
    try:
        metadata = path.lstat()
        raw = path.read_bytes()
    except OSError:
        return False, {"path": str(path), "present": False}
    safe = (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == owner_uid
        and not metadata.st_mode & 0o022
        and raw == expected.encode()
    )
    return safe, {
        "path": str(path),
        "present": True,
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
        "mode": oct(stat.S_IMODE(metadata.st_mode)),
        "sha256": sha256_bytes(raw),
        "expected_sha256": sha256_bytes(expected.encode()),
    }


def effective_ip_acl_exact(properties: dict[str, str], *, docker: bool = False) -> bool:
    deny = set(properties.get("IPAddressDeny", "").split())
    allow_raw = set(properties.get("IPAddressAllow", "").split())
    allow: set[str] = set()
    for item in allow_raw:
        if item == "localhost":
            allow.update({"127.0.0.0/8", "::1/128"})
        else:
            allow.add(item)
    expected_families = {"AF_UNIX", "AF_INET", "AF_INET6"}
    if docker:
        expected_families.add("AF_NETLINK")
    return (
        deny == {"any"}
        and allow == {"127.0.0.0/8", "::1/128", "172.31.255.0/24"}
        and set(properties.get("RestrictAddressFamilies", "").split()) == expected_families
    )


def active_cgroup_present(properties: dict[str, str]) -> bool:
    if properties.get("ActiveState") != "active":
        return True
    control_group = properties.get("ControlGroup", "")
    return bool(control_group) and (Path("/sys/fs/cgroup") / control_group.lstrip("/")).is_dir()


def managed_files_snapshot(service_user: str) -> tuple[dict[str, Any], bool]:
    files: dict[str, Any] = {}
    all_exact = True
    for path, (content, mode) in expected_managed_files(service_user).items():
        expected_text = content.decode("utf-8")
        exact, evidence = file_content_exact(path, expected_text, owner_uid=0)
        exact = exact and evidence.get("gid") == 0 and evidence.get("mode") == oct(mode)
        evidence["exact"] = exact
        files[str(path)] = evidence
        all_exact = all_exact and exact
    return files, all_exact


def systemd_profile_snapshot(
    policy: dict[str, Any], service_user: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    checks: list[dict[str, Any]] = []
    snapshot: dict[str, Any] = {"user_services": {}}
    try:
        prefix, environment = sudo_user_systemctl_prefix(service_user)
        resolve_service_user(service_user)

        user_ok = True
        for unit in policy["required_user_services"]:
            completed = run(
                [
                    *prefix,
                    "show",
                    unit,
                    "--no-pager",
                    (
                        "--property=LoadState,ActiveState,ControlGroup,DropInPaths,"
                        "RestrictAddressFamilies,IPAddressAllow,IPAddressDeny"
                    ),
                ],
                check=False,
                env=environment,
            )
            properties = parse_systemd_properties(completed.stdout)
            dropin_path = SYSTEMD_USER_DIR / f"{unit}.d" / USER_DROPIN_NAME
            file_ok, file_evidence = file_content_exact(
                dropin_path, EXPECTED_DROPIN_TEXT, owner_uid=0
            )
            snapshot["user_services"][unit] = {
                "effective_properties": properties,
                "dropin": file_evidence,
            }
            dropins = properties.get("DropInPaths", "")
            item_ok = (
                completed.returncode == 0
                and properties.get("LoadState") == "loaded"
                and str(dropin_path) in dropins
                and effective_ip_acl_exact(properties)
                and active_cgroup_present(properties)
                and file_ok
                and file_evidence.get("gid") == 0
                and file_evidence.get("mode") == oct(0o644)
            )
            user_ok = user_ok and item_ok
        checks.append(
            check(
                "user_service_ip_firewalls",
                PASS if user_ok else FAIL,
                "all PRAHARI host helpers have exact local-only systemd cgroup IP filters"
                if user_ok
                else "one or more PRAHARI user services lack the exact cgroup IP filter",
            )
        )
    except ZeroEgressError as error:
        checks.append(check("user_service_ip_firewalls", FAIL, str(error)))

    completed = run(
        [
            require_command("systemctl"),
            "show",
            "docker.service",
            "--no-pager",
            (
                "--property=LoadState,ActiveState,ControlGroup,DropInPaths,"
                "RestrictAddressFamilies,IPAddressAllow,IPAddressDeny,Requires,After"
            ),
        ],
        check=False,
    )
    docker_properties = parse_systemd_properties(completed.stdout)
    docker_dropin = Path("/etc/systemd/system") / DOCKER_DROPIN_SUFFIX
    docker_file_ok, docker_file_evidence = file_content_exact(
        docker_dropin, EXPECTED_DOCKER_DROPIN_TEXT, owner_uid=0
    )
    snapshot["docker_service"] = {
        "effective_properties": docker_properties,
        "dropin": docker_file_evidence,
    }
    docker_ok = (
        completed.returncode == 0
        and docker_properties.get("LoadState") == "loaded"
        and docker_properties.get("ActiveState") == "active"
        and str(docker_dropin) in docker_properties.get("DropInPaths", "")
        and effective_ip_acl_exact(docker_properties, docker=True)
        and active_cgroup_present(docker_properties)
        and docker_file_ok
    )
    checks.append(
        check(
            "docker_daemon_ip_firewall",
            PASS if docker_ok else FAIL,
            "dockerd DNS forwarding and image pulls are restricted to local addresses"
            if docker_ok
            else "docker.service lacks the required local-only systemd IP filter",
        )
    )
    lifecycle_files, lifecycle_files_ok = managed_files_snapshot(service_user)
    snapshot["managed_files"] = lifecycle_files
    checks.append(
        check(
            "root_owned_ext4_lifecycle_files",
            PASS if lifecycle_files_ok else FAIL,
            "all lifecycle files are exact root-owned non-writable copies"
            if lifecycle_files_ok
            else "one or more lifecycle files are absent, drifted, or not root-owned",
        )
    )
    early = run(
        [
            require_command("systemctl"),
            "show",
            "prahari-zero-egress-early.service",
            "--no-pager",
            "--property=LoadState,ActiveState,UnitFileState,Before",
        ],
        check=False,
    )
    early_properties = parse_systemd_properties(early.stdout)
    snapshot["early_service"] = early_properties
    docker_requires = set(docker_properties.get("Requires", "").split())
    docker_after = set(docker_properties.get("After", "").split())
    early_ok = (
        early.returncode == 0
        and early_properties.get("LoadState") == "loaded"
        and early_properties.get("ActiveState") == "active"
        and early_properties.get("UnitFileState") == "enabled"
        and "docker.service" in set(early_properties.get("Before", "").split())
        and "prahari-zero-egress-early.service" in docker_requires
        and "prahari-zero-egress-early.service" in docker_after
    )
    checks.append(
        check(
            "pre_docker_fail_closed_ordering",
            PASS if early_ok else FAIL,
            "the enabled early firewall is active and required before Docker"
            if early_ok
            else "the early firewall is not active, enabled, and ordered before Docker",
        )
    )
    persistence = run(
        [
            require_command("systemctl"),
            "show",
            "prahari-zero-egress.timer",
            "--no-pager",
            "--property=LoadState,ActiveState,UnitFileState",
        ],
        check=False,
    )
    persistence_properties = parse_systemd_properties(persistence.stdout)
    snapshot["persistence_timer"] = persistence_properties
    persistence_ok = (
        persistence.returncode == 0
        and persistence_properties.get("LoadState") == "loaded"
        and persistence_properties.get("ActiveState") == "active"
        and persistence_properties.get("UnitFileState") == "enabled"
    )
    checks.append(
        check(
            "zero_egress_persistence",
            PASS if persistence_ok else FAIL,
            "root repair/attestation timer is loaded, enabled, and active"
            if persistence_ok
            else "root repair/attestation timer is not fully active",
        )
    )
    try:
        trust = require_trusted_runtime_files(TRUSTED_POLICY_PATH)
        require_ext4_path(SYSTEMD_SYSTEM_DIR)
        require_ext4_path(SYSTEMD_USER_DIR)
        key = signing_key_pair(create=False)
    except ZeroEgressError as error:
        trust_check = check("trusted_ext4_install", FAIL, str(error))
        trust = {"error": str(error)}
        key = {"error": str(error)}
    else:
        trust_check = check(
            "trusted_ext4_install",
            PASS,
            "tool, policy, lifecycle files, and signing key are rooted on ext4",
        )
    snapshot["trust"] = trust
    snapshot["attestation_key"] = key
    checks.append(trust_check)
    manifest, manifest_ok = install_manifest_snapshot(service_user, policy)
    snapshot["install_manifest"] = manifest
    checks.append(
        check(
            "install_manifest_exact",
            PASS if manifest_ok else FAIL,
            "root install manifest binds the exact tool, policy, key, user, and managed files"
            if manifest_ok
            else "root install manifest is absent, malformed, or drifted",
        )
    )
    return snapshot, checks


def runtime_snapshot(
    policy: dict[str, Any], service_user: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    checks: list[dict[str, Any]] = []
    network, network_checks = docker_network_snapshot(policy)
    checks.extend(network_checks)
    containers, container_checks = docker_container_snapshot(policy, network)
    checks.extend(container_checks)
    kernel, kernel_checks = kernel_snapshot()
    checks.extend(kernel_checks)
    systemd, systemd_checks = systemd_profile_snapshot(policy, service_user)
    checks.extend(systemd_checks)
    return {
        "network": network,
        "containers": containers,
        "kernel": kernel,
        "systemd": systemd,
    }, checks


def rule_counter(snapshot: list[dict[str, Any]], comment: str) -> int | None:
    for item in snapshot:
        rule = item.get("rule")
        if not isinstance(rule, dict) or rule.get("comment") != comment:
            continue
        for expression in rule.get("expr", []):
            counter = expression.get("counter") if isinstance(expression, dict) else None
            if isinstance(counter, dict) and isinstance(counter.get("packets"), int):
                return counter["packets"]
    return None


def live_nft_json_with_counters() -> list[dict[str, Any]]:
    completed = run([require_command("nft"), "-j", "list", "table", "inet", TABLE_NAME])
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ZeroEgressError("nft returned invalid counter JSON") from error
    return [item for item in parsed.get("nftables", []) if "metainfo" not in item]


def tcp_probe(
    container: str,
    destination: str,
    port: int,
    *,
    must_connect: bool,
    family: str = "ipv4",
    timeout_seconds: float = 2.0,
) -> dict[str, Any]:
    code = """import socket, sys
family = socket.AF_INET6 if sys.argv[5] == "ipv6" else socket.AF_INET
sock = socket.socket(family, socket.SOCK_STREAM)
sock.settimeout(float(sys.argv[3]))
try:
    sock.connect((sys.argv[1], int(sys.argv[2])))
except OSError as error:
    print(f"blocked_errno={error.errno};detail={error}")
    sys.exit(0 if sys.argv[4] == "blocked" else 31)
else:
    sock.close()
    sys.exit(32 if sys.argv[4] == "blocked" else 0)
"""
    completed = run(
        [
            require_command("docker"),
            "exec",
            container,
            "/opt/venv/bin/python",
            "-c",
            code,
            destination,
            str(port),
            str(timeout_seconds),
            "allowed" if must_connect else "blocked",
            family,
        ],
        check=False,
        timeout=8,
    )
    return {
        "source_container": container,
        "destination": destination,
        "port": port,
        "family": family,
        "timeout_seconds": timeout_seconds,
        "expectation": "connect" if must_connect else "blocked",
        "passed": completed.returncode == 0,
        "returncode": completed.returncode,
        "result": completed.stdout.strip()[:500],
    }


def active_probes(policy: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return [check("active_zero_egress_probes", NOT_CHECKED, "active probes require root")], {}
    probes: list[dict[str, Any]] = []
    for flow in policy["allowed_new_tcp_flows"]:
        probes.append(
            tcp_probe(
                flow["source"],
                address_for(policy, flow["destination"]),
                flow["port"],
                must_connect=True,
                family="ipv4",
                timeout_seconds=2.0,
            )
        )
    for flow in policy["allowed_new_host_tcp_flows"]:
        probes.append(
            tcp_probe(
                flow["source"],
                flow["destination"],
                flow["port"],
                must_connect=True,
                family="ipv4",
                timeout_seconds=2.0,
            )
        )
    before_snapshot = live_nft_json_with_counters()
    counter_comments = {
        "ipv4": "deny-bridge-forward-default",
        "ipv6": "deny-bridge-ipv6-forward",
    }
    before = {
        family: rule_counter(before_snapshot, comment)
        for family, comment in counter_comments.items()
    }
    blocked_probes: list[dict[str, Any]] = []
    for blocked in policy["enforcement"]["blocked_probes"]:
        result = tcp_probe(
            blocked["container"],
            blocked["destination"],
            blocked["port"],
            must_connect=False,
            family=blocked["family"],
            timeout_seconds=float(blocked["timeout_seconds"]),
        )
        probes.append(result)
        blocked_probes.append(result)
    after_snapshot = live_nft_json_with_counters()
    after = {
        family: rule_counter(after_snapshot, comment)
        for family, comment in counter_comments.items()
    }
    ipv4_probe_count = sum(item["family"] == "ipv4" for item in blocked_probes)
    ipv4_counter_advanced = (
        before["ipv4"] is not None
        and after["ipv4"] is not None
        and after["ipv4"] >= before["ipv4"] + ipv4_probe_count
    )
    # The runtime route/address checks prove there is no routed IPv6. If the
    # kernel rejects the IPv6 connect before forwarding, its nft counter need
    # not advance; the exact installed nft rule remains independently checked.
    passed = all(item["passed"] for item in probes) and ipv4_counter_advanced
    return [
        check(
            "active_zero_egress_probes",
            PASS if passed else FAIL,
            "all local flows connected and all three denied probes failed as expected"
            if passed
            else "an allowed flow, denied flow, or deny-counter assertion failed",
            deny_counters_before=before,
            deny_counters_after=after,
            required_ipv4_counter_increment=ipv4_probe_count,
        )
    ], {
        "probes": probes,
        "deny_counters_before": before,
        "deny_counters_after": after,
    }


def all_pass(checks: list[dict[str, Any]]) -> bool:
    return bool(checks) and all(item["status"] == PASS for item in checks)


def write_evidence(
    command: str,
    policy: dict[str, Any],
    checks: list[dict[str, Any]],
    evidence: dict[str, Any],
    evidence_dir: Path,
) -> Path:
    prepare_evidence_directory(evidence_dir)
    should_sign = False
    signing_error: str | None = None
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        try:
            require_trusted_tool()
            signing_key_pair(create=False)
            should_sign = True
        except ZeroEgressError as error:
            signing_error = str(error)
    if should_sign:
        checks.append(
            check(
                "cryptographic_attestation",
                PASS,
                "report payload is signed by the installed Ed25519 identity",
            )
        )
    else:
        checks.append(
            check(
                "cryptographic_attestation",
                FAIL if hasattr(os, "geteuid") and os.geteuid() == 0 else NOT_CHECKED,
                signing_error or "non-root preflight reports are intentionally unsigned",
            )
        )
    timestamp = datetime.now(UTC)
    report: dict[str, Any] = {
        "schema_version": "prahari-zero-egress-attestation-v1",
        "attestation_id": f"zero-egress-{timestamp.strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}",
        "observed_at": timestamp.isoformat().replace("+00:00", "Z"),
        "command": command,
        "status": "pass" if all_pass(checks) else "not_attested",
        "policy": {
            "id": policy["policy_id"],
            "version": policy["policy_version"],
            "path": policy["_policy_path"],
            "sha256": policy["_policy_sha256"],
            "source_sha256": policy["_source_sha256"],
        },
        "tool": {
            "path": str(Path(__file__).resolve()),
            "sha256": sha256_bytes(Path(__file__).read_bytes()),
            "python": sys.version.split()[0],
            "hostname": socket.gethostname(),
            "effective_uid": os.geteuid() if hasattr(os, "geteuid") else None,
        },
        "checks": checks,
        "evidence": evidence,
        "limitations": [
            (
                "This attests the named dedicated WSL2/Containerlab boundary, "
                "not the Windows host or another Docker network."
            ),
            (
                "A passing report is point-in-time evidence; run verification after every "
                "network, container, policy, kernel, or service change."
            ),
            "This is engineering evidence, not a third-party security certification.",
        ],
    }
    if should_sign:
        report["integrity"] = sign_payload(canonical_json(report))
    else:
        report["integrity"] = {
            "algorithm": None,
            "key_id": None,
            "public_key_pem": None,
            "payload_sha256": sha256_bytes(canonical_json(report)),
            "signature_base64": None,
        }
    report["canonical_report_sha256"] = sha256_bytes(canonical_json(report))
    encoded_report = json.dumps(report, indent=2, sort_keys=True).encode() + b"\n"
    evidence_dir = evidence_dir.absolute()
    filename = evidence_dir / f"{report['attestation_id']}.json"
    descriptor, temporary_name = tempfile.mkstemp(prefix=".zero-egress-", dir=evidence_dir)
    try:
        os.fchmod(descriptor, 0o600)
        if hasattr(os, "fchown") and hasattr(os, "geteuid") and os.geteuid() == 0:
            os.fchown(descriptor, 0, 0)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded_report)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, filename)
        checksum_line = f"{sha256_bytes(encoded_report)}  {filename.name}\n".encode()
        atomic_write_evidence_sidecar(filename.with_suffix(".json.sha256"), checksum_line)
        directory_descriptor = os.open(evidence_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise
    return filename


def atomic_write_evidence_sidecar(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".zero-egress-checksum-", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        if hasattr(os, "fchown") and hasattr(os, "geteuid") and os.geteuid() == 0:
            os.fchown(descriptor, 0, 0)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def verify_evidence_report(report_path: Path, public_key_path: Path) -> dict[str, Any]:
    """Verify strict JSON, semantic checksum, file checksum, key binding, and signature."""
    report_raw = read_candidate_file(report_path, limit=20_000_000)
    report = parse_strict_json(report_raw, "attestation")
    if not isinstance(report, dict):
        raise ZeroEgressError("attestation must be a JSON object")
    integrity = require_exact_keys(
        report.get("integrity"),
        {
            "algorithm",
            "key_id",
            "payload_sha256",
            "public_key_pem",
            "signature_base64",
        },
        "attestation integrity",
    )
    if integrity["algorithm"] != "Ed25519":
        raise ZeroEgressError("attestation is not Ed25519-signed")
    claimed_report_sha = report.pop("canonical_report_sha256", None)
    if not isinstance(claimed_report_sha, str) or not re.fullmatch(
        r"[a-f0-9]{64}", claimed_report_sha
    ):
        raise ZeroEgressError("attestation canonical checksum is invalid")
    if sha256_bytes(canonical_json(report)) != claimed_report_sha:
        raise ZeroEgressError("attestation canonical checksum does not match")
    report.pop("integrity")
    payload = canonical_json(report)
    if sha256_bytes(payload) != integrity["payload_sha256"]:
        raise ZeroEgressError("attestation signed-payload checksum does not match")
    public_raw = read_candidate_file(public_key_path, limit=64_000)
    public_sha = sha256_bytes(public_raw)
    if integrity["key_id"] != f"sha256:{public_sha}":
        raise ZeroEgressError("attestation key ID does not match the trusted public key")
    if integrity["public_key_pem"] != public_raw.decode("ascii"):
        raise ZeroEgressError("embedded attestation public key differs from the trusted key")
    try:
        signature = base64.b64decode(integrity["signature_base64"], validate=True)
    except (TypeError, ValueError) as error:
        raise ZeroEgressError("attestation signature is not valid base64") from error
    checksum_path = report_path.with_suffix(".json.sha256")
    checksum_raw = read_candidate_file(checksum_path, limit=1024).decode("ascii")
    expected_line = f"{sha256_bytes(report_raw)}  {report_path.name}\n"
    if checksum_raw != expected_line:
        raise ZeroEgressError("attestation file checksum sidecar does not match")
    with tempfile.TemporaryDirectory(prefix="prahari-attestation-verify-") as directory:
        payload_path = Path(directory) / "payload"
        signature_path = Path(directory) / "signature"
        payload_path.write_bytes(payload)
        signature_path.write_bytes(signature)
        completed = run(
            [
                require_command("openssl"),
                "pkeyutl",
                "-verify",
                "-rawin",
                "-pubin",
                "-inkey",
                str(public_key_path),
                "-in",
                str(payload_path),
                "-sigfile",
                str(signature_path),
            ],
            check=False,
        )
    if completed.returncode != 0:
        raise ZeroEgressError("attestation Ed25519 signature verification failed")
    return {
        "attestation_id": report.get("attestation_id"),
        "status": report.get("status"),
        "canonical_report_sha256": claimed_report_sha,
        "file_sha256": sha256_bytes(report_raw),
        "key_id": integrity["key_id"],
    }


def require_root() -> None:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise ZeroEgressError("this operation requires sudo/root; no change was made")


def table_exists() -> bool:
    completed = run([require_command("nft"), "list", "table", "inet", TABLE_NAME], check=False)
    return completed.returncode == 0


def apply_firewall(policy: dict[str, Any], bridge: str, expected: list[dict[str, Any]]) -> str:
    require_root()
    current = live_nft_json()
    if current is not None:
        if current == expected:
            return "already_exact"
        raise ZeroEgressError("refusing to replace a present but non-matching PRAHARI nft table")
    config = nft_config(policy, bridge)
    run([require_command("nft"), "--check", "-f", "-"], input_text=config)
    run([require_command("nft"), "-f", "-"], input_text=config)
    if live_nft_json() != expected:
        # The table is isolated and was absent before this transaction, so it is
        # safe to remove our failed installation attempt.
        run(
            [require_command("nft"), "delete", "table", "inet", TABLE_NAME],
            check=False,
        )
        raise ZeroEgressError("post-apply verification failed; the new table was removed")
    return "installed"


def remove_firewall(policy: dict[str, Any], bridge: str, expected: list[dict[str, Any]]) -> str:
    require_root()
    current = live_nft_json()
    if current is None:
        return "already_absent"
    if current != expected:
        raise ZeroEgressError("refusing to delete a present but non-matching PRAHARI nft table")
    run([require_command("nft"), "delete", "table", "inet", TABLE_NAME])
    if table_exists():
        raise ZeroEgressError("firewall table still exists after removal")
    return "removed"


def nft_snapshot_owner(snapshot: list[dict[str, Any]]) -> str | None:
    for item in snapshot:
        table = item.get("table")
        if not isinstance(table, dict):
            continue
        if table.get("family") == "inet" and table.get("name") == TABLE_NAME:
            comment = table.get("comment")
            return comment if isinstance(comment, str) else None
    return None


def reconcile_firewall(
    policy: dict[str, Any], bridge: str, expected: list[dict[str, Any]]
) -> str:
    """Install exact rules or atomically replace only a table carrying our owner marker."""
    require_root()
    current = live_nft_json()
    if current is None:
        return apply_firewall(policy, bridge, expected)
    if current == expected:
        return "already_exact"
    owner = nft_snapshot_owner(current)
    if owner is None or not owner.startswith(OWNER_COMMENT_PREFIX):
        raise ZeroEgressError("refusing to replace an unowned PRAHARI-named nft table")
    config = f"delete table inet {TABLE_NAME}\n{nft_config(policy, bridge)}"
    nft = require_command("nft")
    run([nft, "--check", "-f", "-"], input_text=config)
    # nft applies a complete batch atomically. A parse/evaluation error leaves
    # the previously owned table intact rather than creating an open interval.
    run([nft, "-f", "-"], input_text=config)
    if live_nft_json() != expected:
        raise ZeroEgressError("atomic nft repair completed but exact verification failed")
    return "replaced_owned_drift"


def install_manifest_payload(service_user: str, policy: dict[str, Any], status: str) -> bytes:
    previous: dict[str, Any] | None = None
    if INSTALL_MANIFEST_PATH.exists() and not INSTALL_MANIFEST_PATH.is_symlink():
        try:
            candidate = parse_strict_json(INSTALL_MANIFEST_PATH.read_bytes(), "install manifest")
        except OSError as error:
            raise ZeroEgressError("cannot read the existing install manifest") from error
        if isinstance(candidate, dict):
            previous = candidate
    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    installed_at = (
        previous.get("installed_at")
        if previous and isinstance(previous.get("installed_at"), str)
        else timestamp
    )
    generation = (
        int(previous.get("generation", 0)) + 1
        if previous and type(previous.get("generation")) is int
        else 1
    )
    managed = {
        str(path): {
            "mode": oct(mode),
            "sha256": sha256_bytes(content),
        }
        for path, (content, mode) in sorted(
            expected_managed_files(service_user).items(), key=lambda item: str(item[0])
        )
    }
    body: dict[str, Any] = {
        "schema_version": "prahari-zero-egress-install-v1",
        "status": status,
        "service_user": service_user,
        "installed_at": installed_at,
        "updated_at": timestamp,
        "generation": generation,
        "tool_sha256": sha256_bytes(TRUSTED_TOOL_PATH.read_bytes()),
        "policy_sha256": policy["_policy_sha256"],
        "policy_source_sha256": policy["_source_sha256"],
        "attestation_key_id": signing_key_pair(create=False)["key_id"],
        "managed_files": managed,
    }
    body["manifest_sha256"] = sha256_bytes(canonical_json(body))
    return json.dumps(body, indent=2, sort_keys=True).encode() + b"\n"


def sync_lifecycle_files(service_user: str, policy: dict[str, Any]) -> list[str]:
    require_trusted_runtime_files(TRUSTED_POLICY_PATH)
    signing_key_pair(create=True)
    changed: list[str] = []
    for path, (content, mode) in expected_managed_files(service_user).items():
        if atomic_install_root_file(path, content, mode):
            changed.append(str(path))
    manifest = install_manifest_payload(service_user, policy, "installed")
    if atomic_install_root_file(INSTALL_MANIFEST_PATH, manifest, 0o600):
        changed.append(str(INSTALL_MANIFEST_PATH))
    return changed


def install_manifest_snapshot(
    service_user: str, policy: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    try:
        metadata = secure_path_metadata(INSTALL_MANIFEST_PATH, regular=True)
        if stat.S_IMODE(INSTALL_MANIFEST_PATH.lstat().st_mode) != 0o600:
            raise ZeroEgressError("install manifest must have exact mode 0600")
        document = parse_strict_json(INSTALL_MANIFEST_PATH.read_bytes(), "install manifest")
        document = require_exact_keys(
            document,
            {
                "schema_version",
                "status",
                "service_user",
                "installed_at",
                "updated_at",
                "generation",
                "tool_sha256",
                "policy_sha256",
                "policy_source_sha256",
                "attestation_key_id",
                "managed_files",
                "manifest_sha256",
            },
            "install manifest",
        )
        claimed = document.pop("manifest_sha256")
        expected_managed = {
            str(path): {"mode": oct(mode), "sha256": sha256_bytes(content)}
            for path, (content, mode) in expected_managed_files(service_user).items()
        }
        valid = all(
            (
                document["schema_version"] == "prahari-zero-egress-install-v1",
                document["status"] == "installed",
                document["service_user"] == service_user,
                type(document["generation"]) is int and document["generation"] >= 1,
                document["tool_sha256"] == sha256_bytes(TRUSTED_TOOL_PATH.read_bytes()),
                document["policy_sha256"] == policy["_policy_sha256"],
                document["policy_source_sha256"] == policy["_source_sha256"],
                document["attestation_key_id"] == signing_key_pair(create=False)["key_id"],
                document["managed_files"] == expected_managed,
                claimed == sha256_bytes(canonical_json(document)),
            )
        )
        document["manifest_sha256"] = claimed
        return {"metadata": metadata, "document": document}, valid
    except (KeyError, OSError, ZeroEgressError) as error:
        return {"error": str(error), "path": str(INSTALL_MANIFEST_PATH)}, False


def user_systemctl(service_user: str, *arguments: str, check_result: bool = True) -> None:
    prefix, environment = sudo_user_systemctl_prefix(service_user)
    run([*prefix, *arguments], check=check_result, env=environment, timeout=60)


def enable_lifecycle(
    service_user: str,
    changed: list[str],
    *,
    restart_docker: bool,
) -> dict[str, Any]:
    """Activate persisted controls in security-preserving order."""
    require_root()
    systemctl = require_command("systemctl")
    run([require_command("modprobe"), "br_netfilter"])
    run([require_command("sysctl"), "-p", str(SYSCTL_PATH)])
    run([systemctl, "daemon-reload"])
    user_systemctl(service_user, "daemon-reload")
    run([systemctl, "enable", "prahari-zero-egress-early.service"])
    # Enforce before a Docker restart. The Docker drop-in Requires/After this
    # active oneshot on every subsequent daemon start, including boot.
    run([systemctl, "restart", "prahari-zero-egress-early.service"], timeout=60)
    docker_dropin = str(SYSTEMD_SYSTEM_DIR / DOCKER_DROPIN_SUFFIX)
    docker_restarted = restart_docker or docker_dropin in changed
    if docker_restarted:
        run([systemctl, "restart", "docker.service"], timeout=120)
    changed_user_dropin = any(
        path.startswith(str(SYSTEMD_USER_DIR) + os.sep) for path in changed
    )
    if changed_user_dropin:
        for unit in sorted(EXPECTED_USER_SERVICES):
            user_systemctl(service_user, "try-restart", unit, check_result=False)
    run([systemctl, "enable", "--now", "prahari-zero-egress.timer"])
    return {
        "changed_files": sorted(changed),
        "docker_restarted": docker_restarted,
        "user_units_reloaded": True,
        "early_firewall_started": True,
        "repair_timer_started": True,
    }


def preinstall_runtime_snapshot(
    policy: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    network, network_checks = docker_network_snapshot(policy)
    containers, container_checks = docker_container_snapshot(policy, network)
    kernel, kernel_checks = kernel_snapshot()
    return {
        "network": network,
        "containers": containers,
        "kernel": kernel,
    }, [*network_checks, *container_checks, *kernel_checks]


def enforce_early(policy: dict[str, Any]) -> dict[str, Any]:
    """Load the fixed bridge policy without requiring Docker to be running."""
    require_trusted_runtime_files(TRUSTED_POLICY_PATH)
    kernel, kernel_checks = kernel_snapshot()
    if not all_pass(kernel_checks):
        raise ZeroEgressError("bridge netfilter prerequisites are not enabled")
    bridge = policy["boundary"]["docker_network"]["options"][
        "com.docker.network.bridge.name"
    ]
    rendered = nft_config(policy, bridge)
    expected = isolated_nft_json(rendered)
    result = reconcile_firewall(policy, bridge, expected)
    return {
        "mutation_result": result,
        "bridge": bridge,
        "kernel": kernel,
        "rendered_nftables_sha256": sha256_bytes(rendered.encode()),
    }


def validate_migration_candidates(tool_path: Path, policy_path: Path) -> tuple[bytes, bytes]:
    tool_raw = read_candidate_file(tool_path)
    policy_raw = read_candidate_file(policy_path)
    validate_tool_candidate(tool_raw)
    load_policy_bytes(policy_raw, source=str(policy_path.absolute()))
    return tool_raw, policy_raw


def migrate_installation(
    source_tool: Path,
    source_policy: Path,
    service_user: str,
) -> dict[str, Any]:
    require_trusted_runtime_files(TRUSTED_POLICY_PATH)
    tool_raw, policy_raw = validate_migration_candidates(source_tool, source_policy)
    old_tool = TRUSTED_TOOL_PATH.read_bytes()
    old_policy = TRUSTED_POLICY_PATH.read_bytes()
    tool_changed = tool_raw != old_tool
    policy_changed = policy_raw != old_policy
    if not tool_changed and not policy_changed:
        return {"result": "already_current", "tool_changed": False, "policy_changed": False}
    try:
        atomic_install_root_file(TRUSTED_TOOL_PATH, tool_raw, 0o755)
        atomic_install_root_file(TRUSTED_POLICY_PATH, policy_raw, 0o644)
        completed = run(
            [
                str(TRUSTED_TOOL_PATH),
                "repair",
                "--service-user",
                service_user,
                "--confirm",
                REPAIR_CONFIRMATION,
            ],
            check=False,
            timeout=180,
        )
        if completed.returncode != 0:
            raise ZeroEgressError("migrated tool failed its repair/verification command")
    except BaseException as error:
        atomic_install_root_file(TRUSTED_TOOL_PATH, old_tool, 0o755)
        atomic_install_root_file(TRUSTED_POLICY_PATH, old_policy, 0o644)
        run(
            [
                str(TRUSTED_TOOL_PATH),
                "repair",
                "--service-user",
                service_user,
                "--confirm",
                REPAIR_CONFIRMATION,
            ],
            check=False,
            timeout=180,
        )
        if isinstance(error, ZeroEgressError):
            raise
        raise ZeroEgressError("migration failed and trusted files were rolled back") from error
    return {
        "result": "migrated_and_repaired",
        "tool_changed": tool_changed,
        "policy_changed": policy_changed,
        "new_tool_sha256": sha256_bytes(tool_raw),
        "new_policy_source_sha256": sha256_bytes(policy_raw),
    }


def remove_exact_managed_file(path: Path, expected: bytes) -> str:
    if not path.exists() and not path.is_symlink():
        return "already_absent"
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_mode & 0o022
        or path.read_bytes() != expected
    ):
        raise ZeroEgressError(f"refusing to remove drifted or unowned managed file: {path}")
    path.unlink()
    return "removed"


def uninstall_lifecycle(
    policy: dict[str, Any], service_user: str
) -> tuple[dict[str, Any], list[Path]]:
    require_trusted_runtime_files(TRUSTED_POLICY_PATH)
    signing_key_pair(create=False)
    systemctl = require_command("systemctl")
    run([systemctl, "disable", "--now", "prahari-zero-egress.timer"], check=False)
    run([systemctl, "stop", "prahari-zero-egress.service"], check=False)
    bridge = policy["boundary"]["docker_network"]["options"][
        "com.docker.network.bridge.name"
    ]
    rendered = nft_config(policy, bridge)
    expected = isolated_nft_json(rendered)
    firewall_result = remove_firewall(policy, bridge, expected)
    removed: dict[str, str] = {}
    managed = expected_managed_files(service_user)
    for path, (content, _) in managed.items():
        removed[str(path)] = remove_exact_managed_file(path, content)
    run([systemctl, "daemon-reload"])
    user_systemctl(service_user, "daemon-reload")
    # Remove the daemon-level cgroup filter from the running Docker process.
    run([systemctl, "restart", "docker.service"], timeout=120)
    for unit in sorted(EXPECTED_USER_SERVICES):
        user_systemctl(service_user, "try-restart", unit, check_result=False)
    atomic_install_root_file(
        INSTALL_MANIFEST_PATH,
        install_manifest_payload(service_user, policy, "uninstalled"),
        0o600,
    )
    empty_directories = {
        path.parent for path in managed if path.parent not in {Path("/etc"), SYSTEMD_SYSTEM_DIR}
    }
    for directory in sorted(empty_directories, key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    core_files = [TRUSTED_POLICY_PATH, ATTESTATION_KEY_PATH, TRUSTED_TOOL_PATH]
    return {
        "firewall": firewall_result,
        "managed_files": removed,
        "docker_restarted": True,
        "retained_audit_public_key": str(ATTESTATION_PUBLIC_KEY_PATH),
        "retained_evidence_directory": str(TRUSTED_EVIDENCE_DIR),
    }, core_files


def finalize_uninstall(core_files: list[Path], expected: dict[Path, bytes]) -> None:
    for path in core_files:
        remove_exact_managed_file(path, expected[path])


def command_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Install, enforce, verify, repair, migrate, or uninstall PRAHARI's exact "
            "zero-egress boundary."
        )
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=(
            "dry-run",
            "install",
            "migrate",
            "enforce",
            "repair",
            "apply",
            "verify",
            "remove",
            "uninstall",
            "verify-evidence",
        ),
        default="dry-run",
    )
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--evidence-dir", type=Path, default=DEFAULT_EVIDENCE_DIR)
    parser.add_argument("--confirm", default="")
    parser.add_argument(
        "--service-user",
        default=os.environ.get("SUDO_USER") or os.environ.get("USER") or "",
        help="Non-root owner of the PRAHARI user services (normally the WSL login).",
    )
    parser.add_argument("--source-tool", type=Path)
    parser.add_argument("--source-policy", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--public-key", type=Path, default=ATTESTATION_PUBLIC_KEY_PATH)
    parser.add_argument(
        "--skip-probes",
        action="store_true",
        help="Skip live allow/deny probes during verify (report cannot pass).",
    )
    parser.add_argument(
        "--static-only",
        action="store_true",
        help=(
            "For dry-run only: validate and render policy without Docker, systemd, "
            "root, or live firewall access. The report is intentionally unsigned."
        ),
    )
    return parser


def require_confirmation(actual: str, expected: str) -> None:
    if actual != expected:
        raise ZeroEgressError(f"--confirm {expected} is required; no change was made")


def require_service_user_argument(value: str) -> str:
    if not value:
        raise ZeroEgressError("--service-user is required")
    user, _ = resolve_service_user(value)
    return user


def render_firewall(policy: dict[str, Any], bridge: str) -> tuple[str, list[dict[str, Any]]]:
    rendered = nft_config(policy, bridge)
    return rendered, isolated_nft_json(rendered)


def static_firewall_preflight(
    policy: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """Render and, when available, compile policy without inspecting live state."""
    bridge = policy["boundary"]["docker_network"]["options"][
        "com.docker.network.bridge.name"
    ]
    rendered = nft_config(policy, bridge)
    expected_new_accepts = len(policy["allowed_new_tcp_flows"]) + len(
        policy["allowed_new_host_tcp_flows"]
    )
    required_denials = {
        "deny-bridge-ipv6-forward",
        "deny-bridge-forward-default",
        "deny-lab-source-forward-fallback",
        "deny-bridge-ipv6-host-input",
        "deny-bridge-host-input-default",
        "deny-lab-source-host-input-fallback",
    }
    missing_denials = sorted(
        comment for comment in required_denials if f'comment "{comment}"' not in rendered
    )
    structure_exact = all(
        (
            rendered.startswith(f"table inet {TABLE_NAME} {{\n"),
            rendered.endswith("}\n"),
            rendered.count("type filter hook forward priority -10; policy accept;") == 1,
            rendered.count("type filter hook input priority -10; policy accept;") == 1,
            rendered.count("ct state new counter accept") == expected_new_accepts,
            not missing_denials,
            (
                f'comment "{OWNER_COMMENT_PREFIX}{policy["_policy_sha256"]}"'
                in rendered
            ),
        )
    )
    checks = [
        check(
            "static_firewall_structure",
            PASS if structure_exact else FAIL,
            (
                "rendered rules contain the exact owned allowlist and fail-closed denials"
                if structure_exact
                else "rendered rules are missing an exact allowlist or fail-closed invariant"
            ),
            expected_new_accepts=expected_new_accepts,
            missing_denials=missing_denials,
        )
    ]
    compilation: dict[str, Any]
    if shutil.which("unshare") and shutil.which("nft"):
        try:
            canonical_rules = isolated_nft_json(rendered)
        except ZeroEgressError as error:
            checks.append(
                check(
                    "nftables_syntax",
                    NOT_CHECKED,
                    "isolated nftables compilation is unavailable to this unprivileged user",
                    error=str(error),
                )
            )
            compilation = {"attempted": True, "available": False, "error": str(error)}
        else:
            checks.append(
                check(
                    "nftables_syntax",
                    PASS,
                    "policy compiled in an isolated network namespace",
                )
            )
            compilation = {
                "attempted": True,
                "available": True,
                "canonical_rules": canonical_rules,
            }
    else:
        checks.append(
            check(
                "nftables_syntax",
                NOT_CHECKED,
                "unshare and nft are required for isolated syntax compilation",
            )
        )
        compilation = {"attempted": False, "available": False}
    return {
        "bridge": bridge,
        "rendered_nftables_sha256": sha256_bytes(rendered.encode()),
        "compilation": compilation,
    }, checks, rendered


def emit_report(
    command: str,
    policy: dict[str, Any],
    checks: list[dict[str, Any]],
    evidence: dict[str, Any],
    evidence_dir: Path,
) -> tuple[Path, bool]:
    report = write_evidence(command, policy, checks, evidence, evidence_dir)
    passed = all_pass(checks)
    print(f"Evidence report: {report}")
    if passed:
        print(f"PASS: {command} completed with exact, checksummed evidence.")
    else:
        print("NOT ATTESTED: inspect failed or incomplete checks in the evidence report.")
    return report, passed


def main(argv: Sequence[str] | None = None) -> int:
    arguments = command_parser().parse_args(argv)
    checks: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {}
    try:
        if arguments.static_only and arguments.command != "dry-run":
            raise ZeroEgressError("--static-only is valid only with dry-run")
        if arguments.command == "verify-evidence":
            if arguments.report is None:
                raise ZeroEgressError("--report is required for verify-evidence")
            result = verify_evidence_report(
                arguments.report.absolute(), arguments.public_key.absolute()
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            print("PASS: checksum, key binding, and Ed25519 signature are valid.")
            return 0

        mutation_confirmations = {
            "install": INSTALL_CONFIRMATION,
            "migrate": MIGRATE_CONFIRMATION,
            "enforce": REPAIR_CONFIRMATION,
            "repair": REPAIR_CONFIRMATION,
            "apply": APPLY_CONFIRMATION,
            "remove": REMOVE_CONFIRMATION,
            "uninstall": UNINSTALL_CONFIRMATION,
        }
        if arguments.command in mutation_confirmations:
            require_root()
            require_confirmation(arguments.confirm, mutation_confirmations[arguments.command])

        policy_path = arguments.policy.absolute()
        if arguments.command in mutation_confirmations:
            require_trusted_runtime_files(policy_path)
        policy = load_policy(policy_path)
        checks.append(check("policy_valid", PASS, "policy schema and allowlists are valid"))

        if arguments.command == "dry-run" and arguments.static_only:
            static_evidence, static_checks, rendered = static_firewall_preflight(policy)
            evidence["static_preflight"] = static_evidence
            evidence["rendered_nftables"] = rendered
            checks.extend(static_checks)
            checks.append(
                check(
                    "mutation",
                    PASS,
                    "static dry-run only: no runtime state was read or changed",
                    required_install_confirmation=INSTALL_CONFIRMATION,
                )
            )
            emit_report(arguments.command, policy, checks, evidence, arguments.evidence_dir)
            failed = any(item["status"] == FAIL for item in checks)
            if not failed:
                print("STATIC PREFLIGHT PASS: policy is valid and no state changed.")
            return 2 if failed else 0

        if arguments.command == "enforce":
            evidence["early_enforcement"] = enforce_early(policy)
            checks.extend(
                [
                    check(
                        "pre_docker_firewall",
                        PASS,
                        "exact IPv4/IPv6 boundary is loaded without a Docker dependency",
                    ),
                    check(
                        "trusted_ext4_install",
                        PASS,
                        "enforcement executed from exact root-owned ext4 tool and policy",
                    ),
                ]
            )
            _, passed = emit_report(
                arguments.command, policy, checks, evidence, arguments.evidence_dir
            )
            return 0 if passed else 2

        if arguments.command == "migrate":
            service_user = require_service_user_argument(arguments.service_user)
            if arguments.source_tool is None or arguments.source_policy is None:
                raise ZeroEgressError("migrate requires --source-tool and --source-policy")
            evidence["migration"] = migrate_installation(
                arguments.source_tool.absolute(),
                arguments.source_policy.absolute(),
                service_user,
            )
            policy = load_policy(TRUSTED_POLICY_PATH)
            checks.append(
                check(
                    "transactional_migration",
                    PASS,
                    "candidate tool and policy were validated, installed, and repaired",
                )
            )
            _, passed = emit_report(
                arguments.command, policy, checks, evidence, arguments.evidence_dir
            )
            return 0 if passed else 2

        service_user = ""
        if arguments.command != "dry-run" or arguments.service_user:
            service_user = require_service_user_argument(arguments.service_user)

        if arguments.command == "install":
            runtime, runtime_checks = preinstall_runtime_snapshot(policy)
            checks.extend(runtime_checks)
            evidence["preinstall_runtime"] = runtime
            if not all_pass(runtime_checks):
                raise ZeroEgressError(
                    "install preflight failed; no lifecycle or firewall state was changed"
                )
            changed = sync_lifecycle_files(service_user, policy)
            evidence["activation"] = enable_lifecycle(
                service_user, changed, restart_docker=True
            )
            bridge = policy["boundary"]["docker_network"]["options"][
                "com.docker.network.bridge.name"
            ]
            rendered, expected = render_firewall(policy, bridge)
            firewall_check, firewall_snapshot = nft_firewall_check(policy, bridge, expected)
            checks.append(firewall_check)
            evidence["firewall"] = firewall_snapshot
            systemd, systemd_checks = systemd_profile_snapshot(policy, service_user)
            checks.extend(systemd_checks)
            evidence["systemd"] = systemd
            evidence["rendered_nftables_sha256"] = sha256_bytes(rendered.encode())
            _, passed = emit_report(
                arguments.command, policy, checks, evidence, arguments.evidence_dir
            )
            print("NEXT: recover the lab after the intentional Docker restart, then run verify.")
            return 0 if passed else 2

        if arguments.command == "uninstall":
            expected_core = {
                TRUSTED_POLICY_PATH: TRUSTED_POLICY_PATH.read_bytes(),
                ATTESTATION_KEY_PATH: ATTESTATION_KEY_PATH.read_bytes(),
                TRUSTED_TOOL_PATH: TRUSTED_TOOL_PATH.read_bytes(),
            }
            result, core_files = uninstall_lifecycle(policy, service_user)
            evidence["uninstall"] = result
            checks.append(
                check(
                    "lifecycle_uninstalled",
                    PASS,
                    "firewall and persistent cgroup/systemd controls were removed exactly",
                )
            )
            report, passed = emit_report(
                arguments.command, policy, checks, evidence, arguments.evidence_dir
            )
            if not passed:
                raise ZeroEgressError("uninstall report could not be signed; core files retained")
            finalize_uninstall(core_files, expected_core)
            print(f"Final signed uninstall evidence retained at: {report}")
            print(f"Verification public key retained at: {ATTESTATION_PUBLIC_KEY_PATH}")
            return 0

        if arguments.command == "repair":
            changed = sync_lifecycle_files(service_user, policy)
            evidence["repair_activation"] = enable_lifecycle(
                service_user, changed, restart_docker=False
            )
            evidence["early_enforcement"] = enforce_early(policy)

        if arguments.command in {"apply", "verify", "remove", "repair"}:
            require_trusted_runtime_files(policy_path)

        if service_user:
            runtime, runtime_checks = runtime_snapshot(policy, service_user)
        else:
            runtime, runtime_checks = preinstall_runtime_snapshot(policy)
        checks.extend(runtime_checks)
        evidence["runtime"] = runtime
        bridge = runtime["network"]["bridge"]
        rendered, expected = render_firewall(policy, bridge)
        evidence["rendered_nftables_sha256"] = sha256_bytes(rendered.encode())
        checks.append(
            check(
                "nftables_syntax",
                PASS,
                "policy compiled in an isolated network namespace",
            )
        )
        firewall_check, firewall_snapshot = nft_firewall_check(policy, bridge, expected)
        checks.append(firewall_check)
        evidence["firewall"] = firewall_snapshot

        if arguments.command == "dry-run":
            checks = [item for item in checks if item["id"] != "nftables_policy_exact"]
            checks.append(
                check(
                    "mutation",
                    PASS,
                    "dry-run only: no firewall or service state was changed",
                    required_apply_confirmation=APPLY_CONFIRMATION,
                    required_install_confirmation=INSTALL_CONFIRMATION,
                )
            )
            evidence["rendered_nftables"] = rendered
        elif arguments.command in {"apply", "repair"}:
            prerequisite_ids = {
                "policy_valid",
                "docker_network_exact",
                "docker_bridge_present",
                "container_inventory_exact",
                "container_network_namespaces_exact",
                "bridge_netfilter_enabled",
                "bridge_ipv6_netfilter_enabled",
                "user_service_ip_firewalls",
                "docker_daemon_ip_firewall",
                "root_owned_ext4_lifecycle_files",
                "pre_docker_fail_closed_ordering",
                "zero_egress_persistence",
                "trusted_ext4_install",
                "install_manifest_exact",
                "nftables_syntax",
            }
            prerequisites = [item for item in checks if item["id"] in prerequisite_ids]
            if not all_pass(prerequisites):
                raise ZeroEgressError(
                    f"{arguments.command} prerequisites failed; firewall was left fail-closed"
                )
            result = reconcile_firewall(policy, bridge, expected)
            checks = [item for item in checks if item["id"] != "nftables_policy_exact"]
            firewall_check, firewall_snapshot = nft_firewall_check(policy, bridge, expected)
            checks.append(firewall_check)
            evidence["firewall"] = firewall_snapshot
            probe_checks, probe_evidence = active_probes(policy)
            checks.extend(probe_checks)
            evidence["active_verification"] = probe_evidence
            evidence["mutation_result"] = result
        elif arguments.command == "verify":
            if arguments.skip_probes:
                checks.append(
                    check("active_zero_egress_probes", NOT_CHECKED, "probes skipped by operator")
                )
            elif firewall_check["status"] == PASS:
                probe_checks, probe_evidence = active_probes(policy)
                checks.extend(probe_checks)
                evidence["active_verification"] = probe_evidence
            else:
                checks.append(
                    check(
                        "active_zero_egress_probes",
                        NOT_CHECKED,
                        "blocked probe was not attempted because firewall verification failed",
                    )
                )
        elif arguments.command == "remove":
            timer_state = run(
                [require_command("systemctl"), "is-active", "prahari-zero-egress.timer"],
                check=False,
            )
            if timer_state.returncode == 0:
                raise ZeroEgressError(
                    "repair timer is active; use uninstall to avoid automatic re-enforcement"
                )
            result = remove_firewall(policy, bridge, expected)
            checks = [item for item in checks if item["id"] != "nftables_policy_exact"]
            absent = not table_exists()
            checks.append(
                check(
                    "nftables_policy_removed",
                    PASS if absent else FAIL,
                    (
                        "PRAHARI nftables table is absent"
                        if absent
                        else "PRAHARI nftables table remains"
                    ),
                )
            )
            evidence["mutation_result"] = result

        _, passed = emit_report(
            arguments.command, policy, checks, evidence, arguments.evidence_dir
        )
        if arguments.command == "dry-run":
            print("DRY RUN: no state changed.")
            print(
                f"Install requires the trusted copy and --confirm {INSTALL_CONFIRMATION}; "
                f"firewall-only apply requires --confirm {APPLY_CONFIRMATION}."
            )
        elif arguments.command == "remove":
            print("Firewall enforcement removed; service-level controls were not changed.")
        return 0 if passed else 2
    except (OSError, UnicodeError, ZeroEgressError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
