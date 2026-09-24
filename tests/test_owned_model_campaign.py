"""Campaign safety fixtures; no measured data or model acceptance is fabricated."""

import hashlib
import json
import signal
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import run_owned_model_campaign as collector


@pytest.fixture
def state(tmp_path, monkeypatch):
    root = tmp_path / "pilot"
    script = root / "runtime/network-lab/scripts/run-predictive-experiment.sh"
    script.parent.mkdir(parents=True)
    script.write_text("# synthetic frozen controller fixture\n")
    (root / "pilot-manifest.json").write_text(
        json.dumps(
            {
                "lab": "prahari-owned",
                "action_adapter_enabled": False,
                "runtime_files": {
                    "network-lab/scripts/run-predictive-experiment.sh": hashlib.sha256(
                        script.read_bytes()
                    ).hexdigest()
                },
            }
        )
    )
    # Read-only cleanup semantics differ on Windows; production freezing is
    # exercised on Linux, while these tests exercise orchestration decisions.
    monkeypatch.setattr(Path, "chmod", lambda *args: None)
    return root


def summary(state):
    return json.loads(next((state / "model-campaigns").glob("*/summary.json")).read_text())


def test_complete_on_final_allowed_attempt_keeps_real_labels(state, monkeypatch):
    counters = {"normal": 0, "gradual_loss": 0}

    def measured_fixture(command, log):
        def argument(name):
            return command[command.index(name) + 1]

        scenario = argument("--scenario")
        attempt = counters[scenario]
        counters[scenario] += 1
        if scenario == "gradual_loss" and attempt < 9:
            log.write_text("synthetic failed attempt\n")
            return 1
        run_id = f"fixture-{scenario}-{attempt}"
        sealed = state / "predictive-experiments" / run_id / "manifest.json"
        sealed.parent.mkdir(parents=True)
        sealed.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "status": "completed",
                    "controller_lab_name": "prahari-owned",
                    "scenario": scenario,
                    "experiment_seed": int(argument("--experiment-seed")),
                    "traffic_profile_id": argument("--traffic-profile"),
                    "fault_profile_id": argument("--profile")
                    if scenario == "gradual_loss"
                    else "none",
                    "actual_impact_start_time": "synthetic-impact"
                    if scenario == "gradual_loss"
                    else None,
                }
            )
        )
        log.write_text(f"Predictive experiment sealed: {sealed}\n")
        return 0

    monkeypatch.setattr(collector, "execute", measured_fixture)
    assert collector.campaign(state, 3, 100) == 0
    report = summary(state)
    assert counters["gradual_loss"] == 18
    assert report["status"] == "collection_complete"
    assert len(report["failed_attempts"]) == 9
    assert len(report["normal_runs"]) == 3
    assert len(report["impacted_runs"]) == 9
    assert report["model_approved"] is False


def test_changed_release_aborts_with_incomplete_checkpoint(state, monkeypatch):
    def change_release(command, log):
        (state / "pilot-manifest.json").write_text("{}")
        log.write_text("synthetic interrupted attempt\n")
        return 1

    monkeypatch.setattr(collector, "execute", change_release)
    with pytest.raises(ValueError, match="release changed"):
        collector.campaign(state, 12, 100)
    assert summary(state)["status"] == "incomplete"


def test_foreign_evidence_path_cannot_enter_cohort(state, monkeypatch, tmp_path):
    foreign = tmp_path / "foreign.json"
    foreign.write_text("{}")

    def foreign_result(command, log):
        log.write_text(f"Predictive experiment sealed: {foreign}\n")
        return 0

    monkeypatch.setattr(collector, "execute", foreign_result)
    with pytest.raises(ValueError, match="unexpected evidence path"):
        collector.campaign(state, 12, 100)
    assert summary(state)["status"] == "incomplete"


def test_controller_timeout_terminates_only_child_process_group(tmp_path, monkeypatch):
    child = Mock(pid=4567)
    child.wait.side_effect = [subprocess.TimeoutExpired("synthetic", 1), 143]
    child.poll.return_value = None
    monkeypatch.setattr(collector.subprocess, "Popen", Mock(return_value=child))
    terminate = Mock()
    monkeypatch.setattr(collector.os, "killpg", terminate, raising=False)
    with pytest.raises(subprocess.TimeoutExpired):
        collector.execute(["synthetic-controller"], tmp_path / "log", 1)
    terminate.assert_called_once_with(4567, signal.SIGTERM)


def test_changed_frozen_source_rejected_before_experiment(state, monkeypatch):
    (state / "runtime/network-lab/scripts/run-predictive-experiment.sh").write_text("changed")
    execute = Mock()
    monkeypatch.setattr(collector, "execute", execute)
    with pytest.raises(ValueError, match="source changed"):
        collector.campaign(state, 12, 100)
    execute.assert_not_called()


def test_resume_revalidates_and_continues_after_last_attempt_log(state, monkeypatch):
    interrupted = {"enabled": True}

    def measured_fixture(command, log):
        def argument(name):
            return command[command.index(name) + 1]

        scenario = argument("--scenario")
        attempt = int(log.stem.rsplit("-", 1)[1])
        if interrupted["enabled"] and scenario == "gradual_loss" and attempt == 2:
            log.write_text("synthetic interrupted controller\n")
            raise KeyboardInterrupt("synthetic host shutdown")
        run_id = f"resume-{scenario}-{attempt}"
        sealed = state / "predictive-experiments" / run_id / "manifest.json"
        sealed.parent.mkdir(parents=True)
        sealed.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "status": "completed",
                    "controller_lab_name": "prahari-owned",
                    "scenario": scenario,
                    "experiment_seed": int(argument("--experiment-seed")),
                    "traffic_profile_id": argument("--traffic-profile"),
                    "fault_profile_id": argument("--profile")
                    if scenario == "gradual_loss"
                    else "none",
                    "actual_impact_start_time": "synthetic-impact"
                    if scenario == "gradual_loss"
                    else None,
                }
            )
        )
        log.write_text(f"Predictive experiment sealed: {sealed}\n")
        return 0

    monkeypatch.setattr(collector, "execute", measured_fixture)
    with pytest.raises(KeyboardInterrupt, match="host shutdown"):
        collector.campaign(state, 3, 100)
    report_path = next((state / "model-campaigns").glob("*/summary.json"))
    before = json.loads(report_path.read_text())
    first_log_hash = hashlib.sha256(
        (report_path.parent / "gradual_loss-000.log").read_bytes()
    ).hexdigest()
    assert before["status"] == "incomplete"
    assert len(before["impacted_runs"]) == 2
    before["resumptions"] = [
        {
            "prior_summary_sha256": f"{index:064x}",
            "resumer_source_sha256": "a" * 64,
            "resumed_at": "2026-01-01T00:00:00+00:00",
        }
        for index in range(64)
    ]
    report_path.write_text(json.dumps(before))

    interrupted["enabled"] = False
    assert collector.campaign(state, 3, 100, resume_summary=report_path) == 0

    after = json.loads(report_path.read_text())
    assert after["status"] == "collection_complete"
    assert "failure_type" not in after
    assert len(after["resumptions"]) == 1
    assert after["resumption_archives"][0]["entry_count"] == 64
    assert (report_path.parent / after["resumption_archives"][0]["path"]).is_file()
    assert after["interrupted_attempts"][0]["log"] == "gradual_loss-002.log"
    assert all(item["seed"] != 105 for item in after["impacted_runs"])
    assert (
        hashlib.sha256((report_path.parent / "gradual_loss-000.log").read_bytes()).hexdigest()
        == first_log_hash
    )


def test_measurement_failures_exhaust_once_without_unbounded_attempts(state, monkeypatch):
    def failed_measurement(command, log):
        log.write_text("ERROR: synthetic measurement failure\n")
        return 1

    monkeypatch.setattr(collector, "execute", failed_measurement)
    assert collector.campaign(state, 3, 100) == 2
    report = summary(state)
    assert report["status"] == "attempts_exhausted"
    assert len(report["failed_attempts"]) == 18
    assert all(item["failure_class"] == "measurement_failed" for item in report["failed_attempts"])


def test_infrastructure_outage_does_not_consume_measurement_budget(state, monkeypatch):
    def measured_after_startup(command, log):
        def argument(name):
            return command[command.index(name) + 1]

        scenario = argument("--scenario")
        attempt = int(log.stem.rsplit("-", 1)[1])
        if scenario == "gradual_loss" and attempt < 5:
            log.write_text("ERROR: controlled eth1 is unavailable\n")
            return 1
        run_id = f"startup-{scenario}-{attempt}"
        sealed = state / "predictive-experiments" / run_id / "manifest.json"
        sealed.parent.mkdir(parents=True)
        sealed.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "status": "completed",
                    "controller_lab_name": "prahari-owned",
                    "scenario": scenario,
                    "experiment_seed": int(argument("--experiment-seed")),
                    "traffic_profile_id": argument("--traffic-profile"),
                    "fault_profile_id": argument("--profile")
                    if scenario == "gradual_loss"
                    else "none",
                    "actual_impact_start_time": "synthetic-impact"
                    if scenario == "gradual_loss"
                    else None,
                }
            )
        )
        log.write_text(f"Predictive experiment sealed: {sealed}\n")
        return 0

    monkeypatch.setattr(collector, "execute", measured_after_startup)
    assert collector.campaign(state, 3, 100) == 0
    report = summary(state)
    assert report["status"] == "collection_complete"
    assert len(report["failed_attempts"]) == 5
    assert all(
        item["failure_class"] == "infrastructure_unavailable" for item in report["failed_attempts"]
    )


def test_resumed_campaign_fills_missing_traffic_quota_directly(state, monkeypatch):
    calls: list[str] = []

    def measured_fixture(command, log):
        def argument(name):
            return command[command.index(name) + 1]

        scenario = argument("--scenario")
        traffic = argument("--traffic-profile")
        calls.append(f"{scenario}:{traffic}")
        run_id = f"quota-{scenario}-{len(calls):02d}"
        sealed = state / "predictive-experiments" / run_id / "manifest.json"
        sealed.parent.mkdir(parents=True)
        sealed.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "status": "completed",
                    "controller_lab_name": "prahari-owned",
                    "scenario": scenario,
                    "experiment_seed": int(argument("--experiment-seed")),
                    "traffic_profile_id": traffic,
                    "fault_profile_id": (
                        argument("--profile") if scenario == "gradual_loss" else "none"
                    ),
                    "actual_impact_start_time": (
                        "synthetic-impact" if scenario == "gradual_loss" else None
                    ),
                }
            )
        )
        log.write_text(f"Predictive experiment sealed: {sealed}\n")
        return 0

    monkeypatch.setattr(collector, "execute", measured_fixture)
    assert collector.campaign(state, 12, 100) == 0
    impacted_traffic = [item.split(":", 1)[1] for item in calls if item.startswith("gradual_loss:")]
    assert impacted_traffic[:3] == ["light", "standard", "heavy"]
    assert len(impacted_traffic) == 12
