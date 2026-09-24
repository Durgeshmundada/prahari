// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { api } from "./api";
import { ClosureCases } from "./ClosureCases";
import { incidentDetailSchema } from "./schemas";
import replay from "./__fixtures__/replay.json";
import type { ClosureCase } from "./types";

const detail = incidentDetailSchema.parse(replay.detail);
const closure: ClosureCase = {
  id: "case-test", incident_id: detail.incident.id, prediction_id: detail.incident.prediction.prediction_id,
  created_at: "2026-09-10T10:00:00Z", submission: { actor_id: "operator-01", outcome: "resolved", note: "Application recovery independently measurable." },
  incident: detail.incident, feature_window: detail.feature_window, app_impact_started: false,
  recovery_comparison: null, review: null, reviewed_at: null
};
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it("lets an independent reviewer record a decision against the selected snapshot", async () => {
  const user = userEvent.setup();
  const cases = vi.spyOn(api, "cases").mockResolvedValue([closure]);
  const reviewed: ClosureCase = { ...closure, review: { actor_id: "reviewer-01", decision: "approved", note: "Recovery evidence checked." }, reviewed_at: "2026-09-10T10:10:00Z" };
  const decide = vi.spyOn(api, "decideCase").mockImplementation(async () => { cases.mockResolvedValue([reviewed]); return reviewed; });
  render(<ClosureCases actor={{ actor_id: "reviewer-01", role: "reviewer" }} refreshToken="1" onChanged={async () => {}} />);
  await user.click(await screen.findByRole("button", { name: /resolved/ }));
  expect(screen.getByText(closure.submission.note)).toBeTruthy();
  await user.type(screen.getByLabelText("Independent review note"), "Recovery evidence checked.");
  await user.click(screen.getByRole("button", { name: "Record review decision" }));
  await waitFor(() => expect(decide).toHaveBeenCalledWith("case-test", reviewed.review));
  expect(await screen.findByText("approved by reviewer-01")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Record review decision" })).toBeNull();
});

it("does not offer self-review even to an admin", async () => {
  vi.spyOn(api, "cases").mockResolvedValue([closure]);
  const user = userEvent.setup();
  render(<ClosureCases actor={{ actor_id: "operator-01", role: "admin" }} refreshToken="1" onChanged={async () => {}} />);
  await user.click(await screen.findByRole("button", { name: /resolved/ }));
  expect(screen.queryByLabelText("Independent review note")).toBeNull();
  expect(screen.getByText("An independent reviewer must assess this submission.")).toBeTruthy();
});

it("sends indexed applicability filters to case search", async () => {
  const cases = vi.spyOn(api, "cases").mockResolvedValue([closure]);
  const user = userEvent.setup();
  render(<ClosureCases actor={{ actor_id: "reviewer-01", role: "reviewer" }} refreshToken="1" onChanged={async () => {}} />);
  await screen.findByRole("button", { name: /resolved/ });
  await user.type(screen.getByLabelText("Entity ID"), "tunnel:test");
  expect(cases).not.toHaveBeenLastCalledWith(
    expect.objectContaining({ entity_id: "tunnel:test" }), 0
  );
  await user.click(screen.getByRole("button", { name: "Search cases" }));
  await waitFor(() => expect(cases).toHaveBeenLastCalledWith(
    expect.objectContaining({ entity_id: "tunnel:test" }), 0
  ));
});
