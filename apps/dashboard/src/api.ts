import type { z } from "zod";
import { actionOperationSchema, actionOperationsPageSchema } from "./actionSchemas";
import type { ActionReconciliationRequest } from "./types";
import {
  actionResultSchema, healthSchema, incidentBriefSchema, incidentDetailSchema, incidentSchema,
  incidentsSchema, metricsSchema, modelInfoSchema, parseResponse, readinessSchema,
  topologySchema, trafficDemoRunSchema, trafficDemoSchema
} from "./schemas";
import { closureCasesSchema, closureCaseSchema, emptyResponseSchema, sessionSchema } from "./schemas";
import type {
  ActionResult,
  CaseSearchFilters,
  BrowserSession,
  ClosureReviewRequest,
  ApiErrorBody,
  ClosureRequest,
  Health,
  IncidentCard,
  IncidentDetail,
  IncidentBrief,
  MetricRecord,
  ModelInfo,
  Readiness,
  TrafficDemo,
  TrafficDemoRunResponse,
  TrafficDemoUploadProgress,
  Topology
} from "./types";

const configuredBaseUrl = import.meta.env.VITE_API_BASE_URL?.trim().replace(/\/$/, "") ?? "";
let csrfToken: string | null = null;
const SAFE_TRAFFIC_DEMO_DOWNLOAD_PATH =
  /^\/api\/demo\/traffic\/files\/file-[0-9a-f]{32}\/download$/;

export function isTrafficDemoDownloadUrl(value: unknown): value is string {
  return typeof value === "string" && SAFE_TRAFFIC_DEMO_DOWNLOAD_PATH.test(value);
}

function endpoint(path: string): string {
  return `${configuredBaseUrl}${path}`;
}

export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(status: number, body: unknown) {
    const detail = isApiErrorBody(body) ? body.detail : undefined;
    super(detail || `The API returned HTTP ${status}.`);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

function isApiErrorBody(value: unknown): value is ApiErrorBody {
  return (
    typeof value === "object" &&
    value !== null &&
    "detail" in value &&
    typeof (value as { detail?: unknown }).detail === "string"
  );
}

function parseTextBody(text: string): unknown {
  if (!text) {
    return undefined;
  }

  try {
    return JSON.parse(text) as unknown;
  } catch {
    return text;
  }
}

async function parseBody(response: Response): Promise<unknown> {
  const text = await response.text();
  return parseTextBody(text);
}

async function request<T>(
  path: string, schema: z.ZodType<T>, init: RequestInit = {}, timeoutMs = 15_000
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(endpoint(path), {
      ...init,
      credentials: "same-origin",
      signal: init.signal ?? AbortSignal.timeout(timeoutMs),
      headers: {
        Accept: "application/json",
        ...(init.body ? { "Content-Type": "application/json" } : {}),
        ...(csrfToken && init.method && init.method !== "GET" ? { "X-CSRF-Token": csrfToken } : {}),
        ...init.headers
      }
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : "The API could not be reached.";
    throw new Error(`Unable to reach the PRAHARI API. ${message}`);
  }

  const body = await parseBody(response);
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/auth/login" && typeof window !== "undefined") {
      csrfToken = null;
      window.dispatchEvent(new Event("prahari:session-expired"));
    }
    throw new ApiError(response.status, body);
  }
  return parseResponse(schema, body, path);
}

function isActionResult(value: unknown): value is ActionResult {
  return actionResultSchema.safeParse(value).success;
}

async function actionRequest(
  path: string,
  payload: { actor_id: string; note: string; prediction_id?: string }
): Promise<{ result: ActionResult; httpStatus: number }> {
  try {
    const result = await request<ActionResult>(path, actionResultSchema, {
      method: "POST",
      body: JSON.stringify(payload)
    }, 60_000);
    return { result, httpStatus: 200 };
  } catch (error) {
    if (error instanceof ApiError && isActionResult(error.body)) {
      return { result: error.body, httpStatus: error.status };
    }
    throw error;
  }
}

async function optionalTrafficDemo(): Promise<TrafficDemo | null> {
  try {
    return await request<TrafficDemo>("/api/demo/traffic", trafficDemoSchema);
  } catch (error) {
    // The traffic demo is deliberately additive. A dashboard can still monitor
    // the core NOC API when the demo server has not yet been installed.
    if (error instanceof ApiError && error.status === 404) {
      return null;
    }
    throw error;
  }
}

type TrafficDemoRunCallbacks = {
  onUploadProgress?: (progress: TrafficDemoUploadProgress) => void;
  onUploadComplete?: () => void;
};

function isTrafficDemoRunResponse(value: unknown): value is TrafficDemoRunResponse {
  return trafficDemoRunSchema.safeParse(value).success;
}

function runTrafficDemo(
  file: File,
  callbacks: TrafficDemoRunCallbacks = {}
): Promise<TrafficDemoRunResponse> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", endpoint("/api/demo/traffic/run"));
    xhr.timeout = 125_000;
    xhr.setRequestHeader("Accept", "application/json");
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.setRequestHeader("X-Prahari-File-Name", file.name);
    xhr.setRequestHeader("X-Prahari-Demo-Intent", "run-traffic-test");
    if (csrfToken) xhr.setRequestHeader("X-CSRF-Token", csrfToken);

    xhr.upload.addEventListener("progress", (event) => {
      const totalBytes = file.size;
      const loadedBytes = Math.min(event.loaded, totalBytes);
      callbacks.onUploadProgress?.({
        loaded_bytes: loadedBytes,
        total_bytes: totalBytes,
        percent: totalBytes > 0 ? Math.min(100, Math.round((loadedBytes / totalBytes) * 100)) : 0
      });
    });
    xhr.upload.addEventListener("load", () => callbacks.onUploadComplete?.());

    xhr.addEventListener("load", () => {
      const body = parseTextBody(xhr.responseText);
      if (xhr.status < 200 || xhr.status >= 300) {
        reject(new ApiError(xhr.status, body));
        return;
      }
      if (!isTrafficDemoRunResponse(body)) {
        reject(new Error("The PRAHARI API returned an incomplete file test result."));
        return;
      }
      resolve(body);
    });
    xhr.addEventListener("error", () => {
      reject(new Error("Unable to reach the PRAHARI API. The file test request failed."));
    });
    xhr.addEventListener("abort", () => {
      reject(new Error("The file test request was cancelled."));
    });
    xhr.addEventListener("timeout", () => {
      reject(new Error("The file test timed out. Check its recorded outcome before retrying."));
    });

    xhr.send(file);
  });
}

async function downloadTrafficDemo(downloadUrl: string): Promise<Blob> {
  if (!isTrafficDemoDownloadUrl(downloadUrl)) {
    throw new Error("The PRAHARI API returned an unsafe file download reference.");
  }

  let response: Response;
  try {
    response = await fetch(endpoint(downloadUrl), {
      method: "POST",
      credentials: "same-origin",
      signal: AbortSignal.timeout(60_000),
      headers: {
        Accept: "application/octet-stream",
        ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}),
        "X-Prahari-Demo-Intent": "download-verified-file"
      }
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : "The API could not be reached.";
    throw new Error(`Unable to reach the PRAHARI API. ${message}`);
  }

  if (!response.ok) {
    throw new ApiError(response.status, await parseBody(response));
  }
  return response.blob();
}

export const api = {
  actionOperations: () => request("/api/action-operations", actionOperationsPageSchema),
  reconcileAction: (operationId: string, payload: ActionReconciliationRequest) =>
    request(`/api/action-operations/${encodeURIComponent(operationId)}/reconcile`, actionOperationSchema, {
      method: "POST", body: JSON.stringify(payload)
    }),
  session: async () => {
    const session = await request<BrowserSession>("/api/auth/session", sessionSchema);
    csrfToken = session.csrf_token;
    return session;
  },
  login: async (username: string, password: string) => {
    const session = await request<BrowserSession>("/api/auth/login", sessionSchema, {
      method: "POST", body: JSON.stringify({ username, password })
    });
    csrfToken = session.csrf_token;
    return session;
  },
  logout: async () => {
    await request("/api/auth/logout", emptyResponseSchema, { method: "POST" });
    csrfToken = null;
  },
  cases: (filters: CaseSearchFilters = {}, offset = 0) => {
    const query = new URLSearchParams({ limit: "20", offset: String(offset) });
    Object.entries(filters).forEach(([key, value]) => {
      if (value) query.set(key, value);
    });
    return request(`/api/cases?${query}`, closureCasesSchema);
  },
  decideCase: (caseId: string, payload: ClosureReviewRequest) => request(
    `/api/cases/${encodeURIComponent(caseId)}/decision`, closureCaseSchema,
    { method: "POST", body: JSON.stringify(payload) }
  ),
  health: () => request<Health>("/health", healthSchema),
  readyz: () => request<Readiness>("/readyz", readinessSchema),
  topology: () => request<Topology>("/api/topology", topologySchema),
  incidents: () => request<IncidentCard[]>("/api/incidents", incidentsSchema),
  incidentDetail: (incidentId: string) =>
    request<IncidentDetail>(`/api/incidents/${encodeURIComponent(incidentId)}`, incidentDetailSchema),
  incidentBrief: (incidentId: string, predictionId: string) =>
    request<IncidentBrief>(`/api/incidents/${encodeURIComponent(incidentId)}/brief?prediction_id=${encodeURIComponent(predictionId)}`, incidentBriefSchema, {
      method: "POST"
    }, 190_000),
  metrics: (entityId: string) =>
    request<MetricRecord[]>(`/api/metrics?entity_id=${encodeURIComponent(entityId)}`, metricsSchema),
  models: () => request<ModelInfo>("/api/models", modelInfoSchema),
  trafficDemo: () => optionalTrafficDemo(),
  runTrafficDemo,
  downloadTrafficDemo,
  review: (incidentId: string, payload: { actor_id: string; note: string; prediction_id?: string }) =>
    request<IncidentCard>(`/api/incidents/${encodeURIComponent(incidentId)}/review`, incidentSchema, {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  close: (incidentId: string, payload: ClosureRequest) =>
    request<IncidentCard>(`/api/incidents/${encodeURIComponent(incidentId)}/closure`, incidentSchema, {
      method: "POST",
      body: JSON.stringify(payload)
    }),
  dryRun: (incidentId: string, playbookId: string, payload: { actor_id: string; note: string; prediction_id?: string }) =>
    actionRequest(
      `/api/incidents/${encodeURIComponent(incidentId)}/actions/${encodeURIComponent(
        playbookId
      )}/dry-run`,
      payload
    ),
  approve: (incidentId: string, playbookId: string, payload: { actor_id: string; note: string; prediction_id?: string }) =>
    actionRequest(
      `/api/incidents/${encodeURIComponent(incidentId)}/actions/${encodeURIComponent(
        playbookId
      )}/approve`,
      payload
    ),
  rollback: (incidentId: string, playbookId: string, payload: { actor_id: string; note: string; prediction_id?: string }) =>
    actionRequest(
      `/api/incidents/${encodeURIComponent(incidentId)}/actions/${encodeURIComponent(
        playbookId
      )}/rollback`,
      payload
    )
};
