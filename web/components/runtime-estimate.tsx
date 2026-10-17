"use client";

import { useEffect, useState } from "react";
import { ApiError, estimateRuntime, type RuntimeEstimate as Estimate, type SourceToggles } from "@/lib/api";
import { formatEstimate } from "@/lib/format";

type State =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ready"; estimate: Estimate }
  | { kind: "error"; message: string | null }; // a message only when the request was refused

const DEBOUNCE_MS = 250;

/** The pre-submit estimate: a heuristic from the backend, sent the toggles and the number of
 *  PDFs (never their contents). It does not check that the sources are available, so a Run
 *  may still be refused when submitted. */
export function RuntimeEstimate({ toggles, documentCount, enabled }: {
  toggles: SourceToggles;
  documentCount: number;
  enabled: boolean;
}) {
  const [state, setState] = useState<State>({ kind: "idle" });
  const key = JSON.stringify([toggles, documentCount]);

  useEffect(() => {
    if (!enabled) {
      setState({ kind: "idle" });
      return;
    }
    let live = true;
    setState((s) => (s.kind === "ready" ? s : { kind: "loading" }));
    const timer = setTimeout(() => {
      estimateRuntime(toggles, documentCount)
        .then((estimate) => live && setState({ kind: "ready", estimate }))
        .catch((e: Error) => live && setState({
          kind: "error", message: e instanceof ApiError && e.status === 422 ? e.message : null,
        }));
    }, DEBOUNCE_MS);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [key, enabled]); // `key` stands for toggles and documentCount, compared by value

  return (
    <div className="estimate" aria-live="polite">
      <p className="estimate__label">Estimated runtime</p>
      {state.kind === "idle" && <p className="estimate__value estimate__value--muted">Choose a persona to see an estimate.</p>}
      {state.kind === "loading" && <p className="estimate__value estimate__value--muted">Estimating…</p>}
      {state.kind === "ready" && (
        <p className="estimate__value" data-testid="estimate-value">{formatEstimate(state.estimate.estimated_seconds)}</p>
      )}
      {state.kind === "error" && (
        <p className="estimate__value estimate__value--muted">
          {state.message ? `No estimate: ${state.message}` : "No estimate right now. You can still submit."}
        </p>
      )}
      <p className="estimate__note">
        A rough guide from your sources and documents, not a promise. Runs can take longer.
      </p>
    </div>
  );
}
