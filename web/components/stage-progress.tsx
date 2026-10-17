"use client";

import { STAGE_LABELS } from "@/lib/format";
import { railStages, type RunProgress } from "@/lib/run-reducer";

type StepStatus = "done" | "current" | "pending" | "stopped";

/** The pipeline as an ordered rail. RESUMING and AWAITING_CLARIFICATION never become steps;
 *  FAILED is an outcome shown on the step where the Run stopped, not a step of its own. */
export function StageProgress({ state }: { state: RunProgress }) {
  const steps = railStages(state);
  const reached = new Set(state.seenStages);
  const latest = state.seenStages.at(-1);

  function status(step: (typeof steps)[number]): StepStatus {
    if (state.terminal === "COMPLETE" || (step === "COMPLETE" && reached.has(step))) return "done";
    if (step === latest) return state.terminal === "FAILED" ? "stopped" : "current";
    return reached.has(step) ? "done" : "pending";
  }

  return (
    <ol className="rail" aria-label="Run progress">
      {steps.map((step, i) => {
        const s = status(step);
        return (
          <li key={step} className={`rail__step rail__step--${s}`} aria-current={s === "current" ? "step" : undefined}>
            <span className="rail__marker" aria-hidden="true">{i + 1}</span>
            <span className="rail__name">{STAGE_LABELS[step]}</span>
            <span className="visually-hidden">
              {s === "done" ? " (done)" : s === "current" ? " (in progress)" : s === "stopped" ? " (stopped here)" : " (not started)"}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
