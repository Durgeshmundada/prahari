// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { ActionOperations } from "./ActionOperations";
import { api } from "./api";
import replay from "./__fixtures__/replay.json";
import type { ActionOperation } from "./types";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

const operation: ActionOperation = {
  id: "action-test-01", incident_id: replay.detail.incident.id, playbook_id: replay.detail.playbook.id,
  prediction_id: replay.detail.incident.prediction.prediction_id,
  prediction_snapshot: replay.detail.incident.prediction, kind: "apply", actor_id: "operator-01",
  note: "Test dispatch request.", created_at: "2026-09-14T00:00:00Z", updated_at: "2026-09-14T00:00:30Z",
  status: "unknown", result_status: null, observed_route: null,
  message: "Adapter timed out; route outcome unknown.", reconciliation: null
};
const reviewer = { actor_id: "reviewer-02", role: "reviewer" as const };
const onChanged = vi.fn(async () => {});

async function fillEvidence(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("Evidence reference"), "offline/route-evidence");
  await user.type(screen.getByLabelText("Evidence SHA-256"), "e".repeat(64));
  await user.type(screen.getByLabelText("Verification note"), "Primary route independently verified after dispatch stopped.");
}

it("shows unknown outcomes independently and keeps reconciliation unavailable to viewers", async () => {
  vi.spyOn(api, "actionOperations").mockResolvedValue({ operations: [operation], dispatch_enabled: false });
  render(<ActionOperations actor={{ actor_id: "viewer-01", role: "viewer" }} refreshToken="1" onChanged={onChanged} />);
  expect(await screen.findByText(/Adapter timed out/)).toBeTruthy();
  expect(screen.getByText(/Further dispatch and incident closure are blocked/)).toBeTruthy();
  expect(screen.queryByText("Record verified state")).toBeNull();
});

it("requires independent evidence and quiescence before recording verified state", async () => {
  const user = userEvent.setup();
  const request = vi.spyOn(api, "actionOperations").mockResolvedValue({ operations: [operation], dispatch_enabled: false });
  const reconcile = vi.spyOn(api, "reconcileAction").mockImplementation(async (_id, payload) => {
    const saved = { ...operation, status: "finished" as const, reconciliation: payload, observed_route: payload.observed_route };
    request.mockResolvedValue({ operations: [saved], dispatch_enabled: false });
    return saved;
  });
  render(<ActionOperations actor={reviewer} refreshToken="1" onChanged={onChanged} />);
  const button = await screen.findByRole("button", { name: "Record verified state" });
  expect((button as HTMLButtonElement).disabled).toBe(true);
  await fillEvidence(user);
  expect((button as HTMLButtonElement).disabled).toBe(true);
  await user.click(screen.getByRole("checkbox"));
  await user.click(button);
  await waitFor(() => expect(reconcile).toHaveBeenCalledOnce());
  expect(reconcile.mock.calls[0][1]).toMatchObject({ actor_id: "reviewer-02", dispatch_quiesced: true, evidence_sha256: "e".repeat(64) });
  expect(await screen.findByText(/This records route state; it does not establish application recovery/)).toBeTruthy();
});

it("blocks reconciliation while dispatch is enabled and when the requesting operator is the reviewer", async () => {
  const user = userEvent.setup();
  vi.spyOn(api, "actionOperations").mockResolvedValue({ operations: [operation], dispatch_enabled: true });
  render(<ActionOperations actor={{ actor_id: "operator-01", role: "admin" }} refreshToken="1" onChanged={onChanged} />);
  const button = await screen.findByRole("button", { name: "Record verified state" });
  await fillEvidence(user); await user.click(screen.getByRole("checkbox"));
  expect((button as HTMLButtonElement).disabled).toBe(true);
  expect(screen.getByText(/requesting operator cannot reconcile/)).toBeTruthy();
});

it("retains uncertain records but blocks reconciliation when refresh fails", async () => {
  const request = vi.spyOn(api, "actionOperations").mockResolvedValue({ operations: [operation], dispatch_enabled: false });
  const view = render(<ActionOperations actor={reviewer} refreshToken="1" onChanged={onChanged} />);
  await screen.findByText(/Adapter timed out/);
  request.mockRejectedValue(new Error("Connection lost"));
  view.rerender(<ActionOperations actor={reviewer} refreshToken="2" onChanged={onChanged} />);
  expect(await screen.findByText(/Previously loaded records may be stale/)).toBeTruthy();
  expect(screen.getByText(/Adapter timed out/)).toBeTruthy();
  expect((screen.getByRole("button", { name: "Record verified state" }) as HTMLButtonElement).disabled).toBe(true);
});
