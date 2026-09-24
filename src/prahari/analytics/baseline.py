"""Explainable trend baseline used before any trained model is eligible."""

from __future__ import annotations

from math import ceil, exp

from prahari.analytics.topology import TopologyCorrelator
from prahari.config import MVPConfig, TrendMetricPolicy
from prahari.contracts import FeatureWindow, ImpactRisk, Prediction, TimeToImpact


class PredictionWithheldError(ValueError):
    """Raised when a feature window does not meet the documented quality gate."""


class TrendBaselinePredictor:
    """Combines current level, worsening slope, and baseline deviation transparently."""

    def __init__(self, config: MVPConfig, correlator: TopologyCorrelator) -> None:
        self._config = config
        self._correlator = correlator

    def predict(self, features: FeatureWindow) -> Prediction:
        if features.data_quality.status != "good":
            detail = "; ".join(features.data_quality.reasons) or "unknown data-quality issue"
            raise PredictionWithheldError(f"prediction withheld: {detail}")

        metric_scores = {
            metric: self._score_metric(features.metrics[metric], policy)
            for metric, policy in self._config.trend_policy.metrics.items()
            if metric in features.metrics
        }
        if not metric_scores:
            raise PredictionWithheldError("prediction withheld: no configured metric features")

        total_weight = sum(
            self._config.trend_policy.metrics[metric].weight for metric in metric_scores
        )
        combined_score = sum(
            metric_scores[metric] * self._config.trend_policy.metrics[metric].weight
            for metric in metric_scores
        ) / total_weight
        risk_120 = _sigmoid(5 * (combined_score - 0.45))
        risk_60 = risk_120 * (0.65 + 0.2 * combined_score)
        risk_30 = risk_60 * (0.55 + 0.15 * combined_score)
        impact_risk = ImpactRisk(
            within_30_seconds=round(risk_30, 3),
            within_60_seconds=round(risk_60, 3),
            within_120_seconds=round(risk_120, 3),
        )
        likely_incident_type = (
            "tunnel_degradation" if impact_risk.within_120_seconds >= 0.5 else "normal"
        )
        correlation = self._correlator.correlate(
            features.entity_id, impact_risk.within_120_seconds
        )
        confidence = min(0.95, round(0.45 + 0.55 * combined_score, 2))
        time_to_impact = self._estimate_time_to_impact(features)
        ranked_metrics = sorted(metric_scores, key=metric_scores.get, reverse=True)
        evidence = [
            self._metric_evidence(features, metric)
            for metric in ranked_metrics[:2]
        ]
        evidence.extend(correlation.evidence)

        return Prediction(
            prediction_id=f"pred-{features.window_end.strftime('%Y%m%dT%H%M%SZ')}",
            model_version=self._config.trend_policy.model_version,
            entity_id=features.entity_id,
            likely_incident_type=likely_incident_type,
            incident_type_probabilities={
                "normal": round(1 - impact_risk.within_120_seconds, 3),
                "tunnel_degradation": impact_risk.within_120_seconds,
            },
            impact_risk=impact_risk,
            time_to_impact=time_to_impact,
            root_location_candidates=correlation.root_location_candidates,
            affected_scope=correlation.affected_scope,
            confidence=confidence,
            risk_status="heuristic",
            data_quality=features.data_quality.status,
            evidence=evidence,
        )

    def _score_metric(self, feature: object, policy: TrendMetricPolicy) -> float:
        latest = getattr(feature, "latest")
        slope = getattr(feature, "slope_per_second")
        robust_z = getattr(feature, "robust_z")
        current_level = min(1.0, max(0.0, latest / policy.impact_threshold))
        worsening_slope = min(1.0, max(0.0, slope / policy.slope_reference_per_second))
        baseline_deviation = min(1.0, max(0.0, robust_z / 3.0))
        return 0.5 * current_level + 0.35 * worsening_slope + 0.15 * baseline_deviation

    def _estimate_time_to_impact(self, features: FeatureWindow) -> TimeToImpact:
        projections: list[float] = []
        for metric, policy in self._config.trend_policy.metrics.items():
            feature = features.metrics.get(metric)
            if feature is None or feature.slope_per_second <= 0:
                continue
            if feature.latest >= policy.impact_threshold:
                return TimeToImpact(
                    range="impact threshold already breached",
                    basis=f"{metric} is at or above the provisional impact threshold",
                )
            projections.append(
                (policy.impact_threshold - feature.latest) / feature.slope_per_second
            )
        if not projections:
            return TimeToImpact(
                range="unknown",
                basis="no worsening metric has a usable threshold-crossing projection",
            )
        lower_seconds = max(1, ceil(min(projections)))
        upper_seconds = max(lower_seconds, ceil(max(projections)))
        return TimeToImpact(
            range=f"{lower_seconds}-{upper_seconds} seconds",
            basis=(
                "projected crossing of provisional packet-loss/jitter thresholds; "
                "bootstrap trend baseline"
            ),
        )

    @staticmethod
    def _metric_evidence(features: FeatureWindow, metric: str) -> str:
        feature = features.metrics[metric]
        if feature.unit == "ratio":
            latest = f"{feature.latest * 100:.2f}%"
            baseline = f"{feature.baseline_median * 100:.2f}%"
        else:
            latest = f"{feature.latest:.1f} {feature.unit}"
            baseline = f"{feature.baseline_median:.1f} {feature.unit}"
        return (
            f"{metric} rose from a {baseline} baseline to {latest}; "
            f"slope {feature.slope_per_second:.5f} per second."
        )


def _sigmoid(value: float) -> float:
    return 1 / (1 + exp(-value))
