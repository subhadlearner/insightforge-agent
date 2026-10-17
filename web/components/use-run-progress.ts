"use client";

// Follows a Run through its native EventSource stream (lib/run-events.ts).
//
// One EventSource per Run. Its `error` event is not a failed Run: while EventSource is
// retrying (readyState CONNECTING) the page shows "reconnecting" and keeps the last stage,
// and the browser resumes with Last-Event-ID by itself. Only when EventSource gives up
// (readyState CLOSED, e.g. the stream answered 401 or 404) is the connection "lost"; then
// `retry` opens a new stream, and only because the User asked.

import { useCallback, useEffect, useReducer, useState } from "react";
import { parseRunEnd, parseRunEvent, statusUrl } from "@/lib/run-events";
import { initialRunProgress, runReducer, type RunProgress } from "@/lib/run-reducer";

export function useRunProgress(runId: string): { state: RunProgress; retry: () => void } {
  const [state, dispatch] = useReducer(runReducer, initialRunProgress);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const source = new EventSource(statusUrl(runId));
    source.onopen = () => dispatch({ type: "open" });
    source.onmessage = (message: MessageEvent<string>) => {
      const event = parseRunEvent(message.data);
      if (event) dispatch({ type: "event", event });
    };
    source.addEventListener("end", (message) => {
      const end = parseRunEnd((message as MessageEvent<string>).data);
      if (end) dispatch({ type: "end", state: end.state });
      source.close(); // the Run is over; the server has closed the stream for good
    });
    source.onerror = () => {
      dispatch({ type: source.readyState === EventSource.CLOSED ? "lost" : "error" });
    };
    return () => source.close();
  }, [runId, attempt]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);
  return { state, retry };
}
