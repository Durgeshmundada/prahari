from datetime import datetime

import pytest
from pydantic import ValidationError

from prahari.contracts import ImpactRisk, MetricRecord


def test_metric_record_requires_an_offset_aware_timestamp() -> None:
    payload = {
        "observed_at": datetime(2026, 8, 20, 9, 0, 0),
        "source": "test",
        "entity_id": "tunnel:test",
        "entity_type": "tunnel",
        "metric": "packet_loss_ratio",
        "value": 0.01,
        "unit": "ratio",
        "topology_version": "lab-v1",
        "quality": {"source_healthy": True},
    }
    with pytest.raises(ValidationError, match="UTC offset"):
        MetricRecord.model_validate(payload)


def test_impact_risk_requires_monotonic_horizons() -> None:
    with pytest.raises(ValidationError, match="nondecreasing"):
        ImpactRisk(within_30_seconds=0.8, within_60_seconds=0.7, within_120_seconds=0.9)
