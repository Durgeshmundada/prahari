#!/usr/bin/env python3
"""Check customer acceptance evidence for the exact release without network access."""

import argparse
import importlib.util
import json
import sys
from pathlib import Path


def _backup_module():
    path = Path(__file__).with_name("production-backup-manifest.py")
    spec = importlib.util.spec_from_file_location("prahari_release_identity", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _strict_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate acceptance JSON key")
        value[key] = item
    return value


def validate_release(env_file: Path, *, identity_only=False) -> str:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from prahari.copilot.grounding import snapshot_digest
    from prahari.release_acceptance import ReleaseAcceptance, validate_acceptance

    backup = _backup_module()
    identity = snapshot_digest(backup.release_identity(env_file))
    if identity_only:
        return identity
    values = backup.read_env(env_file)
    root = backup.regular_directory(
        backup.resolve_env_path(
            env_file, backup.required(values, "PRAHARI_ACCEPTANCE_EVIDENCE_DIR")
        ),
        "acceptance evidence root",
    )
    report_file = backup.regular_file(
        backup.resolve_env_path(
            env_file, backup.required(values, "PRAHARI_ACCEPTANCE_REPORT_FILE")
        ),
        "release acceptance report",
        inside=root,
    )
    with report_file.open("rb") as handle:
        payload = handle.read(1024 * 1024 + 1)
    if len(payload) > 1024 * 1024:
        raise ValueError("acceptance report exceeds the 1 MiB limit")
    report = ReleaseAcceptance.model_validate(json.loads(payload, object_pairs_hook=_strict_object))
    validate_acceptance(report, root, release_sha256=identity)
    return identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("env_file", type=Path)
    parser.add_argument("--print-release-identity", action="store_true")
    args = parser.parse_args()
    try:
        identity = validate_release(
            args.env_file.resolve(), identity_only=args.print_release_identity
        )
    except (OSError, ValueError) as error:
        raise SystemExit(f"ERROR: release acceptance failed: {error}") from error
    if args.print_release_identity:
        print(identity)
    else:
        print(f"Release-bound acceptance evidence is structurally valid: {identity}")


if __name__ == "__main__":
    main()
