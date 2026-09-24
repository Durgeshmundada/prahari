import pytest

from prahari.copilot.grounding import BriefSelection, snapshot_digest
from prahari.copilot.service import IncidentCopilot
from prahari.services.demo import DemoService
from scripts.evaluate_copilot_reliability import validate_brief, validate_origin


@pytest.fixture
def supported_brief(project_root):
    incident = DemoService(root=project_root).incident()

    class Client:
        def structured_chat(self, prompt, model, *, json_schema):
            properties = json_schema["properties"]
            return BriefSelection(
                incident_id=incident.id,
                incident_sha256=properties["incident_sha256"]["const"],
                situation_claim_ids=properties["situation_claim_ids"]["items"]["enum"][:1],
                recommended_playbook_id=None,
            )

    brief = IncidentCopilot(project_root, Client(), allowed_playbooks=set()).brief(incident)
    return incident, brief.model_dump(mode="json")


def test_reliability_validator_accepts_reconstructed_support(supported_brief):
    incident, brief = supported_brief
    assert validate_brief(brief, incident) == (True, True)


def test_reliability_validator_rejects_forged_self_consistent_grounding(supported_brief):
    incident, brief = supported_brief
    forged = brief["grounding"]["claims"][0]
    forged["text"] = "A hostile attack has been proven by this network telemetry."
    brief["summary"] = forged["text"]
    brief["citations"][0]["claim"] = forged["text"]
    forged["claim_id"] = "claim-" + snapshot_digest(
        {key: value for key, value in forged.items() if key != "claim_id"}
    )
    with pytest.raises(ValueError, match="not grounded"):
        validate_brief(brief, incident)


def test_reliability_validator_rejects_a_changed_evidence_snapshot(supported_brief):
    incident, brief = supported_brief
    changed = incident.model_copy(update={"state": "escalated"})
    with pytest.raises(ValueError, match="not bound"):
        validate_brief(brief, changed)


def test_reliability_harness_restricts_target_origin():
    assert validate_origin("http://127.0.0.1:8080", None) == "http://127.0.0.1:8080"
    assert (
        validate_origin("http://172.31.255.18:8080", "172.31.255.18") == "http://172.31.255.18:8080"
    )
    with pytest.raises(ValueError, match="exact allowed"):
        validate_origin("http://172.31.255.20:8080", "172.31.255.18")
