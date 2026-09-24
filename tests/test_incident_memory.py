from datetime import timedelta

import pytest

from prahari.contracts import ClosureRequest, ClosureReviewRequest
from prahari.copilot.grounding import BriefSelection
from prahari.copilot.retrieval import approved_case_context
from prahari.copilot.service import CopilotUnavailableError, IncidentCopilot
from prahari.services.demo import DemoService


@pytest.fixture
def approved_case(project_root):
    service = DemoService(root=project_root)
    service.review(service.incident_id, "operator-01", "Reviewed evidence.")
    service.close(
        service.incident_id,
        ClosureRequest(
            actor_id="operator-01", outcome="uncertain", note="No recovery is established."
        ),
    )
    case = service.list_closure_cases()[0]
    approved = service.decide_closure_case(
        case.id,
        ClosureReviewRequest(
            actor_id="reviewer-01", decision="approved", note="Escalation is appropriate."
        ),
    )
    return service.incident(), approved


@pytest.mark.parametrize(
    "change",
    [
        "pending",
        "rejected",
        "self-review",
        "model",
        "path",
        "location",
        "type",
        "invalid-quality",
        "network-profile",
        "model-bundle",
    ],
)
def test_memory_excludes_unreviewed_or_incompatible_cases(approved_case, change):
    incident, case = approved_case
    if change == "pending":
        case = case.model_copy(update={"review": None})
    elif change in {"rejected", "self-review"}:
        review = case.review.model_copy(
            update={
                "decision": "rejected" if change == "rejected" else "approved",
                "actor_id": case.submission.actor_id if change == "self-review" else "reviewer-01",
            }
        )
        case = case.model_copy(update={"review": review})
    else:
        prediction = case.incident.prediction
        if change == "location":
            card = case.incident.model_copy(
                update={
                    "location": case.incident.location.model_copy(
                        update={"entity_id": "interface:other"}
                    )
                }
            )
        else:
            updates = {
                "model": {"model_version": "other-model"},
                "path": {"entity_id": "tunnel:other"},
                "type": {"likely_incident_type": "normal"},
                "invalid-quality": {"data_quality": "invalid"},
                "network-profile": {"network_profile_sha256": "1" * 64},
                "model-bundle": {"model_bundle_sha256": "2" * 64},
            }
            card = case.incident.model_copy(
                update={"prediction": prediction.model_copy(update=updates[change])}
            )
        case = case.model_copy(update={"incident": card})
    assert approved_case_context(incident, [case]) == []


def test_memory_is_bounded_and_labels_uncertain_outcomes_honestly(approved_case):
    incident, case = approved_case
    newer = case.model_copy(
        update={"id": "case-newer", "reviewed_at": case.reviewed_at + timedelta(seconds=1)}
    )
    chunks = approved_case_context(incident, [case, newer], limit=1)
    assert len(chunks) == 1
    assert chunks[0].source_id == "case:case-newer"
    assert "Outcome: uncertain" in chunks[0].text
    assert "not current telemetry" in chunks[0].text
    assert len(chunks[0].sha256) == 64


def test_memory_labels_related_scope_only_with_bound_profile_and_overlap(approved_case):
    incident, case = approved_case
    profile = "1" * 64
    bundle = "2" * 64
    current_prediction = incident.prediction.model_copy(
        update={"network_profile_sha256": profile, "model_bundle_sha256": bundle}
    )
    incident = incident.model_copy(update={"prediction": current_prediction})
    previous = case.incident.prediction.model_copy(
        update={
            "network_profile_sha256": profile,
            "model_bundle_sha256": bundle,
            "entity_id": "tunnel:related-path",
            "affected_scope": case.incident.prediction.affected_scope.model_copy(
                update={"paths": ["path:related"]}
            ),
        }
    )
    related = case.model_copy(
        update={"incident": case.incident.model_copy(update={"prediction": previous})}
    )

    chunks = approved_case_context(incident, [related])
    assert len(chunks) == 1
    assert chunks[0].applicability_level == "related_profile"
    assert chunks[0].score == 0.5
    assert chunks[0].differing_dimensions == ("affected_scope", "entity_id")
    assert "related_profile" in chunks[0].text

    no_overlap = related.model_copy(
        update={
            "incident": related.incident.model_copy(
                update={
                    "prediction": previous.model_copy(
                        update={
                            "affected_scope": previous.affected_scope.model_copy(
                                update={"applications": ["unrelated-app"]}
                            )
                        }
                    )
                }
            )
        }
    )
    assert approved_case_context(incident, [no_overlap]) == []


def test_copilot_receives_approved_memory_and_constrains_its_source_id(project_root, approved_case):
    incident, case = approved_case

    class Client:
        def structured_chat(self, prompt, _model, *, json_schema):
            source_id = f"case:{case.id}"
            assert source_id in prompt and "Outcome: uncertain" in prompt
            properties = json_schema["properties"]
            assert case.submission.note not in prompt
            return BriefSelection(
                incident_id=incident.id,
                incident_sha256=properties["incident_sha256"]["const"],
                situation_claim_ids=properties["situation_claim_ids"]["items"]["enum"][-1:],
                recommended_playbook_id=None,
            )

    copilot = IncidentCopilot(
        project_root, Client(), allowed_playbooks=set(), case_provider=lambda: [case]
    )
    brief = copilot.brief(incident)
    assert any(citation.source_id == f"case:{case.id}" for citation in brief.citations)
    assert any(
        claim.kind == "historical" and "outcome uncertain" in claim.text
        for claim in brief.grounding.claims
    )


def test_case_retrieval_failure_is_explicit(project_root, approved_case):
    incident, _ = approved_case

    def unavailable():
        raise RuntimeError("database unavailable")

    copilot = IncidentCopilot(
        project_root, object(), allowed_playbooks=set(), case_provider=unavailable
    )
    with pytest.raises(CopilotUnavailableError, match="case retrieval is unavailable"):
        copilot.brief(incident)
