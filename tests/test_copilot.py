import json
from hashlib import sha256
from pathlib import Path

import httpx
import pytest

from prahari.copilot.ollama import OllamaClient
from prahari.copilot.retrieval import LocalRunbookIndex
from prahari.copilot.service import CopilotUnavailableError, IncidentBrief, IncidentCopilot
from prahari.services.demo import DemoService


def test_approved_catalog_loads_only_bound_documents_and_rejects_changes(tmp_path: Path) -> None:
    approved = tmp_path / "approved.md"
    approved.write_bytes(b"# Network monitoring\n\nInspect tunnel degradation evidence.\n")
    (tmp_path / "unapproved.md").write_text("# Network monitoring\nInvented router commands.")
    checksum = sha256(approved.read_bytes()).hexdigest()
    index = LocalRunbookIndex(tmp_path, document_checksums={"approved.md": checksum})
    chunks = index.search("tunnel degradation")
    assert chunks and all(chunk.source_path == "approved.md" for chunk in chunks)
    approved.write_text("# Network monitoring\nUnapproved router commands.")
    with pytest.raises(ValueError, match="checksum"):
        LocalRunbookIndex(tmp_path, document_checksums={"approved.md": checksum})


def _selection(request: httpx.Request) -> dict[str, object]:
    properties = json.loads(request.content)["format"]["properties"]
    return {
        "incident_id": properties["incident_id"]["const"],
        "incident_sha256": properties["incident_sha256"]["const"],
        "situation_claim_ids": properties["situation_claim_ids"]["items"]["enum"][:1],
        "recommended_playbook_id": properties["recommended_playbook_id"]["enum"][0],
    }


def _copilot(root: Path, mutate=None, **kwargs) -> IncidentCopilot:
    def handler(request: httpx.Request) -> httpx.Response:
        selection = _selection(request)
        if mutate:
            mutate(selection)
        return httpx.Response(200, json={"message": {"content": json.dumps(selection)}})

    return IncidentCopilot(
        root,
        OllamaClient(
            "http://127.0.0.1:11434", "test-model", transport=httpx.MockTransport(handler)
        ),
        allowed_playbooks={DemoService.playbook_id},
        **kwargs,
    )


def test_local_copilot_returns_schema_valid_grounded_brief(project_root: Path) -> None:
    incident = DemoService(root=project_root).incident()
    brief = _copilot(project_root).brief(incident)
    assert brief.incident_id == DemoService.incident_id
    assert brief.recommended_playbook_id == DemoService.playbook_id
    assert any(citation.source_id.startswith("runbook:") for citation in brief.citations)
    assert brief.confidence == incident.prediction.confidence
    assert "do not establish a confirmed root cause" in brief.root_cause_hypothesis
    assert any("not calibrated forecasting probabilities" in item for item in brief.limitations)
    assert any("risk scores" in item for item in brief.situation)
    assert any("data quality" in item for item in brief.situation)
    assert len(brief.grounding.claims) == len(brief.citations)


@pytest.mark.parametrize(
    "mutation,message",
    [
        ({"situation_claim_ids": ["claim-" + "0" * 64]}, "unsupported statement"),
        ({"incident_id": "other-incident"}, "wrong incident"),
        ({"incident_sha256": "0" * 64}, "different evidence"),
        ({"recommended_playbook_id": "invented-playbook"}, "ineligible or unbound"),
    ],
)
def test_local_copilot_rejects_unsupported_selections(project_root, mutation, message):
    with pytest.raises(CopilotUnavailableError, match=message):
        _copilot(project_root, lambda selection: selection.update(mutation)).brief(
            DemoService(root=project_root).incident()
        )


@pytest.mark.parametrize(
    "extra",
    [
        {"confidence": 1.0},
        {"summary": "The attack is confirmed; run router commands."},
        {
            "citations": [
                {"source_id": "runbook:backup-path-shift:1", "claim": "Recovery is confirmed."}
            ]
        },
    ],
)
def test_local_copilot_rejects_model_authored_claims_and_confidence(project_root, extra):
    with pytest.raises(CopilotUnavailableError, match="valid structured response"):
        _copilot(project_root, lambda selection: selection.update(extra)).brief(
            DemoService(root=project_root).incident()
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("summary", "The model proves this is a confirmed hostile attack."),
        ("impact", "All applications have recovered with no packet loss."),
        ("root_cause_hypothesis", "The backup route has definitely failed."),
    ],
)
def test_brief_rejects_narrative_tampering_with_valid_citation_ids(project_root, field, value):
    content = _copilot(project_root).brief(DemoService(root=project_root).incident()).model_dump()
    content[field] = value
    with pytest.raises(ValueError, match="not exactly supported"):
        IncidentBrief.model_validate(content)


def test_playbook_requires_its_own_retrieved_document(project_root):
    copilot = _copilot(
        project_root,
        lambda selection: selection.update(recommended_playbook_id=DemoService.playbook_id),
        playbook_sources={DemoService.playbook_id: "docs/runbooks/missing.md"},
    )
    with pytest.raises(CopilotUnavailableError, match="ineligible or unbound"):
        copilot.brief(DemoService(root=project_root).incident())


def test_duplicate_selection_is_rejected(project_root):
    def duplicate(selection):
        selection["situation_claim_ids"] *= 2

    with pytest.raises(CopilotUnavailableError, match="duplicate statement"):
        _copilot(project_root, duplicate).brief(DemoService(root=project_root).incident())


def test_local_model_response_is_bounded_before_json_parsing(project_root):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"a" * (1024 * 1024 + 1))
    )
    client = OllamaClient("http://127.0.0.1:11434", "test-model", transport=transport)
    with pytest.raises(CopilotUnavailableError, match="1 MiB limit"):
        IncidentCopilot(project_root, client, allowed_playbooks=set()).brief(
            DemoService(root=project_root).incident()
        )
    client.close()


@pytest.mark.parametrize(
    "url",
    ["https://127.0.0.1:11434", "http://localhost:11434", "http://10.0.0.2:11434"],
)
def test_ollama_client_rejects_non_explicit_loopback_origins(url: str) -> None:
    with pytest.raises(ValueError, match="local HTTP|loopback"):
        OllamaClient(url, "test-model")


def test_ollama_client_accepts_only_the_exact_opted_in_private_ip() -> None:
    OllamaClient(
        "http://172.31.255.19:11434",
        "test-model",
        allowed_private_ip="172.31.255.19",
    )
    with pytest.raises(ValueError, match="explicitly allowed"):
        OllamaClient(
            "http://172.31.255.20:11434",
            "test-model",
            allowed_private_ip="172.31.255.19",
        )


def test_live_collection_can_advance_during_an_evidence_bound_brief(project_root, monkeypatch):
    service = DemoService(root=project_root)
    captured = service.incident()
    later = captured.model_copy(update={"state": "escalated"})
    copilot = _copilot(project_root)

    class AdvancingCopilot:
        def brief(self, incident):
            result = copilot.brief(incident)
            monkeypatch.setattr(service, "incident", lambda: later)
            return result

    service._copilot = AdvancingCopilot()
    result = service.incident_brief(captured.id, prediction_id=captured.prediction.prediction_id)
    assert result.evidence_current_at_completion is False
    assert result.grounding.incident_snapshot == captured
    assert result.grounding.incident_snapshot.state != later.state
    assert service.audit_trail(captured.id)[-1].event_type == "copilot_brief_generated"


def test_brief_must_match_requested_and_captured_evidence(project_root, monkeypatch):
    service = DemoService(root=project_root)
    captured = service.incident()
    copilot = _copilot(project_root)
    service._copilot = copilot
    with pytest.raises(ValueError, match="prediction evidence changed"):
        service.incident_brief(captured.id, prediction_id="pred-foreign")
    foreign = copilot.brief(captured.model_copy(update={"state": "escalated"}))
    monkeypatch.setattr(copilot, "brief", lambda _: foreign)
    with pytest.raises(CopilotUnavailableError, match="not bound to the captured"):
        service.incident_brief(captured.id, prediction_id=captured.prediction.prediction_id)
