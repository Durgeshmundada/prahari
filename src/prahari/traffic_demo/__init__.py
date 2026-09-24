"""Bounded, local-only application traffic demo for PRAHARI labs.

The package deliberately has no router, shell, or device-control integration.
It provides a WebSocket receiver and a branch-side probe that make application
traffic observable through the existing collector API.
"""

from prahari.traffic_demo.protocol import (
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_TEXT_CHARS,
    HARD_MAX_FILE_BYTES,
    TRAFFIC_DEMO_PROTOCOL,
    ProbeMeasurements,
    ReceiverLimits,
    UploadMeasurement,
)

__all__ = [
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_TEXT_CHARS",
    "HARD_MAX_FILE_BYTES",
    "TRAFFIC_DEMO_PROTOCOL",
    "ProbeMeasurements",
    "ReceiverLimits",
    "UploadMeasurement",
]
