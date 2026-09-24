import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from prahari.analytics.horizon import (
    FALSE_ALERT_RATE_CONFIDENCE as RUNTIME_FALSE_ALERT_RATE_CONFIDENCE,
)
from prahari.analytics.horizon import (
    MAX_FALSE_ALERTS_PER_NEGATIVE_LINK_HOUR as RUNTIME_MAX_FALSE_ALERT_RATE,
)
from prahari.analytics.horizon import (
    TECHNICAL_EVALUATION_POLICY_VERSION as RUNTIME_TECHNICAL_EVALUATION_POLICY,
)
from prahari.analytics.horizon import (
    PredictiveHorizonClassifier,
)
from prahari.analytics.predictive import (
    FEATURE_NAMES,
    ApplicationImpactPolicy,
    ApplicationMeasurement,
    InjectedCondition,
    NetworkMeasurement,
    PredictiveFaultRecord,
    PredictiveObservation,
    _validate_applied_qdisc,
    validate_fault_schedule,
)
from scripts import export_predictive_dataset as exporter
from scripts import train_predictive_model as trainer
from scripts.export_predictive_dataset import (
    MINIMUM_RUNS_PER_OUTCOME_CLASS,
    assign_run_splits,
    build_rows,
)
from scripts.finalize_predictive_run import finalize
from scripts.train_predictive_model import (
    MAX_FALSE_ALERTS_PER_NEGATIVE_LINK_HOUR,
    SPLIT_STRATEGY,
    ValidationGateRejected,
    binary_metrics,
    calibrate,
    choose_projected_thresholds,
    enforce_monotonic_horizons,
    fit_platt_calibrator,
    incident_metrics,
    poisson_rate_upper_bound,
    train,
)


def test_predictive_split_is_whole_run_stratified_and_deterministic() -> None:
    runs = {
        **{f"normal-{index}": False for index in range(MINIMUM_RUNS_PER_OUTCOME_CLASS)},
        **{f"impact-{index}": True for index in range(MINIMUM_RUNS_PER_OUTCOME_CLASS)},
    }

    assignments = assign_run_splits(runs)

    assert assignments == assign_run_splits(runs)
    assert set(assignments.values()) == {"train", "validation", "test", "demo_holdout"}
    for split in ("validation", "test", "demo_holdout"):
        selected = [runs[run_id] for run_id, value in assignments.items() if value == split]
        assert selected.count(False) == 2
        assert selected.count(True) == 2
    assert (
        sum(value == "train" and runs[run_id] is False for run_id, value in assignments.items())
        == 6
    )
    assert (
        sum(value == "train" and runs[run_id] is True for run_id, value in assignments.items()) == 6
    )


def test_exposure_aware_split_reserves_three_negative_hours_per_holdout() -> None:
    runs = {
        **{f"normal-{index}": False for index in range(18)},
        **{f"impact-{index}": True for index in range(12)},
    }
    traffic = ("light", "standard", "heavy")
    strata = {
        run_id: (
            traffic[index % 3],
            ("a", "b", "c")[index % 3] if impacted else "none",
        )
        for index, (run_id, impacted) in enumerate(runs.items())
    }
    exposure = {run_id: 0.8 for run_id, impacted in runs.items() if not impacted}

    assignments = assign_run_splits(
        runs,
        strata_by_run=strata,
        negative_exposure_hours_by_run=exposure,
    )

    for split in ("validation", "test", "demo_holdout"):
        normal_ids = [
            run_id
            for run_id, assigned in assignments.items()
            if assigned == split and not runs[run_id]
        ]
        assert len(normal_ids) == 4
        assert sum(exposure[run_id] for run_id in normal_ids) >= 3
        assert len({strata[run_id][0] for run_id in normal_ids}) >= 2
    assert sum(
        assigned == "train" and not runs[run_id]
        for run_id, assigned in assignments.items()
    ) == 6


def test_predictive_split_refuses_less_than_twelve_runs_per_outcome() -> None:
    runs = {
        **{f"normal-{index}": False for index in range(12)},
        **{f"impact-{index}": True for index in range(11)},
    }

    with pytest.raises(ValueError, match="at least 12 independent impact"):
        assign_run_splits(runs)


def test_dataset_caps_prediction_exposure_when_feature_vectors_have_a_gap(monkeypatch) -> None:
    start = datetime(2026, 9, 20, tzinfo=UTC)
    policy = ApplicationImpactPolicy(
        version="test-v2",
        sustained_seconds=15,
        max_inter_sample_gap_seconds=15,
        message_loss_ratio=0.2,
        rtt_p95_ms=150,
        jitter_ms=30,
    )
    manifest = SimpleNamespace(
        run_id="normal-gap-test",
        window_seconds=60,
        sampling_seconds=5,
        impact_policy=policy,
        actual_impact_start_time=None,
        scenario="normal",
        experiment_seed=10,
        traffic_profile_id="standard",
        fault_profile_id="none",
        target_entity_id="interface:branch-edge:eth1",
        root_cause_verification="not_applicable",
    )
    vectors = [
        SimpleNamespace(
            window_start=start,
            window_end=start + timedelta(seconds=60),
            sample_count=10,
            values={},
        ),
        SimpleNamespace(
            window_start=start + timedelta(seconds=20),
            window_end=start + timedelta(seconds=80),
            sample_count=10,
            values={},
        ),
    ]
    monkeypatch.setattr(exporter, "extract_predictive_feature_vectors", lambda *a, **k: vectors)
    run = SimpleNamespace(
        manifest=manifest,
        observations=[
            SimpleNamespace(observed_at=start),
            SimpleNamespace(observed_at=start + timedelta(seconds=100)),
        ],
    )

    rows = build_rows([run], {manifest.run_id: "train"})

    assert [row["prediction_exposure_seconds"] for row in rows] == [15]


def test_calibration_is_finite_and_preserves_probability_order() -> None:
    raw = [0.05, 0.15, 0.25, 0.65, 0.75, 0.95]
    labels = [0, 0, 0, 1, 1, 1]

    slope, intercept = fit_platt_calibrator(raw, labels)
    calibrated = [calibrate(value, slope, intercept) for value in raw]

    assert 0 < calibrated[0] < calibrated[-1] < 1
    assert calibrated == sorted(calibrated)
    assert binary_metrics(labels, calibrated, 0.5)["f1"] == 1


def test_runtime_horizon_probabilities_are_monotonic() -> None:
    assert enforce_monotonic_horizons([0.4, 0.2, 0.8]) == [0.4, 0.4, 0.8]


def test_thresholds_are_chosen_from_runtime_projected_probabilities() -> None:
    base = datetime(2026, 9, 1, tzinfo=UTC)
    rows = []
    for label in (0, 1):
        rows.append(
            {
                "run_id": "impact" if label else "normal",
                "window_end": (base + timedelta(seconds=60)).isoformat(),
                "run_observation_start": base.isoformat(),
                "run_observation_end": (base + timedelta(seconds=200)).isoformat(),
                "actual_impact_start_time": (
                    (base + timedelta(seconds=160)).isoformat() if label else None
                ),
                "time_to_impact_seconds": 100 if label else None,
                "prediction_exposure_seconds": 10800 if not label else 15,
                "root_entity_id": "interface:branch-edge:eth1" if label else None,
                "labels": {
                    field: bool(label)
                    for field in (
                        "impact_within_30_seconds",
                        "impact_within_60_seconds",
                        "impact_within_120_seconds",
                    )
                },
            }
        )
    projected = [
        {30: 0.2, 60: 0.2, 120: 0.2},
        {30: 0.8, 60: 0.8, 120: 0.8},
    ]

    assert choose_projected_thresholds(rows, projected) == {30: 0.8, 60: 0.8, 120: 0.8}


def test_primary_threshold_rejects_validation_false_alert_tradeoff() -> None:
    base = datetime(2026, 9, 1, tzinfo=UTC)
    rows = []
    for label in (0, 1):
        rows.append(
            {
                "run_id": "impact" if label else "normal",
                "window_end": (base + timedelta(seconds=60)).isoformat(),
                "run_observation_start": base.isoformat(),
                "run_observation_end": (base + timedelta(seconds=200)).isoformat(),
                "actual_impact_start_time": (
                    (base + timedelta(seconds=160)).isoformat() if label else None
                ),
                "time_to_impact_seconds": 100 if label else None,
                "prediction_exposure_seconds": 10800 if not label else 15,
                "root_entity_id": "interface:branch-edge:eth1" if label else None,
                "labels": {
                    "impact_within_30_seconds": bool(label),
                    "impact_within_60_seconds": bool(label),
                    "impact_within_120_seconds": bool(label),
                },
            }
        )
    projected = [
        {30: 0.8, 60: 0.8, 120: 0.8},
        {30: 0.9, 60: 0.9, 120: 0.9},
    ]

    thresholds = choose_projected_thresholds(rows, projected)

    assert thresholds[120] == 0.9


def test_primary_threshold_refuses_to_consume_test_when_validation_cannot_pass() -> None:
    base = datetime(2026, 9, 1, tzinfo=UTC)
    rows = []
    for label in (0, 1):
        rows.append(
            {
                "run_id": "impact" if label else "normal",
                "window_end": (base + timedelta(seconds=60)).isoformat(),
                "run_observation_start": base.isoformat(),
                "run_observation_end": (base + timedelta(seconds=200)).isoformat(),
                "actual_impact_start_time": (
                    (base + timedelta(seconds=160)).isoformat() if label else None
                ),
                "time_to_impact_seconds": 100 if label else None,
                "prediction_exposure_seconds": 15,
                "root_entity_id": "interface:branch-edge:eth1" if label else None,
                "labels": {
                    "impact_within_30_seconds": bool(label),
                    "impact_within_60_seconds": bool(label),
                    "impact_within_120_seconds": bool(label),
                },
            }
        )
    projected = [
        {30: 0.9, 60: 0.9, 120: 0.9},
        {30: 0.8, 60: 0.8, 120: 0.8},
    ]

    with pytest.raises(ValidationGateRejected, match="untouched test and demo holdout") as error:
        choose_projected_thresholds(rows, projected)
    assert error.value.diagnostics["checks"] == {
        "validation_incident_precision_at_least_0_70": False,
        "validation_incident_recall_at_least_0_60": True,
        "validation_false_alerts_per_negative_link_hour_at_most_1_0": False,
        "validation_false_alert_rate_upper_bound_at_most_1_0": False,
        "validation_median_useful_lead_at_least_30_seconds": True,
    }


def _observation(
    index: int,
    *,
    run_id: str,
    scenario: str,
    phase: str | None = None,
    injected: InjectedCondition | None = None,
) -> PredictiveObservation:
    observed_at = datetime(2026, 9, 1, tzinfo=UTC) + timedelta(seconds=index * 5)
    return PredictiveObservation(
        run_id=run_id,
        sequence=index,
        scenario=scenario,
        phase=phase or ("baseline" if scenario == "normal" else "ramp"),
        target_entity_id="interface:branch-edge:eth1",
        injected=injected or InjectedCondition(loss_percent=0, delay_ms=0, jitter_ms=0),
        observed_at=observed_at,
        probe_started_at=observed_at - timedelta(seconds=2),
        network_observed_at=observed_at - timedelta(seconds=1),
        application_observed_at=observed_at,
        topology_version="lab-v1",
        config_version="lab-config-v1",
        local_evidence_complete=True,
        network_records_persisted=True,
        application_records_persisted=True,
        network=NetworkMeasurement(
            sent=5,
            received=5,
            packet_loss_ratio=0,
            rtt_avg_ms=1,
            jitter_ms=0,
        ),
        application=ApplicationMeasurement(
            status="passed",
            messages_attempted=5,
            messages_received=5,
            message_loss_ratio=0,
            rtt_p95_ms=2,
            jitter_ms=0,
            upload_status="completed",
            upload_integrity="verified",
            upload_throughput_mbps=20,
            evidence_saved=True,
        ),
    )


@pytest.mark.parametrize("lab_name", ["prahari", "prahari-owned"])
def test_normal_run_is_reproducibly_sealed(tmp_path: Path, lab_name: str) -> None:
    run_id = "predictive-normal-test-001"
    observations = [_observation(index, run_id=run_id, scenario="normal") for index in range(42)]
    observations = [
        item.model_copy(update={"controller_lab_name": lab_name}) for item in observations
    ]
    observations_path = tmp_path / "observations.jsonl"
    observations_path.write_text(
        "".join(observation.model_dump_json() + "\n" for observation in observations),
        encoding="utf-8",
    )
    policy = ApplicationImpactPolicy(
        version="test-v2",
        sustained_seconds=15,
        max_inter_sample_gap_seconds=15,
        message_loss_ratio=0.2,
        rtt_p95_ms=150,
        jitter_ms=30,
    )

    manifest = finalize(
        run_directory=tmp_path,
        run_id=run_id,
        scenario="normal",
        started_at=observations[0].observed_at - timedelta(seconds=3),
        experiment_seed=101,
        traffic_profile_id="standard",
        fault_profile_id="none",
        sampling_seconds=5,
        policy=policy,
        fault_files=[],
    )

    assert manifest.actual_impact_start_time is None
    assert manifest.sample_count == 42
    assert manifest.controller_lab_name == lab_name
    assert json.loads((tmp_path / "manifest.json").read_text())["status"] == "completed"


@pytest.mark.parametrize(
    "condition,options",
    [
        (
            InjectedCondition(loss_percent=0.475, delay_ms=0, jitter_ms=0),
            {"loss-random": {"loss": 0.00475}},
        ),
        (InjectedCondition(loss_percent=0, delay_ms=10, jitter_ms=0), {"delay": {"delay": 0.01}}),
    ],
)
def test_netem_accepts_kernel_omission_only_for_planned_zero(condition, options):
    _, records = _gradual_evidence()
    record = records[1].model_copy(
        update={
            "condition": condition,
            "qdisc_snapshot": [
                {"kind": "netem", "root": True, "options": options},
            ],
        }
    )
    _validate_applied_qdisc(record)


@pytest.mark.parametrize(
    "condition,options",
    [
        (
            InjectedCondition(loss_percent=0.475, delay_ms=0, jitter_ms=0.95),
            {"loss-random": {"loss": 0.00475}},
        ),
        (
            InjectedCondition(loss_percent=0.475, delay_ms=10, jitter_ms=0),
            {"loss-random": {"loss": 0.00475}},
        ),
        (InjectedCondition(loss_percent=0, delay_ms=0, jitter_ms=0), {"delay": None}),
        (InjectedCondition(loss_percent=0, delay_ms=0, jitter_ms=0), {"delay": {"delay": None}}),
    ],
)
def test_netem_rejects_ignored_nonzero_parameters_and_null_evidence(condition, options):
    _, records = _gradual_evidence()
    record = records[1].model_copy(
        update={
            "condition": condition,
            "qdisc_snapshot": [
                {"kind": "netem", "root": True, "options": options},
            ],
        }
    )
    with pytest.raises(ValueError):
        _validate_applied_qdisc(record)


def _gradual_evidence() -> tuple[list[PredictiveObservation], list[PredictiveFaultRecord]]:
    run_id = "predictive-gradual-test-001"
    zero = InjectedCondition(loss_percent=0, delay_ms=0, jitter_ms=0)
    transitions = {
        12: InjectedCondition(loss_percent=1, delay_ms=10, jitter_ms=2),
        15: InjectedCondition(loss_percent=2, delay_ms=20, jitter_ms=3),
        18: InjectedCondition(loss_percent=3, delay_ms=30, jitter_ms=4),
        21: InjectedCondition(loss_percent=4, delay_ms=40, jitter_ms=5),
        24: InjectedCondition(loss_percent=6, delay_ms=50, jitter_ms=7),
        27: InjectedCondition(loss_percent=8, delay_ms=60, jitter_ms=10),
    }
    observations = []
    condition = zero
    for index in range(42):
        if index in transitions:
            condition = transitions[index]
        if index < 12:
            phase, condition = "baseline", zero
        elif index <= 27:
            phase = "ramp"
        elif index < 30:
            phase = "steady"
        else:
            phase, condition = "recovery", zero
        observations.append(
            _observation(
                index,
                run_id=run_id,
                scenario="gradual_loss",
                phase=phase,
                injected=condition,
            )
        )

    def transition(
        sequence: int,
        condition: InjectedCondition,
        planned_state: str,
        acknowledged_state: str,
    ) -> list[PredictiveFaultRecord]:
        probe_start = observations[sequence].probe_started_at
        if acknowledged_state == "applied":
            snapshot = [
                {
                    "kind": "netem",
                    "root": True,
                    "options": {
                        "delay": {
                            "delay": condition.delay_ms / 1_000,
                            "jitter": condition.jitter_ms / 1_000,
                        },
                        "loss-random": {"loss": condition.loss_percent / 100},
                    },
                }
            ]
        else:
            snapshot = [{"kind": "noqueue", "root": True, "options": {}}]
        common = {
            "run_id": run_id,
            "target": "clab-prahari-branch-edge:eth1",
            "sequence": sequence,
            "condition": condition,
        }
        return [
            PredictiveFaultRecord(
                state=planned_state,
                recorded_at=probe_start - timedelta(seconds=1),
                **common,
            ),
            PredictiveFaultRecord(
                state=acknowledged_state,
                recorded_at=probe_start - timedelta(milliseconds=500),
                qdisc_snapshot=snapshot,
                **common,
            ),
        ]

    records = [
        record
        for sequence, applied_condition in transitions.items()
        for record in transition(sequence, applied_condition, "planned", "applied")
    ]
    records.extend(transition(30, zero, "clear_planned", "cleared"))
    return observations, records


def test_fault_schedule_is_causally_bound_to_every_observation() -> None:
    observations, records = _gradual_evidence()

    validate_fault_schedule(records, observations, run_id=observations[0].run_id)

    observations[13] = observations[13].model_copy(
        update={"injected": InjectedCondition(loss_percent=2, delay_ms=10, jitter_ms=2)}
    )
    with pytest.raises(ValueError, match="does not match the acknowledged controller state"):
        validate_fault_schedule(records, observations, run_id=observations[0].run_id)


def test_fault_schedule_rejects_qdisc_mismatch_and_missing_clear() -> None:
    observations, records = _gradual_evidence()
    wrong_snapshot = [
        {
            "kind": "netem",
            "root": True,
            "options": {
                "delay": {"delay": 0.010, "jitter": 0.002},
                "loss-random": {"loss": 0.03},
            },
        }
    ]
    mismatched = list(records)
    mismatched[1] = records[1].model_copy(update={"qdisc_snapshot": wrong_snapshot})

    with pytest.raises(ValueError, match="qdisc loss does not match"):
        validate_fault_schedule(mismatched, observations, run_id=observations[0].run_id)
    with pytest.raises(ValueError, match="followed by one clear"):
        validate_fault_schedule(records[:-2], observations, run_id=observations[0].run_id)


def test_false_alert_rate_uses_negative_exposure_not_wall_clock() -> None:
    base = datetime(2026, 9, 1, tzinfo=UTC)
    rows = []
    for index in range(2):
        rows.append(
            {
                "run_id": "normal-rate-test",
                "window_end": (base + timedelta(seconds=60 + index * 5)).isoformat(),
                "run_observation_start": base.isoformat(),
                "run_observation_end": (base + timedelta(seconds=180)).isoformat(),
                "actual_impact_start_time": None,
                "time_to_impact_seconds": None,
                "prediction_exposure_seconds": 5,
                "root_entity_id": None,
                "labels": {"impact_within_120_seconds": False},
            }
        )

    metrics = incident_metrics(rows, [{120: 0.9}, {120: 0.9}], 120, 0.5)

    assert metrics["false_alert_episodes"] == 1
    assert metrics["eligible_negative_link_hours"] == pytest.approx(10 / 3600)
    assert metrics["false_alerts_per_link_hour"] == pytest.approx(360)
    assert metrics["false_alert_rate_confidence"] == 0.95
    assert metrics["false_alert_rate_upper_bound_per_link_hour"] > 360
    assert MAX_FALSE_ALERTS_PER_NEGATIVE_LINK_HOUR == 1.0


def test_false_alert_confidence_bound_requires_about_three_clean_hours() -> None:
    assert poisson_rate_upper_bound(0, 1) == pytest.approx(2.99573227355399)
    assert poisson_rate_upper_bound(0, 3) < 1
    assert poisson_rate_upper_bound(1, 3) > 1


def test_training_gate_policy_matches_runtime_activation_policy() -> None:
    assert trainer.TECHNICAL_EVALUATION_POLICY_VERSION == RUNTIME_TECHNICAL_EVALUATION_POLICY
    assert trainer.FALSE_ALERT_RATE_CONFIDENCE == RUNTIME_FALSE_ALERT_RATE_CONFIDENCE
    assert (
        trainer.MAX_FALSE_ALERTS_PER_NEGATIVE_LINK_HOUR
        == RUNTIME_MAX_FALSE_ALERT_RATE
    )


def test_training_writes_a_verified_but_unapproved_candidate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    # Keep this small synthetic artifact test fast. Real training retains the
    # predeclared 95% confidence requirement.
    monkeypatch.setattr(trainer, "FALSE_ALERT_RATE_CONFIDENCE", 0.005)
    dataset = tmp_path / "predictive-test-v2.jsonl"
    rows = []
    base = datetime(2026, 9, 1, tzinfo=UTC)
    for split_index, split in enumerate(("train", "validation", "test", "demo_holdout")):
        for impacted in (False, True):
            run_id = f"{split}-{'impact' if impacted else 'normal'}"
            for row_index, lead in enumerate((20, 50, 100)):
                window_end = base + timedelta(hours=split_index, seconds=row_index * 5 + 60)
                values = {
                    name: float(10 + row_index if impacted else row_index) for name in FEATURE_NAMES
                }
                rows.append(
                    {
                        "schema_version": "predictive-training-row-v2",
                        "run_id": run_id,
                        "split": split,
                        "scenario": "gradual_loss" if impacted else "normal",
                        "feature_version": "predictive-window-v2",
                        "window_start": (window_end - timedelta(seconds=60)).isoformat(),
                        "window_end": window_end.isoformat(),
                        "sample_count": 12,
                        "features": values,
                        "labels": {
                            "impact_within_30_seconds": impacted and lead <= 30,
                            "impact_within_60_seconds": impacted and lead <= 60,
                            "impact_within_120_seconds": impacted,
                        },
                        "actual_impact_start_time": (
                            (window_end + timedelta(seconds=lead)).isoformat() if impacted else None
                        ),
                        "run_observation_start": (window_end - timedelta(seconds=60)).isoformat(),
                        "run_observation_end": (window_end + timedelta(seconds=140)).isoformat(),
                        "time_to_impact_seconds": lead if impacted else None,
                        "root_entity_id": ("interface:branch-edge:eth1" if impacted else None),
                        "root_cause_verification": (
                            "controlled_fault_manifest" if impacted else "not_applicable"
                        ),
                    }
                )
    templates = rows
    rows = []
    split_minimums = {"train": 6, "validation": 2, "test": 2, "demo_holdout": 2}
    source_runs = []
    split_assignments = {}
    seed = 100
    for split in ("train", "validation", "test", "demo_holdout"):
        for impacted in (False, True):
            template = [
                row
                for row in templates
                if row["split"] == split
                and (row["actual_impact_start_time"] is not None) is impacted
            ]
            for run_index in range(split_minimums[split]):
                run_id = f"{split}-{'impact' if impacted else 'normal'}-{run_index:02d}"
                seed += 1
                run_start = str(template[0]["window_start"])
                final_window = datetime.fromisoformat(str(template[-1]["window_end"]))
                run_end = (final_window + timedelta(seconds=140)).isoformat()
                impact_at = final_window + timedelta(seconds=30) if impacted else None
                traffic_profile = ("light", "standard", "heavy")[run_index % 3]
                fault_profile = ("a", "b", "c")[run_index % 3] if impacted else "none"
                split_assignments[run_id] = split
                for template_row in template:
                    row = dict(template_row)
                    window_end = datetime.fromisoformat(str(row["window_end"]))
                    lead = (
                        (impact_at - window_end).total_seconds() if impact_at is not None else None
                    )
                    row.update(
                        {
                            "run_id": run_id,
                            "experiment_seed": seed,
                            "traffic_profile_id": traffic_profile,
                            "fault_profile_id": fault_profile,
                            "actual_impact_start_time": (
                                impact_at.isoformat() if impact_at is not None else None
                            ),
                            "run_observation_start": run_start,
                            "run_observation_end": run_end,
                            "time_to_impact_seconds": lead,
                            "prediction_exposure_seconds": 5,
                            "labels": {
                                f"impact_within_{horizon}_seconds": (
                                    lead <= horizon if lead is not None else False
                                )
                                for horizon in (30, 60, 120)
                            },
                        }
                    )
                    rows.append(row)
                source_runs.append(
                    {
                        "run_id": run_id,
                        "scenario": "gradual_loss" if impacted else "normal",
                        "experiment_seed": seed,
                        "traffic_profile_id": traffic_profile,
                        "fault_profile_id": fault_profile,
                        "observed_impact": impacted,
                        "observations_sha256": "a" * 64,
                        "manifest_sha256": "b" * 64,
                        "fault_evidence": (
                            [{"path": "fault-schedule.jsonl", "sha256": "c" * 64}]
                            if impacted
                            else []
                        ),
                        "observation_start": run_start,
                        "observation_end": run_end,
                    }
                )
    rows.sort(key=lambda row: (str(row["run_id"]), str(row["window_end"])))
    dataset.write_text(
        "".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )
    dataset_manifest = {
        "schema_version": "predictive-dataset-manifest-v2",
        "dataset_version": "predictive-test-v2",
        "dataset_sha256": sha256(dataset.read_bytes()).hexdigest(),
        "feature_version": "predictive-window-v2",
        "features": list(FEATURE_NAMES),
        "horizons_seconds": [30, 60, 120],
        "impact_policy": {
            "schema_version": "application-impact-policy-v2",
            "version": "test-v2",
            "sustained_seconds": 15,
            "max_inter_sample_gap_seconds": 15,
            "message_loss_ratio": 0.2,
            "rtt_p95_ms": 150,
            "jitter_ms": 30,
            "failed_upload_is_impact": True,
        },
        "split_strategy": SPLIT_STRATEGY,
        "split_assignments": dict(sorted(split_assignments.items())),
        "run_counts": {split: minimum * 2 for split, minimum in split_minimums.items()},
        "class_run_counts": {
            split: {"impact": minimum, "no_impact": minimum}
            for split, minimum in split_minimums.items()
        },
        "row_counts": {split: minimum * 6 for split, minimum in split_minimums.items()},
        "label_counts": {
            str(horizon): {
                "positive": sum(
                    row["labels"][f"impact_within_{horizon}_seconds"] is True for row in rows
                ),
                "negative": sum(
                    row["labels"][f"impact_within_{horizon}_seconds"] is False for row in rows
                ),
                "censored": sum(
                    row["labels"][f"impact_within_{horizon}_seconds"] is None for row in rows
                ),
            }
            for horizon in (30, 60, 120)
        },
        "source_runs": sorted(source_runs, key=lambda item: str(item["run_id"])),
        "limitations": ["unit-test data only"],
    }
    dataset.with_suffix(".manifest.json").write_text(
        json.dumps(dataset_manifest, sort_keys=True) + "\n", encoding="utf-8"
    )
    output = tmp_path / "model"

    manifest = train(dataset, output, "risk-unit-test-v2")

    assert manifest["approval_status"] == "evaluation_pending"
    assert manifest["predictive_claim_approved"] is False
    assert (
        "test_false_alerts_per_negative_link_hour_at_most_1_0"
        in manifest["technical_evaluation_gate"]["checks"]
    )
    assert (
        "validation_false_alerts_per_negative_link_hour_at_most_1_0"
        in manifest["technical_evaluation_gate"]["checks"]
    )
    assert (
        "test_false_alert_rate_upper_bound_at_most_1_0"
        in manifest["technical_evaluation_gate"]["checks"]
    )
    assert manifest["technical_evaluation_gate"]["policy_version"] == (
        "technical-evaluation-v2"
    )
    assert manifest["technical_evaluation_gate"]["false_alert_rate_policy"][
        "one_sided_confidence"
    ] == pytest.approx(0.005)
    assert manifest["technical_evaluation_gate"]["threshold_selection"]["120"].startswith(
        "validation-only"
    )
    assert manifest["technical_evaluation_gate"]["excluded_from_gate"] == ["demo_holdout"]
    assert set(path.name for path in output.glob("risk-within-*.txt")) == {
        "risk-within-30s.txt",
        "risk-within-60s.txt",
        "risk-within-120s.txt",
    }
    assert PredictiveHorizonClassifier(output / "manifest.json").activated is False


def test_owned_fault_schedule_rejects_foreign_lab_evidence() -> None:
    observations, records = _gradual_evidence()
    observations = [
        item.model_copy(update={"controller_lab_name": "prahari-owned"}) for item in observations
    ]
    owned = [
        item.model_copy(update={"target": "clab-prahari-owned-branch-edge:eth1"})
        for item in records
    ]
    validate_fault_schedule(owned, observations, run_id=observations[0].run_id)
    with pytest.raises(ValueError, match="target does not match"):
        validate_fault_schedule(records, observations, run_id=observations[0].run_id)
    mixed = list(owned)
    mixed[1] = records[1]
    with pytest.raises(ValueError, match="target does not match"):
        validate_fault_schedule(mixed, observations, run_id=observations[0].run_id)
    observations[0] = observations[0].model_copy(update={"controller_lab_name": "prahari"})
    with pytest.raises(ValueError, match="different controller labs"):
        validate_fault_schedule(owned, observations, run_id=observations[0].run_id)
