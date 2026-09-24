"""Deterministic rolling-window features shared by replay and future live ingestion."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from math import sqrt
from statistics import median

from prahari.config import MVPConfig
from prahari.contracts import DataQuality, FeatureWindow, MetricRecord, MetricWindowFeature


class FeatureExtractor:
    feature_version = "window-features-v0"

    def __init__(self, config: MVPConfig) -> None:
        self._config = config

    def extract(
        self,
        records: list[MetricRecord],
        entity_id: str,
        *,
        reference_time: datetime | None = None,
        expected_topology_version: str | None = None,
        expected_config_version: str | None = None,
    ) -> FeatureWindow:
        """Build one window.

        Replay callers omit ``reference_time`` because fixture timestamps are
        historical by design. Live callers must pass their collection time so a
        stale newest sample suppresses prediction.
        """
        entity_records = [record for record in records if record.entity_id == entity_id]
        configured_records = [
            record
            for record in entity_records
            if record.metric in self._config.trend_policy.metrics
        ]
        if not configured_records:
            raise ValueError(f"no telemetry records found for {entity_id!r}")

        newest = max(record.observed_at for record in configured_records)
        window_start = newest - timedelta(seconds=self._config.window_seconds)
        window_records = [
            record for record in configured_records if record.observed_at >= window_start
        ]

        grouped: dict[str, list[MetricRecord]] = defaultdict(list)
        for record in window_records:
            grouped[record.metric].append(record)

        for metric_records in grouped.values():
            metric_records.sort(key=lambda record: record.observed_at)

        reasons: list[str] = []
        expected_metrics = set(self._config.trend_policy.metrics)
        sample_counts = {
            metric: len(grouped.get(metric, []))
            for metric in sorted(expected_metrics)
        }
        for metric in sorted(expected_metrics):
            metric_records = grouped.get(metric, [])
            if len(metric_records) < self._config.minimum_samples_per_metric:
                reasons.append(
                    f"{metric} has {len(metric_records)} samples; "
                    f"requires {self._config.minimum_samples_per_metric}"
                )
                continue
            if any(not record.quality.source_healthy for record in metric_records):
                reasons.append(f"{metric} includes an unhealthy source")
            if any(record.quality.counter_reset for record in metric_records):
                reasons.append(f"{metric} includes a counter-reset sample")
            if any(
                record.quality.late_by_ms > self._config.max_late_by_ms
                for record in metric_records
            ):
                reasons.append(
                    f"{metric} exceeds the allowed {self._config.max_late_by_ms} ms collection lag"
                )
            if expected_topology_version and any(
                record.topology_version != expected_topology_version for record in metric_records
            ):
                reasons.append(f"{metric} topology version does not match the active topology")
            if expected_config_version and any(
                record.config_version != expected_config_version for record in metric_records
            ):
                reasons.append(f"{metric} configuration version does not match the active policy")
            elif any(record.config_version is None for record in metric_records):
                reasons.append(f"{metric} is missing configuration provenance")
            expected_unit = self._config.trend_policy.metrics[metric].unit
            if any(record.unit != expected_unit for record in metric_records):
                reasons.append(f"{metric} unit does not match expected {expected_unit}")
            policy = self._config.trend_policy.metrics[metric]
            if any(
                record.value < policy.minimum_value
                or (
                    policy.maximum_value is not None
                    and record.value > policy.maximum_value
                )
                for record in metric_records
            ):
                reasons.append(f"{metric} includes a value outside its physical policy bounds")
            if any(
                later.observed_at <= earlier.observed_at
                for earlier, later in zip(metric_records, metric_records[1:], strict=False)
            ):
                reasons.append(f"{metric} has duplicate or unordered timestamps")
            max_gap_seconds = self._config.max_inter_sample_gap_seconds
            if any(
                (later.observed_at - earlier.observed_at).total_seconds() > max_gap_seconds
                for earlier, later in zip(metric_records, metric_records[1:], strict=False)
            ):
                reasons.append(f"{metric} contains a telemetry gap above {max_gap_seconds} seconds")

        if reference_time is not None:
            if reference_time.tzinfo is None or reference_time.utcoffset() is None:
                raise ValueError("reference_time must include a UTC offset")
            stale_after_seconds = self._config.sampling_seconds * 2
            future_limit = reference_time + timedelta(
                milliseconds=self._config.max_future_skew_ms
            )
            for metric in sorted(expected_metrics):
                metric_records = grouped.get(metric, [])
                if not metric_records:
                    continue
                metric_newest = metric_records[-1].observed_at
                if any(record.observed_at > future_limit for record in metric_records):
                    reasons.append(
                        f"{metric} includes a timestamp beyond the allowed "
                        f"{self._config.max_future_skew_ms} ms clock skew"
                    )
                staleness_seconds = (reference_time - metric_newest).total_seconds()
                if staleness_seconds > stale_after_seconds:
                    reasons.append(
                        f"{metric} is stale by {staleness_seconds:.0f} seconds; "
                        f"limit is {stale_after_seconds}"
                    )
        data_quality = DataQuality(
            status="good" if not reasons else "degraded",
            reasons=reasons,
            sample_counts=sample_counts,
            newest_observation_at=newest,
        )

        features = {
            metric: self._calculate_metric_feature(metric, metric_records)
            for metric, metric_records in grouped.items()
            if metric_records
        }
        return FeatureWindow(
            feature_version=self.feature_version,
            entity_id=entity_id,
            window_start=window_start,
            window_end=newest,
            data_quality=data_quality,
            metrics=features,
        )

    def _calculate_metric_feature(
        self, metric: str, records: list[MetricRecord]
    ) -> MetricWindowFeature:
        return calculate_metric_feature(
            metric,
            records[-1].unit,
            [(record.observed_at, record.value) for record in records],
        )


def calculate_metric_feature(
    metric: str,
    unit: str,
    observations: list[tuple[datetime, float]],
) -> MetricWindowFeature:
    """Calculate the canonical rolling statistics used in training and live inference.

    Keeping this calculation independent from a storage record type prevents the
    predictive training pipeline from quietly drifting away from runtime feature
    generation when application and network observations are joined.
    """

    if not observations:
        raise ValueError("at least one observation is required")
    if any(
        later[0] <= earlier[0]
        for earlier, later in zip(observations, observations[1:], strict=False)
    ):
        raise ValueError("feature observations must have strictly increasing timestamps")
    values = [value for _, value in observations]
    baseline_values = values[: max(3, len(values) // 2)]
    baseline_median = median(baseline_values)
    baseline_mad = median([abs(value - baseline_median) for value in baseline_values])
    stddev = _standard_deviation(values)
    robust_scale = 1.4826 * baseline_mad
    if robust_scale > 0:
        robust_z = (values[-1] - baseline_median) / robust_scale
    elif stddev > 0:
        robust_z = (values[-1] - baseline_median) / stddev
    else:
        robust_z = 0.0

    return MetricWindowFeature(
        metric=metric,
        unit=unit,
        sample_count=len(values),
        latest=values[-1],
        mean=sum(values) / len(values),
        p95=_percentile(values, 0.95),
        stddev=stddev,
        slope_per_second=_linear_slope_values(observations),
        ewma=_ewma(values),
        baseline_median=baseline_median,
        robust_z=robust_z,
    )


def _ewma(values: list[float], alpha: float = 0.3) -> float:
    estimate = values[0]
    for value in values[1:]:
        estimate = alpha * value + (1 - alpha) * estimate
    return estimate


def _linear_slope(records: list[MetricRecord]) -> float:
    return _linear_slope_values(
        [(record.observed_at, record.value) for record in records]
    )


def _linear_slope_values(observations: list[tuple[datetime, float]]) -> float:
    origin = observations[0][0]
    x_values = [(observed_at - origin).total_seconds() for observed_at, _ in observations]
    y_values = [value for _, value in observations]
    x_mean = sum(x_values) / len(x_values)
    y_mean = sum(y_values) / len(y_values)
    denominator = sum((x - x_mean) ** 2 for x in x_values)
    if denominator == 0:
        return 0.0
    return sum((x - x_mean) * (y - y_mean) for x, y in zip(x_values, y_values)) / denominator


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    fraction = position - lower_index
    return ordered[lower_index] + (ordered[upper_index] - ordered[lower_index]) * fraction


def _standard_deviation(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    average = sum(values) / len(values)
    return sqrt(sum((value - average) ** 2 for value in values) / len(values))
