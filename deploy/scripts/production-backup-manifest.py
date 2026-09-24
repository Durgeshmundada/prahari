#!/usr/bin/env python3
"""Create and verify checksum-bound PRAHARI production database backup metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "prahari-production-backup-v1"
ARCHIVE_NAME = "database.dump"
MANIFEST_NAME = "manifest.json"
DETACHED_HASH_NAME = "manifest.sha256"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SAFE_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
SAFE_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
EXPECTED_TABLES = {
    "alembic_version",
    "telemetry_records",
    "app_probe_records",
    "event_records",
    "incident_states",
    "closure_cases",
    "action_operations",
    "browser_sessions",
    "login_windows",
    "audit_events",
}
IMAGE_KEYS = (
    "PRAHARI_IMAGE",
    "PRAHARI_DASHBOARD_IMAGE",
    "POSTGRES_IMAGE",
    "PRAHARI_OLLAMA_IMAGE",
)


def fail(message: str) -> None:
    raise SystemExit(f"ERROR: {message}")


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        fail(f"cannot hash {path}: {error}")
    return digest.hexdigest()


def file_prefix(path: Path, length: int) -> bytes:
    try:
        with path.open("rb") as handle:
            return handle.read(length)
    except OSError as error:
        fail(f"cannot inspect {path}: {error}")


def reject_symlink_components(path: Path, label: str) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            mode = current.lstat().st_mode
        except OSError as error:
            fail(f"cannot inspect {label} path component {current}: {error}")
        if stat.S_ISLNK(mode):
            fail(f"{label} must not traverse a symbolic link: {current}")


def regular_directory(path: Path, label: str) -> Path:
    reject_symlink_components(path, label)
    if not path.is_dir():
        fail(f"{label} must be an existing directory")
    return path.resolve()


def regular_file(path: Path, label: str, *, inside: Path | None = None) -> Path:
    reject_symlink_components(path, label)
    resolved = path.resolve()
    if inside is not None and not resolved.is_relative_to(inside):
        fail(f"{label} must remain inside {inside}")
    if not resolved.is_file():
        fail(f"{label} must be an existing regular file")
    return resolved


def read_env(path: Path) -> dict[str, str]:
    regular_file(path, "production environment file")
    values: dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            fail(f"invalid environment entry at {path}:{line_number}")
        key, value = line.split("=", 1)
        if not key or key in values or value[:1] in {'"', "'"}:
            fail(f"duplicate, empty, or quoted environment value at {path}:{line_number}")
        values[key] = value
    return values


def required(values: dict[str, str], key: str) -> str:
    value = values.get(key, "")
    if not value:
        fail(f"missing {key}")
    return value


def resolve_env_path(env_file: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else env_file.parent / path


def safe_relative_file(root: Path, name: str, label: str) -> Path:
    relative = Path(name)
    if relative.is_absolute() or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        fail(f"{label} must be a safe relative path")
    return regular_file(root / relative, label, inside=root.resolve())


def release_identity(env_file: Path) -> dict[str, Any]:
    values = read_env(env_file)
    network_profile = regular_file(
        resolve_env_path(env_file, required(values, "PRAHARI_NETWORK_PROFILE_FILE")),
        "network profile",
    )
    deploy_dir = env_file.parent.resolve()
    model_dir = regular_directory(
        resolve_env_path(env_file, required(values, "PRAHARI_MODEL_DIR")),
        "PRAHARI_MODEL_DIR",
    )
    legacy_artifact = safe_relative_file(
        model_dir,
        required(values, "PRAHARI_MODEL_ARTIFACT_NAME"),
        "legacy model artifact",
    )
    legacy_manifest = safe_relative_file(
        model_dir,
        required(values, "PRAHARI_MODEL_MANIFEST_NAME"),
        "legacy model manifest",
    )
    predictive_manifest = safe_relative_file(
        model_dir,
        required(values, "PRAHARI_PREDICTIVE_MODEL_MANIFEST_NAME"),
        "predictive-v2 manifest",
    )
    ollama_root = regular_directory(
        resolve_env_path(env_file, required(values, "PRAHARI_OLLAMA_MODELS_DIR")),
        "PRAHARI_OLLAMA_MODELS_DIR",
    )
    ollama_manifest = regular_file(
        resolve_env_path(env_file, required(values, "PRAHARI_OLLAMA_MODEL_MANIFEST")),
        "Ollama manifest",
        inside=ollama_root,
    )
    gguf_hash = required(values, "PRAHARI_OLLAMA_MODEL_GGUF_SHA256")
    if not SHA256.fullmatch(gguf_hash):
        fail("PRAHARI_OLLAMA_MODEL_GGUF_SHA256 is invalid")
    gguf = regular_file(
        ollama_root / "blobs" / f"sha256-{gguf_hash}",
        "Ollama GGUF blob",
        inside=ollama_root,
    )
    compose_files = {
        name: digest_file(regular_file(deploy_dir / name, f"deployment {name}"))
        for name in ("compose.yaml", "compose.production.yaml")
    }
    return {
        "images": {key: required(values, key) for key in IMAGE_KEYS},
        "ollama_model": required(values, "PRAHARI_OLLAMA_MODEL"),
        "artifacts": {
            "network_profile": digest_file(network_profile),
            "legacy_model": digest_file(legacy_artifact),
            "legacy_manifest": digest_file(legacy_manifest),
            "predictive_v2_manifest": digest_file(predictive_manifest),
            "ollama_manifest": digest_file(ollama_manifest),
            "ollama_gguf": digest_file(gguf),
        },
        "compose_files": compose_files,
    }


def parse_counts(raw: str) -> dict[str, int]:
    try:
        counts = json.loads(raw)
    except json.JSONDecodeError as error:
        fail(f"database table counts are not valid JSON: {error}")
    if not isinstance(counts, dict) or set(counts) != EXPECTED_TABLES:
        fail("database table counts do not cover the exact production schema")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in counts.values()
    ):
        fail("database table counts must be non-negative integers")
    return counts


def validate_timestamp(raw: object) -> str:
    if not isinstance(raw, str):
        fail("backup timestamp is missing")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        fail(f"backup timestamp is invalid: {error}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        fail("backup timestamp must include a UTC offset")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def create_manifest(
    backup_dir: Path,
    env_file: Path,
    database_name: str,
    alembic_revision: str,
    counts_json: str,
    created_at: str,
) -> dict[str, Any]:
    directory = regular_directory(backup_dir, "backup staging directory")
    archive = regular_file(directory / ARCHIVE_NAME, "PostgreSQL backup archive", inside=directory)
    if archive.stat().st_size < 5 or file_prefix(archive, 5) != b"PGDMP":
        fail("database.dump is not a PostgreSQL custom-format archive")
    if not SAFE_IDENTIFIER.fullmatch(database_name):
        fail("source database name is not a safe PostgreSQL identifier")
    if not SAFE_REVISION.fullmatch(alembic_revision):
        fail("Alembic revision is missing or unsafe")
    manifest_path = directory / MANIFEST_NAME
    detached_path = directory / DETACHED_HASH_NAME
    if manifest_path.exists() or detached_path.exists():
        fail("backup manifest outputs already exist")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "backup_id": directory.name,
        "created_at": validate_timestamp(created_at),
        "source_database": database_name,
        "database": {
            "archive_file": ARCHIVE_NAME,
            "archive_format": "postgresql-custom",
            "archive_sha256": digest_file(archive),
            "archive_bytes": archive.stat().st_size,
            "alembic_revision": alembic_revision,
            "table_counts": parse_counts(counts_json),
        },
        "release": release_identity(env_file.resolve()),
        "contains_secrets": False,
    }
    encoded = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    manifest_path.write_bytes(encoded)
    detached_path.write_text(
        f"{hashlib.sha256(encoded).hexdigest()}  {MANIFEST_NAME}\n",
        encoding="ascii",
    )
    return manifest


def load_and_verify(backup_dir: Path, env_file: Path) -> dict[str, Any]:
    directory = regular_directory(backup_dir, "backup directory")
    manifest_path = regular_file(directory / MANIFEST_NAME, "backup manifest", inside=directory)
    detached_path = regular_file(
        directory / DETACHED_HASH_NAME,
        "detached backup-manifest checksum",
        inside=directory,
    )
    detached = detached_path.read_text(encoding="ascii").strip()
    expected_line = f"{digest_file(manifest_path)}  {MANIFEST_NAME}"
    if detached != expected_line:
        fail("detached backup-manifest checksum does not match")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(f"backup manifest is invalid: {error}")
    expected_fields = {
        "schema_version",
        "backup_id",
        "created_at",
        "source_database",
        "database",
        "release",
        "contains_secrets",
    }
    if not isinstance(manifest, dict) or set(manifest) != expected_fields:
        fail("backup manifest fields are incomplete or unexpected")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        fail("backup manifest schema is unsupported")
    if manifest.get("backup_id") != directory.name:
        fail("backup directory name does not match its manifest identity")
    validate_timestamp(manifest.get("created_at"))
    if not isinstance(manifest.get("source_database"), str) or not SAFE_IDENTIFIER.fullmatch(
        manifest["source_database"]
    ):
        fail("backup source database is invalid")
    if manifest.get("contains_secrets") is not False:
        fail("backup is incorrectly marked as containing secrets")
    database = manifest.get("database")
    if not isinstance(database, dict) or set(database) != {
        "archive_file",
        "archive_format",
        "archive_sha256",
        "archive_bytes",
        "alembic_revision",
        "table_counts",
    }:
        fail("backup database metadata is invalid")
    if (
        database.get("archive_file") != ARCHIVE_NAME
        or database.get("archive_format") != "postgresql-custom"
    ):
        fail("backup archive identity or format is invalid")
    archive = regular_file(directory / ARCHIVE_NAME, "PostgreSQL backup archive", inside=directory)
    expected_archive_hash = database.get("archive_sha256")
    if not isinstance(expected_archive_hash, str) or not SHA256.fullmatch(expected_archive_hash):
        fail("backup archive checksum is invalid")
    if digest_file(archive) != expected_archive_hash:
        fail("PostgreSQL backup archive checksum does not match")
    if database.get("archive_bytes") != archive.stat().st_size:
        fail("PostgreSQL backup archive size does not match")
    if archive.stat().st_size < 5 or file_prefix(archive, 5) != b"PGDMP":
        fail("database.dump is not a PostgreSQL custom-format archive")
    revision = database.get("alembic_revision")
    if not isinstance(revision, str) or not SAFE_REVISION.fullmatch(revision):
        fail("backup Alembic revision is invalid")
    parse_counts(json.dumps(database.get("table_counts")))
    if manifest.get("release") != release_identity(env_file.resolve()):
        fail("backup release identity does not match the currently validated release")
    return manifest


def verify_restored_database(
    backup_dir: Path,
    env_file: Path,
    alembic_revision: str,
    counts_json: str,
) -> dict[str, Any]:
    manifest = load_and_verify(backup_dir, env_file)
    database = manifest["database"]
    if database["alembic_revision"] != alembic_revision:
        fail("restored Alembic revision does not match the backup")
    if database["table_counts"] != parse_counts(counts_json):
        fail("restored table counts do not match the backup")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--backup-dir", type=Path, required=True)
    create.add_argument("--env-file", type=Path, required=True)
    create.add_argument("--database-name", required=True)
    create.add_argument("--alembic-revision", required=True)
    create.add_argument("--counts-json", required=True)
    create.add_argument("--created-at", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--backup-dir", type=Path, required=True)
    verify.add_argument("--env-file", type=Path, required=True)
    inspect = subparsers.add_parser("inspect")
    inspect.add_argument("--backup-dir", type=Path, required=True)
    inspect.add_argument("--env-file", type=Path, required=True)
    inspect.add_argument(
        "--field",
        choices=("source_database", "alembic_revision"),
        required=True,
    )
    restored = subparsers.add_parser("verify-restored")
    restored.add_argument("--backup-dir", type=Path, required=True)
    restored.add_argument("--env-file", type=Path, required=True)
    restored.add_argument("--alembic-revision", required=True)
    restored.add_argument("--counts-json", required=True)
    args = parser.parse_args()
    if args.command == "create":
        create_manifest(
            args.backup_dir,
            args.env_file,
            args.database_name,
            args.alembic_revision,
            args.counts_json,
            args.created_at,
        )
        print("Checksum-bound production backup manifest created.")
    elif args.command == "verify":
        manifest = load_and_verify(args.backup_dir, args.env_file)
        print(
            "Production backup integrity and release binding are valid: "
            f"{manifest['backup_id']}."
        )
    elif args.command == "inspect":
        manifest = load_and_verify(args.backup_dir, args.env_file)
        if args.field == "source_database":
            print(manifest["source_database"])
        else:
            print(manifest["database"]["alembic_revision"])
    else:
        manifest = verify_restored_database(
            args.backup_dir,
            args.env_file,
            args.alembic_revision,
            args.counts_json,
        )
        print(
            "Restored schema revision and all production table counts match backup: "
            f"{manifest['backup_id']}."
        )


if __name__ == "__main__":
    main()
