import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ChangeEvent, FormEvent } from "react";
import { api, ApiError, isTrafficDemoDownloadUrl } from "./api";
import { IncidentWorkbench } from "./IncidentWorkbench";
import { ClosureCases } from "./ClosureCases";
import { ActionOperations } from "./ActionOperations";
import { ObservabilityStudio } from "./ObservabilityStudio";
import type {
  ActorIdentity,
  Health,
  IncidentCard,
  IncidentDetail,
  MetricRecord,
  NetworkPath,
  Topology,
  TrafficDemo,
  TrafficDemoRunResponse
} from "./types";

const configuredRefreshInterval = Number(import.meta.env.VITE_DASHBOARD_REFRESH_MS);
const REFRESH_INTERVAL_MS = Number.isFinite(configuredRefreshInterval) && configuredRefreshInterval >= 1_000
  ? Math.min(configuredRefreshInterval, 300_000)
  : 5_000;
const REFRESH_INTERVAL_SECONDS = Math.max(1, Math.round(REFRESH_INTERVAL_MS / 1_000));
const DEFAULT_MAX_FILE_BYTES = 5 * 1024 * 1024;
const SAFE_FILE_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$/;
const SHA256_HEX = /^[0-9a-f]{64}$/;

type ReadinessState = {
  status: "unknown" | "ready" | "degraded";
  reason: string | null;
};

type TrafficDemoState = {
  status: "unknown" | "available" | "not_configured" | "unavailable";
  data: TrafficDemo | null;
  reason: string | null;
  confirmationIssue: string | null;
};

type RunTestState = {
  phase: "idle" | "uploading" | "testing" | "result" | "error";
  outcome: "passed" | "failed" | null;
  progress: number | null;
  loadedBytes: number | null;
  totalBytes: number | null;
  message: string | null;
};

type VerifiedDownload = {
  runId: string;
  transferId: string;
  url: string;
  expiresAt: string;
  fileName: string;
  sizeBytes: number;
  sha256: string;
};

type DownloadState = {
  status: "idle" | "downloading" | "success" | "unavailable" | "error";
  message: string | null;
};

type DashboardState = {
  health: Health | null;
  readiness: ReadinessState;
  topology: Topology | null;
  topologyError: string | null;
  incidents: IncidentCard[];
  incidentsError: string | null;
  traffic: TrafficDemoState;
};

const initialState: DashboardState = {
  health: null,
  readiness: { status: "unknown", reason: null },
  topology: null,
  topologyError: null,
  incidents: [],
  incidentsError: null,
  traffic: { status: "unknown", data: null, reason: null, confirmationIssue: null }
};

const initialRunTestState: RunTestState = {
  phase: "idle",
  outcome: null,
  progress: null,
  loadedBytes: null,
  totalBytes: null,
  message: null
};

const initialDownloadState: DownloadState = {
  status: "idle",
  message: null
};

function messageFrom(error: unknown): string {
  return error instanceof Error ? error.message : "Something went wrong.";
}

function requestMessage(label: string, error: unknown): string {
  if (error instanceof ApiError) {
    return `${label}: ${error.message}`;
  }
  return `${label}: ${messageFrom(error)}`;
}

function formatDateTime(value: string | null | undefined): string {
  if (!value) {
    return "Not available";
  }
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) {
    return value;
  }
  return `${new Intl.DateTimeFormat("en-IN", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "UTC"
  }).format(date)} UTC`;
}

function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "—";
  }
  if (value < 1024) {
    return `${Math.round(value)} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`;
  }
  if (value < 1024 * 1024 * 1024) {
    return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  }
  return `${(value / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

function formatDelay(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "—";
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ms`;
}

function formatSpeed(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "—";
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} Mbps`;
}

function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "—";
  }
  return `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%`;
}

function readableLabel(value: string | null | undefined): string {
  if (!value) {
    return "No fault identified";
  }
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function isRecentMeasurement(value: string | null | undefined): boolean {
  if (!value) {
    return false;
  }
  const measuredAt = Date.parse(value);
  return Number.isFinite(measuredAt) && Math.abs(Date.now() - measuredAt) <= 15 * 60_000;
}

function configuredPath(topology: Topology | null, pathId: string | null | undefined): NetworkPath | null {
  if (!topology?.paths.length) {
    return null;
  }
  return topology.paths.find((path) => path.id === pathId) ?? topology.paths[0];
}

function pathEndpoints(topology: Topology | null, path: NetworkPath | null): [string, string] {
  if (!topology || !path?.site_ids.length) {
    return ["Sender", "Receiver"];
  }
  const siteName = (siteId: string) =>
    topology.inventory.sites.find((site) => site.id === siteId)?.name ?? siteId;
  return [siteName(path.site_ids[0]), siteName(path.site_ids[path.site_ids.length - 1])];
}

function latestTestTitle(traffic: TrafficDemoState): string {
  if (traffic.status === "unknown") {
    return "Loading latest test";
  }
  if (traffic.status === "not_configured") {
    return "Test monitor is not connected";
  }
  if (traffic.status === "unavailable") {
    return "Test monitor is unavailable";
  }
  if (traffic.confirmationIssue) {
    return "Test result needs attention";
  }
  switch (traffic.data?.status) {
    case "passed":
      return isVerifiedResult(traffic) ? "File received and checked" : "Test result is incomplete";
    case "failed":
      return "Test failed";
    case "running":
      return "Test in progress";
    case "unavailable":
      return "Latest test has expired";
    case "idle":
    default:
      return "No test measured yet";
  }
}

function latestTestTone(traffic: TrafficDemoState): "success" | "danger" | "working" | "neutral" {
  if (
    traffic.status === "unavailable" ||
    traffic.data?.status === "failed" ||
    Boolean(traffic.confirmationIssue)
  ) {
    return "danger";
  }
  if (traffic.data?.status === "passed") {
    return isVerifiedResult(traffic) ? "success" : "danger";
  }
  if (traffic.status === "unknown" || traffic.data?.status === "running") {
    return "working";
  }
  return "neutral";
}

function latestTestMessage(traffic: TrafficDemoState): string {
  if (traffic.status === "not_configured") {
    return "Connect the two-server traffic monitor to show measured file delivery here.";
  }
  if (traffic.status === "unavailable") {
    return traffic.reason ?? "The traffic monitor could not be reached.";
  }
  if (traffic.confirmationIssue) {
    return traffic.data?.message
      ? `${traffic.data.message} ${traffic.confirmationIssue}`
      : traffic.confirmationIssue;
  }
  if (traffic.data?.message) {
    return traffic.data.message;
  }
  if (traffic.data?.status === "idle") {
    return "Run a file test between your two servers to see the result.";
  }
  if (traffic.data?.status === "unavailable") {
    return "The previous measurement is too old. Run another test.";
  }
  if (traffic.data?.status === "running") {
    return "The sender and receiver are currently exchanging test data.";
  }
  return "This result comes from the measured sender and receiver test.";
}

function isFileReceived(traffic: TrafficDemoState): boolean {
  const payload = traffic.data?.payload;
  return Boolean(
    payload &&
      payload.size_bytes !== undefined &&
      payload.bytes_received !== undefined &&
      payload.size_bytes !== null &&
      payload.bytes_received !== null &&
      payload.size_bytes > 0 &&
      payload.bytes_received === payload.size_bytes
  );
}

function isFileChecked(traffic: TrafficDemoState): boolean {
  const payload = traffic.data?.payload;
  return Boolean(
    payload?.integrity === "verified" &&
      typeof payload.sha256 === "string" &&
      SHA256_HEX.test(payload.sha256)
  );
}

function isVerifiedResult(traffic: TrafficDemoState): boolean {
  const payload = traffic.data?.payload;
  return Boolean(
    !traffic.confirmationIssue &&
      traffic.data?.status === "passed" &&
      payload &&
      typeof payload.size_bytes === "number" &&
      payload.size_bytes > 0 &&
      payload.bytes_sent === payload.size_bytes &&
      payload.bytes_received === payload.size_bytes &&
      payload.integrity === "verified" &&
      typeof payload.sha256 === "string" &&
      SHA256_HEX.test(payload.sha256)
  );
}

function fileValidationMessage(file: File, maxFileBytes: number): string | null {
  if (file.size < 1) {
    return "Choose a file that is not empty.";
  }
  if (file.size > maxFileBytes) {
    return `Choose a file no larger than ${formatBytes(maxFileBytes)}.`;
  }
  if (!SAFE_FILE_NAME.test(file.name)) {
    return "Rename the file so it starts with a letter or number and uses only letters, numbers, dots, dashes, or underscores (maximum 120 characters).";
  }
  return null;
}

function runConfirmationIssue(
  response: TrafficDemoRunResponse,
  selectedFile: File
): string | null {
  if (!response.run_id?.trim() || !response.transfer_id?.trim()) {
    return "The API did not return identifiers that tie this receipt to the requested test.";
  }
  if (!response.evidence_saved) {
    return "The transfer evidence was not saved, so this run was not marked verified.";
  }
  if (
    response.traffic.payload?.name !== selectedFile.name ||
    response.traffic.payload?.size_bytes !== selectedFile.size
  ) {
    return "The receiver receipt did not match the selected file, so this run was not marked verified.";
  }
  return null;
}

function verifiedDownloadFromRun(
  response: TrafficDemoRunResponse,
  selectedFile: File,
  verified: boolean
): { download: VerifiedDownload | null; issue: string | null } {
  if (!verified || response.downloadable !== true) {
    return { download: null, issue: null };
  }
  if (!isTrafficDemoDownloadUrl(response.download_url)) {
    return {
      download: null,
      issue: "The verified run did not include a safe receiver download reference."
    };
  }
  if (
    typeof response.download_expires_at !== "string" ||
    !Number.isFinite(Date.parse(response.download_expires_at))
  ) {
    return {
      download: null,
      issue: "The verified run did not include a valid download expiry time."
    };
  }
  if (Date.parse(response.download_expires_at) <= Date.now()) {
    return {
      download: null,
      issue: "The retained receiver copy had already expired. Run a new test to download it."
    };
  }
  return {
    download: {
      runId: response.run_id,
      transferId: response.transfer_id,
      url: response.download_url,
      expiresAt: response.download_expires_at,
      fileName: selectedFile.name,
      sizeBytes: selectedFile.size,
      sha256: response.traffic.payload?.sha256 ?? ""
    },
    issue: null
  };
}

function saveBlob(blob: Blob, fileName: string): void {
  const objectUrl = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = objectUrl;
  link.download = fileName;
  link.hidden = true;
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
}

async function blobSha256(blob: Blob): Promise<string | null> {
  if (!globalThis.crypto?.subtle) {
    return null;
  }
  const digest = await globalThis.crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function hasFixtureData(state: DashboardState): boolean {
  return Boolean(state.health?.fixture_only || state.topology?.fixture_only);
}

export default function App({ actor, onSignOut }: { actor: ActorIdentity; onSignOut?: () => void }) {
  const [state, setState] = useState<DashboardState>(initialState);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastUpdated, setLastUpdated] = useState<string | null>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [runTest, setRunTest] = useState<RunTestState>(initialRunTestState);
  const [verifiedDownload, setVerifiedDownload] = useState<VerifiedDownload | null>(null);
  const [downloadState, setDownloadState] = useState<DownloadState>(initialDownloadState);
  const [overviewDetail, setOverviewDetail] = useState<IncidentDetail | null>(null);
  const [overviewMetrics, setOverviewMetrics] = useState<MetricRecord[]>([]);
  const refreshGeneration = useRef(0);
  const runInProgress = useRef(false);

  const refresh = useCallback(async () => {
    const generation = ++refreshGeneration.current;
    setError(null);
    const [healthResult, readinessResult, topologyResult, incidentsResult, trafficResult] =
      await Promise.allSettled([
        api.health(),
        api.readyz(),
        api.topology(),
        api.incidents(),
        api.trafficDemo()
      ]);

    if (generation !== refreshGeneration.current || runInProgress.current) {
      return;
    }

    if (healthResult.status !== "fulfilled") {
      const reason = requestMessage("Dashboard cannot reach the API", healthResult.reason);
      setState({
        ...initialState,
        readiness: { status: "degraded", reason }
      });
      setError(reason);
      setLoading(false);
      return;
    }

    setState({
      health: healthResult.value,
      readiness:
        readinessResult.status === "fulfilled"
          ? { status: "ready", reason: null }
          : {
              status: "degraded",
              reason: requestMessage("Telemetry check needs attention", readinessResult.reason)
            },
      topology: topologyResult.status === "fulfilled" ? topologyResult.value : null,
      topologyError:
        topologyResult.status === "rejected"
          ? requestMessage("Configured route is unavailable", topologyResult.reason)
          : null,
      incidents: incidentsResult.status === "fulfilled" ? incidentsResult.value : [],
      incidentsError:
        incidentsResult.status === "rejected"
          ? requestMessage("Incident list is unavailable", incidentsResult.reason)
          : null,
      traffic:
        trafficResult.status === "fulfilled"
          ? trafficResult.value
            ? {
                status: "available",
                data: trafficResult.value,
                reason: null,
                confirmationIssue: null
              }
            : {
                status: "not_configured",
                data: null,
                reason: null,
                confirmationIssue: null
              }
          : {
              status: "unavailable",
              data: null,
              reason: requestMessage("Traffic monitor is unavailable", trafficResult.reason),
              confirmationIssue: null
            }
    });
    setLastUpdated(new Date().toISOString());
    setLoading(false);
  }, []);

  useEffect(() => {
    void refresh();
    const interval = window.setInterval(() => void refresh(), REFRESH_INTERVAL_MS);
    return () => window.clearInterval(interval);
  }, [refresh]);

  useEffect(() => {
    if (!verifiedDownload) {
      return;
    }

    let timeout: number | undefined;
    const expireWhenNeeded = () => {
      const remainingMs = Date.parse(verifiedDownload.expiresAt) - Date.now();
      if (remainingMs <= 0) {
        setVerifiedDownload(null);
        setDownloadState({
          status: "unavailable",
          message: "The retained receiver copy expired. Run a new test to download it."
        });
        return;
      }
      timeout = window.setTimeout(expireWhenNeeded, Math.min(remainingMs, 60_000));
    };
    expireWhenNeeded();
    return () => {
      if (timeout !== undefined) {
        window.clearTimeout(timeout);
      }
    };
  }, [verifiedDownload]);

  const path = useMemo(
    () => configuredPath(state.topology, state.traffic.data?.path_id),
    [state.topology, state.traffic.data?.path_id]
  );
  const fallbackEndpoints = useMemo(() => pathEndpoints(state.topology, path), [state.topology, path]);
  const sender = state.traffic.data?.source?.name ?? fallbackEndpoints[0];
  const receiver = state.traffic.data?.destination?.name ?? fallbackEndpoints[1];
  const fixtureOnly = hasFixtureData(state);
  const alertCount = state.incidents.filter((incident) => incident.severity !== "informational").length;
  const primaryIncident = state.incidents.reduce<IncidentCard | null>((current, incident) => {
    if (!current) {
      return incident;
    }
    return incident.prediction.impact_risk.within_120_seconds
      > current.prediction.impact_risk.within_120_seconds
      ? incident
      : current;
  }, null);
  const faultDetected = Boolean(
    primaryIncident
      && primaryIncident.prediction.likely_incident_type !== "normal"
      && primaryIncident.severity !== "informational"
  );
  const impactRisk = primaryIncident?.prediction.impact_risk.within_120_seconds ?? 0;
  const riskPercent = Math.round(Math.max(0, Math.min(1, impactRisk)) * 100);
  const simulationActive = Boolean(
    primaryIncident?.evidence_ids.some((evidenceId) => evidenceId === "simulation:local-dashboard-only")
  );
  const detectionUnavailable = Boolean(state.incidentsError);
  const detectionTone = detectionUnavailable ? "unavailable" : faultDetected ? "alert" : "clear";
  const traffic = state.traffic.data;
  const recentTraffic = isRecentMeasurement(traffic?.updated_at);

  useEffect(() => {
    const incidentId = primaryIncident?.id;
    const entityId = primaryIncident?.prediction.entity_id;
    if (!incidentId || !entityId) {
      setOverviewDetail(null);
      setOverviewMetrics([]);
      return;
    }
    let active = true;
    void Promise.allSettled([api.incidentDetail(incidentId), api.metrics(entityId)]).then(([detailResult, metricsResult]) => {
      if (!active) return;
      setOverviewDetail(detailResult.status === "fulfilled" ? detailResult.value : null);
      setOverviewMetrics(metricsResult.status === "fulfilled" ? metricsResult.value : []);
    });
    return () => { active = false; };
  }, [primaryIncident?.id, primaryIncident?.prediction.prediction_id]);
  const tone = latestTestTone(state.traffic);
  const sent = Boolean(
    traffic?.payload?.size_bytes &&
      traffic.payload.size_bytes > 0 &&
      traffic.payload.bytes_sent === traffic.payload.size_bytes
  );
  const received = isFileReceived(state.traffic);
  const checked = isFileChecked(state.traffic);
  const advertisedMaxFileBytes = traffic?.max_file_bytes;
  const maxFileBytes =
    typeof advertisedMaxFileBytes === "number" &&
    Number.isFinite(advertisedMaxFileBytes) &&
    advertisedMaxFileBytes > 0
      ? advertisedMaxFileBytes
      : DEFAULT_MAX_FILE_BYTES;
  const canRunTest = (actor.role === "operator" || actor.role === "admin")
    && state.traffic.status === "available" && traffic?.can_run === true;
  const runBusy = runTest.phase === "uploading" || runTest.phase === "testing";
  const downloadBusy = downloadState.status === "downloading";
  const runUnavailableMessage = loading
    ? "Checking whether file tests are available…"
    : state.traffic.status === "not_configured"
      ? "This API does not provide the two-server file test."
      : state.traffic.status === "unavailable"
        ? "File tests are unavailable while the traffic monitor cannot be reached."
        : !canRunTest
          ? "Browser-run file tests are disabled on this API."
          : null;

  function handleFileSelection(event: ChangeEvent<HTMLInputElement>) {
    const file = event.currentTarget.files?.[0] ?? null;
    if (!file) {
      setSelectedFile(null);
      setRunTest(initialRunTestState);
      return;
    }

    const validationMessage = fileValidationMessage(file, maxFileBytes);
    if (validationMessage) {
      event.currentTarget.value = "";
      setSelectedFile(null);
      setRunTest({
        ...initialRunTestState,
        phase: "error",
        message: validationMessage
      });
      return;
    }

    setSelectedFile(file);
    setRunTest(initialRunTestState);
  }

  async function handleRunTest(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedFile || runBusy || downloadBusy || !canRunTest) {
      return;
    }

    const validationMessage = fileValidationMessage(selectedFile, maxFileBytes);
    if (validationMessage) {
      setRunTest({
        ...initialRunTestState,
        phase: "error",
        message: validationMessage
      });
      return;
    }

    setRunTest({
      phase: "uploading",
      outcome: null,
      progress: 0,
      loadedBytes: 0,
      totalBytes: selectedFile.size,
      message: null
    });
    runInProgress.current = true;
    refreshGeneration.current += 1;

    try {
      const response = await api.runTrafficDemo(selectedFile, {
        onUploadProgress: ({ loaded_bytes, total_bytes, percent }) => {
          setRunTest((current) =>
            current.phase === "uploading"
              ? {
                  ...current,
                  progress: percent,
                  loadedBytes: loaded_bytes,
                  totalBytes: total_bytes
                }
              : current
          );
        },
        onUploadComplete: () => {
          setRunTest((current) =>
            current.phase === "uploading"
              ? {
                  ...current,
                  phase: "testing",
                  progress: null,
                  loadedBytes: selectedFile.size,
                  totalBytes: selectedFile.size
                }
              : current
          );
        }
      });

      const confirmationIssue = runConfirmationIssue(response, selectedFile);
      const returnedTraffic = {
        ...response.traffic,
        can_run: response.traffic.can_run ?? traffic?.can_run,
        max_file_bytes: response.traffic.max_file_bytes ?? traffic?.max_file_bytes
      };
      const returnedTrafficState: TrafficDemoState = {
        status: "available",
        data: returnedTraffic,
        reason: null,
        confirmationIssue
      };
      const verified = isVerifiedResult(returnedTrafficState);
      const downloadOffer = verifiedDownloadFromRun(response, selectedFile, verified);

      refreshGeneration.current += 1;
      setState((current) => ({ ...current, traffic: returnedTrafficState }));
      setVerifiedDownload(downloadOffer.download);
      setDownloadState(
        downloadOffer.issue
          ? { status: "unavailable", message: downloadOffer.issue }
          : initialDownloadState
      );
      setLastUpdated(response.traffic.updated_at ?? new Date().toISOString());
      setRunTest({
        phase: "result",
        outcome: verified ? "passed" : "failed",
        progress: null,
        loadedBytes: selectedFile.size,
        totalBytes: selectedFile.size,
        message: verified
          ? `Test passed. The receiver confirmed all ${formatBytes(selectedFile.size)}, the SHA-256 checksum matched, and the evidence was saved.`
          : [response.traffic.message, confirmationIssue]
              .filter((item): item is string => Boolean(item))
              .join(" ") || "The test finished without a complete verified receiver receipt."
      });
      runInProgress.current = false;
    } catch (runError) {
      refreshGeneration.current += 1;
      runInProgress.current = false;
      setRunTest({
        ...initialRunTestState,
        phase: "error",
        message: requestMessage("File test could not run", runError)
      });
    }
  }

  async function handleDownloadFile() {
    const download = verifiedDownload;
    if (!download || downloadBusy || runBusy) {
      return;
    }
    if (Date.parse(download.expiresAt) <= Date.now()) {
      setVerifiedDownload(null);
      setDownloadState({
        status: "unavailable",
        message: "The retained receiver copy expired. Run a new test to download it."
      });
      return;
    }

    setDownloadState({
      status: "downloading",
      message: `Preparing the retained receiver copy of ${download.fileName}…`
    });
    try {
      const blob = await api.downloadTrafficDemo(download.url);
      if (blob.size !== download.sizeBytes) {
        setVerifiedDownload(null);
        setDownloadState({
          status: "error",
          message: "The downloaded receiver copy did not match the verified byte count, so it was not saved."
        });
        return;
      }

      const downloadedSha256 = await blobSha256(blob);
      if (downloadedSha256 !== null && downloadedSha256 !== download.sha256) {
        setVerifiedDownload(null);
        setDownloadState({
          status: "error",
          message: "The downloaded receiver copy did not match the verified SHA-256 checksum, so it was not saved."
        });
        return;
      }

      saveBlob(blob, download.fileName);
      setDownloadState({
        status: "success",
        message: `The retained receiver copy of ${download.fileName} is ready in your browser downloads.`
      });
    } catch (downloadError) {
      if (downloadError instanceof ApiError && [404, 410].includes(downloadError.status)) {
        setVerifiedDownload(null);
        setDownloadState({
          status: "unavailable",
          message:
            downloadError.status === 410
              ? "The retained receiver copy expired. Run a new test to download it."
              : "The retained receiver copy was deleted or is no longer available. Run a new test to download it."
        });
        return;
      }
      setDownloadState({
        status: "error",
        message: requestMessage("Download could not start", downloadError)
      });
    }
  }

  return (
    <ObservabilityStudio
      actor={actor}
      fixtureOnly={fixtureOnly}
      simulationActive={simulationActive}
      apiConnected={Boolean(state.health)}
      telemetryReady={state.readiness.status === "ready"}
      detectionError={state.incidentsError}
      primaryIncident={primaryIncident}
      detail={overviewDetail}
      metrics={overviewMetrics}
      traffic={traffic}
      trafficVerified={checked}
      fileSent={sent}
      fileReceived={received}
      sender={sender}
      receiver={receiver}
      topology={state.topology}
      path={path}
      alertCount={alertCount}
      lastUpdated={lastUpdated}
      refreshing={loading}
      refreshSeconds={REFRESH_INTERVAL_SECONDS}
      onRefresh={() => void refresh()}
      onSignOut={onSignOut}
    >
      <section id="operator-workspace" className="studio-workflows" aria-labelledby="operator-workspace-heading">
        <div className="studio-workflows-heading">
          <div>
            <p className="section-label">CONTROLLED OPERATIONS</p>
            <h2 id="operator-workspace-heading">Detailed investigation and response</h2>
          </div>
          <p>Review full evidence, run safety checks, record decisions, and preserve the audit trail.</p>
        </div>

        {canRunTest || runBusy || runTest.phase !== "idle" || verifiedDownload || downloadState.message ? (
          <details id="application-controls" className="studio-workflow-disclosure">
            <summary>Dashboard application-test controls</summary>
            <form className="run-test" aria-labelledby="run-test-heading" aria-busy={runBusy || downloadBusy} onSubmit={(event) => void handleRunTest(event)}>
              <div className="run-test-heading">
                <div><h2 id="run-test-heading">Run a new file test</h2><p>Send a file through the configured sender and receiver and verify its SHA-256 receipt.</p></div>
                <span className="run-test-limit">Maximum {formatBytes(maxFileBytes)}</span>
              </div>
              <div className="run-test-controls">
                <div className="file-field"><label htmlFor="traffic-test-file">Choose file</label><input id="traffic-test-file" type="file" disabled={!canRunTest || runBusy || downloadBusy} onChange={handleFileSelection} /></div>
                <button className="run-test-button" type="submit" disabled={!canRunTest || runBusy || downloadBusy || !selectedFile}>{runBusy ? "Running test…" : "Run test"}</button>
              </div>
              <p className="selected-file">{selectedFile ? `Selected: ${selectedFile.name} (${formatBytes(selectedFile.size)})` : "No file selected."}</p>
              {runTest.message ? <p className={`run-test-result run-test-result--${runTest.outcome ?? "error"}`}>{runTest.message}</p> : null}
              {verifiedDownload ? <div className="verified-download"><div><strong>Verified receiver copy</strong><span>{verifiedDownload.fileName} · {formatBytes(verifiedDownload.sizeBytes)}</span></div><button className="download-file-button" type="button" disabled={downloadBusy || runBusy} onClick={() => void handleDownloadFile()}>{downloadBusy ? "Preparing download…" : "Download file"}</button></div> : null}
              {downloadState.message ? <p className={`download-message download-message--${downloadState.status}`}>{downloadState.message}</p> : null}
              {runUnavailableMessage ? <p className="run-test-unavailable">{runUnavailableMessage}</p> : null}
            </form>
          </details>
        ) : null}

        <IncidentWorkbench actor={actor} incidents={state.incidents} loadError={state.incidentsError} refreshToken={lastUpdated} onChanged={refresh} />
        <div id="case-workspace"><ClosureCases actor={actor} refreshToken={lastUpdated} onChanged={refresh} /></div>

        <details id="advanced-details" className="advanced-details">
          <summary>Advanced details</summary>
          <div className="advanced-content">
            <section><h2>Dashboard status</h2><dl className="plain-details"><div><dt>API</dt><dd>{state.health ? "Connected" : "Not connected"}</dd></div><div><dt>Telemetry</dt><dd>{state.readiness.status === "ready" ? "Ready" : "Needs attention"}</dd></div><div><dt>Last refresh</dt><dd>{formatDateTime(lastUpdated)}</dd></div></dl>{state.readiness.reason ? <p className="small-alert">{state.readiness.reason}</p> : null}</section>
            <section><h2>Configured route</h2>{path ? <p><strong>{path.name}</strong>{path.path_role ? ` (${path.path_role})` : ""}</p> : <p>No configured route is available.</p>}{state.topology?.paths.length ? <ul className="route-list">{state.topology.paths.map((item) => <li key={item.id}>{item.name} <span>{item.path_role}</span></li>)}</ul> : null}</section>
            <section><h2>Data sources</h2><p><strong>Prediction:</strong> {simulationActive ? "Safe local simulation" : fixtureOnly ? "Recorded replay" : "Live telemetry"}</p><p><strong>Application test:</strong> {recentTraffic ? "Recent measurement" : "Last recorded measurement"}</p></section>
            {traffic?.trace?.length ? <section className="trace-section"><h2>Application test details</h2><ul className="trace-list">{traffic.trace.map((item, index) => <li key={`${item.stage}-${index}`}><strong>{item.stage.replaceAll("_", " ")}</strong><span>{item.detail ?? item.status}</span></li>)}</ul></section> : null}
          </div>
        </details>
        <ActionOperations actor={actor} refreshToken={lastUpdated} onChanged={refresh} />
      </section>
    </ObservabilityStudio>
  );
}

