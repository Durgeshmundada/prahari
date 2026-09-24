"""Print a plain-language fault-detection status from the local PRAHARI API."""

from __future__ import annotations

import argparse
from typing import Any
from urllib.parse import urlsplit

import httpx

from prahari.persistence import PersistenceStore
from prahari.settings import Settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check local PRAHARI monitoring and detection.")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    parsed = urlsplit(args.api_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        print("ERROR: this checker accepts only a loopback HTTP API URL.")
        return 2

    try:
        with httpx.Client(base_url=args.api_url, timeout=5.0) as client:
            health = _get_json(client, "/health")
            traffic = _get_json(client, "/api/demo/traffic")
            incidents = _get_json(client, "/api/incidents")
    except (httpx.HTTPError, ValueError) as error:
        print(f"ERROR: PRAHARI could not be checked: {error}")
        return 1

    fixture_only = health.get("fixture_only") is True
    print()
    print("PRAHARI FAULT-DETECTION CHECK")
    print("-----------------------------")
    print(f"API connection          : {'PASS' if health.get('status') == 'ok' else 'FAIL'}")
    print(f"Application monitoring  : {_traffic_result(traffic)}")

    if not isinstance(incidents, list) or not incidents:
        print("Fault detector          : NO ACTIVE INCIDENT")
        mode = "RECORDED REPLAY" if fixture_only else "LIVE"
        print(f"Mode                    : {mode}")
        return 1

    incident = incidents[0] if isinstance(incidents[0], dict) else {}
    prediction = incident.get("prediction") if isinstance(incident, dict) else {}
    prediction = prediction if isinstance(prediction, dict) else {}
    incident_type = prediction.get("likely_incident_type")
    if not _prediction_is_active(prediction):
        print("Fault detector          : NO ACTIVE INCIDENT")
        mode = "RECORDED REPLAY" if fixture_only else "LIVE"
        print(f"Mode                    : {mode}")
        return 1
    risk = prediction.get("impact_risk")
    risk = risk if isinstance(risk, dict) else {}
    probability = risk.get("within_120_seconds")
    probability_text = (
        f"{float(probability) * 100:.1f}% within 120 seconds"
        if isinstance(probability, int | float)
        else "risk unavailable"
    )
    time_to_impact = prediction.get("time_to_impact")
    time_to_impact = time_to_impact if isinstance(time_to_impact, dict) else {}
    location = incident.get("location")
    location = location if isinstance(location, dict) else {}

    print("Fault detector          : PASS")
    print(f"Incident type           : {incident_type}")
    print(f"Severity                : {incident.get('severity', 'unknown')}")
    print(f"Predicted risk          : {probability_text}")
    print(f"Estimated impact time   : {time_to_impact.get('range', 'unavailable')}")
    print(f"Located at              : {location.get('entity_id', 'unavailable')}")
    print(f"Data quality            : {prediction.get('data_quality', 'unknown')}")

    evidence = prediction.get("evidence")
    if isinstance(evidence, list) and evidence:
        print("Evidence:")
        for item in evidence[:4]:
            print(f"  - {item}")

    evidence_ids = incident.get("evidence_ids")
    evidence_items = evidence_ids if isinstance(evidence_ids, list) else []
    evidence_simulated = any(
        item == "simulation:local-dashboard-only" for item in evidence_items
    )
    local_scenario = _local_simulation_scenario()
    simulated = evidence_simulated or local_scenario is not None
    if simulated:
        evidence_scenario = next(
            (
                item.removeprefix("scenario:")
                for item in evidence_items
                if isinstance(item, str) and item.startswith("scenario:")
            ),
            "unknown",
        )
        scenario_id = local_scenario or evidence_scenario
        print()
        print(f"SAFE LOCAL SIMULATION: PASS ({scenario_id})")
        print("REAL LIVE FAULT DETECTION: NOT TESTED")
        print("Reason: synthetic records changed the dashboard; the network was not impaired.")
    elif fixture_only:
        print()
        print("RECORDED FAULT DETECTION: PASS")
        print("REAL LIVE FAULT DETECTION: NOT TESTED BY THIS DASHBOARD")
        print("Reason: prediction inputs are the bundled replay fixture.")
    else:
        print()
        print("LIVE DETECTION PATH: ACTIVE")
        print(
            "Confirm the incident timestamp precedes measured application impact "
            "before claiming prediction."
        )
    return 0


def _get_json(client: httpx.Client, path: str) -> Any:
    response = client.get(path)
    response.raise_for_status()
    return response.json()


def _prediction_is_active(prediction: dict[str, Any]) -> bool:
    return prediction.get("likely_incident_type") not in {None, "normal"}


def _traffic_result(traffic: Any) -> str:
    if not isinstance(traffic, dict):
        return "FAIL (invalid response)"
    payload = traffic.get("payload")
    if traffic.get("status") != "passed" or not isinstance(payload, dict):
        return f"NO VERIFIED TRANSFER ({traffic.get('status', 'unknown')})"
    name = payload.get("name", "unnamed file")
    received = payload.get("bytes_received", 0)
    return f"PASS ({name}, {received} verified bytes)"


def _local_simulation_scenario() -> str | None:
    settings = Settings()
    if settings.environment != "development" or settings.data_mode != "replay":
        return None
    store = PersistenceStore(settings.database_url)
    try:
        records = store.list_metrics(
            entity_id="tunnel:branch-01-to-dc-primary",
            limit=5_000,
        )
    finally:
        store.dispose()
    if not records:
        return None
    latest = max(records, key=lambda record: record.observed_at)
    if latest.source != "local-dashboard-fault-simulation":
        return None
    return latest.labels.get("scenario", "unknown")


if __name__ == "__main__":
    raise SystemExit(main())
