// The one place the browser calls FastAPI. Every path is same-origin `/api/*`, which Next
// rewrites to the backend (next.config.ts), so the HttpOnly persona cookie rides along.
// Request and response types come from the generated OpenAPI schema (npm run api:types).
//
// Nothing here retries. In particular `submitRun` is not idempotent: an ambiguous failure
// must never create a second Run behind the User's back, so only the User may submit again.

import type { components } from "./api-schema";

type Schemas = components["schemas"];
export type Persona = Schemas["PersonaOut"];
export type RuntimeEstimate = Schemas["RuntimeEstimateOut"];
export type RunAccepted = Schemas["RunAccepted"];
export type DocumentIn = Schemas["DocumentIn"];
export type ClarificationOut = Schemas["ClarificationOut"];
export type ReportListItem = Schemas["ReportListItem"];
export type ReportOut = Schemas["ReportOut"];
export type Citation = Schemas["Citation"];
export type WatchlistItem = Schemas["WatchlistItem"];
export type Run = Schemas["Run"];

export interface FindingProvenance {
  run_id: string; report_id: string; evidence_id: string; claim: string; statement: string;
  source_ids: string[]; observation_dates: Record<string, string>;
}
export interface Finding {
  key: string; subject: string; predicate: string; object: string; polarity: "affirmed" | "negated";
  scope: string; as_of_period: string; derived_from?: string | null; provenance: FindingProvenance;
}
export interface EstablishedFinding {
  finding: Finding; not_reconfirmed: number; unconfirmed: boolean;
}
export interface FailureRecord {
  run_id: string; failed_at?: string | null; stage?: string | null; reason: string;
  error_codes: string[]; backfilled: boolean;
}
export interface WatchlistDashboard {
  item: WatchlistItem; runs_started: number; completed_runs: number; failed_runs: number;
  recent_failed_runs: Run[]; recent_failures: FailureRecord[];
  unconfirmed_findings: EstablishedFinding[]; baseline_state: "none" | "seed" | "established";
}
export type DiffOutcome = "added" | "changed" | "no_longer_true" | "reconfirmed" |
  "not_reconfirmed" | "older_ignored" | "ambiguous";
export interface DiffEntry {
  entry: { outcome: DiffOutcome; key: string; finding?: Finding | null; previous?: Finding | null;
    not_reconfirmed?: number | null; unconfirmed: boolean; reason: string };
  material: boolean;
}
export interface FailedDiff { run_id: string; report_id?: string | null; error: string; }
export interface DiffRecord {
  watchlist_item_id: string; run_id: string; report_id?: string | null;
  status: "pending" | "succeeded" | "failed"; attempts: number; error: string;
  entries: DiffEntry[]; intervening_failed: FailedDiff[]; alert_id?: string | null; updated_at: string;
}
export interface AlertChange {
  outcome: "added" | "changed" | "no_longer_true"; subject: string; predicate: string;
  scope: string; object: string; polarity: "affirmed" | "negated"; as_of_period: string;
  derived: boolean; when: string; report_id: string; evidence_id: string; previous_object?: string | null;
  previous_report_id?: string | null;
}
export interface Alert {
  id: string; watchlist_item_id: string; run_id: string; report_id: string; brief: string;
  created_at: string; changes: AlertChange[]; failed_diffs: FailedDiff[]; read: boolean;
}
export interface ShareCreated { share_id: string; path: string; expires_at: string; }
export interface PublicClaim { text: string; historical: boolean; historical_date?: string | null; }
export interface PublicWebSource {
  id: string; title: string; url: string; published_at?: string | null; credibility_score?: number | null;
}
export interface PublicReport {
  id: string; created_at: string; brief: string; title: string; summary: string;
  sections: { heading: string; claims: PublicClaim[] }[]; gaps: string[];
  fact_check: Record<string, unknown>; web_sources: PublicWebSource[];
}
export type SourceToggles = NonNullable<Schemas["RuntimeEstimateIn"]["source_toggles"]>;
export type SourceType = keyof SourceToggles;

export const SOURCE_TYPES: readonly SourceType[] = ["web", "documents", "memory"];

export class ApiError extends Error {
  constructor(
    readonly status: number, // 0 when the request never got an answer
    message: string,
    readonly retryAfterSeconds: number | null = null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function describeDetail(detail: unknown): string | null {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const parts = detail.map((d) => {
      if (typeof d !== "object" || d === null) return String(d);
      const { loc, msg } = d as { loc?: unknown; msg?: unknown };
      const field = Array.isArray(loc) ? loc.filter((l) => l !== "body").join(".") : "";
      return field ? `${field}: ${String(msg)}` : String(msg);
    });
    return parts.join("; ") || null;
  }
  return null;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      method,
      credentials: "same-origin",
      headers: body === undefined ? undefined : { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "The server could not be reached. Check that the API is running.");
  }
  const text = await response.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!response.ok) {
    const detail = describeDetail((data as { detail?: unknown } | null)?.detail);
    const retryAfter = Number(response.headers.get("retry-after"));
    throw new ApiError(response.status, detail ?? `The server answered ${response.status}.`,
      Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : null);
  }
  return data as T;
}

// --- persona (selection, not authentication) -----------------------------------------------

export const getPersonas = () => request<Persona[]>("GET", "/personas");

/** The persona the cookie selects, or null when none is selected (401). */
export async function getSession(): Promise<Persona | null> {
  try {
    return await request<Persona>("GET", "/session");
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) return null;
    throw e;
  }
}

export const selectPersona = (personaId: string) =>
  request<Persona>("POST", "/session", { persona_id: personaId });

// --- research -----------------------------------------------------------------------------

export const estimateRuntime = (sourceToggles: SourceToggles, documentCount: number) =>
  request<RuntimeEstimate>("POST", "/research/estimate",
    { source_toggles: sourceToggles, document_count: documentCount });

export const submitRun = (brief: string, sourceToggles: SourceToggles, documents: DocumentIn[]) =>
  request<RunAccepted>("POST", "/research/run",
    { brief, source_toggles: sourceToggles, documents });

export const getClarification = (runId: string) =>
  request<ClarificationOut>("GET", `/research/${encodeURIComponent(runId)}/clarification`);

export const answerClarification = (runId: string, answer: string) =>
  request<Schemas["AcceptedOut"]>("POST", `/research/${encodeURIComponent(runId)}/clarify`, { answer });

// --- reports ------------------------------------------------------------------------------

export const listReports = () => request<ReportListItem[]>("GET", "/reports");

export const getReport = (reportId: string) =>
  request<ReportOut>("GET", `/reports/${encodeURIComponent(reportId)}`);

export const getCitations = (reportId: string) =>
  request<Citation[]>("GET", `/reports/${encodeURIComponent(reportId)}/citations`);


// --- T15 watchlist intelligence -----------------------------------------------------------

export const listWatchlists = () => request<WatchlistDashboard[]>("GET", "/watchlist");

export const getWatchlist = (itemId: string) =>
  request<WatchlistDashboard>("GET", `/watchlist/${encodeURIComponent(itemId)}`);

export const listDiffs = (itemId: string) =>
  request<DiffRecord[]>("GET", `/watchlist/${encodeURIComponent(itemId)}/diffs`);

export const listAlerts = (itemId?: string) =>
  request<Alert[]>("GET", itemId ? `/alerts?item_id=${encodeURIComponent(itemId)}` : "/alerts");

export const markAlertRead = (alertId: string, read: boolean) =>
  request<Alert>("PATCH", `/alerts/${encodeURIComponent(alertId)}`, { read });

// --- T15 export and sharing ---------------------------------------------------------------

export async function downloadReport(reportId: string, format: "md" | "pdf"): Promise<Blob> {
  let response: Response;
  try {
    response = await fetch(
      `/api/reports/${encodeURIComponent(reportId)}/export.${format}`,
      { credentials: "same-origin" },
    );
  } catch {
    throw new ApiError(0, "The server could not be reached. Check that the API is running.");
  }
  if (!response.ok) {
    let detail: string | null = null;
    try {
      detail = describeDetail((await response.json() as { detail?: unknown }).detail);
    } catch {
      detail = null;
    }
    throw new ApiError(response.status, detail ?? `The server answered ${response.status}.`);
  }
  return response.blob();
}

export const createShare = (reportId: string) =>
  request<ShareCreated>("POST", `/reports/${encodeURIComponent(reportId)}/shares`);

export async function revokeShare(reportId: string, shareId: string): Promise<void> {
  await request<null>(
    "DELETE",
    `/reports/${encodeURIComponent(reportId)}/shares/${encodeURIComponent(shareId)}`,
  );
}

export const getSharedReport = (token: string) =>
  request<PublicReport>("GET", `/share/${encodeURIComponent(token)}`);
