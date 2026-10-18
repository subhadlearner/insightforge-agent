"use client";

// A Run's elapsed time, measured from its stored `created_at` (GET /api/research/{id}/timing), so
// time queued and time waiting for an answer count, and a reload shows the same figure. The
// server's clock is read with it, so a browser clock that is off does not skew the count.
// It stops at the Run's last terminal event (`endedAt`), which retention keeps.

import { useEffect, useState } from "react";
import { getRunTiming } from "@/lib/api";
import type { RunProgress } from "@/lib/run-reducer";

type Timing = { kind: "loading" } | { kind: "unavailable" } | { kind: "known"; createdAt: number; offsetMs: number };

export type Elapsed =
  | { kind: "loading" }
  | { kind: "unavailable" } // the creation time could not be read
  | { kind: "running"; ms: number }
  | { kind: "ended"; ms: number }
  | { kind: "ended-unrecorded" }; // ended, but no terminal event survives to stop the clock at

export function useElapsed(runId: string, state: RunProgress): Elapsed {
  const [timing, setTiming] = useState<Timing>({ kind: "loading" });
  const [now, setNow] = useState(() => Date.now());
  const [attempt, setAttempt] = useState(0);
  const connected = state.connection === "CONNECTED";

  // Read once; a stream that (re)connects asks again only if the last read failed.
  useEffect(() => {
    if (connected && timing.kind === "unavailable") setAttempt((n) => n + 1);
  }, [connected]);

  useEffect(() => {
    let live = true;
    getRunTiming(runId)
      .then((t) => {
        if (!live) return;
        const createdAt = Date.parse(t?.created_at ?? "");
        const serverNow = Date.parse(t?.now ?? "");
        setTiming(Number.isFinite(createdAt) && Number.isFinite(serverNow)
          ? { kind: "known", createdAt, offsetMs: serverNow - Date.now() }
          : { kind: "unavailable" });
      })
      .catch(() => live && setTiming({ kind: "unavailable" }));
    return () => {
      live = false;
    };
  }, [runId, attempt]);

  const ended = state.terminal !== null;
  useEffect(() => {
    if (ended) return;
    const tick = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(tick);
  }, [ended]);

  if (timing.kind !== "known") return timing;
  if (ended) {
    const end = state.endedAt ? Date.parse(state.endedAt) : NaN;
    return Number.isFinite(end) ? { kind: "ended", ms: Math.max(0, end - timing.createdAt) } : { kind: "ended-unrecorded" };
  }
  return { kind: "running", ms: Math.max(0, now + timing.offsetMs - timing.createdAt) };
}
