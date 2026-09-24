// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import replay from "./__fixtures__/replay.json";
import { ObservabilityStudio } from "./ObservabilityStudio";
import type { IncidentCard, IncidentDetail, MetricRecord, Topology, TrafficDemo } from "./types";

afterEach(cleanup);

it("renders the selected observability layout from API-shaped replay data", () => {
  const topology = replay.topology as unknown as Topology;
  const incident = replay.incidents[0] as unknown as IncidentCard;
  render(
    <ObservabilityStudio
      actor={{ actor_id: "local-operator", role: "operator" }}
      fixtureOnly
      simulationActive={false}
      apiConnected
      telemetryReady
      detectionError={null}
      primaryIncident={incident}
      detail={replay.detail as unknown as IncidentDetail}
      metrics={replay.metrics as unknown as MetricRecord[]}
      traffic={replay.traffic as unknown as TrafficDemo}
      trafficVerified
      fileSent
      fileReceived
      sender="Branch-01"
      receiver="DC-01"
      topology={topology}
      path={topology.paths[0]}
      alertCount={1}
      lastUpdated="2026-09-21T12:00:00Z"
      refreshing={false}
      refreshSeconds={5}
      onRefresh={vi.fn()}
    >
      <p>Full operator workflow</p>
    </ObservabilityStudio>
  );

  expect(screen.getByRole("heading", { name: "Observability Studio" })).toBeTruthy();
  expect(screen.getByRole("heading", { name: "Fault detected" })).toBeTruthy();
  expect(screen.getByText("Recorded replay")).toBeTruthy();
  expect(screen.getByRole("heading", { name: "Risk of Impact" })).toBeTruthy();
  expect(screen.getByRole("heading", { name: "Application Test" })).toBeTruthy();
  expect(screen.getByText("Full operator workflow")).toBeTruthy();

  const menuButton = screen.getByRole("button", { name: "Open navigation menu" });
  expect(menuButton.getAttribute("aria-expanded")).toBe("false");
  fireEvent.click(menuButton);
  expect(screen.getByRole("button", { name: "Close navigation menu" }).getAttribute("aria-expanded")).toBe("true");
  fireEvent.keyDown(document, { key: "Escape" });
  expect(screen.getByRole("button", { name: "Open navigation menu" }).getAttribute("aria-expanded")).toBe("false");
});
