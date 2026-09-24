// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { api } from "./api";
import { IncidentWorkbench } from "./IncidentWorkbench";
import { incidentDetailSchema } from "./schemas";
import replay from "./__fixtures__/replay.json";
import type { ActionResult, IncidentBrief } from "./types";

const detail = incidentDetailSchema.parse(replay.detail);
// This seam simulates a configured controlled adapter; the replay API has none.
detail.device_dispatch_enabled = true;
const actor = { actor_id: "operator-console", role: "operator" as const };
const onChanged = vi.fn().mockResolvedValue(undefined);
const action: ActionResult = {
  playbook_id: detail.playbook.id, status: "dry_run_passed", message: "Safety check passed.",
  audit_event: {
    occurred_at: "2026-09-09T10:00:00Z", actor_id: "operator-console",
    event_type: "action_dry_run_passed", detail: "Backup route checked."
  }
};

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it("blocks review, safety checks and closure while the server reports an unconfirmed action", async () => {
  const user = userEvent.setup();
  const blocked = structuredClone(detail);
  blocked.workflow.unconfirmed_action = true;
  vi.spyOn(api, "incidentDetail").mockResolvedValue(blocked);
  render(<IncidentWorkbench actor={actor} incidents={[blocked.incident]} loadError={null}
    refreshToken="1" onChanged={onChanged} />);
  await screen.findByLabelText("Review note");
  await waitFor(() => expect(screen.queryByText("Refreshing incident evidence...")).toBeNull());
  await user.type(screen.getByLabelText("Review note"), "New review must wait for reconciliation.");
  expect((screen.getByRole("button", { name: "Mark reviewed" }) as HTMLButtonElement).disabled).toBe(true);
  expect((screen.getByRole("button", { name: "Run safety check" }) as HTMLButtonElement).disabled).toBe(true);
  await user.click(screen.getByText("Close or escalate incident"));
  await user.type(screen.getByLabelText("Validation evidence"), "Must not claim recovery with an unconfirmed action.");
  expect((screen.getByRole("button", { name: "Record outcome" }) as HTMLButtonElement).disabled).toBe(true);
});

it("preserves a safety result across polling and invalidates approval for new evidence", async () => {
  const user = userEvent.setup();
  const detailRequest = vi.spyOn(api, "incidentDetail").mockResolvedValue(detail);
  vi.spyOn(api, "dryRun").mockResolvedValue({ result: action, httpStatus: 200 });
  const props = { actor, incidents: [detail.incident], loadError: null, refreshToken: "1", onChanged };
  const view = render(<IncidentWorkbench {...props} />);
  const approve = await screen.findByRole<HTMLButtonElement>("button", { name: "Approve & shift traffic" });
  expect(approve.disabled).toBe(true);
  await user.type(screen.getByLabelText("Review note"), "Reviewed the route evidence.");
  await user.click(screen.getByRole("button", { name: "Run safety check" }));
  expect(api.dryRun).toHaveBeenCalledWith(detail.incident.id, detail.playbook.id, {
    actor_id: actor.actor_id, note: "Reviewed the route evidence.",
    prediction_id: detail.incident.prediction.prediction_id
  });
  await waitFor(() => expect(approve.disabled).toBe(false));
  view.rerender(<IncidentWorkbench {...props} refreshToken="2" />);
  await waitFor(() => expect(approve.disabled).toBe(false));
  expect(screen.getByText(/Safety check passed/)).toBeTruthy();

  const changed = structuredClone(detail);
  changed.incident.prediction.prediction_id = "pred-new-evidence";
  detailRequest.mockResolvedValue(changed);
  view.rerender(<IncidentWorkbench {...props} incidents={[changed.incident]} refreshToken="3" />);
  await waitFor(() => expect(screen.queryByText("Refreshing incident evidence...")).toBeNull());
  expect(approve.disabled).toBe(true);
  expect(screen.getByText(/earlier evidence window/)).toBeTruthy();
});

it("keeps the captured brief and labels it when prediction evidence changes", async () => {
  const user = userEvent.setup();
  const detailRequest = vi.spyOn(api, "incidentDetail").mockResolvedValue(detail);
  const brief: IncidentBrief = {
    evidence_current_at_completion: true,
    schema_version: "1.0", incident_id: detail.incident.id,
    summary: "A cited summary for this evidence window.", situation: ["Evidence was checked."],
    root_cause_hypothesis: "Primary path degradation remains a hypothesis.",
    impact: "Branch application traffic may be affected.", recommended_playbook_id: null,
    citations: [{ source_id: "runbook:test:1", claim: "Manual checks are required." }],
    confidence: 0.5, limitations: ["Advisory only."],
    grounding: { mode: "evidence-selection-v1", incident_sha256: "a".repeat(64),
      incident_snapshot: detail.incident, claims: [] }
  };
  vi.spyOn(api, "incidentBrief").mockResolvedValue(brief);
  const props = { actor, incidents: [detail.incident], loadError: null, refreshToken: "1", onChanged };
  const view = render(<IncidentWorkbench {...props} />);
  await user.click(await screen.findByRole("button", { name: "Generate brief" }));
  expect(await screen.findByText(brief.summary)).toBeTruthy();
  view.rerender(<IncidentWorkbench {...props} refreshToken="2" />);
  expect(screen.getByText(brief.summary)).toBeTruthy();
  const changed = structuredClone(detail);
  changed.incident.prediction.prediction_id = "pred-new";
  detailRequest.mockResolvedValue(changed);
  view.rerender(<IncidentWorkbench {...props} incidents={[changed.incident]} refreshToken="3" />);
  expect(screen.getByText(brief.summary)).toBeTruthy();
  expect(screen.getByText(/This brief describes captured evidence from an earlier network state/)).toBeTruthy();
});

it("shows an incident loading failure without exposing action controls", async () => {
  vi.spyOn(api, "incidentDetail").mockRejectedValue(new Error("Evidence is unavailable."));
  render(<IncidentWorkbench actor={actor} incidents={[detail.incident]} loadError={null} refreshToken="1" onChanged={onChanged} />);
  expect(await screen.findByRole("alert")).toHaveProperty("textContent", "Evidence is unavailable.");
  expect(screen.queryByRole("button", { name: "Approve & shift traffic" })).toBeNull();
});

it("disables device controls when the server has no configured adapter", async () => {
  const user = userEvent.setup();
  const monitoring = structuredClone(detail);
  monitoring.device_dispatch_enabled = false;
  vi.spyOn(api, "incidentDetail").mockResolvedValue(monitoring);
  const dryRun = vi.spyOn(api, "dryRun");
  render(<IncidentWorkbench actor={actor} incidents={[monitoring.incident]} loadError={null}
    refreshToken="1" onChanged={onChanged} />);
  await user.type(await screen.findByLabelText("Review note"), "Checked live path evidence.");
  for (const name of ["Run safety check", "Approve & shift traffic", "Restore primary path"]) {
    expect((screen.getByRole("button", { name }) as HTMLButtonElement).disabled).toBe(true);
  }
  expect((screen.getByRole("button", { name: "Mark reviewed" }) as HTMLButtonElement).disabled).toBe(false);
  expect(dryRun).not.toHaveBeenCalled();
});
