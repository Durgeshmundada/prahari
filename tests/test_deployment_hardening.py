import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pytest
from fastapi.testclient import TestClient

from prahari.analytics.predictive import FEATURE_NAMES
from prahari.api import create_app
from prahari.network_profile import NetworkProfile
from prahari.persistence import PersistenceStore
from prahari.release_acceptance import BASELINE_REQUIREMENTS
from prahari.settings import Settings


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _technical_gate() -> dict[str, object]:
    checks = {
        name: True
        for name in (
            "validation_incident_precision_at_least_0_70",
            "validation_incident_recall_at_least_0_60",
            "validation_false_alerts_per_negative_link_hour_at_most_1_0",
            "validation_false_alert_rate_upper_bound_at_most_1_0",
            "validation_median_useful_lead_at_least_30_seconds",
            "test_incident_precision_at_least_0_70",
            "test_incident_recall_at_least_0_60",
            "test_brier_at_most_0_25",
            "test_false_alerts_per_negative_link_hour_at_most_1_0",
            "test_false_alert_rate_upper_bound_at_most_1_0",
            "test_median_useful_lead_at_least_30_seconds",
        )
    }
    return {
        "policy_version": "technical-evaluation-v2",
        "passed": True,
        "checks": checks,
        "false_alert_rate_policy": {
            "maximum_per_negative_link_hour": 1.0,
            "one_sided_confidence": 0.95,
            "method": "exact Poisson upper confidence bound",
        },
        "selection_data": ["validation"],
        "evaluation_data": ["test"],
        "excluded_from_gate": ["demo_holdout"],
        "does_not_grant_approval": True,
    }


def _write_predictive_bundle(
    model_dir: Path,
    *,
    deployment_scope: str = "production",
    network_profile_sha256: str | None = None,
) -> Path:
    bundle = model_dir / "risk-lgbm-horizons-v2"
    bundle.mkdir()
    models = {}
    x_values = np.asarray(
        [[float((row + column) % 7) for column in range(len(FEATURE_NAMES))] for row in range(20)]
    )
    labels = np.asarray([row % 2 for row in range(20)])
    for horizon in (30, 60, 120):
        booster = lgb.train(
            {
                "objective": "binary",
                "verbosity": -1,
                "num_leaves": 2,
                "min_data_in_leaf": 1,
                "num_threads": 1,
            },
            lgb.Dataset(x_values, label=labels, feature_name=list(FEATURE_NAMES)),
            num_boost_round=2,
        )
        model_path = bundle / f"risk-within-{horizon}s.txt"
        booster.save_model(str(model_path))
        models[str(horizon)] = {
            "file": model_path.name,
            "sha256": _sha256(model_path.read_bytes()),
            "best_iteration": 2,
            "calibration": {"method": "platt_logistic", "slope": 1, "intercept": 0},
            "decision_threshold": 0.5,
            "top_features_by_gain": [],
        }
    candidate = {
        "schema_version": "predictive-model-manifest-v2",
        "model_version": "risk-production-test-v2",
        "model_kind": "test horizon bundle",
        "approval_status": "evaluation_pending",
        "predictive_claim_approved": False,
        "deployment_scope": "none",
        "feature_version": "predictive-window-v2",
        "features": list(FEATURE_NAMES),
        "horizons_seconds": [30, 60, 120],
        "sampling_seconds": 5,
        "window_seconds": 60,
        "minimum_samples": 10,
        "dataset_sha256": "1" * 64,
        "dataset_manifest_sha256": "2" * 64,
        "dataset_version": "production-test-dataset-v2",
        "impact_policy": {
            "schema_version": "application-impact-policy-v2",
            "version": "production-test-v2",
            "sustained_seconds": 15,
            "max_inter_sample_gap_seconds": 15,
            "message_loss_ratio": 0.2,
            "rtt_p95_ms": 150,
            "jitter_ms": 30,
            "failed_upload_is_impact": True,
        },
        "models": models,
        "run_counts": {},
        "row_counts": {},
        "evaluation": {},
        "technical_evaluation_gate": _technical_gate(),
        "limitations": ["unit test only"],
    }
    candidate_bytes = (json.dumps(candidate, indent=2, sort_keys=True) + "\n").encode()
    candidate_hash = _sha256(candidate_bytes)
    (bundle / "candidate-manifest.json").write_bytes(candidate_bytes)
    approval = {
        "schema_version": "predictive-model-approval-v2",
        "decision": "approved",
        "model_version": candidate["model_version"],
        "dataset_sha256": candidate["dataset_sha256"],
        "dataset_manifest_sha256": candidate["dataset_manifest_sha256"],
        "candidate_manifest_sha256": candidate_hash,
        "deployment_scope": deployment_scope,
        "reviewer_id": "reviewer-production-test",
        "reviewed_at": "2026-09-03T00:00:00+00:00",
        "rationale": "The independent test evidence and documented limitations were reviewed.",
        "technical_gate_reviewed": True,
        "untouched_holdout_reviewed": True,
        "limitations_accepted": True,
        "external_validation_evidence_sha256": "2" * 64,
        "change_ticket": "CHG-TEST-123",
        "network_profile_sha256": network_profile_sha256,
    }
    approval_bytes = (json.dumps(approval, sort_keys=True) + "\n").encode()
    (bundle / "approval-record.json").write_bytes(approval_bytes)
    active = {
        **candidate,
        "approval_status": "approved",
        "predictive_claim_approved": True,
        "deployment_scope": deployment_scope,
        "approval_record": {
            "file": "approval-record.json",
            "sha256": _sha256(approval_bytes),
            "candidate_manifest_file": "candidate-manifest.json",
            "candidate_manifest_sha256": candidate_hash,
            "reviewer_id": approval["reviewer_id"],
            "reviewed_at": approval["reviewed_at"],
        },
    }
    manifest_path = bundle / "manifest.json"
    manifest_path.write_text(json.dumps(active, sort_keys=True) + "\n", encoding="utf-8")
    return manifest_path


def _write_valid_release_inputs(root: Path, *, predictive_scope: str = "production") -> Path:
    model_dir = root / "prediction-model"
    model_dir.mkdir()
    model_path = model_dir / "model.txt"
    features = ["packet_loss_ratio", "jitter_ms", "rtt_avg_ms"]
    training = lgb.Dataset(
        np.asarray(
            [
                [0.0, 0.1, 1.0],
                [0.01, 1.0, 3.0],
                [0.10, 20.0, 30.0],
                [0.20, 40.0, 60.0],
            ]
        ),
        label=np.asarray([0, 0, 1, 1]),
        feature_name=features,
    )
    booster = lgb.train(
        {
            "objective": "binary",
            "verbosity": -1,
            "num_leaves": 2,
            "min_data_in_leaf": 1,
            "num_threads": 1,
        },
        training,
        num_boost_round=1,
    )
    booster.save_model(str(model_path))
    model_bytes = model_path.read_bytes()
    (model_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "model-manifest-v1",
                "approval_status": "approved",
                "predictive_claim_approved": True,
                "model_sha256": _sha256(model_bytes),
                "model_version": "test-risk-model-v1",
                "features": features,
                "decision_threshold": 0.5,
            }
        ),
        encoding="utf-8",
    )
    # Generated test inputs do not constitute real deployment approval.
    profile = json.loads(
        (Path(__file__).resolve().parents[1] / "config/network-profile.lab.example.json").read_text(
            encoding="utf-8"
        )
    )
    profile["deployment_scope"] = "production"
    knowledge_dir = root / "knowledge"
    knowledge_dir.mkdir()
    document = knowledge_dir / "monitoring.md"
    document.write_bytes(b"# Test monitoring\n\nReview evidence and escalate uncertain outcomes.\n")
    profile["approved_runbooks"] = [
        {
            "path": "monitoring.md",
            "sha256": _sha256(document.read_bytes()),
            "reviewer_id": "test-reviewer",
            "approval_reference": "TEST-ONLY-123",
        }
    ]
    profile["playbook"]["source"] = "monitoring.md"
    profile_path = root / "network-profile.json"
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    predictive_manifest = _write_predictive_bundle(
        model_dir,
        deployment_scope=predictive_scope,
        network_profile_sha256=NetworkProfile.model_validate(profile).fingerprint,
    )

    ollama_dir = root / "ollama-models"
    manifest_path = ollama_dir / "manifests" / "registry.ollama.ai" / "library" / "qwen3" / "0.6b"
    manifest_path.parent.mkdir(parents=True)
    config_blob = b'{"model_format":"gguf","model_family":"qwen3"}'
    gguf_blob = b"GGUF" + bytes(range(64))
    blobs = {_sha256(config_blob): config_blob, _sha256(gguf_blob): gguf_blob}
    blobs_dir = ollama_dir / "blobs"
    blobs_dir.mkdir()
    for digest, value in blobs.items():
        (blobs_dir / f"sha256-{digest}").write_bytes(value)
    ollama_manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
            "config": {
                "mediaType": "application/vnd.docker.container.image.v1+json",
                "digest": f"sha256:{_sha256(config_blob)}",
                "size": len(config_blob),
            },
            "layers": [
                {
                    "mediaType": "application/vnd.ollama.image.model",
                    "digest": f"sha256:{_sha256(gguf_blob)}",
                    "size": len(gguf_blob),
                }
            ],
        },
        separators=(",", ":"),
    ).encode()
    manifest_path.write_bytes(ollama_manifest)

    env_file = root / ".env.production"
    env_file.write_text(
        "\n".join(
            [
                f"PRAHARI_IMAGE=prahari@sha256:{'a' * 64}",
                f"PRAHARI_DASHBOARD_IMAGE=prahari-dashboard@sha256:{'b' * 64}",
                f"POSTGRES_IMAGE=postgres@sha256:{'c' * 64}",
                f"PRAHARI_OLLAMA_IMAGE=ollama/ollama@sha256:{'d' * 64}",
                f"PRAHARI_MODEL_DIR={model_dir}",
                f"PRAHARI_NETWORK_PROFILE_FILE={profile_path}",
                f"PRAHARI_KNOWLEDGE_DIR={knowledge_dir}",
                "PRAHARI_MODEL_ARTIFACT_NAME=model.txt",
                "PRAHARI_MODEL_MANIFEST_NAME=manifest.json",
                "PRAHARI_PREDICTIVE_MODEL_MANIFEST_NAME="
                f"{predictive_manifest.relative_to(model_dir).as_posix()}",
                f"PRAHARI_OLLAMA_MODELS_DIR={ollama_dir}",
                "PRAHARI_OLLAMA_MODEL=qwen3:0.6b",
                f"PRAHARI_OLLAMA_MODEL_MANIFEST={manifest_path}",
                f"PRAHARI_OLLAMA_MODEL_MANIFEST_SHA256={_sha256(ollama_manifest)}",
                f"PRAHARI_OLLAMA_MODEL_GGUF_SHA256={_sha256(gguf_blob)}",
                "PRAHARI_COPILOT_SUBNET=172.30.250.0/24",
                "PRAHARI_OLLAMA_STATIC_IP=172.30.250.10",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    for name in ("compose.yaml", "compose.production.yaml"):
        shutil.copyfile(Path(__file__).resolve().parents[1] / "deploy" / name, root / name)
    acceptance_dir = root / "acceptance"
    acceptance_dir.mkdir()
    evidence = acceptance_dir / "test-only-evidence.txt"
    evidence.write_bytes(b"Synthetic validator fixture; no customer acceptance is implied.")
    spec = importlib.util.spec_from_file_location(
        "test_acceptance_validator",
        Path(__file__).resolve().parents[1] / "deploy/scripts/validate-release-acceptance.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    identity = module.validate_release(env_file, identity_only=True)
    now = datetime.now(UTC)
    report = {
        "schema_version": "release-acceptance-v1",
        "deployment_mode": "monitoring_only",
        "release_sha256": identity,
        "customer_requirement_references": ["unit-test-only"],
        "prepared_by": "test-preparer",
        "approved_by": "test-reviewer",
        "approval_reference": "TEST-ONLY-123",
        "approved_at": (now - timedelta(days=1)).isoformat(),
        "expires_at": (now + timedelta(days=1)).isoformat(),
        "requirements": [
            {
                "requirement_id": item,
                "status": "passed",
                "note": "Generated unit-test evidence; not real production acceptance.",
                "evidence": [{"path": evidence.name, "sha256": _sha256(evidence.read_bytes())}],
            }
            for item in sorted(BASELINE_REQUIREMENTS)
        ],
    }
    report_file = acceptance_dir / "release-acceptance.json"
    report_file.write_text(json.dumps(report))
    with env_file.open("a", encoding="utf-8") as handle:
        handle.write(f"PRAHARI_ACCEPTANCE_REPORT_FILE={report_file}\n")
        handle.write(f"PRAHARI_ACCEPTANCE_EVIDENCE_DIR={acceptance_dir}\n")
    return env_file


def _replace_env_value(env_file: Path, key: str, value: str) -> None:
    lines = env_file.read_text(encoding="utf-8").splitlines()
    prefix = f"{key}="
    assert sum(line.startswith(prefix) for line in lines) == 1
    env_file.write_text(
        "\n".join(f"{prefix}{value}" if line.startswith(prefix) else line for line in lines) + "\n",
        encoding="utf-8",
    )


def _validator(project_root: Path) -> Path:
    return project_root / "deploy" / "scripts" / "validate-production-artifacts.py"


def _backup_manifest_tool(project_root: Path) -> Path:
    return project_root / "deploy" / "scripts" / "production-backup-manifest.py"


def _write_backup_manifest(project_root: Path, root: Path) -> tuple[Path, Path]:
    env_file = _write_valid_release_inputs(root)
    for name in ("compose.yaml", "compose.production.yaml"):
        shutil.copyfile(project_root / "deploy" / name, root / name)
    backup_dir = root / "backup-test-001"
    backup_dir.mkdir()
    (backup_dir / "database.dump").write_bytes(b"PGDMP-test-archive")
    counts = {
        "alembic_version": 1,
        "telemetry_records": 123,
        "app_probe_records": 45,
        "event_records": 6,
        "incident_states": 2,
        "closure_cases": 2,
        "action_operations": 0,
        "browser_sessions": 0,
        "login_windows": 0,
        "audit_events": 8,
    }
    result = subprocess.run(
        [
            sys.executable,
            str(_backup_manifest_tool(project_root)),
            "create",
            "--backup-dir",
            str(backup_dir),
            "--env-file",
            str(env_file),
            "--database-name",
            "prahari",
            "--alembic-revision",
            "0004_action_operations",
            "--counts-json",
            json.dumps(counts),
            "--created-at",
            "2026-09-04T00:00:00Z",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return env_file, backup_dir


def test_production_artifact_validator_accepts_checksum_bound_inputs(
    project_root: Path, tmp_path: Path
) -> None:
    result = subprocess.run(
        [sys.executable, str(_validator(project_root)), str(_write_valid_release_inputs(tmp_path))],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "exact offline Ollama GGUF artifacts are valid" in result.stdout


@pytest.mark.parametrize("failure", ["missing", "pending", "release-drift"])
def test_production_preflight_requires_current_complete_acceptance(project_root, tmp_path, failure):
    env_file = _write_valid_release_inputs(tmp_path)
    report = tmp_path / "acceptance/release-acceptance.json"
    if failure == "missing":
        report.unlink()
    elif failure == "pending":
        value = json.loads(report.read_text())
        value["requirements"][0]["status"] = "pending"
        report.write_text(json.dumps(value))
    else:
        _replace_env_value(env_file, "PRAHARI_IMAGE", f"prahari@sha256:{'e' * 64}")
    result = subprocess.run(
        [sys.executable, str(_validator(project_root)), str(env_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "acceptance" in (result.stdout + result.stderr)


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "missing-profile",
        "lab-profile",
        "missing-runbooks",
        "wrong-profile",
        "missing-horizon",
    ],
)
def test_production_factory_enforces_bound_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
) -> None:
    _write_valid_release_inputs(tmp_path)
    database_secret = tmp_path / "database-url"
    database_secret.write_text("postgresql+psycopg://test:test@database/prahari")
    api_secret = tmp_path / "api-keys.json"
    api_secret.write_text(
        json.dumps(
            {
                "test-token-with-more-than-32-characters": {
                    "actor_id": "test-admin",
                    "role": "admin",
                }
            }
        )
    )
    profile_path = tmp_path / "network-profile.json"
    if failure in {"lab-profile", "missing-runbooks", "wrong-profile"}:
        profile = json.loads(profile_path.read_text())
        if failure == "lab-profile":
            profile["deployment_scope"] = "controlled_lab"
        elif failure == "missing-runbooks":
            profile["approved_runbooks"] = []
        else:
            profile["profile_id"] = "changed-profile"
        profile_path.write_text(json.dumps(profile))
    settings = Settings(
        environment="production",
        data_mode="live",
        database_url_file=database_secret,
        auth_mode="api_key",
        api_keys_json_file=api_secret,
        auto_create_schema=False,
        model_approval_status="approved",
        copilot_enabled=True,
        network_profile_path=None if failure == "missing-profile" else profile_path,
        knowledge_root=tmp_path / "knowledge",
        model_artifact_path=tmp_path / "prediction-model/model.txt",
        model_manifest_path=tmp_path / "prediction-model/manifest.json",
        predictive_model_manifest_path=(
            None
            if failure == "missing-horizon"
            else tmp_path / "prediction-model/risk-lgbm-horizons-v2/manifest.json"
        ),
    )
    # Isolate factory guards from connectivity; PostgreSQL has its own integration suite.
    store = PersistenceStore("sqlite://")
    store.create_schema()
    monkeypatch.setattr(store, "schema_is_current", lambda: True)
    expected = {
        "missing-profile": "PRAHARI_NETWORK_PROFILE_PATH",
        "lab-profile": "production-scoped",
        "missing-runbooks": "approved runbooks",
        "wrong-profile": "does not bind",
        "missing-horizon": "activated predictive",
    }
    try:
        if failure:
            with pytest.raises((RuntimeError, ValueError), match=expected[failure]):
                create_app(settings=settings, store=store)
        else:
            client = TestClient(create_app(settings=settings, store=store))
            topology = client.get(
                "/api/topology",
                headers={
                    "Authorization": "Bearer test-token-with-more-than-32-characters",
                },
            )
            assert topology.status_code == 200, topology.text
            assert topology.json()["fixture_only"] is False
            assert topology.json()["profile_sha256"]
    finally:
        store.dispose()


@pytest.mark.parametrize("drift", ["profile", "lab-scope", "runbook"])
def test_production_artifact_validator_rejects_network_configuration_drift(
    project_root: Path,
    tmp_path: Path,
    drift: str,
) -> None:
    env_file = _write_valid_release_inputs(tmp_path)
    profile_path = tmp_path / "network-profile.json"
    profile = json.loads(profile_path.read_text())
    if drift == "runbook":
        (tmp_path / "knowledge/monitoring.md").write_text("Unapproved device instructions.")
    else:
        if drift == "profile":
            profile["profile_id"] = "changed-network"
        else:
            profile["deployment_scope"] = "controlled_lab"
        profile_path.write_text(json.dumps(profile))
    result = subprocess.run(
        [sys.executable, str(_validator(project_root)), str(env_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    expected = {
        "profile": "network profile checksum",
        "lab-scope": "scoped for production",
        "runbook": "runbook checksum",
    }[drift]
    assert expected in result.stderr


def test_production_artifact_validator_rejects_a_tampered_ollama_blob(
    project_root: Path, tmp_path: Path
) -> None:
    env_file = _write_valid_release_inputs(tmp_path)
    blob = next((tmp_path / "ollama-models" / "blobs").iterdir())
    blob.write_bytes(b"tampered")

    result = subprocess.run(
        [sys.executable, str(_validator(project_root)), str(env_file)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "Ollama model blob" in (result.stdout + result.stderr)


def test_production_artifact_validator_rejects_an_unapproved_prediction_model(
    project_root: Path, tmp_path: Path
) -> None:
    env_file = _write_valid_release_inputs(tmp_path)
    manifest = tmp_path / "prediction-model" / "manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["approval_status"] = "experimental"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(_validator(project_root)), str(env_file)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "manifest is not approved" in (result.stdout + result.stderr)


def test_production_artifact_validator_rejects_checksum_bound_non_model(
    project_root: Path, tmp_path: Path
) -> None:
    env_file = _write_valid_release_inputs(tmp_path)
    model = tmp_path / "prediction-model" / "model.txt"
    model.write_bytes(b"not-a-lightgbm-model")
    manifest = tmp_path / "prediction-model" / "manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["model_sha256"] = _sha256(model.read_bytes())
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(_validator(project_root)), str(env_file)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "cannot be loaded" in (result.stdout + result.stderr)


def test_production_artifact_validator_rejects_non_production_predictive_scope(
    project_root: Path, tmp_path: Path
) -> None:
    env_file = _write_valid_release_inputs(tmp_path, predictive_scope="controlled_lab")

    result = subprocess.run(
        [sys.executable, str(_validator(project_root)), str(env_file)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "not explicitly approved for production" in (result.stdout + result.stderr)


def test_production_artifact_validator_rejects_tampered_predictive_horizon(
    project_root: Path, tmp_path: Path
) -> None:
    env_file = _write_valid_release_inputs(tmp_path)
    horizon = tmp_path / "prediction-model" / "risk-lgbm-horizons-v2" / "risk-within-120s.txt"
    horizon.write_bytes(horizon.read_bytes() + b"tampered")

    result = subprocess.run(
        [sys.executable, str(_validator(project_root)), str(env_file)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "horizon 120 model checksum does not match" in (result.stdout + result.stderr)


def test_production_artifact_validator_binds_exact_gguf_digest(
    project_root: Path, tmp_path: Path
) -> None:
    env_file = _write_valid_release_inputs(tmp_path)
    _replace_env_value(env_file, "PRAHARI_OLLAMA_MODEL_GGUF_SHA256", "3" * 64)

    result = subprocess.run(
        [sys.executable, str(_validator(project_root)), str(env_file)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "does not match the exact Ollama model layer" in (result.stdout + result.stderr)


def test_production_artifact_validator_rejects_nested_model_symlink(
    project_root: Path, tmp_path: Path
) -> None:
    env_file = _write_valid_release_inputs(tmp_path)
    model_dir = tmp_path / "prediction-model"
    original = model_dir / "risk-lgbm-horizons-v2"
    nested_real = model_dir / "real-parent" / "bundle"
    nested_real.parent.mkdir()
    shutil.move(str(original), str(nested_real))
    alias = model_dir / "linked-parent"
    try:
        alias.symlink_to(nested_real.parent, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symbolic links are unavailable on this test host: {error}")
    _replace_env_value(
        env_file,
        "PRAHARI_PREDICTIVE_MODEL_MANIFEST_NAME",
        "linked-parent/bundle/manifest.json",
    )

    result = subprocess.run(
        [sys.executable, str(_validator(project_root)), str(env_file)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "must not traverse a symbolic link" in (result.stdout + result.stderr)


def test_production_artifact_validator_rejects_nested_ollama_symlink(
    project_root: Path, tmp_path: Path
) -> None:
    env_file = _write_valid_release_inputs(tmp_path)
    ollama_dir = tmp_path / "ollama-models"
    manifests = ollama_dir / "manifests"
    real_manifests = ollama_dir / "real-manifests"
    manifests.rename(real_manifests)
    try:
        manifests.symlink_to(real_manifests, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symbolic links are unavailable on this test host: {error}")

    result = subprocess.run(
        [sys.executable, str(_validator(project_root)), str(env_file)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "must not traverse a symbolic link" in (result.stdout + result.stderr)


def test_production_backup_manifest_verifies_archive_and_release_binding(
    project_root: Path, tmp_path: Path
) -> None:
    env_file, backup_dir = _write_backup_manifest(project_root, tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            str(_backup_manifest_tool(project_root)),
            "verify",
            "--backup-dir",
            str(backup_dir),
            "--env-file",
            str(env_file),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "integrity and release binding are valid" in result.stdout


def test_production_backup_manifest_rejects_tampered_archive(
    project_root: Path, tmp_path: Path
) -> None:
    env_file, backup_dir = _write_backup_manifest(project_root, tmp_path)
    (backup_dir / "database.dump").write_bytes(b"PGDMP-tampered")

    result = subprocess.run(
        [
            sys.executable,
            str(_backup_manifest_tool(project_root)),
            "verify",
            "--backup-dir",
            str(backup_dir),
            "--env-file",
            str(env_file),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "archive checksum does not match" in (result.stdout + result.stderr)


@pytest.mark.parametrize("drift", ["image", "network-profile"])
def test_production_backup_manifest_rejects_release_drift(
    project_root: Path,
    tmp_path: Path,
    drift: str,
) -> None:
    env_file, backup_dir = _write_backup_manifest(project_root, tmp_path)
    if drift == "image":
        _replace_env_value(env_file, "PRAHARI_IMAGE", f"prahari@sha256:{'e' * 64}")
    else:
        profile_path = tmp_path / "network-profile.json"
        profile = json.loads(profile_path.read_text())
        profile["profile_id"] = "changed-network"
        profile_path.write_text(json.dumps(profile))

    result = subprocess.run(
        [
            sys.executable,
            str(_backup_manifest_tool(project_root)),
            "verify",
            "--backup-dir",
            str(backup_dir),
            "--env-file",
            str(env_file),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "release identity does not match" in (result.stdout + result.stderr)


def test_production_backup_manifest_verifies_restored_counts(
    project_root: Path, tmp_path: Path
) -> None:
    env_file, backup_dir = _write_backup_manifest(project_root, tmp_path)
    wrong_counts = {
        "alembic_version": 1,
        "telemetry_records": 122,
        "app_probe_records": 45,
        "event_records": 6,
        "incident_states": 2,
        "closure_cases": 2,
        "action_operations": 0,
        "browser_sessions": 0,
        "login_windows": 0,
        "audit_events": 8,
    }

    result = subprocess.run(
        [
            sys.executable,
            str(_backup_manifest_tool(project_root)),
            "verify-restored",
            "--backup-dir",
            str(backup_dir),
            "--env-file",
            str(env_file),
            "--alembic-revision",
            "0004_action_operations",
            "--counts-json",
            json.dumps(wrong_counts),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "restored table counts do not match" in (result.stdout + result.stderr)
