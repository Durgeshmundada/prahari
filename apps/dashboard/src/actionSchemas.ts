import { z } from "zod";
import type { ActionOperation } from "./types";

const id = z.string().min(1);
const checksum = z.string().regex(/^[0-9a-f]{64}$/);
const timestamp = z.iso.datetime({ offset: true });

export const actionOperationSchema: z.ZodType<ActionOperation> = z.object({
  id, incident_id: id, playbook_id: id, prediction_id: id,
  prediction_snapshot: z.object({
    prediction_id: id, model_version: id, entity_id: id,
    network_profile_sha256: checksum.nullable().optional(),
    model_bundle_sha256: checksum.nullable().optional()
  }),
  kind: z.enum(["apply", "rollback"]), actor_id: id, note: z.string(),
  created_at: timestamp, updated_at: timestamp,
  status: z.enum(["pending", "unknown", "finished"]),
  result_status: z.enum(["applied", "rolled_back", "blocked", "validation_failed"]).nullable(),
  observed_route: z.enum(["primary", "backup", "unknown"]).nullable(),
  message: z.string().nullable(),
  agent_receipt: z.object({
    operation_id: id.nullable().optional(),
    status: z.enum(["dry_run_passed", "applied", "rolled_back", "blocked", "validation_failed"]),
    message: z.string(), observed_route: z.enum(["primary", "backup", "unknown"]),
    checked_at: timestamp
  }).nullable().optional(),
  reconciliation: z.object({
    actor_id: id, note: z.string().min(20), evidence_reference: z.string().min(3),
    evidence_sha256: checksum, dispatch_quiesced: z.literal(true),
    observed_route: z.enum(["primary", "backup"])
  }).nullable()
}).superRefine((operation, context) => {
  const issue = (message: string) => context.addIssue({ code: "custom", message });
  if (operation.prediction_id !== operation.prediction_snapshot.prediction_id) {
    issue("Action snapshot must match its prediction");
  }
  if (Date.parse(operation.updated_at) < Date.parse(operation.created_at)) {
    issue("Action update cannot precede its creation");
  }
  if (operation.status === "pending" && [
    operation.result_status, operation.observed_route, operation.message,
    operation.agent_receipt, operation.reconciliation
  ].some(value => value != null)) {
    issue("Pending operation cannot contain outcome evidence");
  }
  if (operation.status === "unknown" && ![null, "validation_failed"].includes(operation.result_status)) {
    issue("Unknown operation cannot claim a confirmed result");
  }
  if (operation.status === "finished" && !operation.result_status && !operation.reconciliation) {
    issue("Finished operation requires a result or reconciliation");
  }
  if (operation.result_status === "applied" &&
      (operation.kind !== "apply" || operation.observed_route !== "backup")) {
    issue("Applied operation requires the observed backup route");
  }
  if (operation.result_status === "rolled_back" &&
      (operation.kind !== "rollback" || operation.observed_route !== "primary")) {
    issue("Rolled-back operation requires the observed primary route");
  }
  if (operation.agent_receipt?.operation_id &&
      operation.agent_receipt.operation_id !== operation.id) {
    issue("Action receipt does not match its operation ID");
  }
  if (operation.agent_receipt && operation.result_status &&
      operation.agent_receipt.status !== operation.result_status) {
    issue("Action result does not match its adapter receipt");
  }
  if (operation.reconciliation && (
    operation.reconciliation.actor_id === operation.actor_id ||
    operation.reconciliation.observed_route !== operation.observed_route
  )) {
    issue("Action reconciliation does not match independent route evidence");
  }
});

export const actionOperationsSchema = z.array(actionOperationSchema).max(100);
export const actionOperationsPageSchema = z.object({
  operations: actionOperationsSchema, dispatch_enabled: z.boolean()
});
