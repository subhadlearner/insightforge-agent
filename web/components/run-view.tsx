"use client";

// One Run, from its ID alone, so a reload rebuilds everything from the replayed stream.
// COMPLETE means the Run has exactly one Report: it is found through GET /api/reports (safe
// to repeat; the Run is never submitted again). FAILED has no Report and links to none.

import Link from "next/link";
import { useEffect, useState } from "react";
import { listReports } from "@/lib/api";
import { STAGE_LABELS } from "@/lib/format";
import type { RunProgress } from "@/lib/run-reducer";
import { ClarificationCard } from "./clarification-card";
import { ActivityLog, RunOverview, SubTaskList } from "./run-overview";
import { StageProgress } from "./stage-progress";
import { useElapsed } from "./use-elapsed";
import { useRunProgress } from "./use-run-progress";

type ReportLookup = { kind: "idle" | "looking" } | { kind: "found"; id: string } | { kind: "missing" };

const REPORT_LOOKUPS = 5;
const REPORT_LOOKUP_DELAY_MS = 800;

function headline(state: RunProgress): string {
  if (state.terminal === "COMPLETE") return "Your Report is ready";
  if (state.terminal === "FAILED") return "This Run failed";
  if (state.stage === "AWAITING_CLARIFICATION") return "Waiting for your answer";
  if (state.stage === "RESUMING") return "Resuming research…";
  if (state.stage) return `${STAGE_LABELS[state.stage]}…`;
  return "Connecting to the Run…";
}

function useReportFor(runId: string, complete: boolean): ReportLookup {
  const [lookup, setLookup] = useState<ReportLookup>({ kind: "idle" });
  useEffect(() => {
    if (!complete) return;
    let live = true;
    setLookup({ kind: "looking" });
    (async () => {
      for (let i = 0; i < REPORT_LOOKUPS && live; i++) {
        try {
          const report = (await listReports()).find((r) => r.run_id === runId);
          if (report) {
            if (live) setLookup({ kind: "found", id: report.id });
            return;
          }
        } catch {
          // a GET; try again below
        }
        await new Promise((r) => setTimeout(r, REPORT_LOOKUP_DELAY_MS));
      }
      if (live) setLookup({ kind: "missing" });
    })();
    return () => {
      live = false;
    };
  }, [runId, complete]);
  return lookup;
}

export function RunView({ runId }: { runId: string }) {
  const { state, retry } = useRunProgress(runId);
  const report = useReportFor(runId, state.terminal === "COMPLETE");
  const elapsed = useElapsed(runId, state);
  // Shown from the question until the end of the Run, and afterwards as a record of how it
  // was resolved (a timeout is not a failure; the Run went ahead without the answer).
  const asked = state.stage === "AWAITING_CLARIFICATION" || state.clarification.status !== "none";

  return (
    <div className="run">
      <header className="run__header">
        <p className="run__kicker"><Link href="/">New Brief</Link></p>
        <h1 className="run__title" aria-live="polite">{headline(state)}</h1>
        <p className="run__id">Run {runId}</p>
      </header>

      <div className="run__body">
        <StageProgress state={state} />

        <div className="run__detail">
          <RunOverview state={state} elapsed={elapsed} />

          {state.connection === "RECONNECTING" && (
            <p className="notice notice--quiet" role="status" data-testid="reconnecting">
              Reconnecting to progress… The Run carries on meanwhile.
            </p>
          )}
          {state.connection === "LOST" && (
            <div className="notice notice--error" role="alert">
              <p className="notice__title">Progress for this Run can't be shown</p>
              <p>The server may be unavailable, or this Run may belong to another persona. The Run itself is not affected.</p>
              <button type="button" className="button" onClick={retry}>Try again</button>
            </div>
          )}

          {asked && <ClarificationCard runId={runId} progress={state.clarification} ended={state.terminal !== null} />}

          {state.terminal === "COMPLETE" && (
            <div className="outcome outcome--complete" data-testid="run-complete">
              {report.kind === "found" && (
                <Link className="button button--primary" href={`/reports/${encodeURIComponent(report.id)}`}>Read the Report</Link>
              )}
              {(report.kind === "looking" || report.kind === "idle") && <p>Finding your Report…</p>}
              {report.kind === "missing" && (
                <p>The Report isn't listed yet. It will appear among your Reports on the start page.</p>
              )}
            </div>
          )}

          {state.terminal === "FAILED" && (
            <div className="outcome outcome--failed" data-testid="run-failed">
              <p className="outcome__reason">{state.failureReason ?? "The Run stopped without giving a reason."}</p>
              <p>A failed Run produces no Report. Adjust the Brief and start a new Run.</p>
              <Link className="button" href="/">Write a new Brief</Link>
            </div>
          )}

          <SubTaskList state={state} />
          <ActivityLog state={state} />
        </div>
      </div>
    </div>
  );
}
