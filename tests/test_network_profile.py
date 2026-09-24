import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from prahari.api import create_app
from prahari.contracts import AppProbeRecord, MetricRecord
from prahari.network_profile import NetworkProfile, load_network_profile
from prahari.persistence import PersistenceStore
from prahari.services.demo import PrahariService
from prahari.settings import Settings


def _profile(root: Path) -> tuple[NetworkProfile, dict[str, str]]:
    raw = json.loads((root / "config/network-profile.lab.example.json").read_text())
    identifiers = [
        raw["incident_id"],
        raw["playbook"]["id"],
        raw["inventory"]["version"],
        raw["config"]["telemetry_config_version"],
    ]
    for group in ("sites", "devices", "interfaces"):
        identifiers.extend(item["id"] for item in raw["inventory"][group])
    for index, path in enumerate(raw["topology"]["paths"]):
        path["name"] = f"Configured site path {index}"
        identifiers.extend(
            [path["id"], *path["monitored_entity_ids"], *path["app_probe_entities"].values()]
        )
    mapping = {value: f"target:{index}" for index, value in enumerate(identifiers)}
    return NetworkProfile.model_validate(_rename(raw, mapping)), mapping


def _rename(value, mapping: dict[str, str]):
    if isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, list):
        return [_rename(item, mapping) for item in value]
    if isinstance(value, dict):
        return {key: _rename(item, mapping) for key, item in value.items()}
    return value


def test_live_profile_drives_ingestion_incident_and_independent_case_workflow(
    project_root: Path,
) -> None:
    profile, mapping = _profile(project_root)
    fixture = PrahariService(root=project_root)
    store = PersistenceStore("sqlite://")
    store.create_schema()
    service = PrahariService(
        root=project_root, data_mode="live", store=store, network_profile=profile
    )
    settings = Settings(
        environment="test", data_mode="live", database_url="sqlite://", auth_mode="disabled"
    )
    client = TestClient(create_app(service, settings=settings, store=store))
    offset = (
        datetime.now(UTC)
        - timedelta(seconds=1)
        - max(record.observed_at for record in fixture.metric_records())
    )
    for endpoint, records in (
        ("metrics", fixture.metric_records()),
        ("app-probes", fixture.app_probe_records()),
    ):
        payload = []
        for record in records:
            raw = _rename(record.model_dump(mode="json"), mapping)
            raw["observed_at"] = (record.observed_at + offset).isoformat()
            payload.append(raw)
        response = client.post(
            f"/api/telemetry/{endpoint}",
            json={"run_id": "external-live-test", "records": payload},
            headers={"X-Actor-Role": "collector"},
        )
        assert response.status_code == 202, response.text

    topology = client.get("/api/topology").json()
    assert topology["fixture_only"] is False
    assert topology["profile_sha256"] == profile.fingerprint
    incident = client.get("/api/incidents").json()[0]
    assert incident["id"] == profile.incident_id
    assert "Configured site path" in incident["summary"]
    assert "Branch-01" not in incident["summary"]
    assert incident["prediction"]["entity_id"] == profile.monitored_entity_id
    assert incident["prediction"]["affected_scope"]["paths"] == [profile.primary_path_id]
    prediction_id = incident["prediction"]["prediction_id"]
    prefix = f"/api/incidents/{profile.incident_id}"
    review = client.post(
        f"{prefix}/review",
        json={
            "actor_id": "operator-01",
            "prediction_id": prediction_id,
            "note": "Reviewed live evidence.",
        },
    )
    assert review.status_code == 200, review.text
    dry_run = client.post(
        f"{prefix}/actions/{profile.playbook.id}/dry-run",
        json={
            "actor_id": "operator-01",
            "prediction_id": prediction_id,
            "note": "Confirm device execution remains blocked.",
        },
    )
    assert dry_run.status_code == 200, dry_run.text
    assert dry_run.json()["status"] == "blocked"
    closure = client.post(
        f"{prefix}/closure",
        json={
            "actor_id": "operator-01",
            "prediction_id": prediction_id,
            "outcome": "uncertain",
            "note": "Escalated for target-environment investigation.",
        },
    )
    assert closure.status_code == 200, closure.text
    case = client.get("/api/cases").json()[0]
    assert case["incident_id"] == profile.incident_id
    decision = client.post(
        f"/api/cases/{case['id']}/decision",
        headers={"X-Actor-Role": "reviewer"},
        json={
            "actor_id": "reviewer-02",
            "decision": "approved",
            "note": "Evidence and escalation reviewed independently.",
        },
    )
    assert decision.status_code == 200, decision.text
    assert decision.json()["review"]["decision"] == "approved"
    changed_profile = profile.model_copy(update={"profile_id": "changed-target"})
    restarted = PrahariService(
        root=project_root, data_mode="live", store=store, network_profile=changed_profile
    )
    assert restarted.prediction().network_profile_sha256 == changed_profile.fingerprint
    assert restarted.prediction().prediction_id != prediction_id
    with pytest.raises(ValueError, match="evidence changed"):
        restarted.review(
            profile.incident_id, "operator-01", "Old review must fail.", prediction_id=prediction_id
        )


@pytest.mark.parametrize(
    "invalid",
    [
        "unknown-interface",
        "duplicate-site",
        "missing-probe",
        "version-mismatch",
        "duplicate-probe",
        "lab-adapter",
    ],
)
def test_profile_rejects_invalid_references(project_root: Path, invalid: str) -> None:
    profile, _ = _profile(project_root)
    raw = profile.model_dump(mode="json")
    primary, backup = raw["topology"]["paths"]
    if invalid == "unknown-interface":
        primary["interface_ids"][0] = "unknown"
    elif invalid == "duplicate-site":
        raw["inventory"]["sites"].append(raw["inventory"]["sites"][0])
    elif invalid == "missing-probe":
        del primary["app_probe_entities"]["chat"]
    elif invalid == "version-mismatch":
        raw["inventory"]["version"] = "different-version"
    elif invalid == "duplicate-probe":
        backup["app_probe_entities"]["chat"] = backup["app_probe_entities"]["file-upload"]
    else:
        raw["playbook"]["action_adapter"] = "prahari-lab-action-agent-v1"
    with pytest.raises(ValueError):
        NetworkProfile.model_validate(raw)


def test_profile_checksum_binds_policy_and_loader_rejects_duplicate_keys(
    project_root: Path,
    tmp_path: Path,
) -> None:
    profile, _ = _profile(project_root)
    raw = profile.model_dump(mode="json")
    raw["config"]["trend_policy"]["metrics"]["jitter_ms"]["weight"] = 0.5
    assert NetworkProfile.model_validate(raw).fingerprint != profile.fingerprint
    path = tmp_path / "profile.json"
    path.write_text('{"profile_id":"first","profile_id":"second"}')
    with pytest.raises(ValueError, match="duplicate network profile key"):
        load_network_profile(path)


def test_external_profiles_never_use_lab_replay_or_device_adapter(project_root: Path) -> None:
    profile, _ = _profile(project_root)
    with pytest.raises(ValueError, match="require live"):
        PrahariService(root=project_root, network_profile=profile)
    with pytest.raises(ValueError, match="controlled-lab adapter"):
        PrahariService(
            root=project_root, data_mode="live", network_profile=profile, action_agent=object()
        )  # type: ignore[arg-type]


def test_external_ingestion_rejects_original_lab_identity(project_root: Path) -> None:
    profile, _ = _profile(project_root)
    service = PrahariService(root=project_root, data_mode="live", network_profile=profile)
    fixture = PrahariService(root=project_root)
    with pytest.raises(ValueError, match="not in the current topology"):
        service.ingest_metrics([MetricRecord.model_validate(fixture.metric_records()[0])])
    with pytest.raises(ValueError, match="not in the current topology"):
        service.ingest_app_probes([AppProbeRecord.model_validate(fixture.app_probe_records()[0])])
