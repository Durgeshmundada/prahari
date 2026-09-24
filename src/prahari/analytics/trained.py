"""Verified loading and inference for the experimental degradation classifier."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import lightgbm as lgb
import numpy as np

from prahari.contracts import MetricRecord

EXPECTED_FEATURES = ("packet_loss_ratio", "jitter_ms", "rtt_avg_ms")


@dataclass(frozen=True)
class DegradationScore:
    probability: float
    threshold: float
    degraded: bool
    model_version: str
    approval_status: str


class TrainedDegradationClassifier:
    def __init__(self, model_path: Path, manifest_path: Path) -> None:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.bundle_sha256 = sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        if manifest.get("schema_version") != "model-manifest-v1":
            raise ValueError("unsupported model manifest schema")
        if tuple(manifest.get("features", [])) != EXPECTED_FEATURES:
            raise ValueError("model feature contract does not match the runtime")
        if sha256(model_path.read_bytes()).hexdigest() != manifest.get("model_sha256"):
            raise ValueError("model artifact checksum does not match its manifest")
        self.model_version = str(manifest["model_version"])
        self.approval_status = str(manifest["approval_status"])
        self.predictive_claim_approved = bool(manifest["predictive_claim_approved"])
        self.threshold = float(manifest["decision_threshold"])
        if not 0 < self.threshold < 1:
            raise ValueError("model decision threshold must be between zero and one")
        self._booster = lgb.Booster(model_file=str(model_path))

    def score(self, records: list[MetricRecord]) -> DegradationScore:
        relevant = [
            record
            for record in records
            if record.metric in {"packet_loss_ratio", "jitter_ms"}
        ]
        if not relevant:
            raise ValueError("no model-enabled telemetry records are available")
        latest_at = max(record.observed_at for record in relevant)
        latest = {record.metric: record for record in relevant if record.observed_at == latest_at}
        if set(latest) != {"packet_loss_ratio", "jitter_ms"}:
            raise ValueError("latest model telemetry sample is incomplete")
        packet_loss = latest["packet_loss_ratio"]
        try:
            rtt = float(packet_loss.labels["rtt_avg_ms"])
        except (KeyError, ValueError) as error:
            raise ValueError("latest model telemetry is missing valid RTT evidence") from error
        values = np.asarray([[packet_loss.value, latest["jitter_ms"].value, rtt]])
        probability = float(self._booster.predict(values)[0])
        return DegradationScore(
            probability=probability,
            threshold=self.threshold,
            degraded=probability >= self.threshold,
            model_version=self.model_version,
            approval_status=self.approval_status,
        )
