"""Build an unapproved forecasting candidate only from a completed measured cohort."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def read_json(path: Path) -> dict:
    if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("metadata must be a bounded regular file")
    result = json.loads(path.read_bytes())
    if not isinstance(result, dict):
        raise ValueError("metadata must be an object")
    return result


def validate_owned_tree(path: Path, parent: Path) -> None:
    if path.is_symlink() or not path.resolve(strict=True).is_relative_to(parent):
        raise ValueError("model build path escaped its campaign directory")
    file_count = 0
    total_bytes = 0
    for child in path.rglob("*"):
        if child.is_symlink():
            raise ValueError("model build tree contains a symlink")
        if child.is_file():
            file_count += 1
            total_bytes += child.stat().st_size
            if file_count > 10_000 or total_bytes > 2 * 1024 * 1024 * 1024:
                raise ValueError("model build tree exceeds its recovery bound")


def validate_attempt_archives(campaign: Path, attempts: object) -> list[dict[str, str]]:
    if not isinstance(attempts, list) or len(attempts) > 32:
        raise ValueError("model build attempt history is invalid")
    validated: list[dict[str, str]] = []
    for item in attempts:
        if not isinstance(item, dict) or not re.fullmatch(
            r"model-build-attempt-[a-f0-9]{16}", str(item.get("path", ""))
        ):
            raise ValueError("model build attempt reference is invalid")
        archive = campaign / item["path"]
        validate_owned_tree(archive, campaign)
        inventory_path = archive / "attempt-inventory.json"
        if digest(inventory_path) != item.get("inventory_sha256"):
            raise ValueError("model build attempt inventory changed")
        inventory = read_json(inventory_path)
        artifacts = inventory.get("artifact_sha256")
        if not isinstance(artifacts, dict):
            raise ValueError("model build attempt inventory is invalid")
        if artifacts.get("verification.json") != item.get("verification_sha256"):
            raise ValueError("model build attempt verification reference changed")
        actual = {
            path.relative_to(archive).as_posix(): digest(path)
            for path in archive.rglob("*")
            if path.is_file() and path.name != "attempt-inventory.json"
        }
        if actual != artifacts:
            raise ValueError("model build attempt artifacts changed")
        validated.append(dict(item))
    return validated


def run_worker(
    image: str, runtime: Path, inputs: Path, output: Path, arguments: list[str], stage: str
) -> None:
    name = "prahari-owned-model-build-" + uuid4().hex
    command = [
        "docker",
        "run",
        "--name",
        name,
        "--label",
        "prahari.owned.model-build=true",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        "10001:10001",
        "--memory",
        "1g",
        "--cpus",
        "1",
        "--pids-limit",
        "128",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
        "--env",
        "PYTHONPATH=/workspace/src",
        "--env",
        "OMP_NUM_THREADS=1",
        "--env",
        "OPENBLAS_NUM_THREADS=1",
        "--workdir",
        "/workspace",
        "--mount",
        f"type=bind,src={runtime},dst=/workspace,readonly",
        "--mount",
        f"type=bind,src={inputs},dst=/inputs,readonly",
        "--mount",
        f"type=bind,src={output},dst=/model-build",
        image,
        "/opt/venv/bin/python",
        *arguments,
    ]
    try:
        with (output / f"{stage}.log").open("x") as stream:
            subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=1800,
            )
    finally:
        # The unique name is created by this invocation; verify ownership before
        # removing it, including after Docker client timeout or interruption.
        ownership = subprocess.run(
            [
                "docker",
                "inspect",
                "-f",
                '{{ index .Config.Labels "prahari.owned.model-build" }}',
                name,
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if ownership.returncode == 0 and ownership.stdout.strip() == "true":
            subprocess.run(
                ["docker", "rm", "-f", name],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=30,
                check=True,
            )


def build(state: Path, summary_path: Path) -> Path:
    state = state.resolve(strict=True)
    if summary_path.is_symlink():
        raise ValueError("campaign summary must not be a symlink")
    summary_path = summary_path.resolve(strict=True)
    if not summary_path.is_relative_to(state / "model-campaigns"):
        raise ValueError("campaign does not belong to the owned pilot")
    cohort = read_json(summary_path)
    if cohort["status"] != "collection_complete" or cohort["model_approved"] is not False:
        raise ValueError("only a completed unapproved measured cohort can be built")
    pilot_path = state / "pilot-manifest.json"
    evaluation_exporter = Path(__file__).with_name("export_predictive_dataset.py")
    evaluation_trainer = Path(__file__).with_name("train_predictive_model.py")
    if (
        evaluation_exporter.is_symlink()
        or not evaluation_exporter.is_file()
        or evaluation_trainer.is_symlink()
        or not evaluation_trainer.is_file()
    ):
        raise ValueError("evaluation exporter or trainer is missing from the builder release")
    evaluation_exporter_sha256 = digest(evaluation_exporter)
    evaluation_trainer_sha256 = digest(evaluation_trainer)
    pilot = read_json(pilot_path)
    if (
        pilot["lab"] != "prahari-owned"
        or pilot["action_adapter_enabled"] is not False
        or digest(pilot_path) != cohort["pilot_sha256"]
    ):
        raise ValueError("pilot release differs from the measured cohort")
    runtime = summary_path.parent / "runtime"
    for relative, expected in cohort["runtime_files"].items():
        source = runtime / relative
        if (
            source.is_symlink()
            or not source.resolve().is_relative_to(runtime)
            or digest(source) != expected
        ):
            raise ValueError("campaign runtime differs from its frozen source")
    image = pilot["images"]["api"]
    if not re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}", image):
        raise ValueError("model worker image must have an immutable identity")
    selected = cohort["normal_runs"] + cohort["impacted_runs"]
    if len(cohort["normal_runs"]) < 12 or len(cohort["impacted_runs"]) < 12:
        raise ValueError("cohort lacks the twelve runs per measured outcome class")
    identifiers = [item["run_id"] for item in selected]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("cohort repeats a measured run")
    output = summary_path.parent / "model-build"
    prior_attempts: list[dict[str, str]] = []
    if output.exists():
        if output.is_symlink() or not output.is_dir():
            raise ValueError("existing model-build path is invalid")
        validate_owned_tree(output, summary_path.parent)
        verification_path = output / "verification.json"
        verification = read_json(verification_path)
        if verification.get("cohort_sha256") != digest(summary_path) or verification.get(
            "pilot_sha256"
        ) != digest(pilot_path):
            raise ValueError("existing model build belongs to another cohort")
        prior_attempts = validate_attempt_archives(
            summary_path.parent, verification.get("prior_attempts", [])
        )
        if verification.get("status") == "evaluation_complete":
            if (
                verification.get("builder_sha256") != digest(Path(__file__))
                or verification.get("evaluation_exporter_sha256")
                != evaluation_exporter_sha256
                or verification.get("evaluation_trainer_sha256")
                != evaluation_trainer_sha256
            ):
                raise ValueError("existing model build used another evaluation release")
            if (
                verification.get("model_approved") is not False
                or verification.get("runtime_activated") is not False
            ):
                raise ValueError("existing model build contains an invalid approval state")
            expected_artifacts = verification.get("artifact_sha256")
            if not isinstance(expected_artifacts, dict) or not expected_artifacts:
                raise ValueError("existing model build lacks its artifact inventory")
            actual_artifacts: dict[str, str] = {}
            for path in output.rglob("*"):
                if path.is_file() and path.name not in {"verification.json", "verification.tmp"}:
                    actual_artifacts[path.relative_to(output).as_posix()] = digest(path)
            if actual_artifacts != expected_artifacts:
                raise ValueError("existing model build artifacts changed")
            return output
        if (
            verification.get("status") not in {"preparing", "exporting", "training", "incomplete"}
            or verification.get("pilot_sha256") != digest(pilot_path)
            or verification.get("model_approved") is not False
            or verification.get("runtime_activated") is not False
        ):
            raise ValueError("existing incomplete model build has an invalid state")
        verification_sha256 = digest(verification_path)
        archive = summary_path.parent / f"model-build-attempt-{verification_sha256[:16]}"
        if archive.exists() or archive.is_symlink():
            raise ValueError("model build attempt archive path is already occupied")
        output.replace(archive)
        artifact_sha256 = {
            path.relative_to(archive).as_posix(): digest(path)
            for path in archive.rglob("*")
            if path.is_file()
        }
        attempt_inventory = {
            "schema_version": "owned-model-build-attempt-v1",
            "archived_at": datetime.now(UTC).isoformat(),
            "cohort_sha256": digest(summary_path),
            "pilot_sha256": digest(pilot_path),
            "status": verification["status"],
            "failure_type": str(verification.get("failure_type", "interrupted")),
            "artifact_sha256": artifact_sha256,
        }
        inventory_path = archive / "attempt-inventory.json"
        inventory_path.write_text(json.dumps(attempt_inventory, indent=2) + "\n")
        inventory_sha256 = digest(inventory_path)
        if os.name == "posix":
            for child in archive.rglob("*"):
                child.chmod(0o555 if child.is_dir() else 0o444)
            archive.chmod(0o555)
        prior_attempts.append(
            {
                "path": archive.name,
                "verification_sha256": verification_sha256,
                "inventory_sha256": inventory_sha256,
                "status": verification["status"],
                "failure_type": str(verification.get("failure_type", "interrupted")),
            }
        )
    inputs = summary_path.parent / "model-inputs"
    if inputs.exists() or inputs.is_symlink():
        if inputs.is_symlink() or not inputs.is_dir():
            raise ValueError("existing model input path is invalid")
        validate_owned_tree(inputs, summary_path.parent)
        if os.name == "nt":
            # Production cleanup runs as root on Linux. Windows fixtures need
            # their staged read-only evidence made removable explicitly.
            for child in inputs.rglob("*"):
                child.chmod(0o700 if child.is_dir() else 0o600)
        shutil.rmtree(inputs)
    output.mkdir(mode=0o700)
    inputs.mkdir(mode=0o700)
    if os.name == "posix":
        os.chown(output, 10001, 10001)
        os.chown(inputs, 10001, 10001)
    report = {
        "schema_version": "owned-model-build-v1",
        "status": "preparing",
        "cohort_sha256": digest(summary_path),
        "pilot_sha256": digest(pilot_path),
        "worker_image": image,
        "builder_sha256": digest(Path(__file__)),
        "evaluation_exporter_sha256": evaluation_exporter_sha256,
        "evaluation_trainer_sha256": evaluation_trainer_sha256,
        "selected_run_ids": identifiers,
        "model_approved": False,
        "runtime_activated": False,
        "independent_acceptance": False,
        "prior_attempts": prior_attempts,
    }

    def checkpoint(status: str) -> None:
        report["status"] = status
        report["updated_at"] = datetime.now(UTC).isoformat()
        temporary = output / "verification.tmp"
        temporary.write_text(json.dumps(report, indent=2) + "\n")
        temporary.replace(output / "verification.json")

    try:
        checkpoint("preparing")
        for item in selected:
            run_id = item["run_id"]
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,199}", run_id):
                raise ValueError("invalid measured run identifier")
            original = state / "predictive-experiments" / run_id
            if original.is_symlink() or not original.resolve().is_relative_to(
                state / "predictive-experiments"
            ):
                raise ValueError("measured run escaped owned evidence directory")
            metadata_path = original / "manifest.json"
            metadata = read_json(metadata_path)
            if (
                digest(metadata_path) != item["manifest_sha256"]
                or metadata["run_id"] != run_id
                or metadata["controller_lab_name"] != "prahari-owned"
                or metadata["status"] != "completed"
                or metadata["actual_impact_start_time"] != item["actual_impact_start_time"]
            ):
                raise ValueError("measured manifest changed or differs from its cohort")
            files = {
                "manifest.json": item["manifest_sha256"],
                "observations.jsonl": metadata["observations_sha256"],
            }
            for fault in metadata["fault_manifests"]:
                if fault["path"] != "fault-schedule.jsonl":
                    raise ValueError("unexpected fault artifact path")
                files[fault["path"]] = fault["sha256"]
            destination = inputs / run_id
            destination.mkdir(mode=0o755)
            for filename, expected in files.items():
                source = original / filename
                if (
                    source.is_symlink()
                    or source.stat().st_size > 32 * 1024 * 1024
                    or digest(source) != expected
                ):
                    raise ValueError("measured artifact changed or exceeded its bound")
                target = destination / filename
                target.write_bytes(source.read_bytes())
                target.chmod(0o444)
                if digest(target) != expected:
                    raise ValueError("artifact changed during staging")
        checkpoint("exporting")
        staged_exporter = output / "evaluation-exporter.py"
        staged_exporter.write_bytes(evaluation_exporter.read_bytes())
        staged_exporter.chmod(0o444)
        if digest(staged_exporter) != evaluation_exporter_sha256:
            raise ValueError("evaluation exporter changed during staging")
        staged_trainer = output / "evaluation-trainer.py"
        staged_trainer.write_bytes(evaluation_trainer.read_bytes())
        staged_trainer.chmod(0o444)
        if digest(staged_trainer) != evaluation_trainer_sha256:
            raise ValueError("evaluation trainer changed during staging")
        run_worker(
            image,
            runtime,
            inputs,
            output,
            [
                "/model-build/evaluation-exporter.py",
                "--runs-dir",
                "/inputs",
                "--output",
                "/model-build/dataset.jsonl",
            ],
            "export",
        )
        checkpoint("training")
        run_worker(
            image,
            runtime,
            inputs,
            output,
            [
                "/model-build/evaluation-trainer.py",
                "--dataset",
                "/model-build/dataset.jsonl",
                "--output-dir",
                "/model-build/candidate",
                "--model-version",
                "owned-risk-lgbm-horizons-v4",
            ],
            "train",
        )
        candidate_path = output / "candidate/manifest.json"
        rejection_path = output / "candidate/validation-rejection.json"
        if candidate_path.is_file() and not rejection_path.exists():
            candidate = read_json(candidate_path)
            if (
                candidate["predictive_claim_approved"] is not False
                or candidate["approval_status"] != "evaluation_pending"
                or candidate["deployment_scope"] != "none"
            ):
                raise ValueError("training attempted to grant approval")
            report["validation_rejected"] = False
        elif rejection_path.is_file() and not candidate_path.exists():
            candidate = read_json(rejection_path)
            if (
                candidate.get("schema_version") != "predictive-validation-rejection-v1"
                or candidate.get("status") != "validation_gate_rejected"
                or candidate.get("predictive_claim_approved") is not False
                or candidate.get("approval_status") != "rejected"
                or candidate.get("deployment_scope") != "none"
                or candidate.get("model_approved") is not False
                or candidate.get("runtime_activated") is not False
                or candidate.get("technical_evaluation_gate", {}).get("passed") is not False
                or candidate["technical_evaluation_gate"].get("evaluation_data") != []
                or candidate["technical_evaluation_gate"].get("not_evaluated")
                != ["test", "demo_holdout"]
            ):
                raise ValueError("validation rejection artifact is invalid")
            report["validation_rejected"] = True
        else:
            raise ValueError("trainer produced an ambiguous evaluation result")
        if (
            digest(pilot_path) != report["pilot_sha256"]
            or digest(summary_path) != report["cohort_sha256"]
        ):
            raise ValueError("pilot or cohort changed during model evaluation")
        report["technical_evaluation_gate"] = candidate["technical_evaluation_gate"]
        report["artifact_sha256"] = {
            path.relative_to(output).as_posix(): digest(path)
            for path in output.rglob("*")
            if path.is_file() and path.name not in {"verification.json", "verification.tmp"}
        }
        checkpoint("evaluation_complete")
        return output
    except BaseException as error:
        report["failure_type"] = type(error).__name__
        checkpoint("incomplete")
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--campaign-summary", type=Path, required=True)
    parser.add_argument("--wait-seconds", type=int, default=0)
    args = parser.parse_args()
    if os.name != "posix" or os.geteuid() != 0:
        parser.error("run on the owned Linux server as its lab administrator")
    if not 0 <= args.wait_seconds <= 172800:
        parser.error("wait deadline must be 0..172800 seconds")
    import fcntl
    import signal

    def cancelled(signum: int, frame: object) -> None:
        raise KeyboardInterrupt(f"model build interrupted by signal {signum}")

    signal.signal(signal.SIGTERM, cancelled)
    with (args.state_dir / "model-build.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        deadline = time.monotonic() + args.wait_seconds
        while read_json(args.campaign_summary)["status"] in {"running", "incomplete"}:
            if time.monotonic() >= deadline:
                raise TimeoutError("measured collection has not completed within the wait deadline")
            time.sleep(15)
        result = build(args.state_dir, args.campaign_summary)
        print(f"Model evaluation artifacts: {result}; candidate remains unapproved.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
