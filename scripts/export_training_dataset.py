"""Export labelled, real lab path observations into deterministic run splits."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from hashlib import sha256
from pathlib import Path
from urllib.request import urlopen


def assign_run_splits(run_ids: set[str]) -> dict[str, str]:
    """Assign whole runs deterministically while guaranteeing held-out runs."""
    ordered = sorted(run_ids, key=lambda run_id: sha256(run_id.encode()).hexdigest())
    count = len(ordered)
    if count < 3:
        raise ValueError("at least three experiment runs are required for leakage-safe splits")
    validation_count = max(1, round(count * 0.2))
    test_count = max(1, round(count * 0.1))
    train_count = count - validation_count - test_count
    if train_count < 1:
        raise ValueError("at least one training run is required")
    assignments: dict[str, str] = {}
    for index, run_id in enumerate(ordered):
        if index < train_count:
            assignments[run_id] = "train"
        elif index < train_count + validation_count:
            assignments[run_id] = "validation"
        else:
            assignments[run_id] = "test"
    return assignments


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="http://127.0.0.1:8080")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.api_url.startswith(("http://127.0.0.1", "http://localhost", "https://")):
        parser.error("remote API URLs must use HTTPS")
    with urlopen(f"{args.api_url.rstrip('/')}/api/metrics?limit=5000", timeout=10) as response:  # noqa: S310
        metrics = json.load(response)

    rows: dict[tuple[str, str, str], dict[str, object]] = defaultdict(dict)
    for metric in metrics:
        labels = metric.get("labels", {})
        run_id = labels.get("experiment_run_id")
        phase = labels.get("experiment_phase")
        name = metric.get("metric")
        if not run_id or phase not in {"baseline", "impaired", "recovered"}:
            continue
        if name not in {"packet_loss_ratio", "jitter_ms"}:
            continue
        key = (run_id, phase, metric["observed_at"])
        rows[key].update(
            {
                "schema_version": "training-row-v1",
                "run_id": run_id,
                "phase": phase,
                "observed_at": metric["observed_at"],
                "label": "degraded" if phase == "impaired" else "normal",
                name: metric["value"],
                "rtt_avg_ms": float(labels.get("rtt_avg_ms", 0)),
            }
        )
    complete = [row for row in rows.values() if "packet_loss_ratio" in row and "jitter_ms" in row]
    assignments = assign_run_splits({str(row["run_id"]) for row in complete})
    for row in complete:
        row["split"] = assignments[str(row["run_id"])]
    complete.sort(key=lambda row: (str(row["run_id"]), str(row["observed_at"])))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as target:
        for row in complete:
            target.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
    temporary.replace(args.output)
    print(f"exported {len(complete)} labelled rows to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
