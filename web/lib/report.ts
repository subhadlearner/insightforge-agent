// The Report body is `dict[str, Any]` in the API (ReportOut.body), so OpenAPI cannot type it.
// This module is the only place that narrows it. Components get `ParsedReport` and never
// touch `body` themselves.
//
// The viewer shows `body.verified.draft`: the Fact-Checker's copy, in which an unverified
// Claim's text ends with " [UNVERIFIED]". `body.draft` (before fact-checking) is never read,
// so the mark cannot be lost. Citations come from GET /reports/{id}/citations only.

import type { Citation, ReportOut } from "./api";

export const UNVERIFIED_MARK = " [UNVERIFIED]";

export interface ParsedClaim {
  key: string; // "section:claim", the citation endpoint's position
  number: number; // 1-based, in draft order, for the inline marker
  section: number;
  claim: number;
  text: string; // exactly as the verified draft has it, mark included
  statement: string; // the text without the trailing mark
  unverified: boolean;
  evidenceId: string;
  historical: boolean;
  historicalDate: string | null; // the date of the Report that first made a Historical Claim
}

export interface ParsedSection {
  heading: string;
  claims: ParsedClaim[];
}

export interface FactCheckSummary {
  sampled: number;
  verified: number;
  unverified: number;
  unchecked: number;
}

export interface ParsedReport {
  id: string;
  runId: string;
  createdAt: string;
  brief: string;
  title: string;
  summary: string;
  sections: ParsedSection[];
  gaps: string[];
  factCheck: FactCheckSummary | null;
}

export class ReportShapeError extends Error {
  constructor(message: string) {
    super(`This Report could not be read: ${message}`);
    this.name = "ReportShapeError";
  }
}

type Json = Record<string, unknown>;

function record(value: unknown, what: string): Json {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new ReportShapeError(`${what} is missing`);
  }
  return value as Json;
}

function list(value: unknown, what: string): unknown[] {
  if (!Array.isArray(value)) throw new ReportShapeError(`${what} is not a list`);
  return value;
}

function text(value: unknown, what: string): string {
  if (typeof value !== "string") throw new ReportShapeError(`${what} is not text`);
  return value;
}

function count(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function historicalDates(body: Json): Map<string, string> {
  const dates = new Map<string, string>();
  const items = (body.historical as Json | undefined)?.items;
  if (!Array.isArray(items)) return dates;
  for (const item of items) {
    if (typeof item === "object" && item !== null) {
      const { id, report_date } = item as Json;
      if (typeof id === "string" && typeof report_date === "string") dates.set(id, report_date);
    }
  }
  return dates;
}

export function parseReport(out: ReportOut): ParsedReport {
  const body = record(out.body, "the Report body");
  const verified = record(body.verified, "the fact-checked Report");
  const draft = record(verified.draft, "the fact-checked draft");
  const dates = historicalDates(body);
  let number = 0;
  const sections = list(draft.sections, "sections").map((rawSection, si): ParsedSection => {
    const section = record(rawSection, `section ${si + 1}`);
    return {
      heading: text(section.heading, `section ${si + 1} heading`),
      claims: list(section.claims, `section ${si + 1} claims`).map((rawClaim, ci): ParsedClaim => {
        const claim = record(rawClaim, `claim ${si + 1}.${ci + 1}`);
        const claimText = text(claim.text, `claim ${si + 1}.${ci + 1} text`);
        const evidenceId = text(claim.evidence_id, `claim ${si + 1}.${ci + 1} evidence`);
        const unverified = claimText.endsWith(UNVERIFIED_MARK);
        const historical = claim.historical === true;
        number += 1;
        return {
          key: `${si}:${ci}`, number, section: si, claim: ci, text: claimText,
          statement: unverified ? claimText.slice(0, -UNVERIFIED_MARK.length) : claimText,
          unverified, evidenceId, historical,
          historicalDate: historical ? dates.get(evidenceId) ?? null : null,
        };
      }),
    };
  });
  const summary = verified.summary as Json | undefined;
  return {
    id: out.id,
    runId: out.run_id,
    createdAt: out.created_at,
    brief: typeof body.brief === "string" ? body.brief : "",
    title: text(draft.title, "the title"),
    summary: typeof draft.summary === "string" ? draft.summary : "",
    sections,
    gaps: Array.isArray(draft.gaps) ? draft.gaps.filter((g): g is string => typeof g === "string") : [],
    factCheck: summary && typeof summary === "object"
      ? { sampled: count(summary.sampled), verified: count(summary.verified),
          unverified: count(summary.unverified), unchecked: count(summary.unchecked) }
      : null,
  };
}

/** Each Claim's citation, by the Claim's position. A citation is attached only when its
 *  position and evidence_id both match the Claim; nothing is borrowed from another Claim. */
export function attachCitations(report: ParsedReport, citations: Citation[]): Map<string, Citation> {
  const byPosition = new Map(citations.map((c) => [`${c.section}:${c.claim}`, c]));
  const out = new Map<string, Citation>();
  for (const claim of report.sections.flatMap((s) => s.claims)) {
    const c = byPosition.get(claim.key);
    if (c && c.evidence_id === claim.evidenceId) out.set(claim.key, c);
  }
  return out;
}
