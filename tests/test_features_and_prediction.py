from datetime import timedelta
from pathlib import Path

import pytest

from prahari.analytics.baseline import PredictionWithheldError, TrendBaselinePredictor
from prahari.analytics.features import FeatureExtractor
from prahari.analytics.topology import TopologyCorrelator
from prahari.config import load_mvp_config
from prahari.contracts import MetricRecord
from prahari.repository import load_json, load_jsonl


def _predictor(root: Path) -> tuple[FeatureExtractor, TrendBaselinePredictor, list[MetricRecord]]:
    config = load_mvp_config(root)
    topology = load_json(root / "config" / "topology.lab-v1.json")
    inventory = load_json(root / "config" / "inventory.lab-v1.json")
    records = load_jsonl(root / "fixtures" / "gradual-loss" / "metrics.jsonl", MetricRecord)
    extractor = FeatureExtractor(config)
    predictor = TrendBaselinePredictor(config, TopologyCorrelator(topology, inventory))
    return extractor, predictor, records


def test_replay_features_are_complete_and_worsening(project_root: Path) -> None:
    extractor, _, records = _predictor(project_root)

    features = extractor.extract(records, "tunnel:branch-01-to-dc-primary")

    assert features.data_quality.status == "good"
    assert features.metrics["packet_loss_ratio"].sample_count == 12
    assert features.metrics["packet_loss_ratio"].slope_per_second > 0
    assert features.metrics["jitter_ms"].robust_z > 3


def test_baseline_produces_monotonic_early_risk(project_root: Path) -> None:
    extractor, predictor, records = _predictor(project_root)

    prediction = predictor.predict(extractor.extract(records, "tunnel:branch-01-to-dc-primary"))

    assert prediction.likely_incident_type == "tunnel_degradation"
    assert prediction.risk_status == "heuristic"
    assert prediction.impact_risk.within_30_seconds <= prediction.impact_risk.within_60_seconds
    assert prediction.impact_risk.within_60_seconds <= prediction.impact_risk.within_120_seconds
    assert prediction.impact_risk.within_120_seconds >= 0.65
    assert (
        prediction.root_location_candidates[0].entity_id
        == "interface:branch-01-edge:wan-primary"
    )
    assert prediction.time_to_impact.range != "unknown"


def test_predictor_withholds_when_a_required_metric_is_incomplete(project_root: Path) -> None:
    extractor, predictor, records = _predictor(project_root)
    incomplete = [record for record in records if record.metric != "jitter_ms"]

    features = extractor.extract(incomplete, "tunnel:branch-01-to-dc-primary")

    assert features.data_quality.status == "degraded"
    with pytest.raises(PredictionWithheldError, match="prediction withheld"):
        predictor.predict(features)


def test_live_feature_window_is_degraded_when_the_newest_sample_is_stale(
    project_root: Path,
) -> None:
    extractor, _, records = _predictor(project_root)
    newest = max(record.observed_at for record in records)

    features = extractor.extract(
        records,
        "tunnel:branch-01-to-dc-primary",
        reference_time=newest + timedelta(seconds=11),
    )

    assert features.data_quality.status == "degraded"
    assert any("stale" in reason for reason in features.data_quality.reasons)


def test_unsupported_metric_cannot_hide_stale_required_telemetry(project_root: Path) -> None:
    extractor, _, records = _predictor(project_root)
    delayed_jitter = [
        record.model_copy(update={"observed_at": record.observed_at - timedelta(seconds=15)})
        if record.metric == "jitter_ms"
        else record
        for record in records
    ]
    latest_loss = max(
        record.observed_at for record in delayed_jitter if record.metric == "packet_loss_ratio"
    )
    unsupported_but_fresh = records[-1].model_copy(
        update={
            "metric": "unsupported_metric",
            "observed_at": latest_loss + timedelta(seconds=60),
        }
    )

    features = extractor.extract(
        [*delayed_jitter, unsupported_but_fresh],
        "tunnel:branch-01-to-dc-primary",
        reference_time=latest_loss,
    )

    assert features.data_quality.status == "degraded"
    assert any(reason.startswith("jitter_ms is stale") for reason in features.data_quality.reasons)
    assert features.window_end == latest_loss


def test_feature_window_rejects_an_unmatched_configuration_version(project_root: Path) -> None:
    extractor, _, records = _predictor(project_root)
    config = load_mvp_config(project_root)
    mismatched = [
        record.model_copy(update={"config_version": "old-policy-v0"})
        if record.metric == "jitter_ms"
        else record
        for record in records
    ]

    features = extractor.extract(
        mismatched,
        "tunnel:branch-01-to-dc-primary",
        expected_config_version=config.telemetry_config_version,
    )

    assert features.data_quality.status == "degraded"
    assert any("configuration version" in reason for reason in features.data_quality.reasons)


def test_live_feature_window_rejects_future_dated_telemetry(project_root: Path) -> None:
    extractor, _, records = _predictor(project_root)
    newest = max(record.observed_at for record in records)
    future = [
        record.model_copy(update={"observed_at": record.observed_at + timedelta(seconds=3)})
        for record in records
    ]

    features = extractor.extract(
        future,
        "tunnel:branch-01-to-dc-primary",
        reference_time=newest,
    )

    assert features.data_quality.status == "degraded"
    assert any("clock skew" in reason for reason in features.data_quality.reasons)


def _records_with_one_gap(
    records: list[MetricRecord], gap_seconds: int
) -> list[MetricRecord]:
    observed = sorted({record.observed_at for record in records})
    split_at = observed[6]
    original_gap = (split_at - observed[5]).total_seconds()
    offset = timedelta(seconds=gap_seconds - original_gap)
    return [
        record.model_copy(update={"observed_at": record.observed_at + offset})
        if record.observed_at >= split_at
        else record
        for record in records
    ]


def test_feature_window_tolerates_one_missed_collection_interval(project_root: Path) -> None:
    extractor, _, records = _predictor(project_root)

    features = extractor.extract(
        _records_with_one_gap(records, 11),
        "tunnel:branch-01-to-dc-primary",
    )

    assert features.data_quality.status == "good"


def test_feature_window_rejects_gap_above_explicit_tolerance(project_root: Path) -> None:
    extractor, _, records = _predictor(project_root)

    features = extractor.extract(
        _records_with_one_gap(records, 16),
        "tunnel:branch-01-to-dc-primary",
    )

    assert features.data_quality.status == "degraded"
    assert any("gap above 15 seconds" in reason for reason in features.data_quality.reasons)
