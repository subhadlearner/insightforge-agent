// The Run page's activity log: a deliberate, safe projection of persisted run_events into plain
// words. Pure: no I/O, no React.
//
// Only the event types listed here produce an entry, and each reads only the fields it names,
// as counts, known enum values or a validated Sub-task id. Nothing else of a payload is ever
// shown: not a Planner description or tool argument, a page title or summary, a search query,
// a failure reason (the outcome box shows the Run's own), a prompt or a transcript.

import { STAGE_LABELS } from "./format";
import { isRunStage, type RunEvent } from "./run-events";

export type ActivityTone = "info" | "done" | "warn" | "failed";

export interface ActivityEntry {
  seq: number;
  at: string;
  text: string;
  tone: ActivityTone;
}

const SUBTASK_ID = /^[A-Za-z0-9_]{1,40}$/;

/** A Sub-task id as the pipeline accepts them, or null for anything else. */
export function subTaskId(value: unknown): string | null {
  return typeof value === "string" && SUBTASK_ID.test(value) ? value : null;
}

function count(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** The entry for one event, or null when the log does not show that event. `aspectOf` names a
 *  Sub-task's Aspect from the accepted plan, when known. */
export function describeEvent(event: RunEvent, aspectOf: (id: string) => string | null): ActivityEntry | null {
  const p = event.payload;
  const entry = (text: string, tone: ActivityTone = "info") => ({ seq: event.seq, at: event.created_at, text, tone });
  const named = (id: string) => {
    const aspect = aspectOf(id);
    return aspect ? `Sub-task ${id} (${aspect})` : `Sub-task ${id}`;
  };
  switch (event.type) {
    case "stage": {
      if (!isRunStage(p.state)) return null;
      if (p.state === "COMPLETE") return entry("The Report is ready", "done");
      if (p.state === "AWAITING_CLARIFICATION") return entry("Paused for your answer");
      if (p.state === "RESUMING") return entry("Research resumed after the question was resolved");
      if (p.state === "FAILED") return null; // the run_failed event says so
      return entry(`${STAGE_LABELS[p.state]} started`);
    }
    case "document_ingested":
      return entry("Read an uploaded document", "done");
    case "document_unreadable":
    case "document_unindexed":
      return entry("An uploaded document could not be used", "warn");
    case "plan_correction":
      return entry("The plan broke a planning rule and was sent back for one correction", "warn");
    case "plan_trimmed":
      return entry("The plan was trimmed to the Sub-task limit", "warn");
    case "plan_accepted": {
      const n = Array.isArray(p.sub_tasks) ? p.sub_tasks.length : null;
      return n === null ? null : entry(`Plan accepted: ${plural(n, "Sub-task")}`, "done");
    }
    case "clarification_requested":
      return entry("Asked you a clarifying question");
    case "clarification_resolved":
      if (p.resolution === "answered") return entry("Your answer was received", "done");
      if (p.resolution === "timed_out") return entry("The question timed out; research goes ahead without an answer", "warn");
      return null;
    case "subtask_started": {
      const id = subTaskId(p.subtask_id);
      if (!id) return null;
      return entry(p.phase === "repair" ? `Retrying ${named(id)}` : `Researching ${named(id)}`);
    }
    case "dispatch_rejected":
      return entry("Refused a research request that did not match the approved plan", "warn");
    case "source_summarized":
      return entry("Evidence gathered from a web source");
    case "doc_search": {
      const n = Array.isArray(p.chunk_ids) ? p.chunk_ids.length : null;
      return entry(n === null ? "Searched the uploaded documents" : `Searched the uploaded documents: ${plural(n, "passage")} found`);
    }
    case "memory_search":
      return entry("Searched earlier Reports");
    case "historical_context": {
      const n = count(p.count);
      return n === null ? null : entry(`Found background in earlier Reports: ${plural(n, "item")}`);
    }
    case "repair": {
      const n = Array.isArray(p.missing) ? p.missing.length : null;
      return n === null ? null : entry(`Repair started for ${plural(n, "missing Sub-task")}`, "warn");
    }
    case "subtask_finished": {
      const id = subTaskId(p.subtask_id);
      if (!id) return null;
      if (p.outcome === "succeeded") return entry(`${named(id)} done`, "done");
      if (p.outcome === "failed") return entry(`${named(id)} failed`, "failed");
      return null;
    }
    case "fact_check_summary": {
      const sampled = count(p.sampled);
      const verified = count(p.verified);
      if (sampled === null || verified === null) return entry("Fact check finished");
      return entry(`Fact check finished: ${verified} of ${sampled} sampled claims verified`);
    }
    case "run_failed":
      return entry("Research failed", "failed");
    case "completion_recovered":
      return entry("The Report was recovered after an interruption", "done");
    default:
      return null;
  }
}
