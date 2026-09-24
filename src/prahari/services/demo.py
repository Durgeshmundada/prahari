"""Runtime service for replay and live PRAHARI telemetry modes."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import uuid4

from prahari.actions import (
    ActionAgentError,
    AgentActionRequest,
    AgentActionResponse,
    LabActionAgentClient,
)
from prahari.analytics.baseline import PredictionWithheldError, TrendBaselinePredictor
from prahari.analytics.features import FeatureExtractor
from prahari.analytics.horizon import PredictiveHorizonClassifier, join_runtime_points
from prahari.analytics.topology import TopologyCorrelator
from prahari.analytics.trained import TrainedDegradationClassifier
from prahari.config import MVPConfig, load_mvp_config, project_root
from prahari.contracts import (
    ActionAgentReceipt,
    ActionOperation,
    ActionReconciliationRequest,
    ActionResult,
    AppProbeRecord,
    AuditEvent,
    ClosureCase,
    ClosureRequest,
    ClosureReviewRequest,
    EventRecord,
    FeatureWindow,
    ImpactRisk,
    IncidentCard,
    MetricRecord,
    Prediction,
    RecoveryComparison,
    RecoveryImpactThresholds,
    RecoveryProbeWindow,
    TimeToImpact,
    TrafficDemoEndpoint,
    TrafficDemoLatency,
    TrafficDemoPayload,
    TrafficDemoStatus,
    TrafficDemoTraceStep,
    recovery_evidence_sha256,
)
from prahari.copilot.service import IncidentBrief, IncidentCopilot
from prahari.network_profile import NetworkProfile
from prahari.persistence import PersistenceStore, StoredIncidentState
from prahari.repository import load_json, load_jsonl


class PrahariService:
    """Runs a configured monitored path against persisted live or replay telemetry."""

    incident_id = "inc-branch-01-primary"
    entity_id = "tunnel:branch-01-to-dc-primary"
    playbook_id = "backup-path-shift-v1"

    def __init__(
        self,
        *,
        root: Path | None = None,
        data_mode: Literal["replay", "live"] = "replay",
        store: PersistenceStore | None = None,
        model_artifact_path: Path | None = None,
        model_manifest_path: Path | None = None,
        predictive_model_manifest_path: Path | None = None,
        copilot: IncidentCopilot | None = None,
        action_agent: LabActionAgentClient | None = None,
        network_profile: NetworkProfile | None = None,
    ) -> None:
        self._root = root or project_root()
        self._data_mode = data_mode
        self._store = store
        self._network_profile = network_profile
        if network_profile is not None:
            if data_mode == "replay":
                raise ValueError("external network profiles require live data, not the lab replay")
            if action_agent is not None:
                raise ValueError("external network profiles cannot use the controlled-lab adapter")
            self.incident_id = network_profile.incident_id
            self.entity_id = network_profile.monitored_entity_id
            self.playbook_id = network_profile.playbook.id
            self._config = network_profile.config
            self._topology = network_profile.topology.model_dump(mode="json")
            self._inventory = network_profile.inventory.model_dump(mode="json")
            self._playbook = network_profile.playbook.model_dump(mode="json")
        else:
            self._config: MVPConfig = load_mvp_config(self._root)
            self._topology = load_json(self._root / "config" / "topology.lab-v1.json")
            self._inventory = load_json(self._root / "config" / "inventory.lab-v1.json")
            self._playbook = load_json(
                self._root / "config" / "playbooks" / "backup-path-shift-v1.json"
            )
        correlator = TopologyCorrelator(self._topology, self._inventory)
        self._feature_extractor = FeatureExtractor(self._config)
        self._predictor = TrendBaselinePredictor(self._config, correlator)
        self._classifier = (
            TrainedDegradationClassifier(model_artifact_path, model_manifest_path)
            if model_artifact_path is not None and model_manifest_path is not None
            else None
        )
        self._horizon_classifier = (
            PredictiveHorizonClassifier(predictive_model_manifest_path)
            if predictive_model_manifest_path is not None
            else None
        )
        if network_profile is not None and self._horizon_classifier is not None:
            classifier = self._horizon_classifier
            if (
                classifier.sampling_seconds != self._config.sampling_seconds
                or classifier.window_seconds != self._config.window_seconds
                or classifier.minimum_samples != self._config.minimum_samples_per_metric
            ):
                raise ValueError(
                    "predictive model window policy does not match the network profile"
                )
            if classifier.activated and (
                classifier.network_profile_sha256 != network_profile.fingerprint
                or classifier.deployment_scope != network_profile.deployment_scope
            ):
                raise ValueError(
                    "predictive model approval does not bind this network profile/scope"
                )
        self._copilot = copilot
        self._action_agent = action_agent
        self._metrics: list[MetricRecord] = []
        self._app_probes: list[AppProbeRecord] = []
        self._events: list[EventRecord] = []
        self._reviewed_by: str | None = None
        self._review_note: str | None = None
        self._reviewed_prediction_id: str | None = None
        self._closure: ClosureRequest | None = None
        self._closure_prediction_id: str | None = None
        self._closure_review: ClosureReviewRequest | None = None
        self._closure_case_id: str | None = None
        self._cases: dict[str, ClosureCase] = {}
        self._audit: list[AuditEvent] = []
        self._operations: dict[str, ActionOperation] = {}
        self._operations_lock = threading.RLock()
        self._manifest: dict[str, object] = {"run_id": "live", "fixture_only": False}
        if data_mode == "replay":
            self._load_replay_fixture()

    @property
    def config(self) -> MVPConfig:
        return self._config

    @property
    def fixture_only(self) -> bool:
        return self._data_mode == "replay"

    def topology(self) -> dict[str, object]:
        return {
            "version": self._topology["version"],
            "inventory": self._inventory,
            "paths": self._topology["paths"],
            "fixture_only": self.fixture_only,
            "profile_id": self._network_profile.profile_id if self._network_profile else None,
            "profile_sha256": self._network_profile.fingerprint if self._network_profile else None,
        }

    def metric_records(
        self,
        entity_id: str | None = None,
        metric: str | None = None,
        limit: int = 1_000,
    ) -> list[MetricRecord]:
        if self._store is not None:
            return self._store.list_metrics(entity_id=entity_id, metric=metric, limit=limit)
        records = self._metrics
        if entity_id:
            records = [record for record in records if record.entity_id == entity_id]
        if metric:
            records = [record for record in records if record.metric == metric]
        return records[-limit:]

    def app_probe_records(
        self,
        entity_id: str | None = None,
        *,
        before: datetime | None = None,
        limit: int = 1_000,
    ) -> list[AppProbeRecord]:
        if self._store is not None:
            return self._store.list_app_probes(
                entity_id=entity_id,
                before=before,
                limit=limit,
            )
        records = self._app_probes
        if entity_id:
            records = [record for record in records if record.entity_id == entity_id]
        if before:
            records = [record for record in records if record.observed_at < before]
        return records[-limit:]

    def traffic_demo_status(
        self,
        *,
        path_id: str | None = None,
        limit: int = 50,
        transfer_id: str | None = None,
    ) -> TrafficDemoStatus:
        """Summarise the newest verified two-server transfer from app-probe evidence.

        The active probe record is the single durable source of both path quality and
        transfer outcome.  The API intentionally never stores or returns the file
        body; it exposes only bounded metadata, byte counts, and a checksum outcome.
        """

        selected_path = self._path_for_id(path_id) if path_id is not None else None
        source_records = self.app_probe_records(limit=min(max(limit * 20, 1_000), 5_000))
        transfer_records = [
            record
            for record in source_records
            if record.transfer is not None
            and (selected_path is None or record.path_id == selected_path["id"])
            and (transfer_id is None or record.transfer.transfer_id == transfer_id)
        ]
        transfer_records.sort(key=lambda record: record.observed_at)
        if not transfer_records:
            return TrafficDemoStatus(
                status="idle",
                path_id=str(selected_path["id"]) if selected_path is not None else None,
                message="No application transfer result has been received for this path.",
            )

        latest = transfer_records[-1]
        transfer = latest.transfer
        assert transfer is not None  # Narrowed by the filter above.
        path = selected_path or self._path_for_id(latest.path_id)
        same_transfer = [
            record
            for record in transfer_records
            if record.transfer is not None and record.transfer.transfer_id == transfer.transfer_id
        ]
        status, message = self._transfer_status(latest)
        if self._transfer_is_stale(latest):
            status = "unavailable"
            message = (
                "Latest transfer telemetry is older than the "
                f"{self._config.traffic_demo_ttl_seconds}-second freshness limit."
            )
        source, destination = self._transfer_endpoints(path, transfer.direction)
        return TrafficDemoStatus(
            status=status,
            updated_at=latest.observed_at,
            path_id=latest.path_id,
            source=source,
            destination=destination,
            transport=transfer.protocol,
            payload=TrafficDemoPayload(
                name=transfer.file_name or transfer.transfer_id,
                kind=transfer.kind,
                size_bytes=transfer.bytes_expected,
                bytes_sent=transfer.bytes_transferred,
                bytes_received=transfer.bytes_received,
                sha256=transfer.checksum_sha256,
                integrity=transfer.integrity,
            ),
            latency_ms=_chat_latency_summary(transfer),
            throughput_mbps=_transfer_throughput_mbps(same_transfer),
            loss_percent=_chat_message_loss_percent(transfer),
            trace=_transfer_trace(latest, status),
            message=message,
        )

    def event_records(
        self,
        entity_id: str | None = None,
        *,
        before: datetime | None = None,
        limit: int = 1_000,
    ) -> list[EventRecord]:
        if self._store is not None:
            return self._store.list_events(entity_id=entity_id, before=before, limit=limit)
        records = self._events
        if entity_id:
            records = [record for record in records if record.entity_id == entity_id]
        if before:
            records = [record for record in records if record.observed_at < before]
        return records[-limit:]

    def ingest_metrics(self, records: list[MetricRecord], *, run_id: str | None = None) -> int:
        self._validate_metric_records(records)
        if self._store is not None:
            return self._store.save_metrics(records, run_id=run_id)
        self._metrics.extend(records)
        return len(records)

    def ingest_app_probes(self, records: list[AppProbeRecord], *, run_id: str | None = None) -> int:
        self._validate_app_probe_records(records)
        if self._store is not None:
            return self._store.save_app_probes(records, run_id=run_id)
        self._app_probes.extend(records)
        return len(records)

    def ingest_events(self, records: list[EventRecord], *, run_id: str | None = None) -> int:
        self._validate_event_records(records)
        if self._store is not None:
            return self._store.save_events(records, run_id=run_id)
        self._events.extend(records)
        return len(records)

    def feature_window(self):
        return self._feature_extractor.extract(
            self.metric_records(entity_id=self.entity_id),
            self.entity_id,
            reference_time=None if self.fixture_only else datetime.now(UTC),
            expected_topology_version=str(self._topology["version"]),
            expected_config_version=self._config.telemetry_config_version,
        )

    def prediction(self) -> Prediction:
        features = self.feature_window()
        prediction = self._score_prediction(features)
        model = self._horizon_classifier or self._classifier
        prediction = prediction.model_copy(
            update={
                "network_profile_sha256": (
                    self._network_profile.fingerprint if self._network_profile else None
                ),
                "model_bundle_sha256": model.bundle_sha256 if model else None,
            }
        )
        evidence = {
            "features": features.model_dump(mode="json"),
            "prediction": prediction.model_dump(mode="json"),
        }
        fingerprint = sha256(json.dumps(evidence, sort_keys=True).encode("utf-8")).hexdigest()
        return prediction.model_copy(update={"prediction_id": f"pred-{fingerprint}"})

    def _score_prediction(self, features: FeatureWindow) -> Prediction:
        prediction = self._predictor.predict(features)
        if self._horizon_classifier is not None and self._horizon_classifier.activated:
            try:
                points = join_runtime_points(
                    self.metric_records(entity_id=self.entity_id),
                    self.app_probe_records(),
                    entity_id=self.entity_id,
                    path_id=self._incident_path_id(),
                    topology_version=str(self._topology["version"]),
                    config_version=self._config.telemetry_config_version,
                    app_probe_entities=self._path_for_id(self._incident_path_id())[
                        "app_probe_entities"
                    ],
                )
                score = self._horizon_classifier.score(points)
            except ValueError as error:
                raise PredictionWithheldError(
                    f"prediction withheld: predictive-v2 evidence is unusable: {error}"
                ) from error
            risk = ImpactRisk(
                within_30_seconds=score.probabilities[30],
                within_60_seconds=score.probabilities[60],
                within_120_seconds=score.probabilities[120],
            )
            alert_horizon = next(
                (horizon for horizon in (30, 60, 120) if score.alerts[horizon]),
                None,
            )
            if alert_horizon == 30:
                time_to_impact = TimeToImpact(
                    range="0-30 seconds",
                    basis="approved calibrated 30-second application-impact horizon",
                )
            elif alert_horizon == 60:
                time_to_impact = TimeToImpact(
                    range="30-60 seconds",
                    basis="approved calibrated 60-second application-impact horizon",
                )
            elif alert_horizon == 120:
                time_to_impact = TimeToImpact(
                    range="60-120 seconds",
                    basis="approved calibrated 120-second application-impact horizon",
                )
            else:
                time_to_impact = TimeToImpact(
                    range="no calibrated impact interval",
                    basis="all approved horizon probabilities are below their alert thresholds",
                )
            risk_120 = risk.within_120_seconds
            any_horizon_alert = any(score.alerts.values())
            return prediction.model_copy(
                update={
                    "model_version": score.model_version,
                    "likely_incident_type": (
                        "tunnel_degradation" if any_horizon_alert else "normal"
                    ),
                    "incident_type_probabilities": {
                        "normal": 1 - risk_120,
                        "tunnel_degradation": risk_120,
                    },
                    "impact_risk": risk,
                    "time_to_impact": time_to_impact,
                    "confidence": max(risk_120, 1 - risk_120),
                    "risk_status": "calibrated",
                    "evidence": [
                        *prediction.evidence,
                        (
                            f"Approved {score.model_version} calibrated horizon risks: "
                            f"30s={score.probabilities[30]:.3f}, "
                            f"60s={score.probabilities[60]:.3f}, "
                            f"120s={score.probabilities[120]:.3f}."
                        ),
                    ],
                }
            )
        if self._classifier is None:
            return prediction
        score = self._classifier.score(self.metric_records(entity_id=self.entity_id))
        evidence = [
            *prediction.evidence,
            (
                f"Experimental {score.model_version} current-state probability "
                f"{score.probability:.3f} at threshold {score.threshold:.2f}; "
                "not used as forecast risk."
            ),
        ]
        return prediction.model_copy(update={"evidence": evidence})

    def readiness(self) -> tuple[bool, str]:
        """Report whether this mode has safe input for the incident workflow."""

        if self.fixture_only:
            return True, "replay fixture loaded"
        try:
            feature_window = self.feature_window()
        except ValueError as error:
            return False, str(error)
        if feature_window.data_quality.status != "good":
            reasons = "; ".join(feature_window.data_quality.reasons)
            return False, reasons or "live telemetry quality is not good"
        if not self._has_fresh_valid_app_probe():
            return False, "no fresh valid application probe exists for the incident path"
        return True, "live telemetry is current and complete"

    def incident(self) -> IncidentCard:
        if not self.fixture_only:
            is_ready, reason = self.readiness()
            if not is_ready:
                raise PredictionWithheldError(
                    f"prediction withheld: live telemetry is not ready: {reason}"
                )
        prediction = self.prediction()
        risk = prediction.impact_risk.within_120_seconds
        state_record = self._incident_state()
        app_impact_started = self._app_impact_started()
        current_closure = (
            state_record.closure
            if state_record.closure_prediction_id == prediction.prediction_id
            else None
        )
        unconfirmed_action = self.has_unconfirmed_action()
        if app_impact_started or unconfirmed_action:
            state = "escalated"
        elif current_closure is not None:
            state = _state_for_closure(current_closure.outcome, state_record.closure_review)
        elif (
            state_record.reviewed_by is not None
            and state_record.reviewed_prediction_id == prediction.prediction_id
        ):
            state = "reviewed"
        elif risk >= self._active_alert_threshold():
            state = "predicted"
        else:
            state = "watch"

        severity = (
            "high"
            if app_impact_started or unconfirmed_action or risk >= 0.8
            else "warning"
            if risk >= 0.65
            else "informational"
        )
        root = prediction.root_location_candidates[0]
        path_name = self._path_for_id(self._incident_path_id())["name"]
        if unconfirmed_action:
            summary = (
                f"An action on {path_name} is unconfirmed; independent reconciliation is required."
            )
        elif app_impact_started:
            summary = (
                f"Application impact is already present on {path_name}; "
                "this incident requires active operator escalation rather than a pre-impact claim."
            )
        else:
            score_kind = (
                "calibrated impact risk"
                if prediction.risk_status == "calibrated"
                else "heuristic impact score"
            )
            summary = f"{path_name}: {score_kind} is {risk:.0%} within 120 seconds."
        return IncidentCard(
            id=self.incident_id,
            state=state,
            severity=severity,
            summary=summary,
            prediction=prediction,
            location=root,
            data_quality=prediction.data_quality,
            evidence_ids=self._evidence_ids(),
            playbook_status=self._playbook["status"],
        )

    def list_incidents(self) -> list[IncidentCard]:
        if not self.fixture_only:
            is_ready, reason = self.readiness()
            if not is_ready:
                raise PredictionWithheldError(
                    f"prediction withheld: live telemetry is not ready: {reason}"
                )
        return [self.incident()]

    def incident_detail(self, incident_id: str) -> dict[str, object]:
        self._require_incident(incident_id)
        workflow = self._incident_state()
        return {
            "incident": self.incident(),
            "feature_window": self.feature_window(),
            "device_dispatch_enabled": self.device_dispatch_enabled,
            "run_manifest": self._manifest,
            "playbook": self._playbook,
            "app_impact_started": self._app_impact_started(),
            "audit_trail": self._audit_trail(),
            "fixture_only": self.fixture_only,
            "workflow": {
                "unconfirmed_action": self.has_unconfirmed_action(),
                "reviewed_by": workflow.reviewed_by,
                "reviewed_prediction_id": workflow.reviewed_prediction_id,
                "closure_case_id": workflow.closure_case_id,
                "closure": workflow.closure,
                "closure_review": workflow.closure_review,
                "closure_prediction_id": workflow.closure_prediction_id,
            },
        }

    def incident_brief(
        self,
        incident_id: str,
        actor_id: str = "local-copilot",
        *,
        prediction_id: str | None = None,
    ) -> IncidentBrief:
        self._require_incident(incident_id)
        if self._copilot is None:
            raise RuntimeError("local copilot is disabled")
        captured = self.incident()
        if prediction_id is not None and captured.prediction.prediction_id != prediction_id:
            raise ValueError("prediction evidence changed; refresh before generating a brief")
        brief = self._copilot.brief(captured)
        from prahari.copilot.grounding import snapshot_digest
        from prahari.copilot.service import CopilotUnavailableError

        if brief.grounding.incident_sha256 != snapshot_digest(captured.model_dump(mode="json")):
            raise CopilotUnavailableError("brief is not bound to the captured incident evidence")

        # Keep collection running while the model selects statements. Preserve
        # the immutable verified input and mark supersession. Reviews and device
        # dispatch continue to require the current prediction independently.
        try:
            current = self.incident()
            evidence_current = brief.grounding.incident_sha256 == snapshot_digest(
                current.model_dump(mode="json")
            )
        except PredictionWithheldError:
            evidence_current = False
        brief = brief.model_copy(update={"evidence_current_at_completion": evidence_current})
        citations = ", ".join(citation.source_id for citation in brief.citations)
        self._append_audit(
            incident_id,
            actor_id,
            "copilot_brief_generated",
            (
                f"Schema {brief.schema_version}; "
                f"evidence SHA-256 {brief.grounding.incident_sha256}; "
                f"grounding {brief.grounding.mode}; citations: {citations}; "
                f"current at completion: {evidence_current}; "
                f"recommended playbook: {brief.recommended_playbook_id or 'none'}; "
                f"confidence: {brief.confidence:.3f}."
            ),
        )
        return brief

    def review(
        self, incident_id: str, actor_id: str, note: str, *, prediction_id: str | None = None
    ) -> IncidentCard:
        self._require_incident(incident_id)
        self._require_no_unconfirmed_action()
        self._require_workflow_ready()
        prediction_id = self._require_prediction(prediction_id)
        if self._store is not None:
            self._store.review_with_audit(incident_id, actor_id, note, prediction_id)
        else:
            self._reviewed_by = actor_id
            self._review_note = note
            self._reviewed_prediction_id = prediction_id
            self._closure = None
            self._closure_prediction_id = None
            self._closure_review = None
            self._closure_case_id = None
            self._append_audit(
                incident_id, actor_id, "incident_reviewed", f"Prediction {prediction_id}. {note}"
            )
        return self.incident()

    def dry_run_action(
        self,
        incident_id: str,
        playbook_id: str,
        actor_id: str,
        *,
        prediction_id: str | None = None,
    ) -> ActionResult:
        self._require_incident(incident_id)
        self._require_no_unconfirmed_action()
        self._require_playbook(playbook_id)
        self._require_workflow_ready()
        prediction_id = self._require_prediction(prediction_id)
        if self._action_agent is None:
            event = self._append_audit(
                incident_id,
                actor_id,
                "dry_run_blocked",
                "No approved device adapter is configured; dry-run and approval remain blocked.",
            )
            return ActionResult(
                playbook_id=playbook_id,
                status="blocked",
                message=("Blocked: no approved device adapter exists for this deployment."),
                audit_event=event,
            )
        self._require_reviewed(actor_id, prediction_id)
        operation_id = f"dry-run-{uuid4().hex}"
        response = self._action_agent.dry_run(
            AgentActionRequest(
                operation_id=operation_id,
                incident_id=incident_id,
                playbook_id=playbook_id,
                actor_id=actor_id,
                prediction_id=prediction_id,
            )
        )
        if response.operation_id != operation_id:
            raise ActionAgentError("action agent receipt does not match the dry-run request")
        event_type = (
            "action_dry_run_passed" if response.status == "dry_run_passed" else "dry_run_blocked"
        )
        event = self._append_audit(
            incident_id,
            actor_id,
            event_type,
            f"Dry-run {operation_id}. {response.message}",
        )
        return ActionResult(
            playbook_id=playbook_id,
            status=response.status,
            message=response.message,
            audit_event=event,
        )

    def approve_action(
        self,
        incident_id: str,
        playbook_id: str,
        actor_id: str,
        note: str,
        *,
        prediction_id: str | None = None,
    ) -> ActionResult:
        self._require_incident(incident_id)
        self._require_playbook(playbook_id)
        self._require_workflow_ready()
        prediction_id = self._require_prediction(prediction_id)
        if self._action_agent is None:
            event = self._append_audit(
                incident_id,
                actor_id,
                "action_approval_blocked",
                (
                    "Approval was rejected because no successful dry-run or reviewed action "
                    f"adapter exists. Operator note: {note}"
                ),
            )
            return ActionResult(
                playbook_id=playbook_id,
                status="blocked",
                message=(
                    "Blocked: approval cannot substitute for a configured, successful dry-run. "
                    "No network change was attempted."
                ),
                audit_event=event,
            )
        self._require_reviewed(actor_id, prediction_id)
        operation = self._begin_operation("apply", actor_id, note, prediction_id)
        try:
            self._require_reviewed(actor_id, prediction_id)
        except Exception:
            self._finish_operation(
                operation.model_copy(
                    update={
                        "status": "finished",
                        "result_status": "blocked",
                        "updated_at": datetime.now(UTC),
                        "message": "Evidence changed before dispatch; no network call was made.",
                    }
                ),
                "action_dispatch_cancelled",
            )
            raise
        return self._dispatch_operation(operation)

    def rollback_action(
        self, incident_id: str, playbook_id: str, actor_id: str, note: str
    ) -> ActionResult:
        self._require_incident(incident_id)
        self._require_playbook(playbook_id)
        self._require_workflow_ready()
        if self._action_agent is None:
            event = self._append_audit(
                incident_id, actor_id, "rollback_blocked", "No action adapter is configured."
            )
            return ActionResult(
                playbook_id=playbook_id,
                status="blocked",
                message="Rollback blocked: no action adapter is configured.",
                audit_event=event,
            )
        return self._dispatch_operation(
            self._begin_operation(
                "rollback",
                actor_id,
                note,
                self.prediction().prediction_id,
            )
        )

    def _begin_operation(
        self,
        kind: Literal["apply", "rollback"],
        actor_id: str,
        note: str,
        prediction_id: str,
    ) -> ActionOperation:
        now = datetime.now(UTC)
        operation = ActionOperation(
            id=f"action-{uuid4().hex}",
            incident_id=self.incident_id,
            playbook_id=self.playbook_id,
            prediction_id=prediction_id,
            prediction_snapshot=self.prediction(),
            kind=kind,
            actor_id=actor_id,
            note=note,
            created_at=now,
            updated_at=now,
            status="pending",
        )
        if operation.prediction_snapshot.prediction_id != prediction_id:
            raise ValueError("prediction changed while capturing the action snapshot")
        if self._store is not None:
            self._store.begin_action(operation)
        else:
            with self._operations_lock:
                if any(item.status != "finished" for item in self._operations.values()):
                    raise ValueError("another unconfirmed action owns this incident")
                if any(
                    item.prediction_id == prediction_id and item.kind == kind
                    for item in self._operations.values()
                ):
                    raise ValueError("an operation already exists for this prediction and kind")
                self._append_audit(
                    self.incident_id,
                    actor_id,
                    "action_apply_requested" if kind == "apply" else "rollback_requested",
                    f"Operation {operation.id}; prediction {prediction_id}. {note}",
                )
                self._operations[operation.id] = operation
        return operation

    def _finish_operation(self, operation: ActionOperation, event_type: str) -> AuditEvent:
        operation = ActionOperation.model_validate(operation.model_dump())
        if self._store is not None:
            return self._store.finish_action(operation, event_type)
        with self._operations_lock:
            if self._operations[operation.id].status != "pending":
                raise ValueError("action was already finalized or reconciled")
            event = self._append_audit(
                self.incident_id,
                operation.actor_id,
                event_type,
                f"Operation {operation.id}. {operation.message or ''}",
            )
            self._operations[operation.id] = operation
            return event

    def _dispatch_operation(self, operation: ActionOperation) -> ActionResult:
        assert self._action_agent is not None
        request = AgentActionRequest(
            operation_id=operation.id,
            incident_id=operation.incident_id,
            playbook_id=operation.playbook_id,
            actor_id=operation.actor_id,
            prediction_id=operation.prediction_id,
        )
        receipt = None
        try:
            method = (
                self._action_agent.apply
                if operation.kind == "apply"
                else self._action_agent.rollback
            )
            response = AgentActionResponse.model_validate(method(request))
            if response.operation_id != operation.id:
                raise ActionAgentError(
                    "action agent receipt does not match the requested operation"
                )
            receipt = ActionAgentReceipt.model_validate(response.model_dump())
            allowed = {
                "blocked",
                "validation_failed",
                "applied" if operation.kind == "apply" else "rolled_back",
            }
            if response.status not in allowed:
                raise ActionAgentError("adapter returned an unexpected operation result")
            expected_route = "backup" if operation.kind == "apply" else "primary"
            if (
                response.status in {"applied", "rolled_back"}
                and response.observed_route != expected_route
            ):
                raise ActionAgentError("adapter success was not supported by the observed route")
        except Exception as error:
            message = (
                str(error)
                if isinstance(error, ActionAgentError)
                else "adapter failed; outcome could not be confirmed"
            )
            unknown = operation.model_copy(
                update={
                    "status": "unknown",
                    "updated_at": datetime.now(UTC),
                    "message": message,
                    "agent_receipt": receipt,
                }
            )
            try:
                self._finish_operation(unknown, "action_outcome_unknown")
            except Exception as storage_error:
                raise ActionAgentError(
                    f"Operation {operation.id}: outcome unknown; intent remains unconfirmed."
                ) from storage_error
            raise ActionAgentError(f"Operation {operation.id}: {message}") from error
        unknown = response.status == "validation_failed"
        completed = operation.model_copy(
            update={
                "status": "unknown" if unknown else "finished",
                "result_status": response.status,
                "agent_receipt": receipt,
                "observed_route": response.observed_route,
                "message": response.message,
                "updated_at": datetime.now(UTC),
            }
        )
        event_type = (
            "action_outcome_unknown"
            if unknown
            else "action_applied"
            if response.status == "applied"
            else "action_rolled_back"
            if response.status == "rolled_back"
            else "action_approval_blocked"
        )
        try:
            event = self._finish_operation(completed, event_type)
        except Exception as error:
            raise ActionAgentError(
                f"Operation {operation.id}: adapter replied but its result could not be committed; "
                "outcome requires reconciliation."
            ) from error
        return ActionResult(
            playbook_id=operation.playbook_id,
            status=response.status,
            message=f"Operation {operation.id}: {response.message}",
            audit_event=event,
        )

    @property
    def device_dispatch_enabled(self) -> bool:
        return self._action_agent is not None

    def has_unconfirmed_action(self) -> bool:
        if self._store is not None:
            return self._store.has_unconfirmed_action(self.incident_id)
        with self._operations_lock:
            return any(item.status != "finished" for item in self._operations.values())

    def operational_storage_health(self) -> tuple[bool, int]:
        if self._store is None:
            return False, int(self.has_unconfirmed_action())
        return self._store.healthcheck(), int(self.has_unconfirmed_action())

    def _require_no_unconfirmed_action(self) -> None:
        if self.has_unconfirmed_action():
            raise ValueError("unconfirmed action must be reconciled before changing this workflow")

    def list_action_operations(self) -> list[ActionOperation]:
        if self._store is not None:
            return self._store.list_actions(self.incident_id)
        with self._operations_lock:
            return sorted(
                self._operations.values(), key=lambda item: item.created_at, reverse=True
            )[:100]

    def reconcile_action(
        self,
        operation_id: str,
        request: ActionReconciliationRequest,
    ) -> ActionOperation:
        if self.device_dispatch_enabled:
            raise ValueError(
                "disable the device adapter and quiesce dispatch before reconciliation"
            )
        if not any(item.id == operation_id for item in self.list_action_operations()):
            raise KeyError("unknown action operation for this deployment")
        if self._store is not None:
            return self._store.reconcile_action(operation_id, request)
        with self._operations_lock:
            operation = self._operations[operation_id]
            if operation.actor_id == request.actor_id:
                raise ValueError("action reconciliation requires an independent reviewer")
            if operation.status == "finished":
                raise ValueError("action was already finalized or reconciled")
            updated = ActionOperation.model_validate(
                operation.model_copy(
                    update={
                        "status": "finished",
                        "updated_at": datetime.now(UTC),
                        "reconciliation": request,
                        "observed_route": request.observed_route,
                    }
                ).model_dump()
            )
            self._append_audit(
                self.incident_id,
                request.actor_id,
                "action_reconciled",
                f"Operation {operation_id}; evidence {request.evidence_reference}; "
                f"SHA-256 {request.evidence_sha256}. {request.note}",
            )
            self._operations[operation_id] = updated
            return updated

    def close(self, incident_id: str, request: ClosureRequest) -> IncidentCard:
        self._require_incident(incident_id)
        self._require_no_unconfirmed_action()
        self._require_workflow_ready()
        prediction_id = self._require_prediction(request.prediction_id)
        self._require_reviewed(request.actor_id, prediction_id)
        if request.outcome in {"resolved", "false_positive"} and self._app_impact_started():
            raise PredictionWithheldError(
                "closure cannot claim resolution while application impact is still present"
            )
        if (
            not self.fixture_only
            and request.outcome == "resolved"
            and request.intervention_at is None
        ):
            raise PredictionWithheldError(
                "live resolved closure requires an intervention time for measured recovery evidence"
            )
        recovery_comparison = (
            self._recovery_comparison(request.intervention_at)
            if request.intervention_at is not None
            else None
        )
        if (
            not self.fixture_only
            and request.outcome == "resolved"
            and not recovery_comparison.recovered
        ):
            raise PredictionWithheldError(
                "closure cannot claim resolution without a sustained measured recovery"
            )
        case = ClosureCase(
            id=f"case-{uuid4().hex}",
            incident_id=incident_id,
            prediction_id=prediction_id,
            created_at=datetime.now(UTC),
            submission=request,
            incident=self.incident(),
            feature_window=self.feature_window(),
            app_impact_started=self._app_impact_started(),
            recovery_comparison=recovery_comparison,
        )
        if case.incident.prediction.prediction_id != prediction_id:
            raise ValueError("prediction evidence changed while capturing the closure snapshot")
        self._require_reviewed(request.actor_id, prediction_id)
        if self._store is not None:
            self._store.close_with_audit(incident_id, request, prediction_id, case=case)
        else:
            self._closure = request
            self._closure_prediction_id = prediction_id
            self._closure_review = None
            self._closure_case_id = case.id
            self._cases[case.id] = case
            self._append_audit(
                incident_id,
                request.actor_id,
                "closure_submitted",
                f"Prediction {prediction_id}. {request.outcome}: {request.note}",
            )
        return self.incident()

    def review_closure(
        self,
        incident_id: str,
        request: ClosureReviewRequest,
    ) -> IncidentCard:
        self._require_incident(incident_id)
        self._require_workflow_ready()
        prediction_id = self.prediction().prediction_id
        state = self._incident_state()
        if state.closure is None or state.closure_prediction_id != prediction_id:
            raise PredictionWithheldError(
                "closure case does not match the current prediction evidence"
            )
        if request.actor_id == state.closure.actor_id:
            raise ValueError("closure must be reviewed by a different actor than its submitter")
        if (
            request.decision == "approved"
            and state.closure.outcome in {"resolved", "false_positive"}
            and self._app_impact_started()
        ):
            raise PredictionWithheldError(
                "closure review cannot approve recovery while application impact is present"
            )
        if state.closure_case_id is not None:
            self.decide_closure_case(state.closure_case_id, request)
            return self.incident()
        if self._store is not None:
            self._store.review_closure_with_audit(incident_id, request, prediction_id)
        else:
            self._closure_review = request
            self._append_audit(
                incident_id,
                request.actor_id,
                f"closure_review_{request.decision}",
                f"Prediction {prediction_id}. {request.note}",
            )
        return self.incident()

    def closure_case(self, case_id: str) -> ClosureCase:
        if self._store is not None:
            return self._store.closure_case(case_id)
        if case_id not in self._cases:
            raise KeyError(f"unknown closure case {case_id!r}")
        return self._cases[case_id]

    def list_closure_cases(
        self,
        *,
        status: str | None = None,
        outcome: str | None = None,
        incident_type: str | None = None,
        entity_id: str | None = None,
        network_profile_sha256: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ClosureCase]:
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError("case limit must be 1-200 and offset must be non-negative")
        if status not in {None, "pending", "approved", "rejected"}:
            raise ValueError("unknown closure case status")
        if self._store is not None:
            return self._store.list_closure_cases(
                status=status,
                outcome=outcome,
                incident_type=incident_type,
                entity_id=entity_id,
                network_profile_sha256=network_profile_sha256,
                limit=limit,
                offset=offset,
            )
        cases = [
            case
            for case in self._cases.values()
            if (status is None or (case.review.decision if case.review else "pending") == status)
            and (outcome is None or case.submission.outcome == outcome)
            and (
                incident_type is None
                or case.incident.prediction.likely_incident_type == incident_type
            )
            and (entity_id is None or case.incident.prediction.entity_id == entity_id)
            and (
                network_profile_sha256 is None
                or case.incident.prediction.network_profile_sha256 == network_profile_sha256
            )
        ]
        return sorted(cases, key=lambda case: (case.created_at, case.id), reverse=True)[
            offset : offset + limit
        ]

    def decide_closure_case(self, case_id: str, request: ClosureReviewRequest) -> ClosureCase:
        case = self.closure_case(case_id)
        if request.actor_id == case.submission.actor_id:
            raise ValueError("closure must be reviewed by a different actor than its submitter")
        if case.review is not None:
            raise ValueError("closure case already has a final review")
        if (
            request.decision == "approved"
            and case.recovery_comparison is not None
            and case.recovery_comparison.schema_version == "recovery-comparison-v1"
        ):
            raise PredictionWithheldError(
                "legacy recovery evidence cannot be approved; submit a v2 comparison"
            )
        current = self._incident_state()
        if (
            current.closure_case_id == case_id
            and request.decision == "approved"
            and case.submission.outcome in {"resolved", "false_positive"}
        ):
            self._require_workflow_ready()
            if self._app_impact_started():
                raise PredictionWithheldError(
                    "closure review cannot approve recovery while application impact is present"
                )
            if (
                not self.fixture_only
                and case.submission.outcome == "resolved"
                and (
                    case.recovery_comparison is None
                    or not case.recovery_comparison.recovered
                )
            ):
                raise PredictionWithheldError(
                    "closure review cannot approve recovery without measured before/after evidence"
                )
        if self._store is not None:
            return self._store.decide_closure_case(case_id, request)
        reviewed = case.model_copy(update={"review": request, "reviewed_at": datetime.now(UTC)})
        self._append_audit(
            case.incident_id,
            request.actor_id,
            f"closure_review_{request.decision}",
            f"Case {case_id}; prediction {case.prediction_id}. {request.note}",
        )
        self._cases[case_id] = reviewed
        if current.closure_case_id == case_id:
            self._closure_review = request
        return reviewed

    def model_info(self) -> dict[str, object]:
        info: dict[str, object] = {
            "model_version": self._config.trend_policy.model_version,
            "model_type": "deterministic trend baseline",
            "training_status": "heuristic baseline only",
            "feature_version": self._feature_extractor.feature_version,
            "scenario_version": self._config.scenario_version,
            "impact_policy_version": self._config.impact_policy.version,
            "fixture_only": self.fixture_only,
            "data_mode": self._data_mode,
        }
        if self._classifier is not None:
            info.update(
                {
                    "classifier_version": self._classifier.model_version,
                    "classifier_type": "LightGBM current-state degradation classifier",
                    "classifier_approval_status": self._classifier.approval_status,
                    "training_status": "experimental current-state classifier registered",
                }
            )
        if self._horizon_classifier is not None:
            info.update(
                {
                    "model_version": self._horizon_classifier.model_version,
                    "model_type": "calibrated LightGBM future-impact horizons",
                    "feature_version": "predictive-window-v2",
                    "model_approval_status": self._horizon_classifier.approval_status,
                    "model_deployment_scope": self._horizon_classifier.deployment_scope,
                    "training_status": (
                        "registered and evaluated"
                        if self._horizon_classifier.activated
                        else "predictive candidate evaluated; approval required"
                    ),
                }
            )
        return info

    def _active_alert_threshold(self) -> float:
        if self._horizon_classifier is not None and self._horizon_classifier.activated:
            return self._horizon_classifier.thresholds[120]
        return self._config.trend_policy.alert_risk_within_120_seconds

    def _load_replay_fixture(self) -> None:
        fixture_root = self._root / "fixtures" / "gradual-loss"
        metrics = load_jsonl(fixture_root / "metrics.jsonl", MetricRecord)
        probes = load_jsonl(fixture_root / "app-probes.jsonl", AppProbeRecord)
        self._manifest = load_json(fixture_root / "fault-manifest.json")
        self._validate_metric_records(metrics)
        self._validate_app_probe_records(probes)
        run_id = str(self._manifest["run_id"])
        if self._store is not None:
            self._store.replace_replay_fixture(metrics, probes, run_id=run_id)
        else:
            self._metrics = metrics
            self._app_probes = probes

    def _validate_metric_records(self, records: list[MetricRecord]) -> None:
        known_entities = {
            entity_id
            for path in self._topology["paths"]
            for entity_id in path["monitored_entity_ids"]
        }
        topology_version = str(self._topology["version"])
        for record in records:
            if record.entity_id not in known_entities:
                raise ValueError(
                    f"metric entity is not in the current topology: {record.entity_id}"
                )
            if record.topology_version != topology_version:
                raise ValueError(
                    f"metric topology version {record.topology_version!r} is not "
                    f"{topology_version!r}"
                )
            if record.config_version != self._config.telemetry_config_version:
                raise ValueError(
                    f"metric configuration version {record.config_version!r} is not "
                    f"{self._config.telemetry_config_version!r}"
                )
            policy = self._config.trend_policy.metrics.get(record.metric)
            if policy is None:
                policy = self._config.observability_metrics.get(record.metric)
            if policy is None:
                raise ValueError(f"metric is not enabled for the MVP: {record.metric}")
            if record.unit != policy.unit:
                raise ValueError(
                    f"metric {record.metric!r} must use {policy.unit!r}, not {record.unit!r}"
                )
            if record.value < policy.minimum_value or (
                policy.maximum_value is not None and record.value > policy.maximum_value
            ):
                raise ValueError(f"metric {record.metric!r} is outside its physical policy bounds")
            if not self.fixture_only and record.observed_at > _future_limit(
                self._config.max_future_skew_ms
            ):
                raise ValueError("metric observation is beyond the allowed future clock skew")

    def _validate_event_records(self, records: list[EventRecord]) -> None:
        topology_version = str(self._topology["version"])
        known_entities = self._known_event_entities()
        for record in records:
            if record.entity_id not in known_entities:
                raise ValueError(f"event entity is not in the current topology: {record.entity_id}")
            unknown_related = sorted(set(record.related_entity_ids) - known_entities)
            if unknown_related:
                raise ValueError(
                    "event related entities are not in the current topology: "
                    + ", ".join(unknown_related)
                )
            if record.topology_version != topology_version:
                raise ValueError(
                    f"event topology version {record.topology_version!r} is not "
                    f"{topology_version!r}"
                )
            if record.config_version != self._config.telemetry_config_version:
                raise ValueError(
                    f"event configuration version {record.config_version!r} is not "
                    f"{self._config.telemetry_config_version!r}"
                )
            if not self.fixture_only and record.observed_at > _future_limit(
                self._config.max_future_skew_ms
            ):
                raise ValueError("event observation is beyond the allowed future clock skew")

    def _validate_app_probe_records(self, records: list[AppProbeRecord]) -> None:
        paths_by_id = {path["id"]: path for path in self._topology["paths"]}
        topology_version = str(self._topology["version"])
        for record in records:
            path = paths_by_id.get(record.path_id)
            if path is None:
                raise ValueError(f"app probe path is not in the current topology: {record.path_id}")
            expected_entity = path["app_probe_entities"].get(record.probe_name)
            if expected_entity != record.entity_id:
                raise ValueError(
                    "app probe entity/probe name does not match the configured path mapping"
                )
            if record.topology_version != topology_version:
                raise ValueError(
                    f"app probe topology version {record.topology_version!r} is not "
                    f"{topology_version!r}"
                )
            if record.config_version != self._config.telemetry_config_version:
                raise ValueError(
                    f"app probe configuration version {record.config_version!r} is not "
                    f"{self._config.telemetry_config_version!r}"
                )
            if not self.fixture_only and record.observed_at > _future_limit(
                self._config.max_future_skew_ms
            ):
                raise ValueError("app probe observation is beyond the allowed future clock skew")

    def _incident_state(self) -> StoredIncidentState:
        if self._store is not None:
            return self._store.incident_state(self.incident_id)
        return StoredIncidentState(
            reviewed_by=self._reviewed_by,
            review_note=self._review_note,
            reviewed_prediction_id=self._reviewed_prediction_id,
            closure=self._closure,
            closure_prediction_id=self._closure_prediction_id,
            closure_review=self._closure_review,
            closure_case_id=self._closure_case_id,
        )

    def _app_impact_started(self) -> bool:
        probes = sorted(
            (
                probe
                for probe in self.app_probe_records()
                if probe.path_id == self._incident_path_id()
                and self._has_valid_app_probe_mapping(probe)
                and probe.quality.source_healthy
                and not probe.quality.counter_reset
                and probe.quality.late_by_ms <= self._config.max_late_by_ms
            ),
            key=lambda probe: probe.observed_at,
        )
        if not probes:
            return False
        thresholds = self._config.impact_policy.metrics
        latest_probe = probes[-1]
        if not self.fixture_only:
            reference_time = datetime.now(UTC)
            if latest_probe.observed_at > _future_limit(self._config.max_future_skew_ms):
                return False
            staleness_seconds = (reference_time - latest_probe.observed_at).total_seconds()
            if staleness_seconds > self._config.sampling_seconds * 2:
                return False
        if not _probe_breaches_threshold(latest_probe, thresholds):
            return False
        breach_start = latest_probe.observed_at
        max_gap_seconds = self._config.sampling_seconds * 2
        for probe in reversed(probes[:-1]):
            gap_seconds = (breach_start - probe.observed_at).total_seconds()
            if gap_seconds > max_gap_seconds or not _probe_breaches_threshold(probe, thresholds):
                break
            breach_start = probe.observed_at
        sustained_seconds = (latest_probe.observed_at - breach_start).total_seconds()
        return sustained_seconds >= self._config.impact_policy.sustained_seconds

    def _recovery_comparison(self, intervention_at: datetime) -> RecoveryComparison:
        now = datetime.now(UTC)
        if (
            not self.fixture_only
            and intervention_at > _future_limit(self._config.max_future_skew_ms)
        ):
            raise PredictionWithheldError("recovery intervention time is in the future")
        valid = sorted(
            (
                probe
                for probe in self.app_probe_records(limit=5_000)
                if probe.path_id == self._incident_path_id()
                and self._has_valid_app_probe_mapping(probe)
                and probe.quality.source_healthy
                and not probe.quality.counter_reset
                and probe.quality.late_by_ms <= self._config.max_late_by_ms
                and (self.fixture_only or probe.observed_at <= now)
            ),
            key=lambda probe: probe.observed_at,
        )
        window = timedelta(seconds=self._config.window_seconds)
        before = [
            probe
            for probe in valid
            if intervention_at - window <= probe.observed_at < intervention_at
        ]
        after = [
            probe
            for probe in valid
            if intervention_at <= probe.observed_at <= intervention_at + window
        ]
        required = self._config.impact_policy.sustained_seconds
        before_window = self._recovery_probe_window(before, expect_impact=True, required=required)
        impacted_streams = {
            (sample.probe_name, sample.entity_id) for sample in before_window.samples
        }
        after_window = self._recovery_probe_window(
            after,
            expect_impact=False,
            required=required,
            required_streams=impacted_streams,
        )
        schema_version = "recovery-comparison-v2"
        impact_thresholds = RecoveryImpactThresholds.model_validate(
            self._config.impact_policy.metrics
        )
        recovered = before_window.impact_detected and after_window.within_policy
        evidence_sha256 = recovery_evidence_sha256(
            schema_version=schema_version,
            path_id=self._incident_path_id(),
            intervention_at=intervention_at,
            impact_policy_version=self._config.impact_policy.version,
            impact_thresholds=impact_thresholds,
            required_sustained_seconds=required,
            before=before_window,
            after=after_window,
            recovered=recovered,
        )
        return RecoveryComparison(
            schema_version=schema_version,
            path_id=self._incident_path_id(),
            intervention_at=intervention_at,
            impact_policy_version=self._config.impact_policy.version,
            impact_thresholds=impact_thresholds,
            required_sustained_seconds=required,
            before=before_window,
            after=after_window,
            recovered=recovered,
            evidence_sha256=evidence_sha256,
        )

    def _recovery_probe_window(
        self,
        probes: list[AppProbeRecord],
        *,
        expect_impact: bool,
        required: int,
        required_streams: set[tuple[str, str]] | None = None,
    ) -> RecoveryProbeWindow:
        if not probes:
            raise PredictionWithheldError(
                "recovery comparison has no application probes in one window"
            )
        max_gap = self._config.sampling_seconds * 2
        thresholds = self._config.impact_policy.metrics
        grouped: dict[tuple[str, str], list[AppProbeRecord]] = {}
        for probe in probes:
            grouped.setdefault((probe.probe_name, probe.entity_id), []).append(probe)

        def matching_tail(stream: list[AppProbeRecord]) -> list[AppProbeRecord]:
            if _probe_breaches_threshold(stream[-1], thresholds) != expect_impact:
                return []
            tail = [stream[-1]]
            for probe in reversed(stream[:-1]):
                if (
                    (tail[-1].observed_at - probe.observed_at).total_seconds() > max_gap
                    or _probe_breaches_threshold(probe, thresholds) != expect_impact
                ):
                    break
                tail.append(probe)
            tail.reverse()
            return tail

        if expect_impact:
            candidates = []
            for key, stream in grouped.items():
                tail = matching_tail(stream)
                if not tail:
                    continue
                duration = (tail[-1].observed_at - tail[0].observed_at).total_seconds()
                if duration >= required:
                    candidates.append((duration, tail[-1].observed_at, key, tail))
            if not candidates:
                raise PredictionWithheldError(
                    "recovery comparison does not contain sustained pre-intervention impact"
                )
            contiguous = [probe for candidate in candidates for probe in candidate[3]]
            contiguous.sort(
                key=lambda probe: (
                    probe.observed_at,
                    probe.probe_name,
                    probe.entity_id,
                )
            )
        else:
            missing_streams = (required_streams or set()) - grouped.keys()
            if missing_streams:
                raise PredictionWithheldError(
                    "recovery comparison is missing post-intervention evidence for an "
                    "impacted application stream"
                )
            contiguous = []
            for key, stream in grouped.items():
                tail = matching_tail(stream)
                duration = (
                    (tail[-1].observed_at - tail[0].observed_at).total_seconds()
                    if tail
                    else 0
                )
                if duration < required:
                    if key in (required_streams or set()):
                        raise PredictionWithheldError(
                            "recovery comparison does not establish recovery for an impacted "
                            "application stream"
                        )
                    raise PredictionWithheldError(
                        "recovery comparison does not establish every observed application stream"
                    )
                contiguous.extend(tail)
            contiguous.sort(
                key=lambda probe: (
                    probe.observed_at,
                    probe.probe_name,
                    probe.entity_id,
                )
            )
        if len(contiguous) > 100:
            raise PredictionWithheldError("recovery evidence window exceeds the 100-sample bound")
        duration = (contiguous[-1].observed_at - contiguous[0].observed_at).total_seconds()
        if duration < required:
            raise PredictionWithheldError(
                "recovery comparison requires sustained application probes on both sides"
            )
        breaches = [_probe_breaches_threshold(probe, thresholds) for probe in contiguous]
        impact_detected = all(breaches)
        within_policy = not any(breaches)
        if expect_impact and not impact_detected:
            raise PredictionWithheldError(
                "recovery comparison does not contain sustained pre-intervention impact"
            )
        return RecoveryProbeWindow(
            window_start=contiguous[0].observed_at,
            window_end=contiguous[-1].observed_at,
            samples=contiguous,
            packet_loss_ratio_max=max(probe.packet_loss_ratio for probe in contiguous),
            jitter_ms_max=max(probe.jitter_ms for probe in contiguous),
            request_latency_ms_max=max(probe.request_latency_ms for probe in contiguous),
            impact_detected=impact_detected,
            within_policy=within_policy,
        )

    def _evidence_ids(self) -> list[str]:
        if self.fixture_only:
            records = self.metric_records(entity_id=self.entity_id, limit=5_000)
            if records:
                latest = max(records, key=lambda record: record.observed_at)
                if latest.source == "local-dashboard-fault-simulation":
                    scenario = latest.labels.get("scenario", "unknown")
                    return [
                        "simulation:local-dashboard-only",
                        f"scenario:{scenario}",
                        "network-change:none",
                    ]
            return [
                f"run:{self._manifest['run_id']}",
                "fixture:gradual-loss/metrics.jsonl",
                "fixture:gradual-loss/app-probes.jsonl",
            ]
        return [f"incident:{self.incident_id}", "storage:telemetry", "storage:app-probes"]

    def _incident_path_id(self) -> str:
        for path in self._topology["paths"]:
            if self.entity_id in path["monitored_entity_ids"]:
                return str(path["id"])
        raise ValueError(f"no topology path maps monitored entity {self.entity_id!r}")

    def _path_for_id(self, path_id: str) -> dict[str, object]:
        path = next((item for item in self._topology["paths"] if item["id"] == path_id), None)
        if path is None:
            raise KeyError(f"unknown topology path {path_id!r}")
        return path

    def _transfer_endpoints(
        self,
        path: dict[str, object],
        direction: str,
    ) -> tuple[TrafficDemoEndpoint, TrafficDemoEndpoint]:
        site_ids = path["site_ids"]
        if not isinstance(site_ids, list) or len(site_ids) != 2:
            raise ValueError("traffic demo path must contain exactly two sites")
        source_site_id, destination_site_id = (str(site_ids[0]), str(site_ids[1]))
        if direction == "dc_to_branch":
            source_site_id, destination_site_id = destination_site_id, source_site_id
        sites_by_id = {str(site["id"]): site for site in self._inventory["sites"]}
        source_site = sites_by_id.get(source_site_id)
        destination_site = sites_by_id.get(destination_site_id)
        if source_site is None or destination_site is None:
            raise ValueError("traffic demo path site is missing from inventory")
        return (
            TrafficDemoEndpoint(id=source_site_id, name=str(source_site["name"])),
            TrafficDemoEndpoint(id=destination_site_id, name=str(destination_site["name"])),
        )

    def _transfer_status(self, probe: AppProbeRecord) -> tuple[str, str]:
        """Never call a transfer passed when its observation quality is unsafe."""

        transfer = probe.transfer
        assert transfer is not None
        if (
            not probe.quality.source_healthy
            or probe.quality.counter_reset
            or probe.quality.late_by_ms > self._config.max_late_by_ms
        ):
            return (
                "unavailable",
                "Transfer telemetry is not trustworthy enough to confirm an outcome.",
            )
        if transfer.status == "started":
            return "running", "Transfer is in progress; receiver integrity is still pending."
        if transfer.status == "completed":
            chat = transfer.chat_measurements
            if chat is not None and chat.messages_received < chat.messages_attempted:
                return (
                    "failed",
                    (
                        "File integrity was verified, but chat delivery was incomplete: "
                        f"{chat.messages_received} of {chat.messages_attempted} messages "
                        "were acknowledged."
                    ),
                )
            return "passed", "Transfer completed and SHA-256 integrity was verified."
        if transfer.status == "cancelled":
            return "failed", "Transfer was cancelled before a verified completion."
        return "failed", transfer.error or "Transfer failed before a verified completion."

    def _transfer_is_stale(self, probe: AppProbeRecord) -> bool:
        """Replay has a fixed historical clock; live transfer claims expire quickly."""

        if self.fixture_only:
            return False
        stale_after = datetime.now(UTC) - timedelta(seconds=self._config.traffic_demo_ttl_seconds)
        return probe.observed_at < stale_after

    def _known_event_entities(self) -> set[str]:
        entities = {path["id"] for path in self._topology["paths"]}
        entities.update(
            entity_id
            for path in self._topology["paths"]
            for entity_id in path["monitored_entity_ids"]
        )
        entities.update(
            entity_id
            for path in self._topology["paths"]
            for entity_id in path["app_probe_entities"].values()
        )
        for category in ("sites", "devices", "interfaces"):
            entities.update(item["id"] for item in self._inventory[category])
        return entities

    def _has_valid_app_probe_mapping(self, probe: AppProbeRecord) -> bool:
        path = next(
            (path for path in self._topology["paths"] if path["id"] == probe.path_id),
            None,
        )
        return bool(
            path
            and path["app_probe_entities"].get(probe.probe_name) == probe.entity_id
            and probe.topology_version == self._topology["version"]
            and probe.config_version == self._config.telemetry_config_version
        )

    def _has_fresh_valid_app_probe(self) -> bool:
        reference_time = datetime.now(UTC)
        fresh_after = reference_time - timedelta(seconds=self._config.sampling_seconds * 2)
        future_limit = reference_time + timedelta(milliseconds=self._config.max_future_skew_ms)
        return any(
            probe.path_id == self._incident_path_id()
            and self._has_valid_app_probe_mapping(probe)
            and probe.quality.source_healthy
            and not probe.quality.counter_reset
            and probe.quality.late_by_ms <= self._config.max_late_by_ms
            and fresh_after <= probe.observed_at <= future_limit
            for probe in self.app_probe_records()
        )

    def _audit_trail(self) -> list[AuditEvent]:
        return self.audit_trail(self.incident_id)

    def audit_trail(
        self, incident_id: str, *, limit: int = 1000, offset: int = 0
    ) -> list[AuditEvent]:
        """Read recorded workflow evidence without requiring a fresh prediction."""

        self._require_incident(incident_id)
        if not 1 <= limit <= 1000 or offset < 0:
            raise ValueError("audit limit must be 1-1000 and offset must be non-negative")
        if self._store is not None:
            return self._store.list_audit(incident_id, limit=limit, offset=offset)
        end = max(0, len(self._audit) - offset)
        return self._audit[max(0, end - limit) : end]

    def _require_incident(self, incident_id: str) -> None:
        if incident_id != self.incident_id:
            raise KeyError(f"unknown incident {incident_id!r}")

    def _require_playbook(self, playbook_id: str) -> None:
        if playbook_id != self.playbook_id:
            raise KeyError(f"unknown playbook {playbook_id!r}")

    def _require_workflow_ready(self) -> None:
        if self.fixture_only:
            return
        is_ready, reason = self.readiness()
        if not is_ready:
            raise PredictionWithheldError(
                f"workflow is unavailable while live telemetry is not ready: {reason}"
            )

    def _require_prediction(self, expected: str | None) -> str:
        current = self.prediction().prediction_id
        if expected is not None and expected != current:
            raise ValueError("prediction evidence changed; refresh and review the current evidence")
        return current

    def _require_reviewed(self, actor_id: str, prediction_id: str | None = None) -> None:
        state = self._incident_state()
        if state.reviewed_by != actor_id:
            raise PredictionWithheldError(
                "action requires this operator to review the current incident first"
            )
        current_prediction_id = self._require_prediction(prediction_id)
        if state.reviewed_prediction_id != current_prediction_id:
            raise PredictionWithheldError(
                "action requires a new review because the prediction evidence changed"
            )
        cutoff = datetime.now(UTC) - timedelta(minutes=5)
        recent_review = any(
            event.actor_id == actor_id
            and event.event_type == "incident_reviewed"
            and event.occurred_at >= cutoff
            for event in self._audit_trail()
        )
        if not recent_review:
            raise PredictionWithheldError(
                "action requires an incident review recorded within the last five minutes"
            )

    def _append_audit(
        self,
        incident_id: str,
        actor_id: str,
        event_type: str,
        detail: str,
    ) -> AuditEvent:
        if self._store is not None:
            return self._store.append_audit(
                incident_id=incident_id,
                actor_id=actor_id,
                event_type=event_type,
                detail=detail,
            )
        event = AuditEvent(
            occurred_at=datetime.now(UTC),
            actor_id=actor_id,
            event_type=event_type,
            detail=detail,
        )
        self._audit.append(event)
        return event


class DemoService(PrahariService):
    """Compatibility wrapper for tests and the explicitly replay-only command."""

    def __init__(self, root: Path | None = None, store: PersistenceStore | None = None) -> None:
        super().__init__(root=root, data_mode="replay", store=store)


def _probe_breaches_threshold(probe: AppProbeRecord, thresholds: dict[str, float]) -> bool:
    return (
        probe.packet_loss_ratio >= thresholds["packet_loss_ratio"]
        or probe.jitter_ms >= thresholds["jitter_ms"]
    )


def _chat_latency_summary(transfer) -> TrafficDemoLatency | None:
    """Expose explicitly measured chat RTTs, never file-upload elapsed time as latency."""

    chat = transfer.chat_measurements
    if chat is None:
        return None
    return TrafficDemoLatency(
        min=round(chat.rtt_min_ms, 3),
        p50=round(chat.rtt_p50_ms, 3),
        p95=round(chat.rtt_p95_ms, 3),
        max=round(chat.rtt_max_ms, 3),
    )


def _chat_message_loss_percent(transfer) -> float | None:
    chat = transfer.chat_measurements
    if chat is None:
        return None
    return round(chat.computed_message_loss_ratio * 100, 3)


def _transfer_throughput_mbps(records: list[AppProbeRecord]) -> float | None:
    """Prefer the sender's bounded elapsed measurement, then a probe-series fallback."""

    if not records:
        return None
    ordered = sorted(records, key=lambda record: record.observed_at)
    first = ordered[0]
    last = ordered[-1]
    assert first.transfer is not None and last.transfer is not None
    if last.transfer.throughput_mbps is not None:
        return round(last.transfer.throughput_mbps, 3)
    if last.transfer.elapsed_ms is not None and last.transfer.elapsed_ms > 0:
        return round(
            (last.transfer.bytes_transferred * 8) / (last.transfer.elapsed_ms * 1_000),
            3,
        )
    if len(ordered) < 2:
        return None
    elapsed_seconds = (last.observed_at - first.observed_at).total_seconds()
    transferred_bytes = last.transfer.bytes_transferred - first.transfer.bytes_transferred
    if elapsed_seconds <= 0 or transferred_bytes <= 0:
        return None
    return round((transferred_bytes * 8) / (elapsed_seconds * 1_000_000), 3)


def _transfer_trace(
    probe: AppProbeRecord,
    demo_status: str,
) -> list[TrafficDemoTraceStep]:
    transfer = probe.transfer
    assert transfer is not None
    chat = transfer.chat_measurements
    observation_status = "passed"
    if (
        not probe.quality.source_healthy
        or probe.quality.counter_reset
        or probe.quality.late_by_ms > 0
        or (chat is not None and chat.messages_received < chat.messages_attempted)
    ):
        observation_status = "failed"
    transport_status = {
        "running": "active",
        "passed": "passed",
        "failed": "failed",
        "unavailable": "failed",
    }[demo_status]
    receipt_status = (
        "passed" if demo_status == "passed" else "active" if demo_status == "running" else "failed"
    )
    integrity_status = (
        "passed"
        if transfer.integrity == "verified"
        else "active"
        if transfer.integrity == "pending"
        else "failed"
    )
    chat_detail = _chat_trace_detail(transfer)
    return [
        TrafficDemoTraceStep(
            at=probe.observed_at,
            stage="chat_probe",
            status=observation_status,
            detail=chat_detail,
        ),
        TrafficDemoTraceStep(
            at=probe.observed_at,
            stage="transport",
            status=transport_status,
            detail=transfer.error or f"{transfer.protocol} transfer {transfer.status}.",
        ),
        TrafficDemoTraceStep(
            at=probe.observed_at,
            stage="destination_receipt",
            status=receipt_status,
            detail=(
                f"{transfer.bytes_received} of {transfer.bytes_expected} bytes acknowledged."
                if transfer.bytes_received is not None
                else "Receiver byte acknowledgement has not been reported."
            ),
        ),
        TrafficDemoTraceStep(
            at=probe.observed_at,
            stage="integrity_check",
            status=integrity_status,
            detail=(
                "SHA-256 checksum verified."
                if transfer.integrity == "verified"
                else "Checksum verification is pending."
                if transfer.integrity == "pending"
                else "Checksum verification failed."
            ),
        ),
    ]


def _chat_trace_detail(transfer) -> str:
    chat = transfer.chat_measurements
    if chat is None:
        return "No application-message RTT or loss summary was reported with this transfer."
    return (
        f"Chat RTT P50 {chat.rtt_p50_ms:.1f} ms, P95 {chat.rtt_p95_ms:.1f} ms; "
        f"{chat.computed_message_loss_ratio * 100:.3f}% application-message loss."
    )


def _state_for_closure(
    outcome: str,
    review: ClosureReviewRequest | None,
) -> str:
    if outcome in {"failed", "partially_resolved", "uncertain"} or (
        review is not None and review.decision == "rejected"
    ):
        return "escalated"
    if review is None:
        return "validating"
    if outcome in {"resolved", "false_positive"}:
        return "resolved"
    return "escalated"


def _future_limit(max_future_skew_ms: int) -> datetime:
    return datetime.now(UTC) + timedelta(milliseconds=max_future_skew_ms)
