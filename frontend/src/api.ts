/**
 * Typed API client.
 *
 * Requests go to a same-origin `/api` path: Vite proxies it in development and any reverse
 * proxy can do the same in production, so no CORS origin has to be hard-coded in the browser.
 */
import type {
  ActionReliability,
  AuditEntry,
  Deployment,
  Fragility,
  Health,
  IncidentDetail,
  IncidentSummary,
  Investigation,
  InvestigationListItem,
  LearningEvent,
  LearningLoopResult,
  MemoryItem,
  MemoryReference,
  MemoryStatus,
  Overview,
  PatternInsight,
  PeriodAnalysis,
  Postmortem,
  RecallResponse,
  RemediationCatalogueAction,
  RemediationRun,
  Scenario,
  ServiceBrief,
  Session,
  SimilarNeighbour,
  SimState,
  TimelineEvent,
} from "./types";

const TOKEN_KEY = "opsmemory.token";

export function getToken(): string | null {
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null): void {
  try {
    if (token === null) window.localStorage.removeItem(TOKEN_KEY);
    else window.localStorage.setItem(TOKEN_KEY, token);
  } catch {
    /* storage can be unavailable in private modes; the session simply will not persist */
  }
}

export class ApiError extends Error {
  status: number;
  code: string;
  detail: Record<string, unknown> | null;

  constructor(status: number, code: string, message: string, detail: Record<string, unknown> | null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

type Listener = () => void;
const unauthorizedListeners = new Set<Listener>();  /** Notified when the server rejects the token, so the shell can return to the login screen. */
export function onUnauthorized(listener: Listener): () => void {
  unauthorizedListeners.add(listener);
  return () => unauthorizedListeners.delete(listener);
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  let response: Response;
  try {
    response = await fetch(path, { ...init, headers });
  } catch (cause) {
    throw new ApiError(0, "NETWORK", "Cannot reach the OpsMemory API. Is the backend running?", {
      cause: String(cause),
    });
  }

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }

  if (!response.ok) {
    const envelope = (body ?? {}) as { error?: { code?: string; message?: string; detail?: Record<string, unknown> } };
    const code = envelope.error?.code ?? `HTTP_${response.status}`;
    const message = envelope.error?.message ?? response.statusText ?? "Request failed";
    if (response.status === 401) unauthorizedListeners.forEach((listener) => listener());
    throw new ApiError(response.status, code, message, envelope.error?.detail ?? null);
  }

  return body as T;
}

function query(params: Record<string, string | number | boolean | undefined | null>): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  });
  const text = search.toString();
  return text ? `?${text}` : "";
}

export const api = {
  // -- auth ---------------------------------------------------------------------------
  login: (email: string, password: string) =>
    request<{
      access_token: string;
      token_type: string;
      role: string;
      user: Record<string, unknown>;
      permissions: string[];
    }>("/api/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }),
  me: () => request<Session>("/api/auth/me"),
  users: () =>
    request<{ users: { id: number; email: string; full_name: string; role: string; is_active: boolean }[] }>(
      "/api/auth/users",
    ),

  // -- system -------------------------------------------------------------------------
  health: () => request<Health>("/api/health"),
  overview: () => request<Overview>("/api/system/overview"),
  events: (limit = 40) => request<{ events: LearningEvent[] }>(`/api/system/events${query({ limit })}`),
  simState: () => request<SimState>("/api/sim/state"),
  scenarios: () => request<{ scenarios: Scenario[] }>("/api/scenarios"),
  services: () => request<{ services: ServiceBrief[] }>("/api/services"),
  deployments: (limit = 50) =>
    request<{ deployments: Deployment[] }>(`/api/deployments${query({ limit })}`),

  // -- incidents ----------------------------------------------------------------------
  investigations: (params: { status?: string; memory_used?: boolean; limit?: number } = {}) =>
    request<{ count: number; investigations: InvestigationListItem[] }>(
      `/api/investigations${query(params)}`,
    ),

  incidents: (params: { status?: string; service?: string; limit?: number } = {}) =>
    request<{ count: number; sim_time: string; incidents: IncidentSummary[] }>(
      `/api/incidents${query(params)}`,
    ),
  incident: (id: number) => request<IncidentDetail>(`/api/incidents/${id}`),
  timeline: (id: number) => request<{ incident_id: number; timeline: TimelineEvent[] }>(`/api/incidents/${id}/timeline`),
  investigation: (id: number) =>
    request<{ incident_id: number; investigation: Investigation | null }>(`/api/incidents/${id}/investigation`),
  similar: (id: number) =>
    request<{ incident_id: number; neighbours: SimilarNeighbour[] }>(`/api/incidents/${id}/similar`),
  investigate: (
    id: number,
    body: { execute?: boolean; auto_approve?: boolean; autonomy_level?: number } = {},
  ) =>
    request<{ result: Record<string, any>; incident: IncidentSummary }>(
      `/api/incidents/${id}/investigate`,
      { method: "POST", body: JSON.stringify(body) },
    ),
  proposeRemediation: (
    id: number,
    body: {
      action_code: string;
      rationale?: string;
      autonomy_level?: number;
      parameters?: Record<string, unknown>;
    },
  ) =>
    request<{ run: RemediationRun }>(`/api/incidents/${id}/remediation`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  approve: (id: number, runId: number, body: { reason?: string } = {}) =>
    request<{ run: RemediationRun }>(`/api/incidents/${id}/remediation/${runId}/approve`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  execute: (id: number, runId: number) =>
    request<{ result: Record<string, any>; incident: IncidentSummary; run: RemediationRun }>(
      `/api/incidents/${id}/remediation/${runId}/execute`,
      { method: "POST" },
    ),

  // -- memory -------------------------------------------------------------------------
  memoryStatus: () => request<MemoryStatus>("/api/memory/status"),
  /* The API's query parameter is `memory_type`; sending `type` silently returns everything, so
     the per-type tabs would look identical. Map it here rather than at every call site. */
  memoryItems: (params: { type?: string; limit?: number; scope?: string; search?: string } = {}) =>
    request<{ count: number; backend: string; items: MemoryItem[] }>(
      `/api/memory/items${query({
        memory_type: params.type,
        limit: params.limit,
        search: params.search,
        scope: params.scope,
      })}`,
    ),
  memoryScopes: () => request<{ scopes: string[] }>("/api/memory/scopes"),
  recall: (body: { query: string; types?: string[]; budget?: string; max_tokens?: number }) =>
    request<RecallResponse>("/api/memory/recall", { method: "POST", body: JSON.stringify(body) }),
  /* recall returns `hits`, not `items`: see RecallResult in app/memory/base.py */
  reflect: (body: { query: string; context?: string }) =>
    request<{ text: string; backend: string; degraded: boolean; citations: string[]; note: string }>(
      "/api/memory/reflect",
      { method: "POST", body: JSON.stringify(body) },
    ),
  retain: (body: { content: string; context?: string; scope?: string }) =>
    request<Record<string, unknown>>("/api/memory/retain", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  learningEvents: (limit = 50) =>
    request<{ count: number; events: LearningEvent[] }>(`/api/memory/learning-events${query({ limit })}`),
  memoryReferences: (limit = 50) =>
    request<{ count: number; references: MemoryReference[] }>(`/api/memory/references${query({ limit })}`),

  // -- analysis -----------------------------------------------------------------------
  period: (days = 30) => request<PeriodAnalysis>(`/api/analysis/period${query({ days })}`),
  patterns: (days = 30, minimum = 3) =>
    request<{ window_days: number; minimum_occurrences: number; count: number; patterns: PatternInsight[] }>(
      `/api/analysis/patterns${query({ days, minimum_occurrences: minimum })}`,
    ),
  reliability: (days = 90) =>
    request<{ window_days: number; actions: ActionReliability[] }>(`/api/analysis/reliability${query({ days })}`),
  fragility: (days = 30) => request<{ window_days: number; services: Fragility[] }>(`/api/analysis/fragility${query({ days })}`),

  // -- remediation / postmortems ------------------------------------------------------
  registry: () =>
    request<{ actions: RemediationCatalogueAction[]; note: string }>("/api/remediation/registry"),
  postmortems: (limit = 50) => request<{ count: number; postmortems: Postmortem[] }>(`/api/postmortems${query({ limit })}`),
  postmortem: (id: number) => request<{ postmortem: Postmortem; incident: IncidentSummary }>(`/api/postmortems/${id}`),
  approvePostmortem: (id: number) =>
    request<{ postmortem: Postmortem }>(`/api/postmortems/${id}/approve`, { method: "POST" }),

  // -- audit / integrations -----------------------------------------------------------
  audit: (limit = 100) => request<{ count: number; entries: AuditEntry[] }>(`/api/audit${query({ limit })}`),
  integrations: () => request<Record<string, unknown>>("/api/integrations"),

  // -- demo ---------------------------------------------------------------------------
  runLearningLoop: (params: { scenario?: string; include_failed_attempt?: boolean } = {}) =>
    request<LearningLoopResult>(`/api/demo/learning-loop${query(params)}`, { method: "POST" }),
  cleanRoom: () => request<Record<string, unknown>>("/api/demo/clean-room", { method: "POST" }),
  seedHistory: (count = 60, days = 45) =>
    request<Record<string, unknown>>(`/api/demo/seed-history${query({ count, days })}`, { method: "POST" }),
  advance: (seconds: number) =>
    request<Record<string, unknown>>("/api/sim/advance", {
      method: "POST",
      body: JSON.stringify({ seconds }),
    }),
  injectFault: (body: { scenario: string; service: string; environment?: string; severity?: string }) =>
    request<Record<string, unknown>>("/api/sim/fault", { method: "POST", body: JSON.stringify(body) }),
  pause: () => request<Record<string, unknown>>("/api/sim/pause", { method: "POST" }),
  resume: () => request<Record<string, unknown>>("/api/sim/resume", { method: "POST" }),
};
