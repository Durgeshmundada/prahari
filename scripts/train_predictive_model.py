"""Train, calibrate, and evaluate the three-horizon Track A predictive model."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import tempfile
from collections import defaultdict
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from statistics import median
from typing import Any

import lightgbm as lgb
import numpy as np

from prahari.analytics.predictive import (
    FEATURE_NAMES,
    FEATURE_VERSION,
    ApplicationImpactPolicy,
    calibrate_probability,
    monotonic_horizon_probabilities,
)

HORIZONS = (30, 60, 120)
TECHNICAL_EVALUATION_POLICY_VERSION = "technical-evaluation-v2"
FALSE_ALERT_RATE_CONFIDENCE = 0.95
MAX_FALSE_ALERTS_PER_NEGATIVE_LINK_HOUR = 1.0
SPLITS = ("train", "validation", "test", "demo_holdout")
SPLIT_STRATEGY = (
    "whole-run deterministic outcome/profile stratification; 6+ train runs per class, "
    "2 impacted runs per held-out split, and at least 3 eligible negative link-hours "
    "per held-out split"
)
MIN_VALIDATION_INCIDENT_PRECISION = 0.70
MIN_VALIDATION_INCIDENT_RECALL = 0.60
MIN_VALIDATION_USEFUL_LEAD_SECONDS = 30.0
MINIMUM_CLASS_RUN_COUNTS = {
    "train": 6,
    "validation": 2,
    "test": 2,
    "demo_holdout": 2,
}
LABEL_FIELDS = tuple(f"impact_within_{horizon}_seconds" for horizon in HORIZONS)
ROW_FIELDS = {
    "schema_version",
    "run_id",
    "split",
    "scenario",
    "experiment_seed",
    "traffic_profile_id",
    "fault_profile_id",
    "feature_version",
    "window_start",
    "window_end",
    "sample_count",
    "features",
    "labels",
    "actual_impact_start_time",
    "run_observation_start",
    "run_observation_end",
    "time_to_impact_seconds",
    "prediction_exposure_seconds",
    "root_entity_id",
    "root_cause_verification",
}
DATASET_MANIFEST_FIELDS = {
    "schema_version",
    "dataset_version",
    "dataset_sha256",
    "feature_version",
    "features",
    "horizons_seconds",
    "impact_policy",
    "split_strategy",
    "split_assignments",
    "run_counts",
    "class_run_counts",
    "row_counts",
    "label_counts",
    "source_runs",
    "limitations",
}


class ValidationGateRejected(ValueError):
    """The validation-only policy cannot produce an eligible primary threshold."""

    def __init__(self, diagnostics: dict[str, Any]) -> None:
        super().__init__(
            "validation cannot satisfy the predeclared 120-second incident gate; "
            "the untouched test and demo holdout must not be used to choose a threshold"
        )
        self.diagnostics = diagnostics


def binary_metrics(
    labels: list[int],
    probabilities: list[float],
    threshold: float,
) -> dict[str, float | int]:
    if not labels or len(labels) != len(probabilities):
        raise ValueError("labels and probabilities must be non-empty and equal length")
    predictions = [int(probability >= threshold) for probability in probabilities]
    tp = sum(prediction == label == 1 for prediction, label in zip(predictions, labels))
    fp = sum(prediction == 1 and label == 0 for prediction, label in zip(predictions, labels))
    fn = sum(prediction == 0 and label == 1 for prediction, label in zip(predictions, labels))
    tn = sum(prediction == label == 0 for prediction, label in zip(predictions, labels))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {
        "count": len(labels),
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "true_negative": tn,
        "accuracy": (tp + tn) / len(labels),
        "precision": precision,
        "recall": recall,
        "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        "false_positive_rate": fp / (fp + tn) if fp + tn else 0.0,
        "brier_score": sum(
            (probability - label) ** 2
            for probability, label in zip(probabilities, labels, strict=True)
        )
        / len(labels),
        "expected_calibration_error": expected_calibration_error(labels, probabilities),
    }


def expected_calibration_error(
    labels: list[int], probabilities: list[float], bins: int = 10
) -> float:
    total = len(labels)
    error = 0.0
    for index in range(bins):
        lower = index / bins
        upper = (index + 1) / bins
        selected = [
            position
            for position, probability in enumerate(probabilities)
            if lower <= probability < upper or (index == bins - 1 and probability == 1)
        ]
        if not selected:
            continue
        confidence = sum(probabilities[position] for position in selected) / len(selected)
        observed = sum(labels[position] for position in selected) / len(selected)
        error += len(selected) / total * abs(confidence - observed)
    return error


def fit_platt_calibrator(
    probabilities: list[float],
    labels: list[int],
    *,
    iterations: int = 100,
) -> tuple[float, float]:
    """Fit a regularized logistic calibration map with deterministic Newton steps."""

    if len(probabilities) != len(labels) or len(probabilities) < 2:
        raise ValueError("calibration needs equal, non-trivial probability and label arrays")
    if set(labels) != {0, 1}:
        raise ValueError("calibration requires both outcome classes")
    x_values = np.asarray([_logit(value) for value in probabilities], dtype=float)
    y_values = np.asarray(labels, dtype=float)
    slope = 1.0
    intercept = 0.0
    regularization = 1e-4
    for _ in range(iterations):
        logits = np.clip(slope * x_values + intercept, -35, 35)
        fitted = 1 / (1 + np.exp(-logits))
        residual = fitted - y_values
        weights = np.maximum(fitted * (1 - fitted), 1e-9)
        gradient_slope = float(np.sum(residual * x_values) + regularization * slope)
        gradient_intercept = float(np.sum(residual))
        hessian_slope = float(np.sum(weights * x_values * x_values) + regularization)
        hessian_cross = float(np.sum(weights * x_values))
        hessian_intercept = float(np.sum(weights) + regularization)
        determinant = hessian_slope * hessian_intercept - hessian_cross**2
        if abs(determinant) < 1e-12:
            break
        delta_slope = (
            hessian_intercept * gradient_slope - hessian_cross * gradient_intercept
        ) / determinant
        delta_intercept = (
            hessian_slope * gradient_intercept - hessian_cross * gradient_slope
        ) / determinant
        slope -= delta_slope
        intercept -= delta_intercept
        if max(abs(delta_slope), abs(delta_intercept)) < 1e-8:
            break
    if not math.isfinite(slope) or not math.isfinite(intercept):
        raise ValueError("probability calibration did not converge to finite coefficients")
    if slope < -1e-9:
        raise ValueError("probability calibration inverted the model score ordering")
    # An uninformative validation score has a zero optimum.  Persist the
    # smallest positive order-preserving map accepted by the runtime contract.
    slope = max(slope, 1e-6)
    return slope, intercept


def calibrate(probability: float, slope: float, intercept: float) -> float:
    return calibrate_probability(probability, slope, intercept)


def choose_threshold(labels: list[int], probabilities: list[float]) -> float:
    candidates = sorted(
        {
            *(index / 100 for index in range(5, 96)),
            *(round(probability, 8) for probability in probabilities),
        }
    )
    return max(
        candidates,
        key=lambda threshold: (
            float(binary_metrics(labels, probabilities, threshold)["f1"]),
            float(binary_metrics(labels, probabilities, threshold)["precision"]),
            -float(binary_metrics(labels, probabilities, threshold)["false_positive_rate"]),
            threshold,
        ),
    )


def enforce_monotonic_horizons(probabilities: list[float]) -> list[float]:
    return monotonic_horizon_probabilities(probabilities)


def _logit(probability: float) -> float:
    clipped = min(1 - 1e-6, max(1e-6, probability))
    return math.log(clipped / (1 - clipped))


def load_dataset(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    manifest_path = path.with_suffix(".manifest.json")
    if (
        path.is_symlink()
        or manifest_path.is_symlink()
        or not path.is_file()
        or not manifest_path.is_file()
    ):
        raise ValueError("dataset and manifest must be existing non-symlink files")
    raw_dataset = path.read_bytes()
    raw_manifest = manifest_path.read_bytes()
    if not raw_dataset.endswith(b"\n") or not raw_manifest.endswith(b"\n"):
        raise ValueError("dataset and manifest must end with a newline")
    rows = [
        _load_json_no_duplicate_keys(line)
        for line in raw_dataset.decode("utf-8").splitlines()
        if line
    ]
    manifest = _load_json_no_duplicate_keys(raw_manifest.decode("utf-8"))
    if not isinstance(manifest, dict) or set(manifest) != DATASET_MANIFEST_FIELDS:
        raise ValueError("predictive dataset manifest fields are incomplete or unexpected")
    if manifest.get("schema_version") != "predictive-dataset-manifest-v2":
        raise ValueError("unsupported predictive dataset manifest")
    if sha256(raw_dataset).hexdigest() != manifest.get("dataset_sha256"):
        raise ValueError("predictive dataset checksum does not match its manifest")
    if manifest.get("feature_version") != FEATURE_VERSION:
        raise ValueError("predictive dataset feature version does not match runtime")
    if tuple(manifest.get("features", [])) != FEATURE_NAMES:
        raise ValueError("predictive dataset feature contract does not match runtime")
    if tuple(manifest.get("horizons_seconds", [])) != HORIZONS:
        raise ValueError("predictive dataset horizons do not match the trainer")
    if manifest.get("split_strategy") != SPLIT_STRATEGY:
        raise ValueError("predictive dataset split strategy is not the frozen v2 strategy")
    dataset_version = manifest.get("dataset_version")
    if not isinstance(dataset_version, str) or not dataset_version:
        raise ValueError("predictive dataset version is invalid")
    limitations = manifest.get("limitations")
    if not isinstance(limitations, list) or not limitations or any(
        not isinstance(item, str) or not item for item in limitations
    ):
        raise ValueError("predictive dataset limitations are invalid")
    if not rows:
        raise ValueError("predictive dataset is empty")
    _validate_dataset_rows(rows, manifest)
    return rows, manifest, sha256(raw_manifest).hexdigest()


def _load_json_no_duplicate_keys(value: str) -> Any:
    def build_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"JSON contains duplicate key: {key}")
            result[key] = item
        return result

    def reject_constant(constant: str) -> object:
        raise ValueError(f"JSON contains non-finite number: {constant}")

    return json.loads(
        value,
        object_pairs_hook=build_object,
        parse_constant=reject_constant,
    )


def _timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO-8601 timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a UTC offset")
    return parsed.astimezone(UTC)


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_dataset_rows(rows: list[dict[str, Any]], manifest: dict[str, Any]) -> None:
    policy = ApplicationImpactPolicy.model_validate(manifest.get("impact_policy"))
    row_keys: list[tuple[str, datetime]] = []
    run_metadata: dict[str, tuple[object, ...]] = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != ROW_FIELDS:
            raise ValueError("predictive training row fields are incomplete or unexpected")
        if row.get("schema_version") != "predictive-training-row-v2":
            raise ValueError("unsupported predictive training row")
        run_id = row.get("run_id")
        if (
            not isinstance(run_id, str)
            or not 3 <= len(run_id) <= 100
            or not run_id[0].isalnum()
            or any(not (character.isalnum() or character in "._-") for character in run_id)
        ):
            raise ValueError("training row run ID is invalid")
        split = row.get("split")
        if split not in SPLITS:
            raise ValueError("training row has an unknown split")
        scenario = row.get("scenario")
        if scenario not in {"normal", "gradual_loss"}:
            raise ValueError("training row has an unknown scenario")
        seed = row.get("experiment_seed")
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2_147_483_647:
            raise ValueError("training row experiment seed is invalid")
        for field in ("traffic_profile_id", "fault_profile_id"):
            value = row.get(field)
            if not isinstance(value, str) or not value:
                raise ValueError(f"training row {field} is invalid")
        if row.get("feature_version") != FEATURE_VERSION:
            raise ValueError("training row feature version does not match runtime")
        sample_count = row.get("sample_count")
        if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count < 10:
            raise ValueError("training row sample count is invalid")
        features = row.get("features")
        if not isinstance(features, dict) or set(features) != set(FEATURE_NAMES):
            raise ValueError("training row feature contract does not match runtime")
        for name in FEATURE_NAMES:
            _finite(features[name], f"feature {name}")
        labels = row.get("labels")
        if not isinstance(labels, dict) or set(labels) != set(LABEL_FIELDS):
            raise ValueError("training row label contract is invalid")
        if any(value is not None and type(value) is not bool for value in labels.values()):
            raise ValueError("training labels must be booleans or null")
        if labels[LABEL_FIELDS[0]] is True and labels[LABEL_FIELDS[1]] is not True:
            raise ValueError("training labels violate nested horizon semantics")
        if labels[LABEL_FIELDS[1]] is True and labels[LABEL_FIELDS[2]] is not True:
            raise ValueError("training labels violate nested horizon semantics")
        if labels[LABEL_FIELDS[2]] is False and any(
            labels[field] is not False for field in LABEL_FIELDS[:2]
        ):
            raise ValueError("training labels violate nested horizon semantics")

        window_start = _timestamp(row.get("window_start"), "window_start")
        window_end = _timestamp(row.get("window_end"), "window_end")
        run_start = _timestamp(row.get("run_observation_start"), "run_observation_start")
        run_end = _timestamp(row.get("run_observation_end"), "run_observation_end")
        span = (window_end - window_start).total_seconds()
        if not 50 <= span <= 60 or not run_start <= window_start < window_end < run_end:
            raise ValueError("training row time window is outside its observed run")
        exposure = _finite(
            row.get("prediction_exposure_seconds"), "prediction_exposure_seconds"
        )
        if not 0 < exposure <= policy.max_inter_sample_gap_seconds:
            raise ValueError("prediction exposure is outside the evidence cadence ceiling")
        impact_raw = row.get("actual_impact_start_time")
        impact_at = (
            _timestamp(impact_raw, "actual_impact_start_time")
            if impact_raw is not None
            else None
        )
        time_to_impact = row.get("time_to_impact_seconds")
        if impact_at is not None:
            lead = _finite(time_to_impact, "time_to_impact_seconds")
            if lead <= 0 or abs(lead - (impact_at - window_end).total_seconds()) > 1e-6:
                raise ValueError("time-to-impact does not match the evidence timestamps")
            for horizon, field in zip(HORIZONS, LABEL_FIELDS, strict=True):
                if labels[field] is not (lead <= horizon):
                    raise ValueError("impact label does not reproduce from time-to-impact")
        else:
            if time_to_impact is not None:
                raise ValueError("a no-impact row cannot declare time-to-impact")
            remaining = (run_end - window_end).total_seconds()
            for horizon, field in zip(HORIZONS, LABEL_FIELDS, strict=True):
                expected = False if remaining >= horizon else None
                if labels[field] is not expected:
                    raise ValueError("negative/censored label does not match observed follow-up")

        root_entity = row.get("root_entity_id")
        root_verification = row.get("root_cause_verification")
        fault_profile = row.get("fault_profile_id")
        if scenario == "normal":
            if (
                impact_at is not None
                or root_entity is not None
                or root_verification != "not_applicable"
                or fault_profile != "none"
            ):
                raise ValueError("normal training row contains contradictory fault evidence")
        elif (
            root_entity != "interface:branch-edge:eth1"
            or root_verification != "controlled_fault_manifest"
            or fault_profile not in {"a", "b", "c"}
        ):
            raise ValueError("gradual-loss row lacks controlled root-cause evidence")

        metadata = (
            split,
            scenario,
            seed,
            row.get("traffic_profile_id"),
            fault_profile,
            impact_raw,
            row.get("run_observation_start"),
            row.get("run_observation_end"),
            root_entity,
            root_verification,
        )
        if run_id in run_metadata and run_metadata[run_id] != metadata:
            raise ValueError("one run contains inconsistent metadata or crosses a split")
        run_metadata[run_id] = metadata
        row_keys.append((run_id, window_end))

    if len(row_keys) != len(set(row_keys)):
        raise ValueError("predictive dataset contains duplicate run/window rows")
    if row_keys != sorted(row_keys):
        raise ValueError("predictive dataset rows are not in canonical order")
    _validate_dataset_manifest_counts(rows, run_metadata, manifest)


def _validate_dataset_manifest_counts(
    rows: list[dict[str, Any]],
    run_metadata: dict[str, tuple[object, ...]],
    manifest: dict[str, Any],
) -> None:
    assignments = manifest.get("split_assignments")
    expected_assignments = {
        run_id: str(metadata[0]) for run_id, metadata in sorted(run_metadata.items())
    }
    if assignments != expected_assignments:
        raise ValueError("dataset split assignments do not match whole-run row membership")
    expected_run_counts = {
        split: sum(metadata[0] == split for metadata in run_metadata.values())
        for split in SPLITS
    }
    if manifest.get("run_counts") != expected_run_counts:
        raise ValueError("dataset run counts do not reproduce")
    expected_class_counts = {
        split: {
            "impact": sum(
                metadata[0] == split and metadata[5] is not None
                for metadata in run_metadata.values()
            ),
            "no_impact": sum(
                metadata[0] == split and metadata[5] is None
                for metadata in run_metadata.values()
            ),
        }
        for split in SPLITS
    }
    if manifest.get("class_run_counts") != expected_class_counts:
        raise ValueError("dataset outcome-class run counts do not reproduce")
    for split, minimum in MINIMUM_CLASS_RUN_COUNTS.items():
        if any(expected_class_counts[split][label] < minimum for label in ("impact", "no_impact")):
            raise ValueError(f"dataset {split} split lacks independent outcome-class runs")
    expected_row_counts = {
        split: sum(row["split"] == split for row in rows) for split in SPLITS
    }
    if manifest.get("row_counts") != expected_row_counts:
        raise ValueError("dataset row counts do not reproduce")
    expected_label_counts = {
        str(horizon): {
            "positive": sum(row["labels"][field] is True for row in rows),
            "negative": sum(row["labels"][field] is False for row in rows),
            "censored": sum(row["labels"][field] is None for row in rows),
        }
        for horizon, field in zip(HORIZONS, LABEL_FIELDS, strict=True)
    }
    if manifest.get("label_counts") != expected_label_counts:
        raise ValueError("dataset label counts do not reproduce")
    _validate_source_runs(run_metadata, manifest.get("source_runs"))


def _validate_source_runs(
    run_metadata: dict[str, tuple[object, ...]],
    raw_sources: object,
) -> None:
    source_fields = {
        "run_id",
        "scenario",
        "experiment_seed",
        "traffic_profile_id",
        "fault_profile_id",
        "observed_impact",
        "observations_sha256",
        "manifest_sha256",
        "fault_evidence",
        "observation_start",
        "observation_end",
    }
    if not isinstance(raw_sources, list):
        raise ValueError("dataset source runs must be a list")
    sources: dict[str, dict[str, Any]] = {}
    for source in raw_sources:
        if not isinstance(source, dict) or set(source) != source_fields:
            raise ValueError("dataset source-run metadata is incomplete or unexpected")
        run_id = source.get("run_id")
        if not isinstance(run_id, str) or run_id in sources:
            raise ValueError("dataset source-run IDs must be unique strings")
        if not _is_sha256(source.get("observations_sha256")) or not _is_sha256(
            source.get("manifest_sha256")
        ):
            raise ValueError("dataset source-run evidence checksum is invalid")
        fault_evidence = source.get("fault_evidence")
        if not isinstance(fault_evidence, list) or any(
            not isinstance(item, dict)
            or set(item) != {"path", "sha256"}
            or not _is_sha256(item.get("sha256"))
            for item in fault_evidence
        ):
            raise ValueError("dataset source-run fault evidence is invalid")
        sources[run_id] = source
    if set(sources) != set(run_metadata):
        raise ValueError("dataset source-run list does not match its rows")
    for run_id, metadata in run_metadata.items():
        source = sources[run_id]
        expected = (
            metadata[1],
            metadata[2],
            metadata[3],
            metadata[4],
            metadata[5] is not None,
            metadata[6],
            metadata[7],
        )
        actual = (
            source.get("scenario"),
            source.get("experiment_seed"),
            source.get("traffic_profile_id"),
            source.get("fault_profile_id"),
            source.get("observed_impact"),
            source.get("observation_start"),
            source.get("observation_end"),
        )
        if actual != expected:
            raise ValueError("dataset source-run metadata does not match its rows")
        if (metadata[1] == "normal") is not (len(source["fault_evidence"]) == 0):
            raise ValueError("dataset source-run fault evidence conflicts with its scenario")


def _eligible(
    rows: list[dict[str, Any]], split: str, horizon: int
) -> list[dict[str, Any]]:
    label = f"impact_within_{horizon}_seconds"
    return [
        row
        for row in rows
        if row["split"] == split and row["labels"][label] is not None
    ]


def _matrix(rows: list[dict[str, Any]], horizon: int) -> tuple[np.ndarray, list[int]]:
    label = f"impact_within_{horizon}_seconds"
    return (
        np.asarray(
            [[float(row["features"][name]) for name in FEATURE_NAMES] for row in rows],
            dtype=float,
        ),
        [int(row["labels"][label]) for row in rows],
    )


def _train_booster(
    train_rows: list[dict[str, Any]],
    validation_rows: list[dict[str, Any]],
    horizon: int,
) -> lgb.Booster:
    train_x, train_y = _matrix(train_rows, horizon)
    validation_x, validation_y = _matrix(validation_rows, horizon)
    if set(train_y) != {0, 1} or set(validation_y) != {0, 1}:
        raise ValueError(f"training and validation need both classes for horizon {horizon}")
    positives = sum(train_y)
    negatives = len(train_y) - positives
    return lgb.train(
        {
            "objective": "binary",
            "metric": "binary_logloss",
            "learning_rate": 0.03,
            "num_leaves": 7,
            "min_data_in_leaf": 5,
            "feature_fraction": 0.85,
            "bagging_fraction": 0.9,
            "bagging_freq": 1,
            "lambda_l1": 0.05,
            "lambda_l2": 0.1,
            "scale_pos_weight": negatives / positives,
            "verbosity": -1,
            "seed": 20260903 + horizon,
            "feature_fraction_seed": 20260903 + horizon,
            "bagging_seed": 20260903 + horizon,
            "deterministic": True,
            "force_col_wise": True,
            "num_threads": 1,
        },
        lgb.Dataset(train_x, label=train_y, feature_name=list(FEATURE_NAMES)),
        num_boost_round=300,
        valid_sets=[lgb.Dataset(validation_x, label=validation_y)],
        callbacks=[lgb.early_stopping(25, verbose=False)],
    )


def _all_probabilities(
    rows: list[dict[str, Any]],
    boosters: dict[int, lgb.Booster],
    calibrators: dict[int, tuple[float, float]],
) -> list[dict[int, float]]:
    matrix = np.asarray(
        [[float(row["features"][name]) for name in FEATURE_NAMES] for row in rows],
        dtype=float,
    )
    raw_by_horizon = {
        horizon: boosters[horizon].predict(matrix)
        for horizon in HORIZONS
    }
    results: list[dict[int, float]] = []
    for index in range(len(rows)):
        calibrated = [
            calibrate(
                float(raw_by_horizon[horizon][index]),
                *calibrators[horizon],
            )
            for horizon in HORIZONS
        ]
        monotonic = enforce_monotonic_horizons(calibrated)
        results.append(dict(zip(HORIZONS, monotonic, strict=True)))
    return results


def choose_projected_thresholds(
    validation_rows: list[dict[str, Any]],
    probabilities: list[dict[int, float]],
) -> dict[int, float]:
    """Tune thresholds only on the same monotonic scores used by runtime.

    The primary 120-second policy is selected against the incident-level release
    measures. This prevents row-level F1 from choosing a threshold that is noisy
    across normal runs. The shorter horizons retain row-level selection because
    they are used for ETA banding rather than the technical promotion gate.
    """

    if len(validation_rows) != len(probabilities):
        raise ValueError("validation rows and projected probabilities must align")
    thresholds: dict[int, float] = {}
    for horizon in HORIZONS:
        label_name = f"impact_within_{horizon}_seconds"
        eligible = [
            (int(row["labels"][label_name]), scores[horizon])
            for row, scores in zip(validation_rows, probabilities, strict=True)
            if row["labels"][label_name] is not None
        ]
        labels = [label for label, _ in eligible]
        scores = [score for _, score in eligible]
        if set(labels) != {0, 1}:
            raise ValueError(f"validation needs both classes for horizon {horizon}")
        thresholds[horizon] = (
            choose_incident_threshold(validation_rows, probabilities, horizon)
            if horizon == 120
            else choose_threshold(labels, scores)
        )
    return thresholds


def choose_incident_threshold(
    validation_rows: list[dict[str, Any]],
    probabilities: list[dict[int, float]],
    horizon: int,
) -> float:
    """Choose a validation-only threshold that satisfies the declared incident gate."""

    candidates = sorted(
        {
            *(index / 100 for index in range(5, 96)),
            *(round(scores[horizon], 8) for scores in probabilities),
        }
    )
    assessments: list[tuple[float, dict[str, Any], dict[str, bool]]] = []
    for threshold in candidates:
        metrics = incident_metrics(
            validation_rows,
            probabilities,
            horizon,
            threshold,
        )
        assessments.append((threshold, metrics, validation_incident_checks(metrics)))
    feasible = [item for item in assessments if all(item[2].values())]
    if not feasible:
        threshold, metrics, checks = max(
            assessments,
            key=lambda item: (
                sum(item[2].values()),
                float(item[1]["incident_recall"]),
                float(item[1]["incident_precision"]),
                -float(item[1]["false_alerts_per_link_hour"]),
                item[0],
            ),
        )
        raise ValidationGateRejected(
            {
                "horizon_seconds": horizon,
                "best_validation_threshold": threshold,
                "best_validation_metrics": metrics,
                "checks": checks,
                "candidate_threshold_count": len(candidates),
            }
        )
    threshold, _metrics, _checks = max(
        feasible,
        key=lambda item: (
            float(item[1]["incident_recall"]),
            float(item[1]["incident_precision"]),
            float(item[1]["useful_lead_time_seconds"]["median"]),
            -float(item[1]["false_alerts_per_link_hour"]),
            item[0],
        ),
    )
    return threshold


def validation_incident_checks(metrics: dict[str, Any]) -> dict[str, bool]:
    lead = metrics["useful_lead_time_seconds"]["median"]
    return {
        "validation_incident_precision_at_least_0_70": (
            metrics["incident_precision"] >= MIN_VALIDATION_INCIDENT_PRECISION
        ),
        "validation_incident_recall_at_least_0_60": (
            metrics["incident_recall"] >= MIN_VALIDATION_INCIDENT_RECALL
        ),
        "validation_false_alerts_per_negative_link_hour_at_most_1_0": (
            metrics["false_alerts_per_link_hour"]
            <= MAX_FALSE_ALERTS_PER_NEGATIVE_LINK_HOUR
        ),
        "validation_false_alert_rate_upper_bound_at_most_1_0": (
            isinstance(metrics["false_alert_rate_upper_bound_per_link_hour"], (int, float))
            and metrics["false_alert_rate_upper_bound_per_link_hour"]
            <= MAX_FALSE_ALERTS_PER_NEGATIVE_LINK_HOUR
        ),
        "validation_median_useful_lead_at_least_30_seconds": (
            lead is not None and lead >= MIN_VALIDATION_USEFUL_LEAD_SECONDS
        ),
    }


def _alert_episodes(alerts: list[bool]) -> int:
    return sum(
        alert and (index == 0 or not alerts[index - 1])
        for index, alert in enumerate(alerts)
    )


def poisson_rate_upper_bound(
    event_count: int,
    exposure_hours: float,
    *,
    confidence: float | None = None,
) -> float:
    """Return an exact one-sided Poisson event-rate confidence bound."""

    if confidence is None:
        confidence = FALSE_ALERT_RATE_CONFIDENCE
    if isinstance(event_count, bool) or not isinstance(event_count, int) or event_count < 0:
        raise ValueError("event count must be a non-negative integer")
    if not math.isfinite(exposure_hours) or exposure_hours <= 0:
        raise ValueError("event-rate confidence requires positive finite exposure")
    if not math.isfinite(confidence) or not 0 < confidence < 1:
        raise ValueError("event-rate confidence must be between zero and one")
    alpha_log = math.log1p(-confidence)
    if event_count == 0:
        return -alpha_log / exposure_hours

    def log_cdf(mean: float) -> float:
        terms = [
            -mean + index * math.log(mean) - math.lgamma(index + 1)
            for index in range(event_count + 1)
        ]
        largest = max(terms)
        return largest + math.log(sum(math.exp(term - largest) for term in terms))

    lower = 0.0
    upper = float(max(1, event_count + 1))
    while log_cdf(upper) > alpha_log:
        upper *= 2
        if upper > 1e12:
            raise ValueError("event-rate confidence bound did not converge")
    for _ in range(80):
        midpoint = (lower + upper) / 2
        if log_cdf(midpoint) > alpha_log:
            lower = midpoint
        else:
            upper = midpoint
    return upper / exposure_hours


def incident_metrics(
    rows: list[dict[str, Any]],
    row_probabilities: list[dict[int, float]],
    horizon: int,
    threshold: float,
) -> dict[str, Any]:
    grouped: dict[str, list[tuple[dict[str, Any], float]]] = defaultdict(list)
    for row, probabilities in zip(rows, row_probabilities, strict=True):
        if row["labels"][f"impact_within_{horizon}_seconds"] is not None:
            grouped[str(row["run_id"])].append((row, probabilities[horizon]))
    tp = fp = fn = tn = false_alert_episodes = 0
    lead_times: list[float] = []
    total_observed_hours = 0.0
    negative_exposure_seconds = 0.0
    localized = localized_total = 0
    for run_rows in grouped.values():
        run_rows.sort(key=lambda item: str(item[0]["window_end"]))
        alerts = [probability >= threshold for _, probability in run_rows]
        has_impact = run_rows[0][0]["actual_impact_start_time"] is not None
        start = datetime.fromisoformat(str(run_rows[0][0]["run_observation_start"]))
        end = datetime.fromisoformat(str(run_rows[0][0]["run_observation_end"]))
        total_observed_hours += (end - start).total_seconds() / 3600
        negative_mask = [
            row["labels"][f"impact_within_{horizon}_seconds"] is False
            for row, _ in run_rows
        ]
        negative_exposure_seconds += sum(
            float(row["prediction_exposure_seconds"])
            for (row, _), is_negative in zip(run_rows, negative_mask, strict=True)
            if is_negative
        )
        false_alert_episodes += _alert_episodes(
            [
                alert and is_negative
                for alert, is_negative in zip(alerts, negative_mask, strict=True)
            ]
        )
        if has_impact:
            useful = [
                (row, alert)
                for (row, _), alert in zip(run_rows, alerts, strict=True)
                if row["time_to_impact_seconds"] is not None
                and 0 < float(row["time_to_impact_seconds"]) <= horizon
            ]
            alerted = [row for row, alert in useful if alert]
            if alerted:
                tp += 1
                lead_times.append(max(float(row["time_to_impact_seconds"]) for row in alerted))
                localized_total += 1
                if all(row["root_entity_id"] == "interface:branch-edge:eth1" for row in alerted):
                    localized += 1
            else:
                fn += 1
        elif any(alerts):
            fp += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    negative_exposure_hours = negative_exposure_seconds / 3600
    return {
        "run_count": len(grouped),
        "true_positive_incidents": tp,
        "false_positive_runs": fp,
        "false_negative_incidents": fn,
        "true_negative_runs": tn,
        "incident_precision": precision,
        "incident_recall": recall,
        "false_alert_episodes": false_alert_episodes,
        "total_observed_link_hours": total_observed_hours,
        "eligible_negative_link_hours": negative_exposure_hours,
        "false_alerts_per_link_hour": (
            false_alert_episodes / negative_exposure_hours
            if negative_exposure_hours
            else 0.0
        ),
        "false_alert_rate_confidence": FALSE_ALERT_RATE_CONFIDENCE,
        "false_alert_rate_upper_bound_per_link_hour": (
            poisson_rate_upper_bound(false_alert_episodes, negative_exposure_hours)
            if negative_exposure_hours
            else None
        ),
        "useful_lead_time_seconds": {
            "count": len(lead_times),
            "minimum": min(lead_times) if lead_times else None,
            "median": median(lead_times) if lead_times else None,
            "maximum": max(lead_times) if lead_times else None,
        },
        "topology_manifest_localization": {
            "method": "fixed-scope topology mapping, not an ML localization head",
            "evaluated_incidents": localized_total,
            "top1_accuracy": localized / localized_total if localized_total else None,
        },
    }


def eta_coverage(
    rows: list[dict[str, Any]],
    probabilities: list[dict[int, float]],
    thresholds: dict[int, float],
) -> dict[str, float | int | None]:
    grouped: dict[str, list[tuple[dict[str, Any], dict[int, float]]]] = defaultdict(list)
    for row, scores in zip(rows, probabilities, strict=True):
        if row["actual_impact_start_time"] is not None:
            grouped[str(row["run_id"])].append((row, scores))
    covered = predicted = 0
    for run_rows in grouped.values():
        run_rows.sort(key=lambda item: str(item[0]["window_end"]))
        candidates: list[tuple[float, tuple[int, int]]] = []
        for row, scores in run_rows:
            lead = row["time_to_impact_seconds"]
            if lead is None or not 0 < float(lead) <= 120:
                continue
            interval: tuple[int, int] | None = None
            if scores[30] >= thresholds[30]:
                interval = (0, 30)
            elif scores[60] >= thresholds[60]:
                interval = (30, 60)
            elif scores[120] >= thresholds[120]:
                interval = (60, 120)
            if interval is not None:
                candidates.append((float(lead), interval))
        if not candidates:
            continue
        lead, interval = max(candidates, key=lambda candidate: candidate[0])
        predicted += 1
        covered += int(interval[0] < lead <= interval[1])
    return {
        "eligible_incidents": len(grouped),
        "eta_predictions": predicted,
        "covered": covered,
        "coverage": covered / predicted if predicted else None,
    }


def evaluate_split(
    split_rows: list[dict[str, Any]],
    probabilities: list[dict[int, float]],
    thresholds: dict[int, float],
) -> dict[str, Any]:
    evaluation: dict[str, Any] = {"row_metrics": {}, "incident_metrics": {}}
    for horizon in HORIZONS:
        selected = [
            (row, scores[horizon])
            for row, scores in zip(split_rows, probabilities, strict=True)
            if row["labels"][f"impact_within_{horizon}_seconds"] is not None
        ]
        labels = [int(row["labels"][f"impact_within_{horizon}_seconds"]) for row, _ in selected]
        scores = [score for _, score in selected]
        evaluation["row_metrics"][str(horizon)] = binary_metrics(
            labels, scores, thresholds[horizon]
        )
        evaluation["incident_metrics"][str(horizon)] = incident_metrics(
            split_rows,
            probabilities,
            horizon,
            thresholds[horizon],
        )
    evaluation["eta"] = eta_coverage(split_rows, probabilities, thresholds)
    return evaluation


def _model_card(manifest: dict[str, Any]) -> str:
    test = manifest["evaluation"]["test"]["incident_metrics"]["120"]
    demo = manifest["evaluation"]["demo_holdout"]["incident_metrics"]["120"]
    return f"""# {manifest['model_version']} model card

Status: **{manifest['approval_status']}**. Predictive claim approved:
**{str(manifest['predictive_claim_approved']).lower()}**.

This is a calibrated three-horizon LightGBM candidate for observed application
impact within 30, 60, and 120 seconds. Labels come from sustained same-path chat
and file-probe measurements, never from the injected phase name.

## Evaluation

- Untouched test incident precision/recall at 120 s: {test['incident_precision']:.3f} /
  {test['incident_recall']:.3f}
- Untouched demo-holdout precision/recall at 120 s: {demo['incident_precision']:.3f} /
  {demo['incident_recall']:.3f}
- Test false alerts per eligible negative-exposure link-hour:
  {test['false_alerts_per_link_hour']:.3f}
- Test one-sided {test['false_alert_rate_confidence']:.0%} false-alert rate upper bound:
  {test['false_alert_rate_upper_bound_per_link_hour']:.3f} per eligible negative link-hour
- 120-second threshold selection: validation-only incident precision, recall,
  observed/upper-bound false-alert exposure and useful-lead constraints
- Technical evaluation gate passed: {str(manifest['technical_evaluation_gate']['passed']).lower()}
- Demo holdout excluded from the technical gate: true

## Intended use and boundary

Use only for the versioned PRAHARI Containerlab path and impact policy. The
model cannot authorize a network action. The topology manifest, rather than an
ML localization head, maps an alert to the controlled fault interface.

## Limitations

- Lab packet-loss/delay ramps do not establish production-network generalization.
- A separate accountable approval record is required before runtime risk or a
  production deployment may consume this candidate.
- The immutable demo holdout is reported separately, is excluded from tuning and
  the technical gate, and must not be used to revise this candidate.
"""


def write_validation_rejection(
    dataset: Path,
    output_directory: Path,
    model_version: str,
    error: ValidationGateRejected,
) -> dict[str, Any]:
    """Persist a validation-only rejection without evaluating held-out metrics."""

    if output_directory.exists():
        raise FileExistsError(f"refusing to overwrite model directory: {output_directory}")
    _rows, dataset_manifest, dataset_manifest_sha256 = load_dataset(dataset)
    temporary_parent = output_directory.parent
    temporary_parent.mkdir(parents=True, exist_ok=True)
    temporary_directory = Path(
        tempfile.mkdtemp(prefix=f".{output_directory.name}-", dir=temporary_parent)
    )
    try:
        rejection: dict[str, Any] = {
            "schema_version": "predictive-validation-rejection-v1",
            "status": "validation_gate_rejected",
            "model_version": model_version,
            "approval_status": "rejected",
            "predictive_claim_approved": False,
            "deployment_scope": "none",
            "dataset_version": dataset_manifest["dataset_version"],
            "dataset_sha256": dataset_manifest["dataset_sha256"],
            "dataset_manifest_sha256": dataset_manifest_sha256,
            "technical_evaluation_gate": {
                "passed": False,
                "checks": error.diagnostics["checks"],
                "selection_data": ["validation"],
                "evaluation_data": [],
                "not_evaluated": ["test", "demo_holdout"],
                "does_not_grant_approval": True,
            },
            "validation_diagnostics": error.diagnostics,
            "model_approved": False,
            "runtime_activated": False,
            "independent_acceptance": False,
        }
        (temporary_directory / "validation-rejection.json").write_text(
            json.dumps(rejection, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (temporary_directory / "MODEL_CARD.md").write_text(
            f"""# {model_version} validation rejection

Status: **validation_gate_rejected**. Predictive claim approved: **false**.

The validation-only 120-second incident policy could not simultaneously satisfy
its predeclared precision, recall, false-alert and useful-lead constraints. Test
and demo-holdout metrics were not evaluated. This result cannot be activated or
used to authorize a network action.
""",
            encoding="utf-8",
        )
        temporary_directory.replace(output_directory)
    except Exception:
        shutil.rmtree(temporary_directory, ignore_errors=True)
        raise
    return rejection


def train(dataset: Path, output_directory: Path, model_version: str) -> dict[str, Any]:
    rows, dataset_manifest, dataset_manifest_sha256 = load_dataset(dataset)
    if output_directory.exists():
        raise FileExistsError(f"refusing to overwrite model directory: {output_directory}")
    boosters: dict[int, lgb.Booster] = {}
    calibrators: dict[int, tuple[float, float]] = {}
    for horizon in HORIZONS:
        train_rows = _eligible(rows, "train", horizon)
        validation_rows = _eligible(rows, "validation", horizon)
        booster = _train_booster(train_rows, validation_rows, horizon)
        validation_x, validation_y = _matrix(validation_rows, horizon)
        raw_validation = [float(value) for value in booster.predict(validation_x)]
        calibration = fit_platt_calibrator(raw_validation, validation_y)
        calibrated_validation = [calibrate(value, *calibration) for value in raw_validation]
        boosters[horizon] = booster
        calibrators[horizon] = calibration
        # Exercise the persisted calibration contract now; threshold selection
        # happens only after all three outputs receive the runtime projection.
        if any(not 0 < value < 1 for value in calibrated_validation):
            raise ValueError("validation calibration produced an invalid probability")

    validation_rows = [row for row in rows if row["split"] == "validation"]
    validation_probabilities = _all_probabilities(
        validation_rows,
        boosters,
        calibrators,
    )
    thresholds = choose_projected_thresholds(validation_rows, validation_probabilities)

    evaluation: dict[str, Any] = {}
    for split in SPLITS:
        split_rows = [row for row in rows if row["split"] == split]
        probabilities = _all_probabilities(split_rows, boosters, calibrators)
        evaluation[split] = evaluate_split(split_rows, probabilities, thresholds)

    test_120 = evaluation["test"]["incident_metrics"]["120"]
    test_rows_120 = evaluation["test"]["row_metrics"]["120"]
    validation_120 = evaluation["validation"]["incident_metrics"]["120"]
    lead_median = test_120["useful_lead_time_seconds"]["median"]
    gate_checks = {
        **validation_incident_checks(validation_120),
        "test_incident_precision_at_least_0_70": test_120["incident_precision"] >= 0.70,
        "test_incident_recall_at_least_0_60": test_120["incident_recall"] >= 0.60,
        "test_brier_at_most_0_25": test_rows_120["brier_score"] <= 0.25,
        "test_false_alerts_per_negative_link_hour_at_most_1_0": (
            test_120["false_alerts_per_link_hour"]
            <= MAX_FALSE_ALERTS_PER_NEGATIVE_LINK_HOUR
        ),
        "test_false_alert_rate_upper_bound_at_most_1_0": (
            isinstance(test_120["false_alert_rate_upper_bound_per_link_hour"], (int, float))
            and test_120["false_alert_rate_upper_bound_per_link_hour"]
            <= MAX_FALSE_ALERTS_PER_NEGATIVE_LINK_HOUR
        ),
        "test_median_useful_lead_at_least_30_seconds": (
            lead_median is not None and lead_median >= 30
        ),
    }
    temporary_parent = output_directory.parent
    temporary_parent.mkdir(parents=True, exist_ok=True)
    temporary_directory = Path(
        tempfile.mkdtemp(prefix=f".{output_directory.name}-", dir=temporary_parent)
    )
    try:
        model_artifacts: dict[str, dict[str, Any]] = {}
        for horizon, booster in boosters.items():
            name = f"risk-within-{horizon}s.txt"
            model_path = temporary_directory / name
            booster.save_model(model_path)
            importance = sorted(
                zip(FEATURE_NAMES, booster.feature_importance(importance_type="gain"), strict=True),
                key=lambda item: float(item[1]),
                reverse=True,
            )
            model_artifacts[str(horizon)] = {
                "file": name,
                "sha256": sha256(model_path.read_bytes()).hexdigest(),
                "best_iteration": booster.best_iteration,
                "calibration": {
                    "method": "platt_logistic",
                    "slope": calibrators[horizon][0],
                    "intercept": calibrators[horizon][1],
                },
                "decision_threshold": thresholds[horizon],
                "top_features_by_gain": [
                    {"feature": feature, "gain": float(gain)}
                    for feature, gain in importance[:10]
                ],
            }
        manifest: dict[str, Any] = {
            "schema_version": "predictive-model-manifest-v2",
            "model_version": model_version,
            "model_kind": "calibrated LightGBM binary classifier per future-impact horizon",
            "approval_status": "evaluation_pending",
            "predictive_claim_approved": False,
            "deployment_scope": "none",
            "feature_version": FEATURE_VERSION,
            "features": list(FEATURE_NAMES),
            "horizons_seconds": list(HORIZONS),
            "sampling_seconds": 5,
            "window_seconds": 60,
            "minimum_samples": 10,
            "dataset_version": dataset_manifest["dataset_version"],
            "dataset_sha256": dataset_manifest["dataset_sha256"],
            "dataset_manifest_sha256": dataset_manifest_sha256,
            "impact_policy": dataset_manifest["impact_policy"],
            "run_counts": dataset_manifest["run_counts"],
            "row_counts": dataset_manifest["row_counts"],
            "models": model_artifacts,
            "evaluation": evaluation,
            "technical_evaluation_gate": {
                "policy_version": TECHNICAL_EVALUATION_POLICY_VERSION,
                "passed": all(gate_checks.values()),
                "checks": gate_checks,
                "false_alert_rate_policy": {
                    "maximum_per_negative_link_hour": (
                        MAX_FALSE_ALERTS_PER_NEGATIVE_LINK_HOUR
                    ),
                    "one_sided_confidence": FALSE_ALERT_RATE_CONFIDENCE,
                    "method": "exact Poisson upper confidence bound",
                },
                "selection_data": ["validation"],
                "evaluation_data": ["test"],
                "excluded_from_gate": ["demo_holdout"],
                "threshold_selection": {
                    "30": "maximum validation row F1 after runtime monotonic projection",
                    "60": "maximum validation row F1 after runtime monotonic projection",
                    "120": (
                        "validation-only incident precision/recall/false-alert/lead constraints"
                    ),
                },
                "does_not_grant_approval": True,
            },
            "limitations": dataset_manifest["limitations"],
        }
        manifest_path = temporary_directory / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (temporary_directory / "MODEL_CARD.md").write_text(
            _model_card(manifest),
            encoding="utf-8",
        )
        temporary_directory.replace(output_directory)
    except Exception:
        shutil.rmtree(temporary_directory, ignore_errors=True)
        raise
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--model-version", default="risk-lgbm-horizons-v4")
    args = parser.parse_args()
    try:
        manifest = train(args.dataset, args.output_dir, args.model_version)
    except ValidationGateRejected as error:
        manifest = write_validation_rejection(
            args.dataset,
            args.output_dir,
            args.model_version,
            error,
        )
    print(json.dumps(manifest["technical_evaluation_gate"], indent=2, sort_keys=True))
    print(f"model evaluation result written to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
