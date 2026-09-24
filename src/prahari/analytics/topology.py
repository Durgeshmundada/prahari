"""Transparent topology join for the initial single-path MVP."""

from __future__ import annotations

from dataclasses import dataclass

from prahari.contracts import AffectedScope, RootLocationCandidate


@dataclass(frozen=True)
class TopologyCorrelation:
    root_location_candidates: list[RootLocationCandidate]
    affected_scope: AffectedScope
    evidence: list[str]


class TopologyCorrelator:
    """Ranks path-adjacent interfaces without claiming a learned root cause."""

    def __init__(self, topology: dict[str, object], inventory: dict[str, object]) -> None:
        self._topology = topology
        self._site_by_interface = {
            interface["id"]: interface["site_id"]
            for interface in inventory["interfaces"]  # type: ignore[index]
        }

    def correlate(self, entity_id: str, risk_within_120_seconds: float) -> TopologyCorrelation:
        path = self._path_for_entity(entity_id)
        interfaces = path["interface_ids"]
        candidates: list[RootLocationCandidate] = []
        for index, interface_id in enumerate(interfaces):
            base_score = 0.5 if index == 0 else 0.25
            score = min(0.95, round(base_score + 0.4 * risk_within_120_seconds, 2))
            candidates.append(
                RootLocationCandidate(
                    entity_id=interface_id,
                    site_id=self._site_by_interface[interface_id],
                    score=score,
                    rationale=(
                        "Primary-path endpoint ranked by direct topology relation to the "
                        "degraded monitored tunnel."
                    ),
                )
            )

        candidates.sort(key=lambda candidate: candidate.score, reverse=True)
        affected_scope = AffectedScope(
            paths=[path["id"]],
            applications=list(path["applications"]),
            sites=list(path["site_ids"]),
            backup_path_id=path["backup_path_id"],
        )
        return TopologyCorrelation(
            root_location_candidates=candidates,
            affected_scope=affected_scope,
            evidence=[
                f"Topology {self._topology['version']} maps {entity_id} to {path['id']}.",
                f"Configured fallback path: {path['backup_path_id']}.",
            ],
        )

    def _path_for_entity(self, entity_id: str) -> dict[str, object]:
        for path in self._topology["paths"]:  # type: ignore[index]
            if entity_id in path["monitored_entity_ids"]:  # type: ignore[index]
                return path  # type: ignore[return-value]
        raise ValueError(f"no configured path maps monitored entity {entity_id!r}")
