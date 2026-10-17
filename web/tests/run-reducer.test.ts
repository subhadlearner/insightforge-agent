import { describe, expect, it } from "vitest";
import { initialRunState, railStages, runReducer, type RunAction, type RunState } from "@/lib/run-reducer";
import type { RunEvent } from "@/lib/run-events";

let seq = 0;
function event(type: string, payload: Record<string, unknown> = {}, at?: number): RunAction {
  seq = at ?? seq + 1;
  const e: RunEvent = { seq, type, payload, created_at: "2030-01-01T00:00:00Z" };
  return { type: "event", event: e };
}
const stage = (state: string, at?: number) => event("stage", { state }, at);

function run(...actions: RunAction[]): RunState {
  return actions.reduce(runReducer, initialRunState);
}

describe("runReducer", () => {
  it("starts connecting, with no stage and no outcome", () => {
    expect(initialRunState).toMatchObject({
      connection: "CONNECTING", stage: null, terminal: null, lastSeq: 0, seenStages: [],
    });
  });

  it("advances through stage events", () => {
    const s = run({ type: "open" }, stage("PLANNING"), stage("RESEARCHING"), stage("EXTRACTING"));
    expect(s.stage).toBe("EXTRACTING");
    expect(s.seenStages).toEqual(["PLANNING", "RESEARCHING", "EXTRACTING"]);
    expect(s.connection).toBe("CONNECTED");
  });

  it("ignores an event it has already applied (seq <= lastSeq)", () => {
    const once = run(stage("PLANNING", 10), stage("RESEARCHING", 11));
    const replayed = [stage("PLANNING", 10), stage("RESEARCHING", 11)].reduce(runReducer, once);
    expect(replayed).toEqual(once);
    expect(runReducer(once, event("run_failed", { reason: "x" }, 5)).failureReason).toBeNull();
  });

  it("accepts gaps in seq (the sequence is shared by all Runs)", () => {
    const s = run(stage("PLANNING", 3), stage("RESEARCHING", 40), stage("WRITING", 977));
    expect(s.lastSeq).toBe(977);
    expect(s.stage).toBe("WRITING");
  });

  it("ignores unknown event types without changing what is shown", () => {
    const before = run(stage("PLANNING", 1));
    const after = runReducer(before, event("brand_new_event", { anything: [1, 2] }, 2));
    expect(after).toEqual({ ...before, lastSeq: 2 });
  });

  it("ignores a stage event with an unknown or missing state", () => {
    const before = run(stage("PLANNING", 1));
    expect(runReducer(before, stage("TELEPORTING", 2)).stage).toBe("PLANNING");
    expect(runReducer(before, event("stage", {}, 3)).stage).toBe("PLANNING");
  });

  it("treats a connection error as reconnecting, never as a failed Run", () => {
    const s = run({ type: "open" }, stage("RESEARCHING"), { type: "error" });
    expect(s.connection).toBe("RECONNECTING");
    expect(s.terminal).toBeNull();
    expect(s.stage).toBe("RESEARCHING");
    expect(runReducer(s, { type: "open" }).connection).toBe("CONNECTED");
  });

  it("marks the connection lost (not the Run failed) when the browser gives up", () => {
    const s = run(stage("RESEARCHING"), { type: "lost" });
    expect(s.connection).toBe("LOST");
    expect(s.terminal).toBeNull();
  });

  it("ends COMPLETE from the end event", () => {
    const s = run(stage("FACT_CHECKING"), stage("COMPLETE"), { type: "end", state: "COMPLETE" });
    expect(s.terminal).toBe("COMPLETE");
    expect(s.connection).toBe("CLOSED");
    expect(s.seenStages.at(-1)).toBe("COMPLETE");
  });

  it("ends FAILED from the end event and keeps the run_failed reason", () => {
    const s = run(stage("RESEARCHING"), event("run_failed", { reason: "every Sub-task failed" }),
      { type: "end", state: "FAILED" });
    expect(s.terminal).toBe("FAILED");
    expect(s.failureReason).toBe("every Sub-task failed");
    expect(s.seenStages).not.toContain("FAILED");
  });

  it("ignores connection errors after the Run has ended", () => {
    const ended = run({ type: "end", state: "COMPLETE" });
    expect(runReducer(ended, { type: "error" })).toEqual(ended);
  });

  it("never adds RESUMING or AWAITING_CLARIFICATION to the stage rail", () => {
    const s = run(stage("PLANNING"), stage("AWAITING_CLARIFICATION"), stage("RESUMING"),
      stage("PLANNING"), stage("RESEARCHING"));
    expect(s.seenStages).toEqual(["PLANNING", "RESEARCHING"]);
  });

  it("tracks the clarification from its events", () => {
    const asked = run(stage("PLANNING"), event("clarification_requested", { question: "Which?" }),
      stage("AWAITING_CLARIFICATION"));
    expect(asked.clarification).toEqual({ status: "awaiting", resolution: null });
    expect(asked.stage).toBe("AWAITING_CLARIFICATION");

    const resolved = runReducer(asked, event("clarification_resolved", { resolution: "timed_out" }));
    expect(resolved.clarification).toEqual({ status: "resolved", resolution: "timed_out" });
    expect(resolved.terminal).toBeNull();
    expect(runReducer(resolved, stage("RESUMING")).stage).toBe("RESUMING");
  });

  it("notes when documents were ingested so the rail shows that step", () => {
    expect(run(stage("PLANNING")).seenStages).not.toContain("INGESTING");
    const withUploads = run(stage("INGESTING"), stage("PLANNING"));
    expect(withUploads.seenStages).toEqual(["INGESTING", "PLANNING"]);
  });
});

describe("railStages", () => {
  it("includes INGESTING only when the Run ingested documents", () => {
    expect(railStages(run(stage("PLANNING")))[0]).toBe("PLANNING");
    expect(railStages(run(stage("INGESTING")))[0]).toBe("INGESTING");
    expect(railStages(initialRunState).at(-1)).toBe("COMPLETE");
    expect(railStages(initialRunState)).not.toContain("RESUMING");
  });
});
