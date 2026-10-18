// What the Run page shows, folded from the progress stream. Pure: no I/O, no React.
//
// Connection state and Run state are kept apart on purpose. A dropped or server-closed stream
// is RECONNECTING (EventSource resumes with Last-Event-ID); only the `end` event makes a Run
// COMPLETE or FAILED. RESUMING and AWAITING_CLARIFICATION are shown as the current state but
// never become steps of the stage rail.
//
// T20 B6: the accepted plan and each Sub-task's lifecycle, the activity log and the terminal
// time come from persisted events alone. A Sub-task is queued once `plan_accepted` lists it,
// running only after `subtask_started` (an approved dispatch that reached its Researcher), and
// done or failed only from `subtask_finished` (the outcome the pipeline validated and stored).
// Entering RESEARCHING runs nothing by itself, and a Run that ends first leaves the rest not
// completed, never failed.

import { describeEvent, subTaskId, type ActivityEntry } from "./activity";
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

export type SubTaskStatus = "queued" | "running" | "succeeded" | "failed";

export interface SubTaskProgress {
  id: string;
  aspect: string;
  query: string;
  status: SubTaskStatus;
  repair: boolean; // listed for the repair round
}

/** The activity entries kept: the latest ones; `activityTotal` counts them all. */
export const ACTIVITY_LIMIT = 50;

export interface RunProgress {
  lastSeq: number;
  stage: Stage | null; // the latest stage event, including the non-rail ones
  seenStages: Stage[]; // rail stages reached, each once, in arrival order
  connection: Connection;
  terminal: TerminalState | null;
  failureReason: string | null;
  clarification: ClarificationProgress;
  plan: SubTaskProgress[] | null; // null until a plan is accepted (or for a Run whose detail is gone)
  activity: ActivityEntry[];
  activityTotal: number;
  endedAt: string | null; // the time of the Run's last terminal event, which retention keeps
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
  plan: null,
  activity: [],
  activityTotal: 0,
  endedAt: null,
};

const TERMINAL_EVENTS = new Set(["run_failed", "completion_recovered"]);

function parsePlan(value: unknown): SubTaskProgress[] | null {
  if (!Array.isArray(value)) return null;
  const out: SubTaskProgress[] = [];
  for (const t of value) {
    if (typeof t !== "object" || t === null) return null;
    const { id, aspect, query } = t as Record<string, unknown>;
    const sid = subTaskId(id);
    if (!sid || out.some((o) => o.id === sid)) return null;
    out.push({
      id: sid,
      aspect: typeof aspect === "string" ? aspect : "",
      query: typeof query === "string" ? query : "",
      status: "queued",
      repair: false,
    });
  }
  return out;
}

/** The plan with one Sub-task changed; an unknown id changes nothing. An outcome is final. */
function updateSubTask(state: RunProgress, value: unknown, change: (t: SubTaskProgress) => SubTaskProgress): RunProgress {
  const id = subTaskId(value);
  if (!state.plan || !id || !state.plan.some((t) => t.id === id)) return state;
  return {
    ...state,
    plan: state.plan.map((t) => (t.id !== id || t.status === "succeeded" || t.status === "failed" ? t : change(t))),
  };
}

function lifecycle(state: RunProgress, event: RunEvent): RunProgress {
  const p = event.payload;
  switch (event.type) {
    case "plan_accepted": {
      const plan = parsePlan(p.sub_tasks);
      return plan ? { ...state, plan } : state;
    }
    case "subtask_started":
      return updateSubTask(state, p.subtask_id, (t) => ({ ...t, status: "running" }));
    case "repair": {
      const missing = Array.isArray(p.missing) ? p.missing : [];
      return missing.reduce<RunProgress>(
        (s, id) => updateSubTask(s, id, (t) => ({ ...t, status: "queued", repair: true })), state);
    }
    case "subtask_finished": {
      const outcome = p.outcome;
      if (outcome !== "succeeded" && outcome !== "failed") return state;
      return updateSubTask(state, p.subtask_id, (t) => ({ ...t, status: outcome }));
    }
    default:
      return state;
  }
}

function withActivity(state: RunProgress, event: RunEvent): RunProgress {
  const aspectOf = (id: string) => state.plan?.find((t) => t.id === id)?.aspect || null;
  const entry = describeEvent(event, aspectOf);
  if (!entry) return state;
  const activity = [...state.activity, entry];
  return {
    ...state,
    activity: activity.length > ACTIVITY_LIMIT ? activity.slice(-ACTIVITY_LIMIT) : activity,
    activityTotal: state.activityTotal + 1,
  };
}

function applyEvent(state: RunProgress, event: RunEvent): RunProgress {
  if (event.seq <= state.lastSeq) return state; // already applied (a replay)
  let next: RunProgress = withActivity(lifecycle({ ...state, lastSeq: event.seq }, event), event);
  const terminalStage = event.type === "stage" && event.payload.state === "COMPLETE";
  if ((terminalStage || TERMINAL_EVENTS.has(event.type)) && event.created_at) {
    next = { ...next, endedAt: event.created_at };
  }
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

export interface Milestones {
  passed: number; // workflow stages finished; not a measure of time
  total: number;
  current: Stage | null; // the latest rail stage reached
  stopped: boolean; // the Run failed at `current`
}

/** Known workflow milestones passed: every reached stage before the current one, and all of
 *  them once COMPLETE. Waiting for an answer does not move it. */
export function milestones(state: RunProgress): Milestones {
  const steps = railStages(state);
  const current = state.seenStages.at(-1) ?? null;
  if (state.terminal === "COMPLETE") return { passed: steps.length, total: steps.length, current, stopped: false };
  const passed = current ? steps.filter((s) => s !== current && state.seenStages.includes(s)).length : 0;
  return { passed, total: steps.length, current, stopped: state.terminal === "FAILED" };
}

export interface SubTaskSummary {
  total: number;
  finished: number; // with a validated outcome
  done: number;
  failed: number;
}

export function subTaskSummary(state: RunProgress): SubTaskSummary {
  const plan = state.plan ?? [];
  const done = plan.filter((t) => t.status === "succeeded").length;
  const failed = plan.filter((t) => t.status === "failed").length;
  return { total: plan.length, finished: done + failed, done, failed };
}

export type SubTaskView = "queued" | "queued-for-repair" | "running" | "retrying" | "done" | "failed"
  | "not-completed" | "outcome-unavailable";

export const SUBTASK_LABELS: Record<SubTaskView, string> = {
  queued: "Queued", "queued-for-repair": "Queued for repair", running: "Running", retrying: "Retrying",
  done: "Done", failed: "Failed", "not-completed": "Not completed", "outcome-unavailable": "Outcome unavailable",
};

/** What the page shows for one Sub-task. One without an outcome when the Run ended was not
 *  completed (FAILED) or has no recorded outcome (COMPLETE); it never shows as failed. */
export function subTaskView(state: RunProgress, t: SubTaskProgress): SubTaskView {
  if (t.status === "succeeded") return "done";
  if (t.status === "failed") return "failed";
  if (state.terminal === "FAILED") return "not-completed";
  if (state.terminal === "COMPLETE") return "outcome-unavailable";
  if (t.status === "running") return t.repair ? "retrying" : "running";
  return t.repair ? "queued-for-repair" : "queued";
}

export function subTaskLabel(state: RunProgress, t: SubTaskProgress): string {
  return SUBTASK_LABELS[subTaskView(state, t)];
}

export type SubTaskDetail = "available" | "pending" | "awaiting-answer" | "never-planned" | "unavailable";

const AFTER_PLANNING: readonly Stage[] = ["RESEARCHING", "EXTRACTING", "SYNTHESIZING", "WRITING", "FACT_CHECKING", "COMPLETE"];

/** Whether the Sub-task list can be shown, and if not, why. A Run past planning with no plan
 *  event has lost its detail (retention, or a Run recorded before plans were logged). */
export function subTaskDetail(state: RunProgress): SubTaskDetail {
  if (state.plan) return "available";
  if (state.seenStages.some((s) => AFTER_PLANNING.includes(s))) return "unavailable";
  if (state.terminal) return "never-planned";
  if (state.stage === "AWAITING_CLARIFICATION" || state.clarification.status === "awaiting") return "awaiting-answer";
  return "pending";
}
