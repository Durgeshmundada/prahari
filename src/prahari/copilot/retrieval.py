"""Deterministic local retrieval for approved repository documents."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from prahari.contracts import ClosureCase, IncidentCard

_TOKEN = re.compile(r"[a-z0-9][a-z0-9_.:-]*", re.IGNORECASE)


@dataclass(frozen=True)
class RetrievedChunk:
    source_id: str
    source_path: str
    title: str
    text: str
    sha256: str
    score: float
    historical_outcome: (
        Literal["resolved", "partially_resolved", "failed", "false_positive", "uncertain"] | None
    ) = None
    applicability_level: Literal["exact_scope", "related_profile"] | None = None
    matched_dimensions: tuple[str, ...] = ()
    differing_dimensions: tuple[str, ...] = ()


class LocalRunbookIndex:
    """Small BM25-style index with stable source IDs and no network dependency."""

    def __init__(
        self,
        root: Path,
        *,
        chunk_chars: int = 1800,
        document_checksums: dict[str, str] | None = None,
    ) -> None:
        self._root = root.resolve()
        runbook_root = (self._root / "docs" / "runbooks").resolve()
        if not runbook_root.is_relative_to(self._root):
            raise ValueError("runbook directory must remain inside the project root")
        self._chunks: list[RetrievedChunk] = []
        paths = (
            sorted(runbook_root.glob("*.md"))
            if document_checksums is None
            else sorted(self._root / name for name in document_checksums)
        )
        for path in paths:
            resolved = path.resolve()
            if not resolved.is_relative_to(self._root) or path.is_symlink():
                raise ValueError("runbook document must remain inside the knowledge root")
            relative = path.relative_to(self._root).as_posix()
            if document_checksums is not None and any(
                (self._root / Path(*Path(relative).parts[:index])).is_symlink()
                for index in range(1, len(Path(relative).parts))
            ):
                raise ValueError("approved runbook must not traverse a symbolic link")
            with path.open("rb") as handle:
                payload = handle.read(1024 * 1024 + 1)
            if len(payload) > 1024 * 1024:
                raise ValueError("runbook exceeds the 1 MiB document limit")
            raw = payload.decode("utf-8")
            digest = hashlib.sha256(payload).hexdigest()
            if document_checksums is not None and document_checksums[relative] != digest:
                raise ValueError("runbook checksum does not match its profile approval")
            title = next(
                (
                    line.removeprefix("# ").strip()
                    for line in raw.splitlines()
                    if line.startswith("# ")
                ),
                path.stem,
            )
            paragraphs = [part.strip() for part in re.split(r"\n\s*\n", raw) if part.strip()]
            for number, text in enumerate(_pack(paragraphs, chunk_chars), start=1):
                relative = path.relative_to(self._root).as_posix()
                self._chunks.append(
                    RetrievedChunk(
                        source_id=(
                            f"runbook:{path.stem}:{number}"
                            if document_checksums is None
                            else f"runbook:{hashlib.sha256(relative.encode()).hexdigest()[:20]}"
                            f":{number}"
                        ),
                        source_path=relative,
                        title=title,
                        text=text,
                        sha256=digest,
                        score=0.0,
                    )
                )
        if not self._chunks:
            raise ValueError("no local runbook documents were found")

    def search(self, query: str, *, limit: int = 4) -> list[RetrievedChunk]:
        if not 1 <= limit <= 10:
            raise ValueError("retrieval limit must be between 1 and 10")
        terms = set(_tokens(query))
        if not terms:
            return []
        documents = [list(_tokens(chunk.text)) for chunk in self._chunks]
        average_length = sum(map(len, documents)) / len(documents)
        ranked: list[RetrievedChunk] = []
        for chunk, tokens in zip(self._chunks, documents, strict=True):
            score = 0.0
            for term in terms:
                frequency = tokens.count(term)
                if not frequency:
                    continue
                document_frequency = sum(term in document for document in documents)
                inverse = math.log(
                    1 + (len(documents) - document_frequency + 0.5) / (document_frequency + 0.5)
                )
                denominator = frequency + 1.2 * (0.25 + 0.75 * len(tokens) / average_length)
                score += inverse * frequency * 2.2 / denominator
            if score > 0:
                ranked.append(RetrievedChunk(**{**chunk.__dict__, "score": score}))
        return sorted(ranked, key=lambda item: (-item.score, item.source_id))[:limit]


def _tokens(text: str):
    return (match.group(0).lower() for match in _TOKEN.finditer(text))


def approved_case_context(
    incident: IncidentCard,
    cases: Iterable[ClosureCase],
    *,
    limit: int = 2,
) -> list[RetrievedChunk]:
    """Typed historical context; never infer current applicability or action permission."""
    if not 1 <= limit <= 5:
        raise ValueError("case context limit must be between 1 and 5")
    matches = []
    current = incident.prediction
    for case in cases:
        previous = case.incident.prediction
        if (
            case.review is None
            or case.review.decision != "approved"
            or case.review.actor_id == case.submission.actor_id
            or case.reviewed_at is None
            or case.prediction_id != previous.prediction_id
            or previous.data_quality != "good"
            or previous.model_version != current.model_version
            or previous.likely_incident_type != current.likely_incident_type
        ):
            continue
        exact_dimensions = {
            "entity_id": previous.entity_id == current.entity_id,
            "location_entity_id": case.incident.location.entity_id == incident.location.entity_id,
            "affected_scope": previous.affected_scope == current.affected_scope,
            "network_profile_sha256": (
                previous.network_profile_sha256 == current.network_profile_sha256
            ),
            "model_bundle_sha256": previous.model_bundle_sha256 == current.model_bundle_sha256,
        }
        if all(exact_dimensions.values()):
            applicability = "exact_scope"
            score = 1.0
        else:
            previous_apps = set(previous.affected_scope.applications)
            current_apps = set(current.affected_scope.applications)
            if (
                previous.network_profile_sha256 is None
                or previous.model_bundle_sha256 is None
                or previous.network_profile_sha256 != current.network_profile_sha256
                or previous.model_bundle_sha256 != current.model_bundle_sha256
                or not previous_apps.intersection(current_apps)
            ):
                continue
            applicability = "related_profile"
            score = 0.5
        matched = tuple(sorted(name for name, matches in exact_dimensions.items() if matches))
        differing = tuple(sorted(name for name, matches in exact_dimensions.items() if not matches))
        text = (
            "Historical independently approved case. This is not current telemetry, "
            "proof of a current root cause, or permission to execute an action.\n"
            f"Applicability: {applicability}; matched dimensions: {', '.join(matched)}; "
            f"differing dimensions: {', '.join(differing) or 'none'}.\n"
            f"Outcome: {case.submission.outcome}. Submitted by {case.submission.actor_id}; "
            f"reviewed by {case.review.actor_id} at {case.reviewed_at.isoformat()}.\n"
            f"Model: {previous.model_version}. Prediction: {case.prediction_id}.\n"
            f"Impact present at submission: {case.app_impact_started}.\n"
            "Free-text operator/reviewer notes are excluded from model context."
        )
        matches.append(
            (
                case.reviewed_at,
                RetrievedChunk(
                    source_id=f"case:{case.id}",
                    source_path=f"/api/cases/{case.id}",
                    title=f"Reviewed {case.submission.outcome} case",
                    text=text,
                    sha256=hashlib.sha256(case.model_dump_json().encode()).hexdigest(),
                    score=score,
                    historical_outcome=case.submission.outcome,
                    applicability_level=applicability,
                    matched_dimensions=matched,
                    differing_dimensions=differing,
                ),
            )
        )
    return [
        item
        for _, item in sorted(
            matches,
            key=lambda pair: (pair[1].score, pair[0], pair[1].source_id),
            reverse=True,
        )[
            :limit
        ]
    ]


def _pack(paragraphs: list[str], limit: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if current and len(current) + len(paragraph) + 2 > limit:
            chunks.append(current)
            current = ""
        current = f"{current}\n\n{paragraph}" if current else paragraph
    if current:
        chunks.append(current)
    return chunks
