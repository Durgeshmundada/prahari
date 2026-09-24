"""Loading and validating the versioned MVP configuration."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ImpactPolicy(ConfigModel):
    version: str
    description: str
    sustained_seconds: int = Field(gt=0)
    metrics: dict[str, float]


class TrendMetricPolicy(ConfigModel):
    weight: float = Field(gt=0)
    impact_threshold: float = Field(gt=0)
    slope_reference_per_second: float = Field(gt=0)
    unit: str
    minimum_value: float
    maximum_value: float | None = None


class TrendPolicy(ConfigModel):
    model_version: str
    alert_risk_within_120_seconds: float = Field(gt=0, lt=1)
    metrics: dict[str, TrendMetricPolicy]


class ObservabilityMetricPolicy(ConfigModel):
    unit: str = Field(min_length=1)
    minimum_value: float
    maximum_value: float | None = None


class MVPConfig(ConfigModel):
    schema_version: str
    scenario_version: str
    telemetry_config_version: str = Field(min_length=1)
    sampling_seconds: int = Field(gt=0)
    window_seconds: int = Field(gt=0)
    minimum_samples_per_metric: int = Field(gt=0)
    max_late_by_ms: int = Field(ge=0)
    max_future_skew_ms: int = Field(ge=0)
    max_inter_sample_gap_seconds: int = Field(gt=0)
    traffic_demo_ttl_seconds: int = Field(gt=0, le=3_600)
    horizons_seconds: list[int]
    supported_incident_types: list[str]
    impact_policy: ImpactPolicy
    trend_policy: TrendPolicy
    observability_metrics: dict[str, ObservabilityMetricPolicy] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_sampling_tolerances(self) -> MVPConfig:
        if self.max_inter_sample_gap_seconds < self.sampling_seconds:
            raise ValueError("max_inter_sample_gap_seconds must cover at least one sample interval")
        return self


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


@lru_cache
def load_mvp_config(root: Path | None = None) -> MVPConfig:
    config_root = root or project_root()
    raw = json.loads((config_root / "config" / "mvp.json").read_text(encoding="utf-8"))
    return MVPConfig.model_validate(raw)
