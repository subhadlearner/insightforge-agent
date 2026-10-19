"use client";

// T20 B6: what the Run page adds beside the stage rail, all from persisted events (lib/run-reducer.ts)
// and the stored creation time (use-elapsed.ts). The bar counts workflow milestones passed; it is
// never an estimate of the time left.

import { formatElapsed, STAGE_LABELS } from "@/lib/format";
import {
  milestones, SUBTASK_LABELS, subTaskDetail, subTaskSummary, subTaskView, type RunProgress,
} from "@/lib/run-reducer";
import type { Elapsed } from "./use-elapsed";

function elapsedText(elapsed: Elapsed): string {
  switch (elapsed.kind) {
    case "loading": return "Elapsed time: reading…";
    case "unavailable": return "Elapsed time unavailable";
    case "running": return `Elapsed ${formatElapsed(elapsed.ms)}`;
    case "ended": return `Took ${formatElapsed(elapsed.ms)}`;
    case "ended-unrecorded": return "Ended; its total time was not recorded";
  }
}

export function RunOverview({ state, elapsed }: { state: RunProgress; elapsed: Elapsed }) {
  const m = milestones(state);
  const summary = subTaskSummary(state);
  const percent = m.total ? Math.round((m.passed / m.total) * 100) : 0;
  const where = m.current ? STAGE_LABELS[m.current] : null;
  const asked = state.clarification.status !== "none" || state.stage === "AWAITING_CLARIFICATION";
  return (
    <section className="overview" aria-label="Run overview">
      <div
        className={`milestones${m.stopped ? " milestones--stopped" : ""}`}
        role="progressbar"
        aria-label="Workflow milestones passed"
        aria-valuemin={0}
        aria-valuemax={m.total}
        aria-valuenow={m.passed}
        aria-valuetext={`${m.passed} of ${m.total} workflow milestones passed`}
      >
        <span className="milestones__fill" style={{ width: `${percent}%` }} />
      </div>
      <p className="overview__line" data-testid="milestones">
        {m.passed} of {m.total} workflow milestones passed
        {m.stopped && where ? ` · stopped at ${where}` : ""}
        <span className="overview__note"> · a count of stages, not a time estimate</span>
      </p>
      {state.plan && (
        <p className="overview__line" data-testid="subtask-count">
          Sub-tasks: {summary.finished} of {summary.total} finished
          {summary.finished > 0 && ` (${summary.done} done, ${summary.failed} failed)`}
        </p>
      )}
      <p className="overview__line" data-testid="elapsed">
        {elapsedText(elapsed)}
        {(elapsed.kind === "running" || elapsed.kind === "ended") && (
          <span className="overview__note">
            {" "}· since submission, including time queued{asked ? " and waiting for your answer" : ""}
          </span>
        )}
      </p>
    </section>
  );
}

const DETAIL_TEXT = {
  pending: "The Sub-tasks appear here once the plan is accepted.",
  "awaiting-answer": "Planning continues once your question is answered or times out.",
  "never-planned": "No plan was accepted for this Run.",
  unavailable: "Sub-task details are not available for this Run. Runs past their retention period, and Runs recorded before Sub-task progress was kept, keep only their stage history.",
} as const;

export function SubTaskList({ state }: { state: RunProgress }) {
  const detail = subTaskDetail(state);
  return (
    <section className="subtasks" aria-labelledby="subtasks-title">
      <h2 className="run__subtitle" id="subtasks-title">Sub-tasks</h2>
      {detail === "available" && state.plan ? (
        <ol className="subtasks__list">
          {state.plan.map((t) => {
            const view = subTaskView(state, t);
            return (
              <li key={t.id} className={`subtask subtask--${view}`} data-testid={`subtask-${t.id}`}>
                <span className="subtask__head">
                  <span className="subtask__id">{t.id}</span>
                  {t.aspect && <span className="subtask__aspect">{t.aspect}</span>}
                  <span className="subtask__status">{SUBTASK_LABELS[view]}</span>
                </span>
                {t.query && <span className="subtask__query">{t.query}</span>}
              </li>
            );
          })}
        </ol>
      ) : (
        <p className="subtasks__empty" data-testid="subtasks-unavailable">{DETAIL_TEXT[detail as keyof typeof DETAIL_TEXT]}</p>
      )}
    </section>
  );
}

function timeOfDay(at: string): string {
  const d = new Date(at);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function ActivityLog({ state }: { state: RunProgress }) {
  const hidden = state.activityTotal - state.activity.length;
  return (
    <section className="activity" aria-labelledby="activity-title">
      <h2 className="run__subtitle" id="activity-title">Activity</h2>
      {state.activity.length === 0 ? (
        <p className="activity__empty">Nothing has happened yet.</p>
      ) : (
        <div className="activity__log" role="log" aria-label="Run activity">
          <ol className="activity__list">
            {state.activity.map((a) => (
              <li key={a.seq} className={`activity__entry activity__entry--${a.tone}`}>
                <time className="activity__time" dateTime={a.at}>{timeOfDay(a.at)}</time>
                <span className="activity__text">{a.text}</span>
              </li>
            ))}
          </ol>
        </div>
      )}
      {hidden > 0 && <p className="activity__note">Showing the latest {state.activity.length} of {state.activityTotal} entries.</p>}
      {subTaskDetail(state) === "unavailable" && (
        <p className="activity__note">Detailed activity is no longer available for this Run; its stage history is shown.</p>
      )}
    </section>
  );
}
