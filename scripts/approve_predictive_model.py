"""Attach an accountable approval record to an evaluated predictive-v2 bundle."""

from __future__ import annotations

import argparse
import json
from hashlib import sha256
from pathlib import Path

from prahari.analytics.horizon import (
    PredictiveHorizonClassifier,
    technical_evaluation_gate_passed,
)
from prahari.analytics.predictive import PredictiveModelApproval, parse_strict_json


def approve(manifest_path: Path, approval_path: Path) -> dict[str, object]:
    if manifest_path.is_symlink() or approval_path.is_symlink():
        raise ValueError("model manifest and approval input must not be symlinks")
    original_manifest = manifest_path.read_bytes()
    if not original_manifest.endswith(b"\n"):
        raise ValueError("candidate model manifest must end with a newline")
    manifest = parse_strict_json(original_manifest)
    if not isinstance(manifest, dict):
        raise ValueError("model manifest must be a JSON object")
    if manifest.get("schema_version") != "predictive-model-manifest-v2":
        raise ValueError("only predictive-v2 model bundles can use this approval workflow")
    if manifest.get("approval_status") != "evaluation_pending":
        raise ValueError("model is not in the evaluation-pending state")
    technical_gate = manifest.get("technical_evaluation_gate")
    if not technical_evaluation_gate_passed(technical_gate):
        raise ValueError("the current uncertainty-aware technical evaluation gate did not pass")
    approval_bytes = approval_path.read_bytes()
    if not approval_bytes.endswith(b"\n"):
        raise ValueError("approval record must end with a newline")
    approval = PredictiveModelApproval.model_validate(parse_strict_json(approval_bytes))
    if approval.model_version != manifest.get("model_version"):
        raise ValueError("approval record names a different model version")
    if approval.dataset_sha256 != manifest.get("dataset_sha256"):
        raise ValueError("approval record names a different training dataset")
    if approval.dataset_manifest_sha256 != manifest.get("dataset_manifest_sha256"):
        raise ValueError("approval record names a different training dataset manifest")
    candidate_manifest_hash = sha256(original_manifest).hexdigest()
    if approval.candidate_manifest_sha256 != candidate_manifest_hash:
        raise ValueError("approval record does not bind the exact candidate manifest")
    # Load every LightGBM artifact and verify all checksums/features before the
    # approval mutates the registration manifest.
    PredictiveHorizonClassifier(manifest_path)
    destination = manifest_path.parent / "approval-record.json"
    candidate_destination = manifest_path.parent / "candidate-manifest.json"
    if destination.exists() or candidate_destination.exists():
        raise FileExistsError("model bundle already contains approval evidence")
    temporary_approval = manifest_path.parent / "approval-record.json.tmp"
    temporary_candidate = manifest_path.parent / "candidate-manifest.json.tmp"
    temporary_manifest = manifest_path.with_suffix(".json.tmp")
    try:
        temporary_approval.write_bytes(approval_bytes)
        temporary_candidate.write_bytes(original_manifest)
        temporary_approval.replace(destination)
        temporary_candidate.replace(candidate_destination)
        manifest.update(
            {
                "approval_status": "approved",
                "predictive_claim_approved": True,
                "deployment_scope": approval.deployment_scope,
                "approval_record": {
                    "file": destination.name,
                    "sha256": sha256(approval_bytes).hexdigest(),
                    "candidate_manifest_file": candidate_destination.name,
                    "candidate_manifest_sha256": candidate_manifest_hash,
                    "reviewer_id": approval.reviewer_id,
                    "reviewed_at": approval.reviewed_at.isoformat(),
                },
            }
        )
        temporary_manifest.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary_manifest.replace(manifest_path)
        # Re-load under the stricter activated path before reporting success.
        activated = PredictiveHorizonClassifier(manifest_path)
        if not activated.activated:
            raise ValueError("approved model did not activate after registration")
    except Exception:
        destination.unlink(missing_ok=True)
        candidate_destination.unlink(missing_ok=True)
        temporary_manifest.unlink(missing_ok=True)
        temporary_approval.unlink(missing_ok=True)
        temporary_candidate.unlink(missing_ok=True)
        rollback = manifest_path.with_suffix(".json.rollback.tmp")
        rollback.write_bytes(original_manifest)
        rollback.replace(manifest_path)
        raise
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--approval-record", type=Path, required=True)
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    if args.confirm != "APPROVE-PREDICTIVE-V2":
        parser.error("--confirm must exactly equal APPROVE-PREDICTIVE-V2")
    manifest = approve(args.manifest, args.approval_record)
    print(
        f"approved {manifest['model_version']} for {manifest['deployment_scope']} runtime use"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
