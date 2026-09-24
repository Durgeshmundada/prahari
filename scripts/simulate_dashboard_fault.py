"""Inject labelled synthetic telemetry into the local replay dashboard only."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta

from prahari.config import project_root
from prahari.contracts import MetricRecord, RecordQuality
from prahari.persistence import PersistenceStore
from prahari.services.demo import DemoService
from prahari.settings import Settings

SIMULATION_RUN_ID = "local-dashboard-fault-simulation"
SAMPLE_SECONDS = 5

SCENARIOS: dict[str, dict[str, object]] = {
    "healthy": {
        "title": "Healthy network",
        "loss": [
            0.0010, 0.0011, 0.0009, 0.0010, 0.0012, 0.0010,
            0.0008, 0.0011, 0.0010, 0.0009, 0.0010, 0.0010,
        ],
        "jitter": [3.0, 3.2, 2.9, 3.1, 3.3, 3.0, 2.8, 3.1, 3.0, 2.9, 3.1, 3.0],
    },
    "gradual-loss": {
        "title": "Gradual packet-loss fault",
        "loss": [
            0.002, 0.0025, 0.003, 0.004, 0.005, 0.0065,
            0.008, 0.0105, 0.013, 0.016, 0.020, 0.024,
        ],
        "jitter": [4.0, 4.5, 5.0, 6.0, 7.5, 9.0, 11.0, 13.0, 15.0, 17.0, 18.5, 20.0],
    },
    "high-jitter": {
        "title": "High-jitter fault",
        "loss": [
            0.002, 0.002, 0.0025, 0.003, 0.004, 0.005,
            0.006, 0.007, 0.008, 0.009, 0.010, 0.012,
        ],
        "jitter": [4.0, 5.0, 7.0, 10.0, 14.0, 18.0, 23.0, 29.0, 36.0, 44.0, 52.0, 60.0],
    },
    "burst-loss": {
        "title": "Burst packet-loss fault",
        "loss": [
            0.002, 0.002, 0.003, 0.004, 0.006, 0.010,
            0.018, 0.030, 0.045, 0.060, 0.075, 0.090,
        ],
        "jitter": [4.0, 4.0, 4.5, 5.0, 5.5, 6.0, 6.5, 7.0, 7.5, 8.0, 8.0, 8.5],
    },
    "combined": {
        "title": "Combined loss-and-jitter fault",
        "loss": [
            0.002, 0.003, 0.004, 0.006, 0.009, 0.013,
            0.019, 0.027, 0.038, 0.052, 0.068, 0.085,
        ],
        "jitter": [4.0, 5.0, 6.5, 8.0, 11.0, 15.0, 20.0, 26.0, 33.0, 41.0, 50.0, 60.0],
    },
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Load a safe synthetic scenario into the local replay dashboard."
    )
    parser.add_argument(
        "--scenario",
        choices=sorted([*SCENARIOS, "recorded-replay"]),
        required=True,
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Calculate the result without changing the local demo database.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    settings = Settings()
    if settings.environment != "development" or settings.data_mode != "replay":
        print("REFUSED: fault simulation is allowed only in development replay mode.")
        return 2

    store = PersistenceStore(settings.database_url)
    try:
        if args.scenario == "recorded-replay":
            if args.preview:
                print("Preview: restore the bundled recorded replay scenario.")
                return 0
            store.replace_replay_fixture([], [], run_id=SIMULATION_RUN_ID)
            service = DemoService(root=project_root(), store=store)
            prediction = service.prediction()
            title = "Bundled recorded replay"
        else:
            scenario = SCENARIOS[args.scenario]
            records = _records(args.scenario, scenario)
            if args.preview:
                service = DemoService(root=project_root())
                service._metrics = records  # noqa: SLF001 - bounded local simulation preview
            else:
                store.replace_replay_fixture(records, [], run_id=SIMULATION_RUN_ID)
                service = DemoService(root=project_root(), store=store)
            prediction = service.prediction()
            title = str(scenario["title"])
    finally:
        store.dispose()

    print()
    print("PRAHARI SAFE DASHBOARD SIMULATION")
    print("---------------------------------")
    print(f"Scenario          : {title}")
    print("Network changed   : NO")
    print("Wi-Fi affected    : NO")
    print(
        "Telemetry source  : RECORDED FIXTURE"
        if args.scenario == "recorded-replay"
        else "Telemetry source  : SYNTHETIC / LOCAL DASHBOARD ONLY"
    )
    print(f"Detected type     : {prediction.likely_incident_type}")
    print(f"Risk within 120 s : {prediction.impact_risk.within_120_seconds:.1%}")
    print(f"Estimated impact  : {prediction.time_to_impact.range}")
    print(f"Data quality      : {prediction.data_quality}")
    if args.preview:
        print("Database changed  : NO (preview)")
    elif args.scenario == "recorded-replay":
        print("Database changed  : simulation rows removed")
        print("Next step         : wait a few seconds or press Refresh on the dashboard")
    else:
        print("Database changed  : YES (simulation rows only)")
        print("Next step         : wait a few seconds or press Refresh on the dashboard")
    print()
    if args.scenario == "recorded-replay":
        print("This is recorded evidence, not a measurement of the current network.")
    else:
        print(
            "This result tests dashboard and detection logic. "
            "It is not a measured network fault."
        )
    return 0


def _records(scenario_name: str, scenario: dict[str, object]) -> list[MetricRecord]:
    loss_values = scenario["loss"]
    jitter_values = scenario["jitter"]
    if not isinstance(loss_values, list) or not isinstance(jitter_values, list):
        raise ValueError("scenario samples are invalid")
    if len(loss_values) != 12 or len(jitter_values) != 12:
        raise ValueError("each simulation scenario must contain exactly 12 samples")
    finished_at = datetime.now(UTC) - timedelta(milliseconds=250)
    started_at = finished_at - timedelta(seconds=(len(loss_values) - 1) * SAMPLE_SECONDS)
    records: list[MetricRecord] = []
    common = {
        "source": "local-dashboard-fault-simulation",
        "entity_id": "tunnel:branch-01-to-dc-primary",
        "entity_type": "tunnel",
        "labels": {
            "site_id": "site:branch-01",
            "path_id": "path:branch-01-to-dc:primary",
            "traffic_class": "video",
            "simulation": "true",
            "scenario": scenario_name,
        },
        "topology_version": "lab-v1",
        "config_version": "lab-config-v1",
        "quality": RecordQuality(
            source_healthy=True,
            late_by_ms=0,
            is_estimated=True,
            counter_reset=False,
        ),
    }
    for index, value in enumerate(loss_values):
        records.append(
            MetricRecord(
                observed_at=started_at + timedelta(seconds=index * SAMPLE_SECONDS),
                metric="packet_loss_ratio",
                value=float(value),
                unit="ratio",
                **common,
            )
        )
    for index, value in enumerate(jitter_values):
        records.append(
            MetricRecord(
                observed_at=started_at + timedelta(seconds=index * SAMPLE_SECONDS),
                metric="jitter_ms",
                value=float(value),
                unit="ms",
                **common,
            )
        )
    return records


if __name__ == "__main__":
    raise SystemExit(main())
