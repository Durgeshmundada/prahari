import { z } from "zod";
import type {
  ActionResult, BrowserSession, ClosureCase, Health, IncidentBrief, IncidentCard, IncidentDetail, MetricRecord,
  ModelInfo, Readiness, Topology, TrafficDemo, TrafficDemoRunResponse
} from "./types";

const text = z.string();
const id = text.min(1);
const timestamp = z.iso.datetime({ offset: true });
const probability = z.number().min(0).max(1);
const nonnegative = z.number().nonnegative();
const quality = z.enum(["good", "degraded", "invalid"]);
const location = z.object({ entity_id: id, site_id: id, score: probability, rationale: text });
const auditEvent = z.object({
  occurred_at: timestamp, actor_id: id, event_type: id, detail: text
});
const closureSubmission = z.object({
  actor_id: id,
  prediction_id: id.nullable().optional(),
  outcome: z.enum(["resolved", "partially_resolved", "failed", "false_positive", "uncertain"]),
  note: text,
  intervention_at: timestamp.nullable().optional()
});
const closureReview = z.object({ actor_id: id, decision: z.enum(["approved", "rejected"]), note: text });

export const healthSchema: z.ZodType<Health> = z.object({
  status: id, fixture_only: z.boolean(), environment: text.optional()
});
export const readinessSchema: z.ZodType<Readiness> = z.object({
  status: z.literal("ready"), fixture_only: z.boolean()
});
export const topologySchema: z.ZodType<Topology> = z.object({
  profile_id: id.nullable().optional(),
  profile_sha256: text.regex(/^[0-9a-f]{64}$/).nullable().optional(),
  version: id,
  inventory: z.object({
    version: id,
    sites: z.array(z.object({ id, name: text, city: text.optional() })),
    devices: z.array(z.object({ id, name: text, site_id: id, role: text }))
  }),
  paths: z.array(z.object({
    id, name: text, path_role: text, site_ids: z.array(id),
    monitored_entity_ids: z.array(id), interface_ids: z.array(id),
    backup_path_id: id.nullable(), applications: z.array(text)
  })),
  fixture_only: z.boolean()
});
export const incidentSchema: z.ZodType<IncidentCard> = z.object({
  id,
  state: z.enum([
    "watch", "predicted", "reviewed", "mitigating", "validating", "blocked", "resolved", "escalated"
  ]),
  severity: z.enum(["informational", "warning", "high"]),
  summary: text,
  prediction: z.object({
    network_profile_sha256: text.regex(/^[0-9a-f]{64}$/).nullable().optional(),
    model_bundle_sha256: text.regex(/^[0-9a-f]{64}$/).nullable().optional(),
    prediction_id: id, model_version: id, entity_id: id, likely_incident_type: text,
    incident_type_probabilities: z.record(text, probability),
    impact_risk: z.object({
      within_30_seconds: probability, within_60_seconds: probability, within_120_seconds: probability
    }).refine((risk) => risk.within_30_seconds <= risk.within_60_seconds
      && risk.within_60_seconds <= risk.within_120_seconds, "Risk horizons must be monotonic"),
    time_to_impact: z.object({ range: text, basis: text }),
    root_location_candidates: z.array(location),
    affected_scope: z.object({
      paths: z.array(id), applications: z.array(text), sites: z.array(id), backup_path_id: id.nullable()
    }),
    confidence: probability, risk_status: z.enum(["heuristic", "calibrated"]),
    data_quality: quality, evidence: z.array(text)
  }),
  location, data_quality: quality, evidence_ids: z.array(id), playbook_status: text
});
export const incidentsSchema = z.array(incidentSchema);
export const incidentDetailSchema = z.object({
  device_dispatch_enabled: z.boolean().default(false),
  incident: incidentSchema,
  feature_window: z.object({
    feature_version: id, entity_id: id, window_start: timestamp, window_end: timestamp,
    data_quality: z.object({
      status: quality, reasons: z.array(text), sample_counts: z.record(text, nonnegative.int()),
      newest_observation_at: timestamp.nullable()
    }),
    metrics: z.record(text, z.object({
      metric: text, unit: text, sample_count: nonnegative.int(), latest: z.number(),
      mean: z.number(), p95: z.number(), stddev: nonnegative, slope_per_second: z.number(),
      ewma: z.number(), baseline_median: z.number(), robust_z: z.number()
    }))
  }),
  run_manifest: z.discriminatedUnion("fixture_only", [z.object({
    run_id: id, scenario: text, fixture_only: z.literal(true), random_seed: z.number(),
    topology_version: id, config_version: id, target_entity_id: id, monitored_entity_id: id,
    start_time: timestamp, expected_incident_type: text, note: text
  }), z.object({ run_id: id, fixture_only: z.literal(false) }).strict()]),
  playbook: z.object({
    id, title: text, status: text, source: text, applicable_incident_types: z.array(text),
    requires_operator_approval: z.boolean(), action_adapter: text.nullable(), safety_note: text
  }),
  app_impact_started: z.boolean(), audit_trail: z.array(auditEvent), fixture_only: z.boolean(),
  workflow: z.object({
    unconfirmed_action: z.boolean().optional(),
    reviewed_by: id.nullable(), reviewed_prediction_id: id.nullable(), closure_case_id: id.nullable(),
    closure: closureSubmission.nullable(), closure_review: closureReview.nullable(),
    closure_prediction_id: id.nullable()
  })
}) satisfies z.ZodType<IncidentDetail>;
export const closureCaseSchema: z.ZodType<ClosureCase> = z.object({
  id, incident_id: id, prediction_id: id, created_at: timestamp, submission: closureSubmission,
  incident: incidentSchema, feature_window: incidentDetailSchema.shape.feature_window,
  app_impact_started: z.boolean(),
  recovery_comparison: z.object({
    schema_version: z.enum(["recovery-comparison-v1", "recovery-comparison-v2"]), path_id: id,
    intervention_at: timestamp, impact_policy_version: id,
    impact_thresholds: z.object({
      packet_loss_ratio: probability, jitter_ms: nonnegative
    }).nullable().optional(),
    required_sustained_seconds: z.number().int().positive(),
    before: z.object({
      window_start: timestamp, window_end: timestamp,
      samples: z.array(z.object({
        schema_version: z.literal("1.0"), record_type: z.literal("app_probe"),
        observed_at: timestamp, entity_id: id, probe_name: id,
        packet_loss_ratio: probability, jitter_ms: nonnegative,
        request_latency_ms: nonnegative, path_id: id, topology_version: id,
        config_version: id,
        quality: z.object({ source_healthy: z.boolean(), late_by_ms: nonnegative,
          is_estimated: z.boolean(), counter_reset: z.boolean() })
      })).min(2).max(100), packet_loss_ratio_max: probability,
      jitter_ms_max: nonnegative, request_latency_ms_max: nonnegative,
      impact_detected: z.boolean(), within_policy: z.boolean()
    }),
    after: z.object({
      window_start: timestamp, window_end: timestamp,
      samples: z.array(z.object({
        schema_version: z.literal("1.0"), record_type: z.literal("app_probe"),
        observed_at: timestamp, entity_id: id, probe_name: id,
        packet_loss_ratio: probability, jitter_ms: nonnegative,
        request_latency_ms: nonnegative, path_id: id, topology_version: id,
        config_version: id,
        quality: z.object({ source_healthy: z.boolean(), late_by_ms: nonnegative,
          is_estimated: z.boolean(), counter_reset: z.boolean() })
      })).min(2).max(100), packet_loss_ratio_max: probability,
      jitter_ms_max: nonnegative, request_latency_ms_max: nonnegative,
      impact_detected: z.boolean(), within_policy: z.boolean()
    }),
    recovered: z.boolean(), evidence_sha256: text.regex(/^[0-9a-f]{64}$/)
  }).superRefine((comparison, context) => {
    if (comparison.schema_version === "recovery-comparison-v2" && !comparison.impact_thresholds) {
      context.addIssue({ code: "custom", message: "V2 recovery evidence requires captured thresholds" });
    }
    if (comparison.recovered !== (comparison.before.impact_detected && comparison.after.within_policy)) {
      context.addIssue({ code: "custom", message: "Recovery verdict does not match its windows" });
    }
    for (const sample of [...comparison.before.samples, ...comparison.after.samples]) {
      if (sample.path_id !== comparison.path_id) {
        context.addIssue({ code: "custom", message: "Recovery sample path does not match" });
        break;
      }
    }
  }).nullable(),
  review: closureReview.nullable(), reviewed_at: timestamp.nullable()
});
export const closureCasesSchema = z.array(closureCaseSchema);
export const sessionSchema: z.ZodType<BrowserSession> = z.object({
  authenticated: z.boolean(), mode: z.enum(["disabled", "local", "api_key"]),
  actor: z.object({ actor_id: id, role: z.enum(["viewer", "operator", "reviewer", "admin"]) }).nullable(),
  csrf_token: id.nullable(), expires_at: timestamp.nullable()
}).refine((session) => !session.authenticated || session.actor !== null,
  "An authenticated session requires an actor");
export const emptyResponseSchema = z.undefined();
export const actionResultSchema: z.ZodType<ActionResult> = z.object({
  playbook_id: id,
  status: z.enum(["blocked", "dry_run_passed", "applied", "rolled_back", "validation_failed"]),
  message: text, audit_event: auditEvent
});
export const incidentBriefSchema: z.ZodType<IncidentBrief> = z.object({
  evidence_current_at_completion: z.boolean(),
  schema_version: z.literal("1.0"), incident_id: id, summary: text,
  situation: z.array(text), root_cause_hypothesis: text, impact: text,
  recommended_playbook_id: id.nullable(),
  citations: z.array(z.object({ source_id: id, claim: text })),
  confidence: probability, limitations: z.array(text),
  grounding: z.object({
    mode: z.literal("evidence-selection-v1"),
    incident_sha256: text.regex(/^[a-f0-9]{64}$/),
    incident_snapshot: incidentSchema,
    claims: z.array(z.object({
      claim_id: text.regex(/^claim-[a-f0-9]{64}$/),
      field: z.enum(["summary", "situation", "root_cause_hypothesis", "impact", "limitations"]),
      text, source_id: id, source_sha256: text.regex(/^[a-f0-9]{64}$/), support_path: text,
      kind: z.enum(["current", "historical", "advisory", "limitation"])
    })).min(5).max(12)
  })
}).superRefine((brief, context) => {
  if (brief.incident_id !== brief.grounding.incident_snapshot.id ||
      brief.confidence !== brief.grounding.incident_snapshot.prediction.confidence) {
    context.addIssue({ code: "custom", message: "Brief evidence identity mismatch" });
  }
  for (const field of ["summary", "situation", "root_cause_hypothesis", "impact", "limitations"] as const) {
    const actual = brief[field];
    const texts = typeof actual === "string" ? [actual] : actual;
    const supported = brief.grounding.claims.filter(claim => claim.field === field).map(claim => claim.text);
    if (JSON.stringify(texts) !== JSON.stringify(supported)) {
      context.addIssue({ code: "custom", message: "Brief narrative support mismatch" });
    }
  }
  if (JSON.stringify(brief.citations) !== JSON.stringify(brief.grounding.claims.map(claim => ({
    source_id: claim.source_id, claim: claim.text
  })))) context.addIssue({ code: "custom", message: "Brief citation support mismatch" });
});
export const modelInfoSchema: z.ZodType<ModelInfo> = z.object({
  model_version: id, model_type: text, training_status: text, feature_version: id,
  scenario_version: id, impact_policy_version: id, fixture_only: z.boolean(),
  data_mode: text.optional()
});
export const metricsSchema: z.ZodType<MetricRecord[]> = z.array(z.object({
  schema_version: id, record_type: z.literal("metric"), observed_at: timestamp,
  source: text, entity_id: id, entity_type: text, metric: text, value: z.number(), unit: text,
  labels: z.record(text, text), topology_version: id, config_version: text.nullable(),
  quality: z.object({
    source_healthy: z.boolean(), late_by_ms: nonnegative,
    is_estimated: z.boolean(), counter_reset: z.boolean()
  })
}));
export const trafficDemoSchema: z.ZodType<TrafficDemo> = z.object({
  status: id, can_run: z.boolean().optional(), max_file_bytes: nonnegative.int().nullish(),
  updated_at: timestamp.nullish(), path_id: id.nullish(),
  source: z.object({ id, name: text }).nullish(),
  destination: z.object({ id, name: text }).nullish(), transport: text.nullish(),
  payload: z.object({
    name: text.nullish(), kind: text.nullish(), size_bytes: nonnegative.int().nullish(),
    bytes_sent: nonnegative.int().nullish(), bytes_received: nonnegative.int().nullish(),
    sha256: text.regex(/^[0-9a-f]{64}$/).nullish(), integrity: text.nullish()
  }).nullish(),
  latency_ms: z.object({
    min: nonnegative.nullish(), p50: nonnegative.nullish(),
    p95: nonnegative.nullish(), max: nonnegative.nullish()
  }).nullish(),
  throughput_mbps: nonnegative.nullish(), loss_percent: z.number().min(0).max(100).nullish(),
  trace: z.array(z.object({
    at: timestamp.nullish(), stage: text, status: text, detail: text.nullish()
  })).optional(),
  message: text.nullish()
});
export const trafficDemoRunSchema: z.ZodType<TrafficDemoRunResponse> = z.object({
  run_id: id, transfer_id: id, evidence_saved: z.boolean(), downloadable: z.boolean().optional(),
  download_url: text.regex(/^\/api\/demo\/traffic\/files\/file-[0-9a-f]{32}\/download$/).nullish(),
  download_expires_at: timestamp.nullish(), traffic: trafficDemoSchema
});

export function parseResponse<T>(schema: z.ZodType<T>, body: unknown, resource: string): T {
  const parsed = schema.safeParse(body);
  if (!parsed.success) {
    // Do not echo response data: it may contain private telemetry or hostile markup.
    throw new Error(`The PRAHARI API returned an incompatible ${resource} response. Refresh or contact your operator.`);
  }
  return parsed.data;
}
