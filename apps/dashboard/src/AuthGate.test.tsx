// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { api } from "./api";
import { AuthGate } from "./AuthGate";
import type { BrowserSession } from "./types";

vi.mock("./App", () => ({
  default: ({ actor, onSignOut }: { actor: { actor_id: string }; onSignOut?: () => void }) => <>
    <p>Workspace for {actor.actor_id}</p>
    {onSignOut ? <button type="button" onClick={onSignOut}>Sign out</button> : null}
  </>
}));
const loggedOut: BrowserSession = { authenticated: false, mode: "local", actor: null, csrf_token: null, expires_at: null };
const loggedIn: BrowserSession = {
  authenticated: true, mode: "local", actor: { actor_id: "reviewer-01", role: "reviewer" },
  csrf_token: "csrf-example", expires_at: "2026-09-10T20:00:00Z"
};
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it("uses the server identity and removes the workspace after sign-out", async () => {
  const user = userEvent.setup();
  vi.spyOn(api, "session").mockResolvedValue(loggedOut);
  const login = vi.spyOn(api, "login").mockResolvedValue(loggedIn);
  const logout = vi.spyOn(api, "logout").mockResolvedValue(undefined);
  render(<AuthGate />);
  await user.type(await screen.findByLabelText("Username"), "reviewer");
  await user.type(screen.getByLabelText("Password"), "long passphrase for testing");
  await user.click(screen.getByRole("button", { name: "Sign in" }));
  expect(await screen.findByText("Workspace for reviewer-01")).toBeTruthy();
  expect(login).toHaveBeenCalledWith("reviewer", "long passphrase for testing");
  expect(screen.queryByLabelText("Password")).toBeNull();
  await user.click(screen.getByRole("button", { name: "Sign out" }));
  await waitFor(() => expect(logout).toHaveBeenCalledOnce());
  expect(await screen.findByLabelText("Password")).toHaveProperty("value", "");
  expect(screen.queryByText("Workspace for reviewer-01")).toBeNull();
});

it("shows a sign-in failure without mounting the workspace", async () => {
  const user = userEvent.setup();
  vi.spyOn(api, "session").mockResolvedValue(loggedOut);
  vi.spyOn(api, "login").mockRejectedValue(new Error("Invalid username or password."));
  render(<AuthGate />);
  await user.type(await screen.findByLabelText("Username"), "reviewer");
  await user.type(screen.getByLabelText("Password"), "wrong password");
  await user.click(screen.getByRole("button", { name: "Sign in" }));
  expect(await screen.findByRole("alert")).toHaveProperty("textContent", "Invalid username or password.");
  expect(screen.queryByText("Workspace for reviewer-01")).toBeNull();
});
