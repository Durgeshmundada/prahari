import json
from pathlib import Path

import pytest

from prahari.analytics.trained import TrainedDegradationClassifier


def test_classifier_rejects_artifact_checksum_mismatch(tmp_path: Path) -> None:
    model = tmp_path / "model.txt"
    model.write_text("not-a-model", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "model-manifest-v1",
                "features": ["packet_loss_ratio", "jitter_ms", "rtt_avg_ms"],
                "model_sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="checksum"):
        TrainedDegradationClassifier(model, manifest)
