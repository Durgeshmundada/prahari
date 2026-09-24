"""Prepare an isolated, authenticated self-hosted pilot; never approve a model."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
from pathlib import Path

from prahari.identity import hash_password

LAB = "prahari-owned"
SUBNET = "172.30.254.0/24"
FRR = (
    "quay.io/frrouting/frr@sha256:e47e67bd612030cb1bedee2bde85b73913f2ea021573b749deb21d94940f03c1"
)
PROBE = "prahari-lab-app@sha256:66624bf03c707cbe2ae19a72aedbca3c9d5cfdf424ee6a9cc529d2378d5bfb9e"


def write(path: Path, text: str, mode: int = 0o444) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(text)
    path.chmod(mode)


def prepare(root: Path, repo: Path, images: dict[str, str], port: int = 8520) -> None:
    if root.exists() or root.is_symlink():
        raise ValueError("pilot state must be a new directory; existing credentials are preserved")
    if not 1024 <= port <= 65535:
        raise ValueError("port must be 1024..65535")
    for image in images.values():
        if not re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}", image):
            raise ValueError("all server images must be pinned by digest")
    root = root.absolute()
    repo = repo.resolve(strict=True)
    # Generated paths enter env/YAML files. Restrict the pilot's Linux deployment
    # paths instead of attempting shell interpolation of arbitrary path text.
    for path in (root, repo):
        if not re.fullmatch(r"[/a-zA-Z0-9._:-]+", str(path)):
            raise ValueError("pilot paths must contain only simple path characters")
    root.mkdir(parents=True, mode=0o700)
    runtime = root / "runtime"
    runtime.mkdir(mode=0o755)
    for name in ("src", "scripts", "config"):
        shutil.copytree(
            repo / name, runtime / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
    shutil.copytree(repo / "network-lab/configs", runtime / "network-lab/configs")
    shutil.copytree(
        repo / "network-lab/scripts",
        runtime / "network-lab/scripts",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copytree(
        repo / "deploy",
        runtime / "deploy",
        ignore=shutil.ignore_patterns("secrets", ".env*", "__pycache__", "*.pyc"),
    )
    for name in ("pyproject.toml", "README.md"):
        shutil.copyfile(repo / name, runtime / name)
    secret_dir = root / "secrets"
    secret_dir.mkdir(mode=0o700)
    collector = secrets.token_hex(32)
    traffic = secrets.token_hex(32)
    monitor = secrets.token_hex(32)
    write(secret_dir / "collector-token", collector)
    write(secret_dir / "traffic-token", traffic)
    write(secret_dir / "monitor-token", monitor)
    write(
        secret_dir / "api-keys.json",
        json.dumps(
            {
                collector: {"role": "collector", "actor_id": "owned-collector"},
                monitor: {"role": "viewer", "actor_id": "owned-monitor"},
            }
        ),
    )
    users, credentials = {}, []
    for username, role in (("operator", "operator"), ("reviewer", "reviewer")):
        password = secrets.token_urlsafe(24)
        users[username] = {
            "role": role,
            "actor_id": f"owned-{username}",
            "password_hash": hash_password(password),
        }
        credentials.append(f"{username}: {password}")
    write(secret_dir / "browser-users.json", json.dumps(users))
    write(root / "credentials.txt", "\n".join(credentials) + "\n", 0o600)
    env = {
        "PRAHARI_IMAGE": images["api"],
        "PRAHARI_DASHBOARD_IMAGE": images["dashboard"],
        "POSTGRES_IMAGE": images["postgres"],
        "PRAHARI_HOST_PORT": str(port),
        "PRAHARI_ENVIRONMENT": "development",
        "PRAHARI_DATA_MODE": "live",
        "PRAHARI_AUTH_MODE": "api_key",
        "PRAHARI_DATABASE_NAME": "prahari",
        "POSTGRES_BOOTSTRAP_USER": "prahari_bootstrap",
        "PRAHARI_MIGRATOR_USER": "prahari_migrator",
        "PRAHARI_APP_USER": "prahari_app",
        "PRAHARI_CORS_ORIGINS": f"http://127.0.0.1:{port}",
        "PRAHARI_API_KEYS_JSON_FILE": str(secret_dir / "api-keys.json"),
    }
    for prefix, user in (
        ("POSTGRES_BOOTSTRAP", "prahari_bootstrap"),
        ("PRAHARI_MIGRATOR", "prahari_migrator"),
        ("PRAHARI_APP", "prahari_app"),
    ):
        password = secrets.token_hex(32)
        password_path = secret_dir / f"{user}-password"
        write(password_path, password)
        env[f"{prefix}_PASSWORD_FILE"] = str(password_path)
        if prefix != "POSTGRES_BOOTSTRAP":
            url_path = secret_dir / f"{user}-url"
            write(url_path, f"postgresql+psycopg://{user}:{password}@postgres:5432/prahari")
            env[f"{prefix}_DATABASE_URL_FILE"] = str(url_path)
    write(root / "pilot.env", "\n".join(f"{k}={v}" for k, v in env.items()) + "\n", 0o600)
    overlay = {
        "name": LAB,
        "services": {
            "api": {
                "environment": {
                    "PRAHARI_BROWSER_AUTH_ENABLED": "true",
                    "PRAHARI_BROWSER_USERS_FILE": "/run/secrets/browser-users.json",
                    "PRAHARI_BROWSER_ORIGIN": f"http://127.0.0.1:{port}",
                    "PRAHARI_ACTION_ADAPTER_ENABLED": "false",
                    "PRAHARI_COPILOT_ENABLED": "true",
                    "PRAHARI_OLLAMA_URL": "http://172.30.254.19:11434",
                    "PRAHARI_OLLAMA_ALLOWED_PRIVATE_IP": "172.30.254.19",
                    "PRAHARI_OLLAMA_MODEL": "qwen3:0.6b",
                    "PRAHARI_OLLAMA_TIMEOUT_SECONDS": "90",
                },
                "volumes": [f"{secret_dir}/browser-users.json:/run/secrets/browser-users.json:ro"],
                "networks": {"app_internal": {}, "pilot_mgmt": {"ipv4_address": "172.30.254.18"}},
            },
            "ollama": {
                "image": images["ollama"],
                "pull_policy": "never",
                "environment": {
                    "OLLAMA_HOST": "0.0.0.0:11434",
                    "OLLAMA_NUM_PARALLEL": "1",
                    "OLLAMA_MAX_LOADED_MODELS": "1",
                    "OLLAMA_NO_CLOUD": "true",
                    "OLLAMA_MODELS": "/models/models",
                    "HOME": "/home/ollama",
                },
                "volumes": ["ollama_models:/models:ro"],
                "user": "10001:10001",
                "networks": {"pilot_mgmt": {"ipv4_address": "172.30.254.19"}},
                "read_only": True,
                "tmpfs": ["/tmp:size=64m", "/home/ollama:uid=10001,gid=10001,mode=0700,size=4m"],
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "restart": "unless-stopped",
                "mem_limit": "2g",
                "cpus": 2,
                "pids_limit": 256,
                "healthcheck": {
                    "test": ["CMD", "/bin/ollama", "list"],
                    "interval": "15s",
                    "timeout": "5s",
                    "retries": 6,
                    "start_period": "20s",
                },
                "logging": {"driver": "local", "options": {"max-size": "10m", "max-file": "3"}},
            },
        },
        "networks": {"pilot_mgmt": {"external": True, "name": f"{LAB}_mgmt"}},
        "volumes": {"ollama_models": {}},
    }
    write(root / "compose.pilot.json", json.dumps(overlay, indent=2))
    nodes = {}
    routers = ["branch-edge", "pe-primary", "core", "pe-secondary", "pe-backup", "dc-edge"]
    for index, name in enumerate(routers, 11):
        nodes[name] = {
            "kind": "linux",
            "image": FRR,
            "mgmt-ipv4": f"172.30.254.{index}",
            "privileged": True,
            "sysctls": {"net.ipv4.ip_forward": "1"},
            "binds": [
                f"{runtime}/network-lab/configs/{name}/frr.conf:/etc/frr/frr.conf:ro",
                f"{runtime}/network-lab/configs/frr/daemons:/etc/frr/daemons:ro",
                f"{runtime}/network-lab/configs/frr/vtysh.conf:/etc/frr/vtysh.conf:ro",
            ],
        }
    for name, octet, lan in (("branch-client", 10, "10"), ("app-server", 17, "20")):
        nodes[name] = {
            "kind": "linux",
            "image": PROBE,
            "mgmt-ipv4": f"172.30.254.{octet}",
            "privileged": True,
            "exec": [
                "ip link set dev eth1 up",
                f"ip address replace 172.16.{lan}.2/24 dev eth1",
                f"ip route replace default via 172.16.{lan}.1",
            ],
            "binds": [
                f"{runtime}/src:/app/src:ro",
                f"{runtime}/scripts:/app/scripts:ro",
                f"{secret_dir}/traffic-token:/run/prahari-traffic-token:ro",
                f"{secret_dir}/collector-token:/run/prahari-lab-api-key:ro",
            ],
        }
        if name == "branch-client":
            nodes[name]["binds"].append(f"{root}/spool:/var/lib/prahari/path-spool")
    links = [
        ("branch-client:eth1", "branch-edge:eth3"),
        ("dc-edge:eth3", "app-server:eth1"),
        ("branch-edge:eth1", "pe-primary:eth1"),
        ("pe-primary:eth2", "core:eth1"),
        ("core:eth2", "pe-secondary:eth1"),
        ("pe-secondary:eth2", "dc-edge:eth1"),
        ("branch-edge:eth2", "pe-backup:eth1"),
        ("pe-backup:eth2", "dc-edge:eth2"),
    ]
    topology = {
        "name": LAB,
        "mgmt": {
            "network": f"{LAB}_mgmt",
            "ipv4-subnet": SUBNET,
            "external-access": False,
            "driver-opts": {
                "com.docker.network.bridge.enable_ip_masquerade": "false",
            },
        },
        "topology": {"nodes": nodes, "links": [{"endpoints": list(link)} for link in links]},
    }
    write(root / "topology.clab.json", json.dumps(topology, indent=2))
    config_paths = [repo / "network-lab/configs" / name / "frr.conf" for name in routers]
    write(
        root / "pilot-manifest.json",
        json.dumps(
            {
                "schema_version": "owned-pilot-v1",
                "status": "prepared",
                "lab": LAB,
                "management_subnet": SUBNET,
                "images": images,
                "logical_scope": "lab-v1",
                "runtime_root": str(runtime),
                "runtime_files": {
                    str(p.relative_to(runtime)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in runtime.rglob("*")
                    if p.is_file()
                },
                "model_status": "experimental; no approved forecasting bundle",
                "action_adapter_enabled": False,
                "artifacts": {
                    name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                    for name in ("compose.pilot.json", "topology.clab.json", "pilot.env")
                },
                "router_configs": {
                    str(p.relative_to(repo)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in config_paths
                },
            },
            indent=2,
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8520)
    for name in ("api", "dashboard", "postgres", "ollama"):
        parser.add_argument(f"--{name}-image", required=True)
    args = parser.parse_args()
    if os.name != "posix":
        parser.error("prepare the pilot on the Linux server, where secret permissions are enforced")
    prepare(
        args.state_dir,
        Path(__file__).resolve().parents[1],
        {
            name: getattr(args, f"{name}_image")
            for name in ("api", "dashboard", "postgres", "ollama")
        },
        args.port,
    )
    print(f"Pilot prepared at {args.state_dir}; credentials saved locally, never printed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
