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
