"""Train and evaluate the versioned Track A degradation classifier."""

from __future__ import annotations

import argparse
import json
from hashlib import sha256
from pathlib import Path

import lightgbm as lgb
import numpy as np

FEATURES = ("packet_loss_ratio", "jitter_ms", "rtt_avg_ms")


def load_rows(path: Path) -> list[dict[str, object]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    if not rows:
        raise ValueError("training dataset is empty")
    return rows


def matrix(rows: list[dict[str, object]]) -> tuple[list[list[float]], list[int]]:
    return (
        [[float(row[name]) for name in FEATURES] for row in rows],
        [1 if row["label"] == "degraded" else 0 for row in rows],
    )


def metrics(labels: list[int], probabilities: list[float], threshold: float) -> dict[str, float]:
    predictions = [int(value >= threshold) for value in probabilities]
    pairs = list(zip(predictions, labels, strict=True))
    tp = sum(prediction == label == 1 for prediction, label in pairs)
    fp = sum(prediction == 1 and label == 0 for prediction, label in pairs)
    fn = sum(prediction == 0 and label == 1 for prediction, label in pairs)
    tn = sum(prediction == label == 0 for prediction, label in pairs)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {
        "accuracy": (tp + tn) / len(labels),
        "precision": precision,
        "recall": recall,
        "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        "false_positive_rate": fp / (fp + tn) if fp + tn else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    rows = load_rows(args.dataset)
    partitions = {
        name: [row for row in rows if row["split"] == name]
        for name in ("train", "validation", "test")
    }
    if any(not partition for partition in partitions.values()):
        raise ValueError("training, validation, and test partitions are all required")
    train_x, train_y = matrix(partitions["train"])
    validation_x, validation_y = matrix(partitions["validation"])
    test_x, test_y = matrix(partitions["test"])
    model = lgb.train(
        {
            "objective": "binary",
            "metric": "binary_logloss",
            "learning_rate": 0.05,
            "num_leaves": 7,
            "min_data_in_leaf": 3,
            "feature_fraction": 1.0,
            "bagging_fraction": 1.0,
            "verbosity": -1,
            "seed": 20260901,
            "num_threads": 1,
        },
        lgb.Dataset(np.asarray(train_x), label=train_y, feature_name=list(FEATURES)),
        num_boost_round=100,
        valid_sets=[lgb.Dataset(np.asarray(validation_x), label=validation_y)],
        callbacks=[lgb.early_stopping(10, verbose=False)],
    )
    validation_probabilities = list(model.predict(np.asarray(validation_x)))
    candidates = [index / 100 for index in range(10, 91)]
    threshold = max(
        candidates,
        key=lambda value: metrics(validation_y, validation_probabilities, value)["f1"],
    )
    test_probabilities = list(model.predict(np.asarray(test_x)))
    validation_metrics = metrics(validation_y, validation_probabilities, threshold)
    test_metrics = metrics(test_y, test_probabilities, threshold)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    model_path = args.output_dir / "degradation-lgbm-v1.txt"
    model.save_model(model_path)
    model_hash = sha256(model_path.read_bytes()).hexdigest()
    dataset_hash = sha256(args.dataset.read_bytes()).hexdigest()
    manifest = {
        "schema_version": "model-manifest-v1",
        "model_version": "degradation-lgbm-v1",
        "model_kind": "current-state binary classifier",
        "approval_status": "experimental",
        "predictive_claim_approved": False,
        "features": list(FEATURES),
        "decision_threshold": threshold,
        "dataset_sha256": dataset_hash,
        "model_sha256": model_hash,
        "run_counts": {
            name: len({str(row["run_id"]) for row in partition})
            for name, partition in partitions.items()
        },
        "row_counts": {name: len(partition) for name, partition in partitions.items()},
        "validation_metrics": validation_metrics,
        "test_metrics": test_metrics,
        "limitations": [
            "trained on six controlled Containerlab runs",
            "classifies observed degradation; it does not forecast arbitrary future faults",
            "not approved for production network action",
        ],
    }
    (args.output_dir / "degradation-lgbm-v1.manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
