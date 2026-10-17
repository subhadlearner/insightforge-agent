// What the Run page shows, folded from the progress stream. Pure: no I/O, no React.
//
// Connection state and Run state are kept apart on purpose. A dropped or server-closed stream
// is RECONNECTING (EventSource resumes with Last-Event-ID); only the `end` event makes a Run
// COMPLETE or FAILED. RESUMING and AWAITING_CLARIFICATION are shown as the current state but
// never become steps of the stage rail.

import { isRunStage, type RunEvent, type RunStage as Stage, type TerminalState } from "./run-events";

/** The rail, in pipeline order. INGESTING appears only for a Run that ingested uploads. */
export const RAIL: readonly Stage[] = [
  "INGESTING", "PLANNING", "RESEARCHING", "EXTRACTING", "SYNTHESIZING", "WRITING",
  "FACT_CHECKING", "COMPLETE",
];

export type Connection = "CONNECTING" | "CONNECTED" | "RECONNECTING" | "LOST" | "CLOSED";

export interface ClarificationProgress {
  status: "none" | "awaiting" | "resolved";
  resolution: "answered" | "timed_out" | null;
}

export interface RunProgress {
  lastSeq: number;
  stage: Stage | null; // the latest stage event, including the non-rail ones
  seenStages: Stage[]; // rail stages reached, each once, in arrival order
  connection: Connection;
  terminal: TerminalState | null;
  failureReason: string | null;
  clarification: ClarificationProgress;
}

export type RunAction =
  | { type: "open" }
  | { type: "error" } // EventSource is retrying by itself
  | { type: "lost" } // EventSource gave up (e.g. the stream answered 401 or 404)
  | { type: "event"; event: RunEvent }
  | { type: "end"; state: TerminalState };

export const initialRunProgress: RunProgress = {
  lastSeq: 0,
  stage: null,
  seenStages: [],
  connection: "CONNECTING",
  terminal: null,
  failureReason: null,
  clarification: { status: "none", resolution: null },
};

function applyEvent(state: RunProgress, event: RunEvent): RunProgress {
  if (event.seq <= state.lastSeq) return state; // already applied (a replay)
  const next = { ...state, lastSeq: event.seq };
  const p = event.payload;
  switch (event.type) {
    case "stage": {
      if (!isRunStage(p.state)) return next;
      const stage = p.state;
      const onRail = RAIL.includes(stage) && !next.seenStages.includes(stage);
      return {
        ...next,
        stage,
        seenStages: onRail ? [...next.seenStages, stage] : next.seenStages,
        clarification: stage === "AWAITING_CLARIFICATION" && next.clarification.status === "none"
          ? { status: "awaiting", resolution: null }
          : next.clarification,
      };
    }
    case "clarification_requested":
      return { ...next, clarification: { status: "awaiting", resolution: null } };
    case "clarification_resolved": {
      const r = p.resolution === "answered" || p.resolution === "timed_out" ? p.resolution : null;
      return { ...next, clarification: { status: "resolved", resolution: r } };
    }
    case "run_failed":
      return { ...next, failureReason: typeof p.reason === "string" ? p.reason : next.failureReason };
    default:
      return next; // event types the page does not show
  }
}

export function runReducer(state: RunProgress, action: RunAction): RunProgress {
  switch (action.type) {
    case "open":
      return state.terminal ? state : { ...state, connection: "CONNECTED" };
    case "error":
      return state.terminal ? state : { ...state, connection: "RECONNECTING" };
    case "lost":
      return state.terminal ? state : { ...state, connection: "LOST" };
    case "event":
      return applyEvent(state, action.event);
    case "end":
      return { ...state, terminal: action.state, connection: "CLOSED" };
  }
}

/** The steps to draw: the rail, with INGESTING only if this Run reached it. */
export function railStages(state: RunProgress): Stage[] {
  return RAIL.filter((s) => s !== "INGESTING" || state.seenStages.includes("INGESTING"));
}
