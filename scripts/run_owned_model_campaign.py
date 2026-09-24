"""Collect a bounded balanced cohort on the owned pilot; never approve a model."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

RUN_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,199}")
LOG_NAME = re.compile(r"(gradual_loss|normal)-(\d{3})\.log")
INFRASTRUCTURE_FAILURES = (
    "ERROR: controlled eth1 is unavailable",
    "ERROR: Docker daemon is not reachable by this user",
)


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def read_object(path: Path, *, maximum_bytes: int = 2 * 1024 * 1024) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum_bytes:
        raise ValueError("campaign metadata must be a bounded regular file")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("campaign metadata must be an object")
    return value


def failure_class(log: Path) -> str:
    text = log.read_text(errors="replace")
    if any(marker in text for marker in INFRASTRUCTURE_FAILURES):
        return "infrastructure_unavailable"
    return "measurement_failed"


def execute(command: list[str], log: Path, deadline_seconds: int = 7200) -> int:
    """Bound each controller and terminate only its own process group on cancellation."""
    with log.open("w") as stream:
        process = subprocess.Popen(
            command, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True
        )
        try:
            return process.wait(timeout=deadline_seconds)
        except BaseException:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)
            raise


def campaign(
    state: Path,
    runs_per_class: int,
    base_seed: int,
    delay_scale: int = 200,
    resume_summary: Path | None = None,
    normal_samples: int = 600,
) -> int:
    state = state.resolve(strict=True)
    manifest_path = state / "pilot-manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest["lab"] != "prahari-owned" or manifest["action_adapter_enabled"] is not False:
        raise ValueError("campaign requires the explicitly owned monitoring-only pilot")
    frozen_root = state / "runtime"
    for relative, expected in manifest["runtime_files"].items():
        path = frozen_root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(frozen_root):
            raise ValueError("unexpected frozen source path")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("frozen pilot source changed")
    original = frozen_root / "network-lab/scripts/run-predictive-experiment.sh"
    if (
        hashlib.sha256(original.read_bytes()).hexdigest()
        != manifest["runtime_files"]["network-lab/scripts/run-predictive-experiment.sh"]
    ):
        raise ValueError("campaign controller is not the frozen pilot controller")
    current_source_sha256 = digest(Path(__file__))
    if resume_summary is None:
        identifier = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
        output = state / "model-campaigns" / identifier
        output.mkdir(parents=True, mode=0o700)
        # Stage a separate immutable controller release. Existing live collectors
        # and prior captures retain their original source identity.
        runtime = output / "runtime"
        shutil.copytree(frozen_root, runtime)
        script = runtime / "network-lab/scripts/run-predictive-experiment.sh"
        source_root = Path(__file__).resolve().parent.parent
        for relative in (
            "network-lab/scripts/run-predictive-experiment.sh",
            "src/prahari/analytics/predictive.py",
        ):
            staged = runtime / relative
            staged.parent.mkdir(parents=True, exist_ok=True)
            if staged.exists():
                staged.chmod(0o644)
            staged.write_bytes((source_root / relative).read_bytes())
        runtime_hashes = {}
        for path in runtime.rglob("*"):
            if path.is_symlink():
                raise ValueError("campaign runtime must not contain symlinks")
            if path.is_file():
                path.chmod(0o444)
                runtime_hashes[path.relative_to(runtime).as_posix()] = digest(path)
        report = {
            "schema_version": "owned-model-campaign-v1",
            "status": "running",
            "pilot_sha256": digest(manifest_path),
            "controller_sha256": digest(script),
            "campaign_source_sha256": current_source_sha256,
            "requested_runs_per_class": runs_per_class,
            "base_seed": base_seed,
            "delay_scale_percent": delay_scale,
            "normal_samples": normal_samples,
            "runtime_files": runtime_hashes,
            "normal_runs": [],
            "impacted_runs": [],
            "nonimpact_fault_runs": [],
            "failed_attempts": [],
            "interrupted_attempts": [],
            "resumptions": [],
            "model_approved": False,
        }
    else:
        if resume_summary.is_symlink():
            raise ValueError("resume summary must not be a symlink")
        summary_path = resume_summary.resolve(strict=True)
        campaigns_root = (state / "model-campaigns").resolve(strict=True)
        if not summary_path.is_relative_to(campaigns_root) or summary_path.name != "summary.json":
            raise ValueError("resume summary does not belong to the owned pilot")
        output = summary_path.parent
        prior_summary_sha256 = digest(summary_path)
        report = read_object(summary_path)
        expected = {
            "schema_version": "owned-model-campaign-v1",
            "pilot_sha256": digest(manifest_path),
            "requested_runs_per_class": runs_per_class,
            "base_seed": base_seed,
            "delay_scale_percent": delay_scale,
            "normal_samples": normal_samples,
            "model_approved": False,
        }
        if report.get("status") not in {"running", "incomplete"} or any(
            report.get(key) != value for key, value in expected.items()
        ):
            raise ValueError("resume request differs from the interrupted campaign")
        runtime = output / "runtime"
        if runtime.is_symlink() or not runtime.resolve(strict=True).is_relative_to(output):
            raise ValueError("campaign runtime escaped its evidence directory")
        runtime_hashes = report.get("runtime_files")
        if not isinstance(runtime_hashes, dict) or not runtime_hashes:
            raise ValueError("campaign runtime manifest is missing")
        for relative, expected_hash in runtime_hashes.items():
            if not isinstance(relative, str) or not re.fullmatch(r"[a-zA-Z0-9_./-]+", relative):
                raise ValueError("campaign runtime path is invalid")
            path = runtime / relative
            if (
                path.is_symlink()
                or not path.resolve(strict=True).is_relative_to(runtime)
                or digest(path) != expected_hash
            ):
                raise ValueError("campaign source changed before resumption")
        script = runtime / "network-lab/scripts/run-predictive-experiment.sh"
        if digest(script) != report.get("controller_sha256"):
            raise ValueError("resumed controller identity changed")
        for key in ("normal_runs", "impacted_runs", "nonimpact_fault_runs", "failed_attempts"):
            if not isinstance(report.get(key), list):
                raise ValueError("campaign checkpoint has an invalid collection")
        seen_run_ids: set[str] = set()
        for collection, scenario, impacted in (
            (report["normal_runs"], "normal", False),
            (report["impacted_runs"], "gradual_loss", True),
            (report["nonimpact_fault_runs"], "gradual_loss", False),
        ):
            for item in collection:
                if not isinstance(item, dict) or not RUN_ID.fullmatch(str(item.get("run_id", ""))):
                    raise ValueError("campaign checkpoint has an invalid run identity")
                run_id = item["run_id"]
                if run_id in seen_run_ids:
                    raise ValueError("campaign checkpoint repeats a measured run")
                seen_run_ids.add(run_id)
                metadata_path = state / "predictive-experiments" / run_id / "manifest.json"
                metadata = read_object(metadata_path, maximum_bytes=32 * 1024 * 1024)
                if (
                    digest(metadata_path) != item.get("manifest_sha256")
                    or metadata.get("run_id") != run_id
                    or metadata.get("status") != "completed"
                    or metadata.get("controller_lab_name") != "prahari-owned"
                    or metadata.get("scenario") != scenario
                    or metadata.get("experiment_seed") != item.get("seed")
                    or metadata.get("traffic_profile_id") != item.get("traffic_profile")
                    or metadata.get("fault_profile_id") != item.get("fault_profile")
                    or metadata.get("actual_impact_start_time")
                    != item.get("actual_impact_start_time")
                    or (metadata.get("actual_impact_start_time") is not None) != impacted
                ):
                    raise ValueError("campaign checkpoint differs from sealed measured evidence")
        resumptions = report.setdefault("resumptions", [])
        if not isinstance(resumptions, list):
            raise ValueError("campaign resumption history is invalid")
        archives = report.setdefault("resumption_archives", [])
        if not isinstance(archives, list):
            raise ValueError("campaign resumption archive inventory is invalid")
        for archive in archives:
            if not isinstance(archive, dict) or not re.fullmatch(
                r"resumption-history-[a-f0-9]{16}\.json", str(archive.get("path", ""))
            ):
                raise ValueError("campaign resumption archive record is invalid")
            archive_path = output / archive["path"]
            if (
                archive_path.is_symlink()
                or not archive_path.is_file()
                or digest(archive_path) != archive.get("sha256")
            ):
                raise ValueError("campaign resumption archive changed")
        if len(resumptions) >= 64:
            archive_bytes = (json.dumps(resumptions, indent=2) + "\n").encode()
            archive_sha256 = hashlib.sha256(archive_bytes).hexdigest()
            archive_name = f"resumption-history-{archive_sha256[:16]}.json"
            archive_path = output / archive_name
            if archive_path.exists():
                if archive_path.is_symlink() or digest(archive_path) != archive_sha256:
                    raise ValueError("campaign resumption archive path is already occupied")
            else:
                with archive_path.open("xb") as stream:
                    stream.write(archive_bytes)
                archive_path.chmod(0o444)
            archives.append(
                {
                    "path": archive_name,
                    "sha256": archive_sha256,
                    "entry_count": len(resumptions),
                }
            )
            resumptions = []
            report["resumptions"] = resumptions
        resumptions.append(
            {
                "prior_summary_sha256": prior_summary_sha256,
                "resumer_source_sha256": current_source_sha256,
                "resumed_at": datetime.now(UTC).isoformat(),
            }
        )
        # The interruption remains in interrupted_attempts and the resumption
        # chain. A recovered campaign must not carry a stale terminal failure
        # marker into its new running or completed state.
        report.pop("failure_type", None)

    def existing_attempts(scenario: str) -> set[int]:
        attempts: set[int] = set()
        for log in output.glob(f"{scenario}-*.log"):
            match = LOG_NAME.fullmatch(log.name)
            if (
                match is None
                or match.group(1) != scenario
                or log.is_symlink()
                or not log.is_file()
                or log.stat().st_size > 2 * 1024 * 1024
            ):
                raise ValueError("campaign attempt log is invalid")
            attempt = int(match.group(2))
            if attempt in attempts:
                raise ValueError("campaign repeats an attempt log")
            attempts.add(attempt)
        return attempts

    attempt_logs = {
        scenario: existing_attempts(scenario) for scenario in ("gradual_loss", "normal")
    }
    if resume_summary is not None:
        recorded: dict[str, set[int]] = {"gradual_loss": set(), "normal": set()}
        for collection, scenario in (
            (report["normal_runs"], "normal"),
            (report["impacted_runs"], "gradual_loss"),
            (report["nonimpact_fault_runs"], "gradual_loss"),
        ):
            for item in collection:
                offset = item["seed"] - base_seed - (scenario == "gradual_loss")
                if offset < 0 or offset % 2:
                    raise ValueError("campaign run seed is outside its attempt sequence")
                attempt = offset // 2
                if attempt not in attempt_logs[scenario] or attempt in recorded[scenario]:
                    raise ValueError("campaign run does not have one unique attempt log")
                recorded[scenario].add(attempt)
        for item in report["failed_attempts"]:
            if not isinstance(item, dict) or not LOG_NAME.fullmatch(str(item.get("log", ""))):
                raise ValueError("campaign failed-attempt record is invalid")
            match = LOG_NAME.fullmatch(item["log"])
            assert match is not None
            scenario, attempt = match.group(1), int(match.group(2))
            expected_seed = base_seed + attempt * 2 + (scenario == "gradual_loss")
            if (
                item.get("scenario") != scenario
                or item.get("seed") != expected_seed
                or attempt not in attempt_logs[scenario]
                or attempt in recorded[scenario]
            ):
                raise ValueError("campaign failed-attempt record differs from its log")
            actual_failure_class = failure_class(output / item["log"])
            if item.get("failure_class", actual_failure_class) != actual_failure_class:
                raise ValueError("campaign failed-attempt classification differs from its log")
            item["failure_class"] = actual_failure_class
            recorded[scenario].add(attempt)
        interrupted = report.setdefault("interrupted_attempts", [])
        if not isinstance(interrupted, list):
            raise ValueError("campaign interrupted-attempt history is invalid")
        for item in interrupted:
            if not isinstance(item, dict) or not LOG_NAME.fullmatch(str(item.get("log", ""))):
                raise ValueError("campaign interrupted-attempt record is invalid")
            match = LOG_NAME.fullmatch(item["log"])
            assert match is not None
            scenario, attempt = match.group(1), int(match.group(2))
            if attempt not in attempt_logs[scenario] or attempt in recorded[scenario]:
                raise ValueError("campaign interrupted-attempt record differs from its log")
            recorded[scenario].add(attempt)
        for scenario, attempts in attempt_logs.items():
            for attempt in sorted(attempts - recorded[scenario]):
                interrupted.append(
                    {
                        "scenario": scenario,
                        "seed": base_seed + attempt * 2 + (scenario == "gradual_loss"),
                        "log": f"{scenario}-{attempt:03d}.log",
                        "recorded_at": datetime.now(UTC).isoformat(),
                    }
                )

    def checkpoint(status: str) -> None:
        report["status"] = status
        report["updated_at"] = datetime.now(UTC).isoformat()
        temporary = output / "summary.tmp"
        temporary.write_text(json.dumps(report, indent=2) + "\n")
        temporary.replace(output / "summary.json")

    def run(scenario: str, attempt: int, traffic: str, profile: str = "a") -> dict | None:
        if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != report["pilot_sha256"]:
            raise ValueError("pilot release changed during the campaign; restart after review")
        for relative, expected in runtime_hashes.items():
            path = runtime / relative
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("campaign source changed during collection")
        seed = base_seed + attempt * 2 + (scenario == "gradual_loss")
        command = [
            "bash",
            str(script),
            "--lab-name",
            "prahari-owned",
            "--state-dir",
            str(state),
            "--scenario",
            scenario,
            "--profile",
            profile,
            "--experiment-seed",
            str(seed),
            "--traffic-profile",
            traffic,
            "--delay-scale-percent",
            str(delay_scale),
            "--confirm",
            "RUN-PREDICTIVE-V2",
        ]
        if scenario == "normal":
            command += ["--normal-samples", str(normal_samples + seed % 31)]
        log = output / f"{scenario}-{attempt:03d}.log"
        returncode = execute(command, log)
        candidates = [
            line.removeprefix("Predictive experiment sealed: ")
            for line in log.read_text().splitlines()
            if line.startswith("Predictive experiment sealed: ")
        ]
        if returncode or not candidates:
            report["failed_attempts"].append(
                {
                    "scenario": scenario,
                    "seed": seed,
                    "log": log.name,
                    "failure_class": failure_class(log),
                }
            )
            checkpoint("running")
            return None
        candidate = Path(candidates[-1])
        if candidate.is_symlink():
            raise ValueError("sealed evidence must not be a symlink")
        sealed = candidate.resolve(strict=True)
        if not sealed.is_relative_to(state / "predictive-experiments"):
            raise ValueError("controller returned an unexpected evidence path")
        metadata = json.loads(sealed.read_bytes())
        if (
            metadata["status"] != "completed"
            or metadata["controller_lab_name"] != "prahari-owned"
            or metadata["scenario"] != scenario
            or metadata["experiment_seed"] != seed
            or metadata["traffic_profile_id"] != traffic
            or metadata["fault_profile_id"] != (profile if scenario == "gradual_loss" else "none")
            or metadata["run_id"] != sealed.parent.name
        ):
            raise ValueError("sealed run differs from the requested owned experiment")
        return {
            "run_id": metadata["run_id"],
            "manifest_sha256": hashlib.sha256(sealed.read_bytes()).hexdigest(),
            "traffic_profile": traffic,
            "fault_profile": metadata["fault_profile_id"],
            "actual_impact_start_time": metadata["actual_impact_start_time"],
            "seed": seed,
        }

    checkpoint("running")
    traffic_names, profile_names = ("light", "standard", "heavy"), ("a", "b", "c")
    quotas = [runs_per_class // 3 + (index < runs_per_class % 3) for index in range(3)]

    def measured_attempt_count(scenario: str) -> int:
        unavailable = {
            int(LOG_NAME.fullmatch(item["log"]).group(2))
            for item in report["failed_attempts"]
            if item.get("scenario") == scenario
            and item.get("failure_class") == "infrastructure_unavailable"
            and LOG_NAME.fullmatch(str(item.get("log", ""))) is not None
        }
        return len(attempt_logs[scenario] - unavailable)

    try:
        attempt = max(attempt_logs["gradual_loss"], default=-1) + 1
        impacted_budget_used = measured_attempt_count("gradual_loss")
        while attempt < runs_per_class * 12 and impacted_budget_used < runs_per_class * 6:
            profile_counts = [
                sum(item["fault_profile"] == name for item in report["impacted_runs"])
                for name in profile_names
            ]
            traffic_counts = [
                sum(item["traffic_profile"] == name for item in report["impacted_runs"])
                for name in traffic_names
            ]
            if (
                len(report["impacted_runs"]) >= runs_per_class
                and all(count >= quota for count, quota in zip(profile_counts, quotas, strict=True))
                and all(count >= 3 for count in traffic_counts)
            ):
                break
            unfinished = [i for i in range(3) if profile_counts[i] < quotas[i]]
            profile = profile_names[
                unfinished[attempt % len(unfinished)] if unfinished else attempt % 3
            ]
            unfinished_traffic = [i for i in range(3) if traffic_counts[i] < 3]
            traffic = traffic_names[
                unfinished_traffic[attempt % len(unfinished_traffic)]
                if unfinished_traffic
                else (attempt // 3) % 3
            ]
            item = run("gradual_loss", attempt, traffic, profile)
            attempt_logs["gradual_loss"].add(attempt)
            if item is not None or failure_class(output / f"gradual_loss-{attempt:03d}.log") != (
                "infrastructure_unavailable"
            ):
                impacted_budget_used += 1
            if item is not None:
                key = (
                    "impacted_runs" if item["actual_impact_start_time"] else "nonimpact_fault_runs"
                )
                report[key].append(item)
                checkpoint("running")
                print(
                    f"Impacted runs sealed: {len(report['impacted_runs'])}/{runs_per_class}",
                    flush=True,
                )
            attempt += 1
        profile_counts = [
            sum(item["fault_profile"] == name for item in report["impacted_runs"])
            for name in profile_names
        ]
        traffic_counts = [
            sum(item["traffic_profile"] == name for item in report["impacted_runs"])
            for name in traffic_names
        ]
        if (
            len(report["impacted_runs"]) < runs_per_class
            or any(count < quota for count, quota in zip(profile_counts, quotas, strict=True))
            or any(count < 3 for count in traffic_counts)
        ):
            report["failure_type"] = "AttemptBudgetExhausted"
            checkpoint("attempts_exhausted")
            return 2
        attempt = max(attempt_logs["normal"], default=-1) + 1
        normal_budget_used = measured_attempt_count("normal")
        while attempt < runs_per_class * 12 and normal_budget_used < runs_per_class * 3:
            counts = [
                sum(item["traffic_profile"] == name for item in report["normal_runs"])
                for name in traffic_names
            ]
            unfinished = [i for i in range(3) if counts[i] < quotas[i]]
            if not unfinished:
                break
            traffic = traffic_names[unfinished[attempt % len(unfinished)]]
            item = run("normal", attempt, traffic)
            attempt_logs["normal"].add(attempt)
            if item is not None or failure_class(output / f"normal-{attempt:03d}.log") != (
                "infrastructure_unavailable"
            ):
                normal_budget_used += 1
            if item is not None:
                if item["actual_impact_start_time"] is not None:
                    raise ValueError("a supposed normal run actually contains application impact")
                report["normal_runs"].append(item)
                checkpoint("running")
                print(
                    f"Normal runs sealed: {len(report['normal_runs'])}/{runs_per_class}", flush=True
                )
            attempt += 1
        if len(report["normal_runs"]) < runs_per_class:
            report["failure_type"] = "AttemptBudgetExhausted"
            checkpoint("attempts_exhausted")
            return 2
        report.pop("failure_type", None)
        checkpoint("collection_complete")
        print(
            f"Measured cohort collected: {output / 'summary.json'}; model remains unapproved.",
            flush=True,
        )
        return 0
    except BaseException as error:
        report["failure_type"] = type(error).__name__
        checkpoint("incomplete")
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--runs-per-class", type=int, default=18)
    parser.add_argument("--base-seed", type=int, required=True)
    parser.add_argument("--delay-scale-percent", type=int, default=200)
    parser.add_argument("--normal-samples", type=int, default=600)
    parser.add_argument(
        "--resume-summary",
        type=Path,
        help=(
            "resume one root-protected interrupted campaign after revalidating its sealed evidence"
        ),
    )
    args = parser.parse_args()
    if os.name != "posix" or os.geteuid() != 0:
        parser.error("run on the owned Linux server as its lab administrator")
    if not 12 <= args.runs_per_class <= 60:
        parser.error("runs per class must be 12..60")
    if not 0 <= args.base_seed <= 2147483647 - args.runs_per_class * 12:
        parser.error("base seed leaves insufficient room for bounded retries")
    if not 100 <= args.delay_scale_percent <= 250:
        parser.error("owned experimental delay scale must be 100..250")
    if not 120 <= args.normal_samples <= 1170:
        parser.error("normal sample baseline must be 120..1170")
    import fcntl

    def cancelled(signum: int, frame: object) -> None:
        raise KeyboardInterrupt(f"campaign interrupted by signal {signum}")

    signal.signal(signal.SIGTERM, cancelled)
    with (args.state_dir / "model-campaign.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return campaign(
            args.state_dir,
            args.runs_per_class,
            args.base_seed,
            args.delay_scale_percent,
            args.resume_summary,
            args.normal_samples,
        )


if __name__ == "__main__":
    raise SystemExit(main())
