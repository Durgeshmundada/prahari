from datetime import UTC, datetime, timedelta

from prahari.analytics.predictive import (
    FEATURE_NAMES,
    ApplicationImpactPolicy,
    ApplicationMeasurement,
    NetworkMeasurement,
    PredictivePoint,
    detect_sustained_application_impact,
    extract_predictive_feature_vectors,
)


def _point(index: int, *, app_rtt_ms: float = 5.0) -> PredictivePoint:
    return PredictivePoint(
        observed_at=datetime(2026, 9, 1, tzinfo=UTC) + timedelta(seconds=index * 5),
        network=NetworkMeasurement(
            sent=5,
            received=5,
            packet_loss_ratio=0,
            rtt_avg_ms=1 + index,
            jitter_ms=index / 10,
        ),
        application=ApplicationMeasurement(
            status="passed",
            messages_attempted=5,
            messages_received=5,
            message_loss_ratio=0,
            rtt_p95_ms=app_rtt_ms,
            jitter_ms=1,
            upload_status="completed",
            upload_integrity="verified",
            upload_throughput_mbps=10,
            evidence_saved=True,
        ),
    )


def _policy() -> ApplicationImpactPolicy:
    return ApplicationImpactPolicy(
        version="test-v2",
        sustained_seconds=15,
        max_inter_sample_gap_seconds=15,
        message_loss_ratio=0.2,
        rtt_p95_ms=150,
        jitter_ms=30,
    )


def test_impact_is_derived_from_sustained_application_measurements() -> None:
    observations = [_point(index, app_rtt_ms=200 if index >= 3 else 5) for index in range(7)]

    impact = detect_sustained_application_impact(observations, _policy())

    assert impact.actual_impact_start_time == observations[3].observed_at
    assert impact.confirmed_at == observations[6].observed_at
    assert impact.triggering_signals == ["application_rtt"]


def test_impact_streak_resets_across_measurement_gap() -> None:
    observations = [_point(index, app_rtt_ms=200) for index in range(3)]
    observations.extend(
        [
            _point(10, app_rtt_ms=200),
            _point(11, app_rtt_ms=200),
            _point(12, app_rtt_ms=200),
        ]
    )

    impact = detect_sustained_application_impact(observations, _policy())

    assert impact.actual_impact_start_time is None


def test_training_and_runtime_share_an_exact_feature_contract() -> None:
    observations = [_point(index) for index in range(14)]

    vectors = extract_predictive_feature_vectors(observations, run_id="run-001")

    assert len(vectors) == 3
    assert vectors[0].window_start == observations[0].observed_at
    assert vectors[0].window_end == observations[11].observed_at
    assert tuple(vectors[0].values) == FEATURE_NAMES
    assert len(vectors[0].values) == 56
    assert vectors[-1].values["network_rtt_ms__slope_per_second"] > 0


def test_feature_window_with_a_large_gap_is_withheld() -> None:
    observations = [_point(index) for index in range(12)]
    observations[-1] = observations[-1].model_copy(
        update={"observed_at": observations[-1].observed_at + timedelta(seconds=16)}
    )

    vectors = extract_predictive_feature_vectors(observations, run_id="run-001")

    assert all(vector.window_end != observations[-1].observed_at for vector in vectors)
