"""Bounded metric labels: no incident IDs, usernames, notes, tokens or raw URLs."""

from prometheus_client import Counter, Gauge, Histogram

HTTP_REQUEST_DURATION = Histogram(
    "prahari_http_request_duration_seconds",
    "API request duration including body receipt",
    ("method", "path", "status"),
    buckets=(0.01, 0.05, 0.1, 0.5, 1, 5, 15, 45, 90, 180),
)
DATABASE_AVAILABLE = Gauge("prahari_database_available", "Durable storage health check: 1 or 0")
PREDICTION_READY = Gauge("prahari_prediction_ready", "Prediction evidence readiness: 1 or 0")
UNCONFIRMED_ACTIONS = Gauge(
    "prahari_unconfirmed_actions", "Unconfirmed durable actions for the configured incident"
)
COPILOT_BRIEFS = Counter(
    "prahari_copilot_briefs_total", "Evidence-grounded brief outcomes", ("outcome",)
)


def metric_method(method: str) -> str:
    return (
        method
        if method in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
        else "OTHER"
    )
