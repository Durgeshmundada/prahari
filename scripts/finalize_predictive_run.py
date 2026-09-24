"""Validate and seal one real predictive experiment as immutable evidence."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from prahari.analytics.predictive import (
    ApplicationImpactPolicy,
    EvidenceFile,
    PredictiveExperimentManifest,
    PredictiveFaultRecord,
    PredictiveObservation,
    detect_sustained_application_impact,
    parse_strict_json,
    validate_fault_schedule,
)


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("experiment timestamps must include a UTC offset")
    return parsed.astimezone(UTC)


def finalize(
    *,
    run_directory: Path,
    run_id: str,
    scenario: str,
    started_at: datetime,
    experiment_seed: int,
    traffic_profile_id: str,
    fault_profile_id: str,
    sampling_seconds: int,
    policy: ApplicationImpactPolicy,
    fault_files: list[Path],
) -> PredictiveExperimentManifest:
    if run_directory.is_symlink() or not run_directory.is_dir():
        raise ValueError("run directory must be an existing non-symlink directory")
    observations_path = run_directory / "observations.jsonl"
    if observations_path.is_symlink() or not observations_path.is_file():
        raise ValueError("observations must be an existing non-symlink file")
    if sampling_seconds != 5:
        raise ValueError("predictive-v2 experiments require the frozen five-second cadence")
    raw = observations_path.read_bytes()
    if not raw.endswith(b"\n"):
        raise ValueError("observations JSONL must end with a newline")
    observations = [
        PredictiveObservation.model_validate(parse_strict_json(line))
        for line in raw.decode("utf-8").splitlines()
        if line
    ]
    if len(observations) < 36:
        raise ValueError("a predictive experiment requires at least 36 joined observations")
    if [observation.sequence for observation in observations] != list(range(len(observations))):
        raise ValueError("observation sequence must be contiguous and zero-based")
    if any(observation.run_id != run_id for observation in observations):
        raise ValueError("observation file contains a foreign run ID")
    if any(observation.scenario != scenario for observation in observations):
        raise ValueError("observation file contains a mismatched scenario")
    labs = {observation.controller_lab_name for observation in observations}
    if len(labs) != 1:
        raise ValueError("observation file contains different controller labs")
    if observations[0].probe_started_at < started_at:
        raise ValueError("an observation probe began before the experiment start")
    if any(not observation.local_evidence_complete for observation in observations):
        raise ValueError("every observation must contain complete local evidence")
    if any(
        not observation.network_records_persisted or not observation.application_records_persisted
        for observation in observations
    ):
        raise ValueError("every observation must be durably accepted by the telemetry API")
    if any(
        (later.observed_at - earlier.observed_at).total_seconds()
        > policy.max_inter_sample_gap_seconds
        for earlier, later in zip(observations, observations[1:], strict=False)
    ):
        raise ValueError("observation cadence exceeded the policy gap ceiling")
    detection = detect_sustained_application_impact(observations, policy)
    if scenario == "normal":
        zero = {"loss_percent": 0.0, "delay_ms": 0.0, "jitter_ms": 0.0}
        if fault_files:
            raise ValueError("normal runs cannot contain fault evidence")
        if fault_profile_id != "none":
            raise ValueError("normal runs must use the none fault profile")
        if any(
            observation.phase != "baseline" or observation.injected.model_dump() != zero
            for observation in observations
        ):
            raise ValueError("normal observations must remain at the zero-condition baseline")
    elif scenario == "gradual_loss":
        if len(fault_files) != 1:
            raise ValueError("gradual-loss runs require exactly one controller schedule")
        if fault_profile_id not in {"a", "b", "c"}:
            raise ValueError("gradual-loss runs require fault profile a, b, or c")
    else:
        raise ValueError("unsupported predictive experiment scenario")
    evidence_files: list[EvidenceFile] = []
    for path in fault_files:
        resolved = path.resolve()
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"fault evidence must be a non-symlink file: {path}")
        try:
            relative = resolved.relative_to(run_directory.resolve())
        except ValueError as error:
            raise ValueError("fault evidence must be stored inside the run directory") from error
        _validate_fault_evidence(path, run_id, observations)
        evidence_files.append(
            EvidenceFile(
                path=relative.as_posix(),
                sha256=sha256(path.read_bytes()).hexdigest(),
            )
        )
    completed_at = datetime.now(UTC)
    manifest = PredictiveExperimentManifest(
        schema_version="predictive-experiment-manifest-v2",
        run_id=run_id,
        controller_lab_name=observations[0].controller_lab_name,
        status="completed",
        scenario=scenario,
        experiment_seed=experiment_seed,
        traffic_profile_id=traffic_profile_id,
        fault_profile_id=fault_profile_id,
        started_at=started_at,
        completed_at=completed_at,
        target_entity_id="interface:branch-edge:eth1",
        topology_version="lab-v1",
        config_version="lab-config-v1",
        sampling_seconds=sampling_seconds,
        window_seconds=60,
        horizons_seconds=(30, 60, 120),
        sample_count=len(observations),
        observations_sha256=sha256(raw).hexdigest(),
        impact_policy=policy,
        actual_impact_start_time=detection.actual_impact_start_time,
        impact_confirmed_at=detection.confirmed_at,
        impact_triggering_signals=detection.triggering_signals,
        root_cause_verification=(
            "controlled_fault_manifest" if scenario == "gradual_loss" else "not_applicable"
        ),
        fault_manifests=evidence_files,
    )
    manifest_path = run_directory / "manifest.json"
    if manifest_path.exists():
        raise FileExistsError("refusing to overwrite a sealed experiment manifest")
    temporary = run_directory / "manifest.json.tmp"
    temporary.write_text(
        json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(manifest_path)
    return manifest


def _validate_fault_evidence(
    path: Path,
    run_id: str,
    observations: list[PredictiveObservation],
) -> None:
    raw = path.read_bytes()
    if not raw.endswith(b"\n"):
        raise ValueError("fault schedule JSONL must end with a newline")
    records = [
        PredictiveFaultRecord.model_validate(parse_strict_json(line))
        for line in raw.decode("utf-8").splitlines()
        if line
    ]
    validate_fault_schedule(records, observations, run_id=run_id)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--scenario", choices=("normal", "gradual_loss"), required=True)
    parser.add_argument("--started-at", type=_timestamp, required=True)
    parser.add_argument("--experiment-seed", type=int, required=True)
    parser.add_argument("--traffic-profile", required=True)
    parser.add_argument("--fault-profile", choices=("none", "a", "b", "c"), required=True)
    parser.add_argument("--sampling-seconds", type=int, default=5)
    parser.add_argument("--policy", type=Path, default=Path("config/predictive-v2.json"))
    parser.add_argument("--fault-file", type=Path, action="append", default=[])
    args = parser.parse_args()
    if args.scenario == "gradual_loss" and not args.fault_file:
        parser.error("gradual_loss requires at least one --fault-file")
    if args.scenario == "normal" and args.fault_file:
        parser.error("normal runs cannot include --fault-file")
    policy = ApplicationImpactPolicy.model_validate(parse_strict_json(args.policy.read_bytes()))
    manifest = finalize(
        run_directory=args.run_dir,
        run_id=args.run_id,
        scenario=args.scenario,
        started_at=args.started_at,
        experiment_seed=args.experiment_seed,
        traffic_profile_id=args.traffic_profile,
        fault_profile_id=args.fault_profile,
        sampling_seconds=args.sampling_seconds,
        policy=policy,
        fault_files=args.fault_file,
    )
    print(manifest.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
