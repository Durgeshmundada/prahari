"""Validated deployment-specific identities and network reference data."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from prahari.config import ConfigModel, MVPConfig

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,99}$")]


class Site(ConfigModel):
    id: Identifier
    name: str = Field(min_length=1, max_length=200)
    city: str = Field(default="", max_length=200)


class Device(ConfigModel):
    id: Identifier
    name: str = Field(min_length=1, max_length=200)
    site_id: Identifier
    role: str = Field(min_length=1, max_length=100)


class Interface(ConfigModel):
    id: Identifier
    device_id: Identifier
    site_id: Identifier
    role: str = Field(min_length=1, max_length=100)
    capacity_mbps: float = Field(gt=0, allow_inf_nan=False)


class Inventory(ConfigModel):
    version: Identifier
    sites: list[Site] = Field(min_length=1, max_length=10_000)
    devices: list[Device] = Field(min_length=1, max_length=10_000)
    interfaces: list[Interface] = Field(min_length=1, max_length=10_000)


class NetworkPath(ConfigModel):
    id: Identifier
    name: str = Field(min_length=1, max_length=200)
    path_role: Literal["primary", "backup"]
    site_ids: list[Identifier] = Field(min_length=2, max_length=2)
    monitored_entity_ids: list[Identifier] = Field(min_length=1, max_length=100)
    interface_ids: list[Identifier] = Field(max_length=100)
    backup_path_id: Identifier | None
    applications: list[Identifier] = Field(min_length=1, max_length=100)
    app_probe_entities: dict[Identifier, Identifier]


class Topology(ConfigModel):
    version: Identifier
    paths: list[NetworkPath] = Field(min_length=1, max_length=10_000)


class Playbook(ConfigModel):
    id: Identifier
    title: str = Field(min_length=1, max_length=200)
    status: Literal["draft", "available_pending_review", "blocked"]
    source: str = Field(min_length=1, max_length=1000)
    applicable_incident_types: list[Identifier] = Field(min_length=1, max_length=100)
    requires_operator_approval: Literal[True]
    action_adapter: Identifier | None = None
    safety_note: str = Field(min_length=1, max_length=2000)


class RunbookReference(ConfigModel):
    path: str = Field(min_length=1, max_length=500)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reviewer_id: Identifier
    approval_reference: str = Field(min_length=3, max_length=200)

    @model_validator(mode="after")
    def validate_path(self) -> RunbookReference:
        parts = self.path.split("/")
        if not self.path.endswith(".md") or any(
            not part or part in {".", ".."} or "\\" in part or ":" in part for part in parts
        ):
            raise ValueError("runbook path must be a relative Markdown path without traversal")
        return self


class NetworkProfile(ConfigModel):
    schema_version: Literal["network-profile-v1"]
    profile_id: Identifier
    deployment_scope: Literal["controlled_lab", "production"]
    incident_id: Identifier
    monitored_entity_id: Identifier
    primary_path_id: Identifier
    config: MVPConfig
    inventory: Inventory
    topology: Topology
    playbook: Playbook
    approved_runbooks: list[RunbookReference] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_references(self) -> NetworkProfile:
        if self.config.horizons_seconds != [30, 60, 120]:
            raise ValueError("the current prediction schema supports only 30/60/120s horizons")
        if set(self.config.supported_incident_types) != {"normal", "tunnel_degradation"}:
            raise ValueError("the current classifier supports normal and tunnel_degradation")
        if not {"packet_loss_ratio", "jitter_ms"} <= self.config.trend_policy.metrics.keys():
            raise ValueError("network evidence requires loss and jitter policies")
        for policy in self.config.trend_policy.metrics.values():
            if policy.maximum_value is not None and (
                policy.maximum_value <= policy.minimum_value
                or policy.impact_threshold > policy.maximum_value
            ):
                raise ValueError("trend metric bounds or impact threshold are inconsistent")
            if policy.impact_threshold <= policy.minimum_value:
                raise ValueError("impact threshold must exceed the metric minimum")
        sites = _unique(self.inventory.sites, "site")
        devices = _unique(self.inventory.devices, "device")
        interfaces = _unique(self.inventory.interfaces, "interface")
        paths = _unique(self.topology.paths, "path")
        if self.inventory.version != self.topology.version:
            raise ValueError("inventory and topology versions must match")
        for device in devices.values():
            if device.site_id not in sites:
                raise ValueError("device references an unknown site")
        for interface in interfaces.values():
            device = devices.get(interface.device_id)
            if device is None or interface.site_id != device.site_id:
                raise ValueError("interface device/site references are inconsistent")
        all_entities: set[str] = set()
        all_probe_entities: set[str] = set()
        for path in paths.values():
            if len(set(path.site_ids)) != 2 or not set(path.site_ids) <= sites.keys():
                raise ValueError("path must reference two distinct known sites")
            if len(set(path.interface_ids)) != len(path.interface_ids):
                raise ValueError("path interface IDs must be unique")
            for interface_id in path.interface_ids:
                if interface_id not in interfaces:
                    raise ValueError("path references an unknown interface")
                if interfaces[interface_id].site_id not in path.site_ids:
                    raise ValueError("path interface is outside its site scope")
            if set(path.app_probe_entities) != set(path.applications):
                raise ValueError("every path application needs an exact probe entity mapping")
            if len(set(path.applications)) != len(path.applications):
                raise ValueError("path applications must be unique")
            if len(set(path.app_probe_entities.values())) != len(path.app_probe_entities):
                raise ValueError("application probe entity IDs must be distinct within a path")
            entities = set(path.monitored_entity_ids)
            if len(entities) != len(path.monitored_entity_ids) or entities & all_entities:
                raise ValueError("monitored entity IDs must map to exactly one path")
            all_entities |= entities
            probes = set(path.app_probe_entities.values())
            all_probe_entities |= probes
            if path.backup_path_id is not None:
                if path.path_role != "primary":
                    raise ValueError("only primary paths can reference a backup")
                backup = paths.get(path.backup_path_id)
                if backup is None or backup.path_role != "backup" or backup.id == path.id:
                    raise ValueError("backup path reference is invalid")
                if set(backup.site_ids) != set(path.site_ids):
                    raise ValueError("backup path must connect the same sites")
        primary = paths.get(self.primary_path_id)
        if primary is None or primary.path_role != "primary" or not primary.interface_ids:
            raise ValueError("primary_path_id must select a primary path with known interfaces")
        if self.monitored_entity_id not in primary.monitored_entity_ids:
            raise ValueError("monitored_entity_id must belong to the selected primary path")
        if all_entities & all_probe_entities:
            raise ValueError("network and application entities must be distinct")
        if not {"chat", "file-upload"} <= set(primary.applications):
            raise ValueError("the current evidence schema requires chat and file-upload probes")
        if self.playbook.action_adapter is not None:
            raise ValueError("external profiles currently support monitoring-only playbooks")
        if not set(self.playbook.applicable_incident_types) <= set(
            self.config.supported_incident_types
        ):
            raise ValueError("playbook incident types must be enabled in the policy")
        if len({book.path for book in self.approved_runbooks}) != len(self.approved_runbooks):
            raise ValueError("approved runbook paths must be unique")
        if self.approved_runbooks and self.playbook.source not in {
            book.path for book in self.approved_runbooks
        }:
            raise ValueError("playbook source must reference an approved runbook")
        return self

    @property
    def fingerprint(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return sha256(canonical.encode()).hexdigest()


def _unique(items: list, label: str) -> dict:
    result = {item.id: item for item in items}
    if len(result) != len(items):
        raise ValueError(f"duplicate {label} ID")
    return result


def load_network_profile(path: Path) -> NetworkProfile:
    if not path.is_file():
        raise ValueError("network profile must be a regular file")
    with path.open("rb") as handle:
        payload = handle.read(4 * 1024 * 1024 + 1)
    if len(payload) > 4 * 1024 * 1024:
        raise ValueError("network profile exceeds the 4 MiB configuration limit")
    return NetworkProfile.model_validate(json.loads(payload, object_pairs_hook=_strict_object))


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate network profile key: {key}")
        result[key] = value
    return result
