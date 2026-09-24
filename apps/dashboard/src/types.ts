export type DataQuality = "good" | "degraded" | "invalid";
export type IncidentState =
  | "watch"
  | "predicted"
  | "reviewed"
  | "mitigating"
  | "validating"
  | "blocked"
  | "resolved"
  | "escalated";
export type RiskStatus = "heuristic" | "calibrated";
export type Severity = "informational" | "warning" | "high";
export type ClosureOutcome =
  | "resolved"
  | "partially_resolved"
  | "failed"
  | "false_positive"
  | "uncertain";

export interface Health {
  status: string;
  fixture_only: boolean;
  environment?: string;
}

export interface Readiness {
  status: "ready";
  fixture_only: boolean;
}

export interface Site {
  id: string;
  name: string;
  city?: string;
}

export interface Device {
  id: string;
  name: string;
  site_id: string;
  role: string;
}

export interface NetworkInventory {
  version: string;
  sites: Site[];
  devices: Device[];
}

export interface NetworkPath {
  id: string;
  name: string;
  path_role: "primary" | "backup" | string;
  site_ids: string[];
  monitored_entity_ids: string[];
  interface_ids: string[];
  backup_path_id: string | null;
  applications: string[];
}

export interface Topology {
  profile_id?: string | null;
  profile_sha256?: string | null;
  version: string;
  inventory: NetworkInventory;
  paths: NetworkPath[];
  fixture_only: boolean;
}

export interface RootLocationCandidate {
  entity_id: string;
  site_id: string;
  score: number;
  rationale: string;
}

export interface ImpactRisk {
  within_30_seconds: number;
  within_60_seconds: number;
  within_120_seconds: number;
}

export interface Prediction {
  network_profile_sha256?: string | null;
  model_bundle_sha256?: string | null;
  prediction_id: string;
  model_version: string;
  entity_id: string;
  likely_incident_type: "normal" | "tunnel_degradation" | string;
  incident_type_probabilities: Record<string, number>;
  impact_risk: ImpactRisk;
  time_to_impact: {
    range: string;
    basis: string;
  };
  root_location_candidates: RootLocationCandidate[];
  affected_scope: {
    paths: string[];
    applications: string[];
    sites: string[];
    backup_path_id: string | null;
  };
  confidence: number;
  risk_status: RiskStatus;
  data_quality: DataQuality;
  evidence: string[];
}

export interface IncidentCard {
  id: string;
  state: IncidentState;
  severity: Severity;
  summary: string;
  prediction: Prediction;
  location: RootLocationCandidate;
  data_quality: DataQuality;
  evidence_ids: string[];
  playbook_status: "draft" | "available_pending_review" | "blocked" | string;
}

export interface DataQualityDetail {
  status: DataQuality;
  reasons: string[];
  sample_counts: Record<string, number>;
  newest_observation_at: string | null;
}

export interface MetricWindowFeature {
  metric: string;
  unit: string;
  sample_count: number;
  latest: number;
  mean: number;
  p95: number;
  stddev: number;
  slope_per_second: number;
  ewma: number;
  baseline_median: number;
  robust_z: number;
}

export interface FeatureWindow {
  feature_version: string;
  entity_id: string;
  window_start: string;
  window_end: string;
  data_quality: DataQualityDetail;
  metrics: Record<string, MetricWindowFeature>;
}

export interface Playbook {
  id: string;
  title: string;
  status: "draft" | "available_pending_review" | "blocked" | string;
  source: string;
  applicable_incident_types: string[];
  requires_operator_approval: boolean;
  action_adapter: string | null;
  safety_note: string;
}

export interface ReplayRunManifest {
  run_id: string;
  scenario: string;
  fixture_only: true;
  random_seed: number;
  topology_version: string;
  config_version: string;
  target_entity_id: string;
  monitored_entity_id: string;
  start_time: string;
  expected_incident_type: string;
  note: string;
}

export type RunManifest = ReplayRunManifest | { run_id: string; fixture_only: false };

export interface AuditEvent {
  occurred_at: string;
  actor_id: string;
  event_type: string;
  detail: string;
}

export interface IncidentDetail {
  device_dispatch_enabled: boolean;
  incident: IncidentCard;
  feature_window: FeatureWindow;
  run_manifest: RunManifest;
  playbook: Playbook;
  app_impact_started: boolean;
  audit_trail: AuditEvent[];
  fixture_only: boolean;
  workflow: {
    unconfirmed_action?: boolean;
    reviewed_by: string | null;
    reviewed_prediction_id: string | null;
    closure_case_id: string | null;
    closure: ClosureRequest | null;
    closure_review: ClosureReviewRequest | null;
    closure_prediction_id: string | null;
  };
}

export interface ActionReconciliationRequest {
  actor_id: string;
  note: string;
  evidence_reference: string;
  evidence_sha256: string;
  dispatch_quiesced: true;
  observed_route: "primary" | "backup";
}

export interface ActionOperation {
  id: string;
  incident_id: string;
  playbook_id: string;
  prediction_id: string;
  prediction_snapshot: Pick<Prediction, "prediction_id" | "model_version" | "entity_id"
    | "network_profile_sha256" | "model_bundle_sha256">;
  kind: "apply" | "rollback";
  actor_id: string;
  note: string;
  created_at: string;
  updated_at: string;
  status: "pending" | "unknown" | "finished";
  result_status: "applied" | "rolled_back" | "blocked" | "validation_failed" | null;
  observed_route: "primary" | "backup" | "unknown" | null;
  message: string | null;
  reconciliation: ActionReconciliationRequest | null;
  agent_receipt?: {
    operation_id?: string | null;
    status: "dry_run_passed" | "applied" | "rolled_back" | "blocked" | "validation_failed";
    message: string;
    observed_route: "primary" | "backup" | "unknown";
    checked_at: string;
  } | null;
}

export interface ActionResult {
  playbook_id: string;
  status:
    | "blocked"
    | "dry_run_passed"
    | "applied"
    | "rolled_back"
    | "validation_failed";
  message: string;
  audit_event: AuditEvent;
}

export interface IncidentBrief {
  schema_version: "1.0";
  incident_id: string;
  evidence_current_at_completion: boolean;
  summary: string;
  situation: string[];
  root_cause_hypothesis: string;
  impact: string;
  recommended_playbook_id: string | null;
  citations: Array<{ source_id: string; claim: string }>;
  confidence: number;
  limitations: string[];
  grounding: {
    mode: "evidence-selection-v1";
    incident_sha256: string;
    incident_snapshot: IncidentCard;
    claims: Array<{
      claim_id: string;
      field: "summary" | "situation" | "root_cause_hypothesis" | "impact" | "limitations";
      text: string;
      source_id: string;
      source_sha256: string;
      support_path: string;
      kind: "current" | "historical" | "advisory" | "limitation";
    }>;
  };
}

export interface ModelInfo {
  model_version: string;
  model_type: string;
  training_status: string;
  feature_version: string;
  scenario_version: string;
  impact_policy_version: string;
  fixture_only: boolean;
  data_mode?: string;
}

export interface MetricRecord {
  schema_version: string;
  record_type: "metric";
  observed_at: string;
  source: string;
  entity_id: string;
  entity_type: string;
  metric: string;
  value: number;
  unit: string;
  labels: Record<string, string>;
  topology_version: string;
  config_version: string | null;
  quality: {
    source_healthy: boolean;
    late_by_ms: number;
    is_estimated: boolean;
    counter_reset: boolean;
  };
}

/**
 * Optional contract for the two-server traffic demonstration monitor.
 *
 * The dashboard treats this as an additive API: an older PRAHARI API can omit it
 * (404) without affecting the NOC view. A future traffic-demo service can expose
 * this shape at GET /api/demo/traffic.
 */
export interface TrafficDemo {
  status: "idle" | "running" | "passed" | "failed" | "unavailable" | string;
  can_run?: boolean;
  max_file_bytes?: number | null;
  updated_at?: string | null;
  path_id?: string | null;
  source?: {
    id: string;
    name: string;
  } | null;
  destination?: {
    id: string;
    name: string;
  } | null;
  transport?: "websocket" | "http" | string | null;
  payload?: {
    name?: string | null;
    kind?: "photo" | "video" | "file" | string | null;
    size_bytes?: number | null;
    bytes_sent?: number | null;
    bytes_received?: number | null;
    sha256?: string | null;
    integrity?: "pending" | "verified" | "failed" | string | null;
  } | null;
  latency_ms?: {
    min?: number | null;
    p50?: number | null;
    p95?: number | null;
    max?: number | null;
  } | null;
  throughput_mbps?: number | null;
  loss_percent?: number | null;
  trace?: Array<{
    at?: string | null;
    stage: string;
    status: "pending" | "active" | "passed" | "failed" | string;
    detail?: string | null;
  }>;
  message?: string | null;
}

export interface TrafficDemoRunResponse {
  run_id: string;
  transfer_id: string;
  evidence_saved: boolean;
  downloadable?: boolean;
  download_url?: string | null;
  download_expires_at?: string | null;
  traffic: TrafficDemo;
}

export interface TrafficDemoUploadProgress {
  loaded_bytes: number;
  total_bytes: number;
  percent: number;
}

export interface ApiErrorBody {
  detail?: string;
}

export interface ClosureRequest {
  actor_id: string;
  prediction_id?: string | null;
  outcome: ClosureOutcome;
  note: string;
  intervention_at?: string | null;
}

export interface ClosureReviewRequest {
  actor_id: string;
  decision: "approved" | "rejected";
  note: string;
}

export interface RecoveryProbeSample {
  schema_version: "1.0";
  record_type: "app_probe";
  observed_at: string;
  entity_id: string;
  probe_name: string;
  packet_loss_ratio: number;
  jitter_ms: number;
  request_latency_ms: number;
  path_id: string;
  topology_version: string;
  config_version: string;
  quality: {
    source_healthy: boolean;
    late_by_ms: number;
    is_estimated: boolean;
    counter_reset: boolean;
  };
}

export interface RecoveryProbeWindow {
  window_start: string;
  window_end: string;
  samples: RecoveryProbeSample[];
  packet_loss_ratio_max: number;
  jitter_ms_max: number;
  request_latency_ms_max: number;
  impact_detected: boolean;
  within_policy: boolean;
}

export interface RecoveryComparison {
  schema_version: "recovery-comparison-v1" | "recovery-comparison-v2";
  path_id: string;
  intervention_at: string;
  impact_policy_version: string;
  impact_thresholds?: { packet_loss_ratio: number; jitter_ms: number } | null;
  required_sustained_seconds: number;
  before: RecoveryProbeWindow;
  after: RecoveryProbeWindow;
  recovered: boolean;
  evidence_sha256: string;
}

export interface ClosureCase {
  id: string;
  incident_id: string;
  prediction_id: string;
  created_at: string;
  submission: ClosureRequest;
  incident: IncidentCard;
  feature_window: FeatureWindow;
  app_impact_started: boolean;
  recovery_comparison: RecoveryComparison | null;
  review: ClosureReviewRequest | null;
  reviewed_at: string | null;
}

export interface CaseSearchFilters {
  status?: "pending" | "approved" | "rejected" | "";
  outcome?: ClosureOutcome | "";
  incident_type?: "normal" | "tunnel_degradation" | "";
  entity_id?: string;
  network_profile_sha256?: string;
}

export interface ActorIdentity {
  actor_id: string;
  role: "viewer" | "operator" | "reviewer" | "admin";
}

export interface BrowserSession {
  authenticated: boolean;
  mode: "disabled" | "local" | "api_key";
  actor: ActorIdentity | null;
  csrf_token: string | null;
  expires_at: string | null;
}
