"""Synthetic orchestration fixtures; these do not approve a forecasting model."""

import json
from unittest.mock import Mock

import pytest

from scripts import complete_owned_model_campaign as builder


@pytest.fixture
def cohort(tmp_path, monkeypatch):
    monkeypatch.setattr(builder.os, "chown", lambda *args: None, raising=False)
    state = tmp_path / "pilot"
    campaign = state / "model-campaigns" / "test-only"
    runtime = campaign / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "test-source.txt").write_bytes(b"synthetic-frozen-source")
    pilot = state / "pilot-manifest.json"
    pilot.write_text(
        json.dumps(
            {
                "lab": "prahari-owned",
                "action_adapter_enabled": False,
                "images": {"api": "synthetic@sha256:" + "a" * 64},
            }
        )
    )
    report = {
        "status": "collection_complete",
        "model_approved": False,
        "pilot_sha256": builder.digest(pilot),
        "runtime_files": {"test-source.txt": builder.digest(runtime / "test-source.txt")},
        "normal_runs": [],
        "impacted_runs": [],
    }
    for index in range(24):
        run_id = f"synthetic-{index:02d}"
        original = state / "predictive-experiments" / run_id
        original.mkdir(parents=True)
        observations = original / "observations.jsonl"
        observations.write_bytes(b"synthetic-observations-not-training-evidence")
        impact = None if index < 12 else "synthetic-impact"
        manifest = original / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "controller_lab_name": "prahari-owned",
                    "status": "completed",
                    "actual_impact_start_time": impact,
                    "observations_sha256": builder.digest(observations),
                    "fault_manifests": [],
                }
            )
        )
        report["normal_runs" if index < 12 else "impacted_runs"].append(
            {
                "run_id": run_id,
                "manifest_sha256": builder.digest(manifest),
                "actual_impact_start_time": impact,
            }
        )
    summary = campaign / "summary.json"
    summary.write_text(json.dumps(report))
    return state, summary


def test_technical_evaluation_never_activates_or_approves_model(cohort, monkeypatch):
    state, summary = cohort

    def worker(image, runtime, inputs, output, arguments, stage):
        assert not inputs.is_relative_to(output)
        assert len(list(inputs.iterdir())) == 24
        if stage == "train":
            candidate = output / "candidate"
            candidate.mkdir()
            (candidate / "manifest.json").write_text(
                json.dumps(
                    {
                        "predictive_claim_approved": False,
                        "approval_status": "evaluation_pending",
                        "deployment_scope": "none",
                        "technical_evaluation_gate": {"passed": False},
                    }
                )
            )

    monkeypatch.setattr(builder, "run_worker", worker)
    original = (state / "pilot-manifest.json").read_bytes()
    output = builder.build(state, summary)
    verification = json.loads((output / "verification.json").read_bytes())
    assert verification["status"] == "evaluation_complete"
    assert verification["technical_evaluation_gate"]["passed"] is False
    assert verification["model_approved"] is False
    assert verification["runtime_activated"] is False
    assert (state / "pilot-manifest.json").read_bytes() == original


def test_incomplete_cohort_cannot_start_model_worker(cohort, monkeypatch):
    state, summary = cohort
    data = json.loads(summary.read_bytes())
    data["status"] = "running"
    summary.write_text(json.dumps(data))
    worker = Mock()
    monkeypatch.setattr(builder, "run_worker", worker)
    with pytest.raises(ValueError, match="completed"):
        builder.build(state, summary)
    worker.assert_not_called()


def test_validation_rejection_completes_without_test_evaluation(cohort, monkeypatch):
    state, summary = cohort

    def worker(image, runtime, inputs, output, arguments, stage):
        if stage == "train":
            candidate = output / "candidate"
            candidate.mkdir()
            (candidate / "validation-rejection.json").write_text(
                json.dumps(
                    {
                        "schema_version": "predictive-validation-rejection-v1",
                        "status": "validation_gate_rejected",
                        "predictive_claim_approved": False,
                        "approval_status": "rejected",
                        "deployment_scope": "none",
                        "model_approved": False,
                        "runtime_activated": False,
                        "technical_evaluation_gate": {
                            "passed": False,
                            "evaluation_data": [],
                            "not_evaluated": ["test", "demo_holdout"],
                        },
                    }
                )
            )

    monkeypatch.setattr(builder, "run_worker", worker)
    output = builder.build(state, summary)
    verification = json.loads((output / "verification.json").read_bytes())

    assert verification["status"] == "evaluation_complete"
    assert verification["validation_rejected"] is True
    assert verification["technical_evaluation_gate"]["evaluation_data"] == []
    assert verification["technical_evaluation_gate"]["not_evaluated"] == [
        "test",
        "demo_holdout",
    ]
    assert verification["model_approved"] is False
    assert verification["runtime_activated"] is False


def test_changed_observation_is_excluded_before_training(cohort, monkeypatch):
    state, summary = cohort
    (state / "predictive-experiments/synthetic-00/observations.jsonl").write_bytes(b"changed")
    worker = Mock()
    monkeypatch.setattr(builder, "run_worker", worker)
    with pytest.raises(ValueError, match="artifact changed"):
        builder.build(state, summary)
    worker.assert_not_called()
    assert (
        json.loads((summary.parent / "model-build/verification.json").read_bytes())["status"]
        == "incomplete"
    )


def test_pilot_change_prevents_model_fit(cohort, monkeypatch):
    state, summary = cohort
    (state / "pilot-manifest.json").write_text("{}")
    worker = Mock()
    monkeypatch.setattr(builder, "run_worker", worker)
    with pytest.raises((ValueError, KeyError)):
        builder.build(state, summary)
    worker.assert_not_called()


def test_completed_build_is_idempotent_only_while_all_hashes_match(cohort, monkeypatch):
    state, summary = cohort
    calls = []

    def worker(image, runtime, inputs, output, arguments, stage):
        calls.append(stage)
        if stage == "train":
            candidate = output / "candidate"
            candidate.mkdir()
            (candidate / "manifest.json").write_text(
                json.dumps(
                    {
                        "predictive_claim_approved": False,
                        "approval_status": "evaluation_pending",
                        "deployment_scope": "none",
                        "technical_evaluation_gate": {"passed": True},
                    }
                )
            )

    monkeypatch.setattr(builder, "run_worker", worker)
    first = builder.build(state, summary)
    second = builder.build(state, summary)
    assert first == second
    assert calls == ["export", "train"]

    (first / "candidate/manifest.json").write_text("changed")
    with pytest.raises(ValueError, match="artifacts changed"):
        builder.build(state, summary)


def test_incomplete_build_is_preserved_and_retried_from_sealed_cohort(cohort, monkeypatch):
    state, summary = cohort
    fail = {"enabled": True}

    def worker(image, runtime, inputs, output, arguments, stage):
        if fail["enabled"]:
            raise RuntimeError("synthetic worker interruption")
        if stage == "train":
            candidate = output / "candidate"
            candidate.mkdir()
            (candidate / "manifest.json").write_text(
                json.dumps(
                    {
                        "predictive_claim_approved": False,
                        "approval_status": "evaluation_pending",
                        "deployment_scope": "none",
                        "technical_evaluation_gate": {"passed": True},
                    }
                )
            )

    monkeypatch.setattr(builder, "run_worker", worker)
    with pytest.raises(RuntimeError, match="worker interruption"):
        builder.build(state, summary)
    failed = json.loads((summary.parent / "model-build/verification.json").read_bytes())
    assert failed["status"] == "incomplete"
    assert failed["failure_type"] == "RuntimeError"
    failed["evaluation_exporter_sha256"] = "0" * 64
    (summary.parent / "model-build/verification.json").write_text(json.dumps(failed))

    fail["enabled"] = False
    output = builder.build(state, summary)
    verification = json.loads((output / "verification.json").read_bytes())
    assert verification["status"] == "evaluation_complete"
    assert verification["model_approved"] is False
    assert verification["prior_attempts"][0]["status"] == "incomplete"
    archive = summary.parent / verification["prior_attempts"][0]["path"]
    assert archive.is_dir()
    archived = json.loads((archive / "verification.json").read_bytes())
    assert archived["failure_type"] == "RuntimeError"
    inventory = json.loads((archive / "attempt-inventory.json").read_bytes())
    assert inventory["artifact_sha256"]["verification.json"]

    builder.build(state, summary)
    (archive / "verification.json").write_text("changed")
    with pytest.raises(ValueError, match="attempt artifacts changed"):
        builder.build(state, summary)
