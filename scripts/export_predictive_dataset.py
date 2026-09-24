"""Build an immutable, leakage-safe predictive-v2 dataset from real lab runs."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

from prahari.analytics.predictive import (
    FEATURE_NAMES,
    FEATURE_VERSION,
    ApplicationImpactPolicy,
    PredictiveExperimentManifest,
    PredictiveFaultRecord,
    PredictiveObservation,
    detect_sustained_application_impact,
    extract_predictive_feature_vectors,
    parse_strict_json,
    validate_fault_schedule,
)

HORIZONS = (30, 60, 120)
SPLITS = ("train", "validation", "test", "demo_holdout")
MINIMUM_RUNS_PER_OUTCOME_CLASS = 12
HELD_OUT_RUNS_PER_CLASS = 2
MINIMUM_RUNS_PER_PROFILE = 3
MINIMUM_DISTINCT_FAULT_TRAJECTORIES = 6
MINIMUM_TRAINING_RUNS_PER_CLASS = 6
MINIMUM_HELD_OUT_NEGATIVE_EXPOSURE_HOURS = 3.0
SPLIT_STRATEGY = (
    "whole-run deterministic outcome/profile stratification; 6+ train runs per class, "
    "2 impacted runs per held-out split, and at least 3 eligible negative link-hours "
    "per held-out split"
)


@dataclass(frozen=True)
class VerifiedRun:
    directory: Path
    manifest: PredictiveExperimentManifest
    observations: list[PredictiveObservation]


def assign_run_splits(
    impact_by_run: dict[str, bool],
    *,
    strata_by_run: dict[str, tuple[str, str]] | None = None,
    negative_exposure_hours_by_run: dict[str, float] | None = None,
) -> dict[str, str]:
    """Stratify whole runs without sharing evidence between dataset splits."""

    assignments: dict[str, str] = {}
    for has_impact in (False, True):
        run_ids = [run_id for run_id, value in impact_by_run.items() if value is has_impact]
        ordered = sorted(
            run_ids,
            key=lambda run_id: sha256(f"predictive-v2:{run_id}".encode()).hexdigest(),
        )
        if len(ordered) < MINIMUM_RUNS_PER_OUTCOME_CLASS:
            label = "impact" if has_impact else "no-impact"
            raise ValueError(
                f"at least {MINIMUM_RUNS_PER_OUTCOME_CLASS} independent {label} runs are required"
            )
        if not has_impact and negative_exposure_hours_by_run is not None:
            if strata_by_run is None:
                raise ValueError("exposure-aware splitting requires declared run strata")
            held_out = _assign_negative_exposure_heldouts(
                ordered,
                strata_by_run,
                negative_exposure_hours_by_run,
            )
        elif strata_by_run is None:
            holdout_start = -(3 * HELD_OUT_RUNS_PER_CLASS)
            validation_end = holdout_start + HELD_OUT_RUNS_PER_CLASS
            test_end = validation_end + HELD_OUT_RUNS_PER_CLASS
            held_out = {
                **{run_id: "validation" for run_id in ordered[holdout_start:validation_end]},
                **{run_id: "test" for run_id in ordered[validation_end:test_end]},
                **{run_id: "demo_holdout" for run_id in ordered[test_end:]},
            }
        else:
            if set(strata_by_run) != set(impact_by_run):
                raise ValueError("split strata must cover every run exactly once")
            remaining = list(ordered)
            held_out: dict[str, str] = {}
            for split in ("validation", "test", "demo_holdout"):
                selected: list[str] = []
                for _slot in range(HELD_OUT_RUNS_PER_CLASS):
                    candidates = [
                        run_id
                        for run_id in remaining
                        if _preserves_training_profile_coverage(
                            remaining,
                            run_id,
                            strata_by_run,
                            require_fault_diversity=has_impact,
                        )
                    ]
                    if selected:
                        candidates = [
                            run_id
                            for run_id in candidates
                            if strata_by_run[run_id][0] != strata_by_run[selected[0]][0]
                            and (
                                not has_impact
                                or strata_by_run[run_id][1] != strata_by_run[selected[0]][1]
                            )
                        ]
                    if not candidates:
                        raise ValueError(
                            f"cannot create a profile-diverse {split} split for "
                            f"the {'impact' if has_impact else 'no-impact'} class"
                        )
                    chosen = min(
                        candidates,
                        key=lambda run_id: sha256(
                            f"predictive-v2:{split}:{run_id}".encode()
                        ).hexdigest(),
                    )
                    remaining.remove(chosen)
                    selected.append(chosen)
                    held_out[chosen] = split
        assignments.update({run_id: held_out.get(run_id, "train") for run_id in ordered})
    return assignments


def _assign_negative_exposure_heldouts(
    ordered: list[str],
    strata_by_run: dict[str, tuple[str, str]],
    exposure_by_run: dict[str, float],
) -> dict[str, str]:
    if set(exposure_by_run) != set(ordered) or any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
        for value in exposure_by_run.values()
    ):
        raise ValueError("normal-run exposure must cover every no-impact run")
    required_total = len(("validation", "test", "demo_holdout")) * (
        MINIMUM_HELD_OUT_NEGATIVE_EXPOSURE_HOURS
    )
    if sum(exposure_by_run.values()) < required_total:
        raise ValueError(
            "normal evidence lacks the predeclared held-out negative-exposure hours"
        )
    remaining = list(ordered)
    held_out: dict[str, str] = {}
    splits = ("validation", "test", "demo_holdout")
    for split_index, split in enumerate(splits):
        selected: list[str] = []
        exposure = 0.0
        while (
            exposure < MINIMUM_HELD_OUT_NEGATIVE_EXPOSURE_HOURS
            or len(selected) < 2
            or len({strata_by_run[run_id][0] for run_id in selected}) < 2
        ):
            future_required = (len(splits) - split_index - 1) * (
                MINIMUM_HELD_OUT_NEGATIVE_EXPOSURE_HOURS
            )
            candidates = [
                run_id
                for run_id in remaining
                if len(remaining) > MINIMUM_TRAINING_RUNS_PER_CLASS
                and _preserves_training_profile_coverage(
                    remaining,
                    run_id,
                    strata_by_run,
                    require_fault_diversity=False,
                )
                and sum(
                    exposure_by_run[item] for item in remaining if item != run_id
                )
                >= future_required
            ]
            if not candidates:
                raise ValueError(
                    f"cannot allocate {split} negative exposure while preserving training evidence"
                )
            profile_counts = Counter(strata_by_run[item][0] for item in selected)
            chosen = min(
                candidates,
                key=lambda run_id: (
                    profile_counts[strata_by_run[run_id][0]],
                    -exposure_by_run[run_id],
                    sha256(f"predictive-v2:{split}:{run_id}".encode()).hexdigest(),
                ),
            )
            remaining.remove(chosen)
            selected.append(chosen)
            exposure += exposure_by_run[chosen]
            held_out[chosen] = split
    if len(remaining) < MINIMUM_TRAINING_RUNS_PER_CLASS:
        raise ValueError("exposure allocation left too few normal training runs")
    return held_out


def _preserves_training_profile_coverage(
    remaining: list[str],
    candidate: str,
    strata_by_run: dict[str, tuple[str, str]],
    *,
    require_fault_diversity: bool,
) -> bool:
    after = [strata_by_run[run_id] for run_id in remaining if run_id != candidate]
    if len({traffic for traffic, _fault in after}) < 3:
        return False
    return not require_fault_diversity or len({fault for _traffic, fault in after}) >= 3


def build_rows(
    runs: list[VerifiedRun],
    assignments: dict[str, str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for run in runs:
        manifest = run.manifest
        vectors = extract_predictive_feature_vectors(
            run.observations,
            run_id=manifest.run_id,
            window_seconds=manifest.window_seconds,
            sampling_seconds=manifest.sampling_seconds,
            minimum_samples=10,
            max_inter_sample_gap_seconds=manifest.impact_policy.max_inter_sample_gap_seconds,
        )
        impact_at = manifest.actual_impact_start_time
        run_end = run.observations[-1].observed_at
        for vector_index, vector in enumerate(vectors):
            if impact_at is not None and vector.window_end >= impact_at:
                continue
            next_vector_at = (
                vectors[vector_index + 1].window_end if vector_index + 1 < len(vectors) else run_end
            )
            exposure_end = min(
                next_vector_at,
                impact_at if impact_at is not None else run_end,
                vector.window_end
                + timedelta(seconds=manifest.impact_policy.max_inter_sample_gap_seconds),
            )
            prediction_exposure_seconds = max(
                0.0,
                (exposure_end - vector.window_end).total_seconds(),
            )
            if prediction_exposure_seconds <= 0:
                continue
            seconds_to_impact = (
                (impact_at - vector.window_end).total_seconds() if impact_at is not None else None
            )
            labels: dict[str, bool | None] = {}
            for horizon in HORIZONS:
                key = f"impact_within_{horizon}_seconds"
                if seconds_to_impact is not None:
                    labels[key] = 0 < seconds_to_impact <= horizon
                elif (run_end - vector.window_end).total_seconds() >= horizon:
                    labels[key] = False
                else:
                    # The observation period ended before this negative label's
                    # entire future horizon was seen. Preserve it as censored.
                    labels[key] = None
            if all(value is None for value in labels.values()):
                continue
            rows.append(
                {
                    "schema_version": "predictive-training-row-v2",
                    "run_id": manifest.run_id,
                    "split": assignments[manifest.run_id],
                    "scenario": manifest.scenario,
                    "experiment_seed": manifest.experiment_seed,
                    "traffic_profile_id": manifest.traffic_profile_id,
                    "fault_profile_id": manifest.fault_profile_id,
                    "feature_version": FEATURE_VERSION,
                    "window_start": vector.window_start.isoformat(),
                    "window_end": vector.window_end.isoformat(),
                    "sample_count": vector.sample_count,
                    "features": vector.values,
                    "labels": labels,
                    "actual_impact_start_time": (
                        impact_at.isoformat() if impact_at is not None else None
                    ),
                    "run_observation_start": run.observations[0].observed_at.isoformat(),
                    "run_observation_end": run_end.isoformat(),
                    "time_to_impact_seconds": seconds_to_impact,
                    "prediction_exposure_seconds": prediction_exposure_seconds,
                    "root_entity_id": (
                        manifest.target_entity_id
                        if manifest.root_cause_verification == "controlled_fault_manifest"
                        else None
                    ),
                    "root_cause_verification": manifest.root_cause_verification,
                }
            )
    return sorted(rows, key=lambda row: (str(row["run_id"]), str(row["window_end"])))


def load_verified_runs(
    runs_directory: Path,
    policy: ApplicationImpactPolicy,
) -> list[VerifiedRun]:
    if not runs_directory.is_dir() or runs_directory.is_symlink():
        raise ValueError("runs directory must be an existing, non-symlink directory")
    runs: list[VerifiedRun] = []
    for directory in sorted(path for path in runs_directory.iterdir() if path.is_dir()):
        if directory.is_symlink():
            raise ValueError(f"run directory cannot be a symlink: {directory}")
        manifest_path = directory / "manifest.json"
        observations_path = directory / "observations.jsonl"
        if not manifest_path.is_file() or not observations_path.is_file():
            continue
        if manifest_path.is_symlink() or observations_path.is_symlink():
            raise ValueError(f"run evidence cannot be a symlink: {directory}")
        raw_manifest = manifest_path.read_bytes()
        if not raw_manifest.endswith(b"\n"):
            raise ValueError(f"run {directory.name} manifest lacks a final newline")
        manifest = PredictiveExperimentManifest.model_validate(parse_strict_json(raw_manifest))
        if directory.name != manifest.run_id:
            raise ValueError(f"run directory name does not match sealed run ID: {directory}")
        if manifest.impact_policy != policy:
            raise ValueError(f"run {manifest.run_id} uses a different impact policy")
        raw_observations = observations_path.read_bytes()
        if not raw_observations.endswith(b"\n"):
            raise ValueError(f"run {manifest.run_id} observations lack a final newline")
        if sha256(raw_observations).hexdigest() != manifest.observations_sha256:
            raise ValueError(f"run {manifest.run_id} observation checksum does not match")
        observations = [
            PredictiveObservation.model_validate(parse_strict_json(line))
            for line in raw_observations.decode("utf-8").splitlines()
            if line
        ]
        _validate_run(manifest, observations, policy)
        _validate_fault_files(directory, manifest, observations)
        runs.append(VerifiedRun(directory, manifest, observations))
    if not runs:
        raise ValueError("no completed predictive-v2 runs were found")
    run_ids = [run.manifest.run_id for run in runs]
    if len(run_ids) != len(set(run_ids)):
        raise ValueError("predictive experiment run IDs must be unique")
    seeds = [(run.manifest.scenario, run.manifest.experiment_seed) for run in runs]
    if len(seeds) != len(set(seeds)):
        raise ValueError("predictive experiment seeds must be unique within each scenario")
    return runs


def _validate_run(
    manifest: PredictiveExperimentManifest,
    observations: list[PredictiveObservation],
    policy: ApplicationImpactPolicy,
) -> None:
    if manifest.sampling_seconds != 5:
        raise ValueError(f"run {manifest.run_id} does not use the frozen 5-second cadence")
    if len(observations) != manifest.sample_count:
        raise ValueError(f"run {manifest.run_id} sample count does not match its manifest")
    if [observation.sequence for observation in observations] != list(range(len(observations))):
        raise ValueError(f"run {manifest.run_id} observation sequence is incomplete")
    if any(
        later.observed_at <= earlier.observed_at
        for earlier, later in zip(observations, observations[1:], strict=False)
    ):
        raise ValueError(f"run {manifest.run_id} observation timestamps are not increasing")
    if any(
        (later.observed_at - earlier.observed_at).total_seconds()
        > policy.max_inter_sample_gap_seconds
        for earlier, later in zip(observations, observations[1:], strict=False)
    ):
        raise ValueError(f"run {manifest.run_id} observation cadence exceeded the gap ceiling")
    if any(observation.run_id != manifest.run_id for observation in observations):
        raise ValueError(f"run {manifest.run_id} contains a foreign run ID")
    if any(
        observation.controller_lab_name != manifest.controller_lab_name
        for observation in observations
    ):
        raise ValueError(f"run {manifest.run_id} contains a mismatched controller lab")
    if any(observation.scenario != manifest.scenario for observation in observations):
        raise ValueError(f"run {manifest.run_id} contains a mismatched scenario")
    if any(not observation.local_evidence_complete for observation in observations):
        raise ValueError(f"run {manifest.run_id} contains incomplete local evidence")
    if any(
        not observation.network_records_persisted or not observation.application_records_persisted
        for observation in observations
    ):
        raise ValueError(f"run {manifest.run_id} contains telemetry not persisted by the API")
    if observations[0].observed_at < manifest.started_at:
        raise ValueError(f"run {manifest.run_id} has an observation before its start")
    if observations[-1].observed_at > manifest.completed_at:
        raise ValueError(f"run {manifest.run_id} has an observation after its completion")
    detection = detect_sustained_application_impact(observations, policy)
    if detection.actual_impact_start_time != manifest.actual_impact_start_time:
        raise ValueError(f"run {manifest.run_id} impact start does not reproduce")
    if detection.confirmed_at != manifest.impact_confirmed_at:
        raise ValueError(f"run {manifest.run_id} impact confirmation does not reproduce")
    if detection.triggering_signals != manifest.impact_triggering_signals:
        raise ValueError(f"run {manifest.run_id} impact signals do not reproduce")


def _validate_fault_files(
    directory: Path,
    manifest: PredictiveExperimentManifest,
    observations: list[PredictiveObservation],
) -> None:
    if manifest.scenario == "normal":
        zero = {"loss_percent": 0.0, "delay_ms": 0.0, "jitter_ms": 0.0}
        if any(
            observation.phase != "baseline" or observation.injected.model_dump() != zero
            for observation in observations
        ):
            raise ValueError(f"run {manifest.run_id} normal evidence contains a fault condition")
        return
    if len(manifest.fault_manifests) != 1:
        raise ValueError(f"run {manifest.run_id} must bind exactly one fault schedule")
    evidence = manifest.fault_manifests[0]
    path = directory / evidence.path
    try:
        path.resolve().relative_to(directory.resolve())
    except ValueError as error:
        raise ValueError(f"run {manifest.run_id} fault path escapes its directory") from error
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"run {manifest.run_id} fault evidence is not a regular file")
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != evidence.sha256:
        raise ValueError(f"run {manifest.run_id} fault checksum does not match")
    if not raw.endswith(b"\n"):
        raise ValueError(f"run {manifest.run_id} fault schedule lacks a final newline")
    records = [
        PredictiveFaultRecord.model_validate(parse_strict_json(line))
        for line in raw.decode("utf-8").splitlines()
        if line
    ]
    validate_fault_schedule(records, observations, run_id=manifest.run_id)


def write_dataset(
    output: Path,
    rows: list[dict[str, Any]],
    runs: list[VerifiedRun],
    assignments: dict[str, str],
    policy: ApplicationImpactPolicy,
) -> Path:
    if output.exists():
        raise FileExistsError(f"refusing to overwrite immutable dataset: {output}")
    manifest_path = output.with_suffix(".manifest.json")
    if manifest_path.exists():
        raise FileExistsError(f"refusing to overwrite dataset manifest: {manifest_path}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as target:
        for row in rows:
            target.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
    temporary.replace(output)
    row_counts = {split: sum(row["split"] == split for row in rows) for split in SPLITS}
    run_counts = {split: sum(value == split for value in assignments.values()) for split in SPLITS}
    class_run_counts = {
        split: {
            "impact": sum(
                assignments[run.manifest.run_id] == split
                and run.manifest.actual_impact_start_time is not None
                for run in runs
            ),
            "no_impact": sum(
                assignments[run.manifest.run_id] == split
                and run.manifest.actual_impact_start_time is None
                for run in runs
            ),
        }
        for split in SPLITS
    }
    label_counts = {
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
        for horizon in HORIZONS
    }
    dataset_manifest = {
        "schema_version": "predictive-dataset-manifest-v2",
        "dataset_version": output.stem,
        "dataset_sha256": sha256(output.read_bytes()).hexdigest(),
        "feature_version": FEATURE_VERSION,
        "features": list(FEATURE_NAMES),
        "horizons_seconds": list(HORIZONS),
        "impact_policy": policy.model_dump(mode="json"),
        "split_strategy": SPLIT_STRATEGY,
        "split_assignments": dict(sorted(assignments.items())),
        "run_counts": run_counts,
        "class_run_counts": class_run_counts,
        "row_counts": row_counts,
        "label_counts": label_counts,
        "source_runs": [
            {
                "run_id": run.manifest.run_id,
                "scenario": run.manifest.scenario,
                "experiment_seed": run.manifest.experiment_seed,
                "traffic_profile_id": run.manifest.traffic_profile_id,
                "fault_profile_id": run.manifest.fault_profile_id,
                "observed_impact": run.manifest.actual_impact_start_time is not None,
                "observations_sha256": run.manifest.observations_sha256,
                "manifest_sha256": sha256(
                    (run.directory / "manifest.json").read_bytes()
                ).hexdigest(),
                "fault_evidence": [
                    evidence.model_dump(mode="json") for evidence in run.manifest.fault_manifests
                ],
                "observation_start": run.observations[0].observed_at.isoformat(),
                "observation_end": run.observations[-1].observed_at.isoformat(),
            }
            for run in sorted(runs, key=lambda item: item.manifest.run_id)
        ],
        "limitations": [
            "controlled Containerlab packet-loss/delay ramps only",
            "application impact labels come from the versioned chat/file probe policy",
            "demo_holdout is never available to training or threshold selection",
            "lab evaluation does not establish generalization to a production network",
        ],
    }
    manifest_path.write_text(
        json.dumps(dataset_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest_path


def validate_run_collection(runs: list[VerifiedRun]) -> None:
    """Require enough independent, varied outcome evidence before any split is frozen."""

    for has_impact in (False, True):
        selected = [
            run for run in runs if (run.manifest.actual_impact_start_time is not None) is has_impact
        ]
        label = "impact" if has_impact else "no-impact"
        if len(selected) < MINIMUM_RUNS_PER_OUTCOME_CLASS:
            raise ValueError(
                f"at least {MINIMUM_RUNS_PER_OUTCOME_CLASS} independent {label} runs are required"
            )
        traffic_profiles = {run.manifest.traffic_profile_id for run in selected}
        profile_counts = Counter(run.manifest.traffic_profile_id for run in selected)
        if (
            set(traffic_profiles) != {"light", "standard", "heavy"}
            or min(profile_counts.values()) < MINIMUM_RUNS_PER_PROFILE
        ):
            raise ValueError(
                f"{label} runs require light, standard, and heavy traffic with at least "
                f"{MINIMUM_RUNS_PER_PROFILE} runs each"
            )
        if len({run.manifest.sample_count for run in selected}) < 3:
            raise ValueError(f"{label} runs require at least three observation durations")
    impacted = [run for run in runs if run.manifest.actual_impact_start_time is not None]
    fault_profile_counts = Counter(run.manifest.fault_profile_id for run in impacted)
    if (
        set(fault_profile_counts) != {"a", "b", "c"}
        or min(fault_profile_counts.values()) < MINIMUM_RUNS_PER_PROFILE
    ):
        raise ValueError(
            "impacted runs require fault profiles a, b, and c with at least "
            f"{MINIMUM_RUNS_PER_PROFILE} runs each"
        )
    trajectories = {
        tuple(
            (
                observation.injected.loss_percent,
                observation.injected.delay_ms,
                observation.injected.jitter_ms,
            )
            for index, observation in enumerate(run.observations)
            if observation.injected.loss_percent
            + observation.injected.delay_ms
            + observation.injected.jitter_ms
            > 0
            and (index == 0 or observation.injected != run.observations[index - 1].injected)
        )
        for run in impacted
    }
    if len(trajectories) < MINIMUM_DISTINCT_FAULT_TRAJECTORIES:
        raise ValueError(
            "impacted runs require at least "
            f"{MINIMUM_DISTINCT_FAULT_TRAJECTORIES} distinct measured fault trajectories"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=Path("network-lab/state/predictive-experiments"),
    )
    parser.add_argument("--policy", type=Path, default=Path("config/predictive-v2.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    policy = ApplicationImpactPolicy.model_validate(parse_strict_json(args.policy.read_bytes()))
    runs = load_verified_runs(args.runs_dir, policy)
    validate_run_collection(runs)
    preliminary_rows = build_rows(
        runs,
        {run.manifest.run_id: "train" for run in runs},
    )
    normal_run_ids = {
        run.manifest.run_id
        for run in runs
        if run.manifest.actual_impact_start_time is None
    }
    negative_exposure = {
        run_id: sum(
            float(row["prediction_exposure_seconds"]) / 3600
            for row in preliminary_rows
            if row["run_id"] == run_id
            and row["labels"]["impact_within_120_seconds"] is False
        )
        for run_id in normal_run_ids
    }
    assignments = assign_run_splits(
        {run.manifest.run_id: run.manifest.actual_impact_start_time is not None for run in runs},
        strata_by_run={
            run.manifest.run_id: (
                run.manifest.traffic_profile_id,
                run.manifest.fault_profile_id,
            )
            for run in runs
        },
        negative_exposure_hours_by_run=negative_exposure,
    )
    rows = build_rows(runs, assignments)
    if not rows:
        raise ValueError("no uncensored predictive feature rows were produced")
    if {str(row["run_id"]) for row in rows} != set(assignments):
        raise ValueError("every assigned run must contribute at least one uncensored feature row")
    for horizon in HORIZONS:
        label = f"impact_within_{horizon}_seconds"
        for split in ("train", "validation", "test", "demo_holdout"):
            values = {
                row["labels"][label]
                for row in rows
                if row["split"] == split and row["labels"][label] is not None
            }
            if values != {False, True}:
                raise ValueError(f"{split} lacks both classes for the {horizon}-second horizon")
    manifest_path = write_dataset(args.output, rows, runs, assignments, policy)
    print(f"exported {len(rows)} predictive rows to {args.output}")
    print(f"dataset manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
