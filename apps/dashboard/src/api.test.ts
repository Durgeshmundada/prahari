import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "./api";
import replay from "./__fixtures__/replay.json";
import liveDetail from "./__fixtures__/owned-live-detail.json";
import { actionOperationSchema, actionOperationsPageSchema } from "./actionSchemas";
import {
  healthSchema, readinessSchema, topologySchema, incidentsSchema, incidentDetailSchema,
  modelInfoSchema, metricsSchema, trafficDemoSchema, trafficDemoRunSchema, incidentBriefSchema
} from "./schemas";

afterEach(() => vi.unstubAllGlobals());

describe("API response contracts", () => {
  it("accepts measured live incident details without replay-only fields", () => {
    expect(incidentDetailSchema.safeParse(liveDetail).success).toBe(true);
    const mislabeled = structuredClone(replay.detail);
    mislabeled.run_manifest.fixture_only = false;
    expect(incidentDetailSchema.safeParse(mislabeled).success).toBe(false);
  });
  it("accepts real backend replay responses", () => {
    for (const [schema, payload] of [
      [healthSchema, replay.health], [readinessSchema, replay.readiness],
      [topologySchema, replay.topology], [incidentsSchema, replay.incidents],
      [actionOperationsPageSchema, replay.actions],
      [incidentBriefSchema, replay.brief],
      [incidentDetailSchema, replay.detail], [modelInfoSchema, replay.models],
      [metricsSchema, replay.metrics], [trafficDemoSchema, replay.traffic]
    ] as const) {
      expect(schema.safeParse(payload).success).toBe(true);
    }
  });

  it.each([null, {}, "unexpected HTML", [{ id: "partial" }]])(
    "rejects a malformed incident list before it reaches rendering", async (body) => {
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(body))));
      await expect(api.incidents()).rejects.toThrow("incompatible");
    }
  );

  it("rejects invalid nested risk numbers", () => {
    const detail = structuredClone(replay.detail);
    detail.incident.prediction.impact_risk.within_30_seconds = 2;
    expect(incidentDetailSchema.safeParse(detail).success).toBe(false);
  });

  it("rejects contradictory action outcome evidence", () => {
    const prediction = replay.detail.incident.prediction;
    const operation = {
      id: "action-contract-test", incident_id: replay.detail.incident.id,
      playbook_id: replay.detail.playbook.id, prediction_id: prediction.prediction_id,
      prediction_snapshot: prediction, kind: "apply", actor_id: "operator-01",
      note: "Contradictory response fixture.", created_at: "2026-09-20T10:00:00Z",
      updated_at: "2026-09-20T10:00:01Z", status: "unknown",
      result_status: "applied", observed_route: "backup", message: "Unconfirmed.",
      agent_receipt: null, reconciliation: null
    };
    expect(actionOperationSchema.safeParse(operation).success).toBe(false);
  });

  it("rejects a changed brief narrative even with valid citation identifiers", () => {
    const brief = structuredClone(replay.brief);
    brief.impact = "Every application has recovered completely.";
    expect(incidentBriefSchema.safeParse(brief).success).toBe(false);
  });

  it("preserves a valid blocked action result returned with HTTP 409", async () => {
    const result = {
      playbook_id: "backup-path-shift-v1", status: "blocked", message: "Review required.",
      audit_event: {
        occurred_at: "2026-09-09T10:00:00Z", actor_id: "operator-01",
        event_type: "dry_run_blocked", detail: "Prerequisites are missing."
      }
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response(JSON.stringify(result), { status: 409 })
    ));
    const response = await api.dryRun("inc-01", result.playbook_id, {
      actor_id: "operator-01", note: "Check safety."
    });
    expect(response).toEqual({ result, httpStatus: 409 });
  });

  it("does not accept a malformed error body as an action result", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      playbook_id: 10, status: "applied", message: null, audit_event: null
    }), { status: 409 })));
    await expect(api.approve("inc-01", "playbook", {
      actor_id: "operator-01", note: "Approve."
    })).rejects.toBeInstanceOf(ApiError);
  });

  it("rejects unsafe download references even in a successful run response", () => {
    const response = {
      run_id: "run-01", transfer_id: "transfer-01", evidence_saved: true,
      downloadable: true, download_url: "https://external.example/private-file",
      traffic: replay.traffic
    };
    expect(trafficDemoRunSchema.safeParse(response).success).toBe(false);
  });

  it("treats an absent optional traffic endpoint separately from a server failure", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response("", { status: 404 }))
      .mockResolvedValueOnce(new Response("", { status: 503 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(api.trafficDemo()).resolves.toBeNull();
    await expect(api.trafficDemo()).rejects.toBeInstanceOf(ApiError);
  });
});
