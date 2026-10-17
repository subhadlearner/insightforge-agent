// The Run progress stream (`GET /api/research/status?run_id=`), as api/research.py defines it.
// It is Server-Sent Events, not request/response JSON, so it is not in the OpenAPI schema.
//
// - Default `message` events carry one persisted run_event: {seq, type, payload, created_at}.
//   `seq` increases within a Run but has gaps (the sequence is shared by all Runs).
// - A named `end` event carries {run_id, state} once the Run is COMPLETE or FAILED; the server
//   then closes the stream.
// - An unfinished stream is closed by the server after a bounded lifetime or at shutdown.
//   EventSource reconnects by itself and sends Last-Event-ID, so nothing is lost or repeated.

export type RunState =
  | "INGESTING"
  | "PLANNING"
  | "AWAITING_CLARIFICATION"
  | "RESUMING"
  | "RESEARCHING"
  | "EXTRACTING"
  | "SYNTHESIZING"
  | "WRITING"
  | "FACT_CHECKING"
  | "COMPLETE"
  | "FAILED";

export const RUN_STATES: readonly RunState[] = [
  "INGESTING", "PLANNING", "AWAITING_CLARIFICATION", "RESUMING", "RESEARCHING", "EXTRACTING",
  "SYNTHESIZING", "WRITING", "FACT_CHECKING", "COMPLETE", "FAILED",
];

export type TerminalState = "COMPLETE" | "FAILED";

export interface RunEvent {
  seq: number;
  type: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface RunEnd {
  run_id: string;
  state: TerminalState;
}

export function isRunState(value: unknown): value is RunState {
  return typeof value === "string" && (RUN_STATES as readonly string[]).includes(value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** One `message` frame's data, or null if it is not a well-formed run_event. */
export function parseRunEvent(data: string): RunEvent | null {
  try {
    const v: unknown = JSON.parse(data);
    if (!isRecord(v) || typeof v.seq !== "number" || typeof v.type !== "string") return null;
    return {
      seq: v.seq,
      type: v.type,
      payload: isRecord(v.payload) ? v.payload : {},
      created_at: typeof v.created_at === "string" ? v.created_at : "",
    };
  } catch {
    return null;
  }
}

/** The `end` frame's data, or null if malformed. */
export function parseRunEnd(data: string): RunEnd | null {
  try {
    const v: unknown = JSON.parse(data);
    if (!isRecord(v) || (v.state !== "COMPLETE" && v.state !== "FAILED")) return null;
    return { run_id: String(v.run_id ?? ""), state: v.state };
  } catch {
    return null;
  }
}

export function statusUrl(runId: string): string {
  return `/api/research/status?run_id=${encodeURIComponent(runId)}`;
}
