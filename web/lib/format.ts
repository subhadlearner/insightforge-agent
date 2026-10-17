// Display formatting shared by the components.

import type { RunStage } from "./run-events";

/** "about 2 min 15 sec": an estimate, worded as one. */
export function formatEstimate(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(s / 60);
  const rest = s % 60;
  if (minutes === 0) return `about ${rest} sec`;
  return rest === 0 ? `about ${minutes} min` : `about ${minutes} min ${rest} sec`;
}

/** "14:05" until the deadline, or null once it has passed (the server decides what then). */
export function formatCountdown(deadline: string, now: number): string | null {
  const left = Math.floor((Date.parse(deadline) - now) / 1000);
  if (!Number.isFinite(left) || left <= 0) return null;
  const h = Math.floor(left / 3600);
  const m = Math.floor((left % 3600) / 60);
  const s = left % 60;
  const mmss = `${String(m).padStart(h ? 2 : 1, "0")}:${String(s).padStart(2, "0")}`;
  return h ? `${h}:${mmss}` : mmss;
}

/** A calendar date, e.g. "25 Mar 2025". Dates without a time are read as calendar days. */
export function formatDate(value: string | null | undefined): string | null {
  if (!value) return null;
  const date = new Date(/^\d{4}-\d{2}-\d{2}$/.test(value) ? `${value}T00:00:00Z` : value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}

export const STAGE_LABELS: Record<RunStage, string> = {
  INGESTING: "Reading documents",
  PLANNING: "Planning",
  AWAITING_CLARIFICATION: "Waiting for your answer",
  RESUMING: "Resuming research",
  RESEARCHING: "Researching",
  EXTRACTING: "Extracting entities",
  SYNTHESIZING: "Weighing evidence",
  WRITING: "Writing the Report",
  FACT_CHECKING: "Fact-checking",
  COMPLETE: "Report ready",
  FAILED: "Failed",
};
