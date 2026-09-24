"""Evidence-bound statements; model text is never evidence of claim support."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from prahari.contracts import ContractModel, IncidentCard
from prahari.copilot.retrieval import RetrievedChunk

BriefField = Literal["summary", "situation", "root_cause_hypothesis", "impact", "limitations"]


def snapshot_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


class SupportedStatement(ContractModel):
    claim_id: str = Field(pattern=r"^claim-[a-f0-9]{64}$")
    field: BriefField
    text: str = Field(min_length=12, max_length=1000)
    source_id: str = Field(min_length=1, max_length=200)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    support_path: str = Field(min_length=1, max_length=300)
    kind: Literal["current", "historical", "advisory", "limitation"]


class BriefGrounding(ContractModel):
    mode: Literal["evidence-selection-v1"] = "evidence-selection-v1"
    incident_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    incident_snapshot: IncidentCard
    claims: list[SupportedStatement] = Field(min_length=5, max_length=12)

    @model_validator(mode="after")
    def bind_snapshot(self):
        if snapshot_digest(self.incident_snapshot.model_dump(mode="json")) != self.incident_sha256:
            raise ValueError("grounding snapshot hash mismatch")
        return self


class BriefSelection(ContractModel):
    incident_id: str
    incident_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    situation_claim_ids: list[str] = Field(min_length=1, max_length=6)
    recommended_playbook_id: str | None


def statement(
    field: BriefField,
    text: str,
    source_id: str,
    source_sha256: str,
    support_path: str,
    kind: Literal["current", "historical", "advisory", "limitation"],
) -> SupportedStatement:
    fields = dict(
        field=field,
        text=text,
        source_id=source_id,
        source_sha256=source_sha256,
        support_path=support_path,
        kind=kind,
    )
    return SupportedStatement(claim_id="claim-" + snapshot_digest(fields), **fields)


def evidence_statements(incident: IncidentCard) -> list[SupportedStatement]:
    """Only typed state contributes facts; narrative evidence is intentionally excluded."""
    prediction = incident.prediction
    digest = snapshot_digest(incident.model_dump(mode="json"))
    source = f"incident:{incident.id}"

    def fact(field: BriefField, text: str, path: str, kind="current"):
        return statement(field, text, source, digest, path, kind)

    risks = prediction.impact_risk
    return [
        fact(
            "summary",
            f"Model {prediction.model_version} reports "
            f"{prediction.likely_incident_type} for {prediction.entity_id} "
            f"with confidence {prediction.confidence:.3f}.",
            "prediction",
        ),
        fact(
            "situation",
            f"Incident state is {incident.state}; severity is {incident.severity}.",
            "state,severity",
        ),
        fact(
            "situation",
            f"Incident data quality is {incident.data_quality}; "
            f"prediction data quality is {prediction.data_quality}.",
            "data_quality",
        ),
        fact(
            "situation",
            f"The {prediction.risk_status} risk scores are "
            f"{risks.within_30_seconds:.3f}, {risks.within_60_seconds:.3f} and "
            f"{risks.within_120_seconds:.3f} for 30, 60 and 120 seconds respectively.",
            "prediction.impact_risk,prediction.risk_status",
        ),
        fact(
            "situation",
            "Configured affected paths: "
            f"{', '.join(prediction.affected_scope.paths) or 'none'}; "
            f"sites: {', '.join(prediction.affected_scope.sites) or 'none'}.",
            "prediction.affected_scope",
        ),
        fact(
            "root_cause_hypothesis",
            "The model classification is a hypothesis. "
            "These observations do not establish a confirmed root cause.",
            "prediction.likely_incident_type",
            "limitation",
        ),
        fact(
            "impact",
            "The model's affected application scope is "
            f"{', '.join(prediction.affected_scope.applications) or 'unspecified'}. "
            "This scope does not establish an observed application outage or recovery.",
            "prediction.affected_scope.applications",
            "limitation",
        ),
        fact(
            "limitations",
            "This brief is advisory; it does not authorize a device command "
            "or replace operator review and independent recovery checks.",
            "playbook_status",
            "limitation",
        ),
        fact(
            "limitations",
            f"Risk scores are {prediction.risk_status}. "
            + (
                "Heuristic scores are not calibrated forecasting probabilities."
                if prediction.risk_status == "heuristic"
                else "Calibration applies only within the approved model deployment scope."
            ),
            "prediction.risk_status",
            "limitation",
        ),
    ]


def runbook_statement(chunk: RetrievedChunk, playbook_id: str) -> SupportedStatement:
    return statement(
        "situation",
        f"Playbook {playbook_id} is registered for operator review with "
        f"document {chunk.source_path}. "
        "This identifies the document and does not authorize execution.",
        chunk.source_id,
        chunk.sha256,
        chunk.source_path,
        "advisory",
    )


def historical_statement(chunk: RetrievedChunk) -> SupportedStatement:
    if (
        chunk.historical_outcome is None
        or chunk.applicability_level is None
        or not chunk.source_id.startswith("case:")
    ):
        raise ValueError("historical statement requires typed approved case metadata")
    applicability = (
        "matches the exact current scope"
        if chunk.applicability_level == "exact_scope"
        else "shares an approved profile but differs in "
        + ", ".join(chunk.differing_dimensions)
    )
    return statement(
        "situation",
        f"Historical case {chunk.source_id.removeprefix('case:')} has independently "
        f"reviewed outcome {chunk.historical_outcome} and {applicability}. "
        "It does not establish current recovery, root cause or action permission.",
        chunk.source_id,
        chunk.sha256,
        "submission.outcome,review.decision,incident.prediction,incident.location",
        "historical",
    )
