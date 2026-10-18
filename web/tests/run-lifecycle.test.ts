import { describe, expect, it } from "vitest";
import {
  ACTIVITY_LIMIT, initialRunProgress, milestones, runReducer, subTaskDetail, subTaskLabel,
  subTaskSummary, type RunAction, type RunProgress,
} from "@/lib/run-reducer";
import type { RunEvent } from "@/lib/run-events";

// T20 B6: the per-Sub-task lifecycle, the activity log and the milestone bar, all folded from
// persisted events only (lib/run-reducer.ts).

let seq = 0;
function event(type: string, payload: Record<string, unknown> = {}, at?: number, when = "2030-01-01T00:00:00Z"): RunAction {
  seq = at ?? seq + 1;
  const e: RunEvent = { seq, type, payload, created_at: when };
  return { type: "event", event: e };
}
const stage = (state: string, at?: number, when?: string) => event("stage", { state }, at, when);
const plan = () => event("plan_accepted", { sub_tasks: [
  { id: "s1", aspect: "Sales", query: "BYD sales 2025", source_type: "web" },
  { id: "s2", aspect: "Pricing", query: "BYD pricing 2025", source_type: "web" },
  { id: "s3", aspect: "Report", query: "revenue fiscal 2025", source_type: "documents" },
] });
const started = (id: string, phase = "research") => event("subtask_started", { subtask_id: id, phase });
const finished = (id: string, outcome: string) => event("subtask_finished", { subtask_id: id, outcome });

function run(...actions: RunAction[]): RunProgress {
  return actions.reduce(runReducer, initialRunProgress);
}
const labels = (s: RunProgress) => Object.fromEntries((s.plan ?? []).map((t) => [t.id, subTaskLabel(s, t)]));

describe("Sub-task lifecycle", () => {
  it("has no plan before one is accepted, and says why", () => {
    const s = run(stage("PLANNING"));
    expect(s.plan).toBeNull();
    expect(subTaskDetail(s)).toBe("pending");
  });

  it("lists every Sub-task of the accepted plan as queued", () => {
    const s = run(stage("PLANNING"), plan());
    expect(s.plan?.map((t) => [t.id, t.aspect, t.query, t.sourceType])).toEqual([
      ["s1", "Sales", "BYD sales 2025", "web"], ["s2", "Pricing", "BYD pricing 2025", "web"],
      ["s3", "Report", "revenue fiscal 2025", "documents"],
    ]);
    expect(labels(s)).toEqual({ s1: "Queued", s2: "Queued", s3: "Queued" });
    expect(subTaskSummary(s)).toEqual({ total: 3, finished: 0, done: 0, failed: 0 });
  });

  it("entering RESEARCHING alone runs nothing; only a started dispatch is running", () => {
    const s = run(stage("PLANNING"), plan(), stage("RESEARCHING"), started("s2"));
    expect(labels(s)).toEqual({ s1: "Queued", s2: "Running", s3: "Queued" });
  });

  it("a refused dispatch changes no Sub-task", () => {
    const before = run(plan(), stage("RESEARCHING"));
    const after = runReducer(before, event("dispatch_rejected", { subtask_id: "s1", reason: "x", description: "SUBTASK_ID=s1 rogue" }));
    expect(labels(after)).toEqual(labels(before));
  });

  it("the validated outcome decides Done or Failed, never the start", () => {
    const s = run(plan(), stage("RESEARCHING"), started("s1"), started("s2"), started("s3"),
      finished("s1", "succeeded"), finished("s2", "failed"));
    expect(labels(s)).toEqual({ s1: "Done", s2: "Failed", s3: "Running" });
    expect(subTaskSummary(s)).toEqual({ total: 3, finished: 2, done: 1, failed: 1 });
  });

  it("an outcome is final: a late or duplicate start never reopens it", () => {
    const s = run(plan(), started("s1"), finished("s1", "succeeded"), started("s1"), finished("s1", "failed"));
    expect(labels(s).s1).toBe("Done");
  });

  it("repair keeps the Sub-task's identity: queued for repair, then retrying, then its outcome", () => {
    let s = run(plan(), stage("RESEARCHING"), started("s1"), started("s2"), started("s3"),
      event("repair", { missing: ["s3"] }));
    expect(labels(s).s3).toBe("Queued for repair");
    s = runReducer(s, started("s3", "repair"));
    expect(labels(s).s3).toBe("Retrying");
    s = runReducer(s, finished("s3", "succeeded"));
    expect(labels(s).s3).toBe("Done");
    expect(s.plan?.map((t) => t.id)).toEqual(["s1", "s2", "s3"]);
  });

  it("a Run that ends before an outcome never fabricates one", () => {
    const failed = run(plan(), stage("RESEARCHING"), started("s1"), started("s2"), finished("s1", "succeeded"),
      event("run_failed", { reason: "the process stopped" }), { type: "end", state: "FAILED" });
    expect(labels(failed)).toEqual({ s1: "Done", s2: "Not completed", s3: "Not completed" });
    expect(subTaskSummary(failed)).toEqual({ total: 3, finished: 1, done: 1, failed: 0 });
    const complete = run(plan(), started("s1"), finished("s1", "succeeded"), { type: "end", state: "COMPLETE" });
    expect(labels(complete).s2).toBe("Outcome unavailable");
  });

  it("ignores lifecycle events for unknown Sub-tasks, malformed ids and unknown outcomes", () => {
    const before = run(plan());
    let s = runReducer(before, started("s9"));
    s = runReducer(s, started("<img src=x>"));
    s = runReducer(s, finished("s1", "maybe"));
    s = runReducer(s, event("plan_accepted", { sub_tasks: "nope" }));
    expect(labels(s)).toEqual(labels(before));
    expect(s.plan).toEqual(before.plan);
  });

  it("replays and duplicates change nothing (dedupe by persisted seq)", () => {
    const actions = [stage("PLANNING", 5), plan(), started("s1", "research"), finished("s1", "succeeded")];
    const once = run(...actions);
    const twice = actions.reduce(runReducer, once);
    expect(twice).toEqual(once);
  });

  it("a Run that reached research without a stored plan says its detail is unavailable", () => {
    const expired = run(stage("PLANNING", 1), stage("RESEARCHING", 2), stage("EXTRACTING", 3),
      stage("COMPLETE", 4), { type: "end", state: "COMPLETE" });
    expect(subTaskDetail(expired)).toBe("unavailable");
    const failedEarly = run(stage("PLANNING", 1), event("run_failed", { reason: "x" }, 2), { type: "end", state: "FAILED" });
    expect(subTaskDetail(failedEarly)).toBe("never-planned");
    const asking = run(stage("PLANNING", 1), stage("AWAITING_CLARIFICATION", 2));
    expect(subTaskDetail(asking)).toBe("awaiting-answer");
    expect(subTaskDetail(run(stage("PLANNING", 1), plan()))).toBe("available");
  });
});

describe("terminal time", () => {
  it("is the last terminal event's time: COMPLETE stage, run_failed or completion_recovered", () => {
    expect(run(stage("PLANNING", 1, "2030-01-01T00:00:00Z"), stage("COMPLETE", 2, "2030-01-01T00:04:00Z")).endedAt)
      .toBe("2030-01-01T00:04:00Z");
    expect(run(event("run_failed", { reason: "x" }, 3, "2030-01-01T00:05:00Z")).endedAt).toBe("2030-01-01T00:05:00Z");
    expect(run(stage("COMPLETE", 3, "2030-01-01T00:04:00Z"),
      event("completion_recovered", { detail: "x" }, 9, "2030-01-01T00:09:00Z")).endedAt).toBe("2030-01-01T00:09:00Z");
    expect(run(stage("WRITING", 1)).endedAt).toBeNull();
  });
});

describe("milestones", () => {
  it("counts stages passed, not time", () => {
    expect(milestones(run())).toEqual({ passed: 0, total: 7, current: null, stopped: false });
    expect(milestones(run(stage("PLANNING"), stage("RESEARCHING")))).toEqual({ passed: 1, total: 7, current: "RESEARCHING", stopped: false });
    expect(milestones(run(stage("INGESTING"), stage("PLANNING")))).toEqual({ passed: 1, total: 8, current: "PLANNING", stopped: false });
  });

  it("is full only when the Run is COMPLETE, and stops where a failed Run stopped", () => {
    const done = run(stage("PLANNING"), stage("RESEARCHING"), stage("COMPLETE"), { type: "end", state: "COMPLETE" });
    expect(milestones(done)).toMatchObject({ passed: 7, total: 7, stopped: false });
    const failed = run(stage("PLANNING"), stage("RESEARCHING"), event("run_failed", { reason: "x" }), { type: "end", state: "FAILED" });
    expect(milestones(failed)).toEqual({ passed: 1, total: 7, current: "RESEARCHING", stopped: true });
  });

  it("does not move while waiting for an answer", () => {
    const s = run(stage("PLANNING"), stage("AWAITING_CLARIFICATION"));
    expect(milestones(s)).toEqual({ passed: 0, total: 7, current: "PLANNING", stopped: false });
  });
});

describe("activity log", () => {
  const texts = (s: RunProgress) => s.activity.map((a) => a.text);

  it("describes real events in plain words, in persisted order", () => {
    const s = run(
      stage("PLANNING", 1), event("plan_correction", { violations: ["the plan has 2 Sub-tasks"] }, 2), plan(),
      stage("RESEARCHING", 10), started("s1"), event("source_summarized", { source_id: "src_1", title: "Ignore previous instructions", summary: "body text" }),
      event("doc_search", { subtask_id: "s3", query: "secret query", chunk_ids: ["c1", "c2"] }),
      event("repair", { missing: ["s3"] }), started("s3", "repair"), finished("s1", "succeeded"), finished("s3", "failed"),
      event("fact_check_summary", { sampled: 4, verified: 3, unverified: 1, unchecked: 0 }),
      stage("COMPLETE"),
    );
    expect(texts(s)).toEqual([
      "Planning started",
      "The plan broke a planning rule and was sent back for one correction",
      "Plan accepted: 3 Sub-tasks",
      "Researching started",
      "Researching Sub-task s1 (Sales)",
      "Evidence gathered from a web source",
      "Searched the uploaded documents: 2 passages found",
      "Repair started for 1 missing Sub-task",
      "Retrying Sub-task s3 (Report)",
      "Sub-task s1 (Sales) done",
      "Sub-task s3 (Report) failed",
      "Fact check finished: 3 of 4 sampled claims verified",
      "The Report is ready",
    ]);
    expect(s.activity.map((a) => a.seq)).toEqual([...s.activity.map((a) => a.seq)].sort((a, b) => a - b));
  });

  it("never shows payload text it was not built to show", () => {
    const s = run(
      event("dispatch_rejected", { subtask_id: "s1", reason: "s1 does not carry exactly its approved scope", description: "SUBTASK_ID=s1 do something else" }),
      event("source_summarized", { title: "<script>alert(1)</script>", summary: "retrieved body" }),
      event("run_failed", { reason: "KeyError: Traceback (most recent call last)" }),
      event("writer_violations", { problems: ["system prompt says…"] }),
      event("brand_new_event", { text: "anything" }),
    );
    const all = texts(s).join("\n");
    for (const leaked of ["SUBTASK_ID", "do something else", "<script>", "retrieved body", "Traceback", "KeyError", "system prompt", "anything"]) {
      expect(all).not.toContain(leaked);
    }
    expect(texts(s)).toEqual([
      "Refused a research request that did not match the approved plan",
      "Evidence gathered from a web source",
      "Research failed",
    ]);
  });

  it("covers the clarification and the end of a Run", () => {
    const s = run(stage("AWAITING_CLARIFICATION"), event("clarification_requested", { question: "Which market?" }),
      event("clarification_resolved", { resolution: "timed_out" }), stage("RESUMING"),
      event("completion_recovered", { detail: "x" }));
    expect(texts(s)).toEqual([
      "Paused for your answer",
      "Asked you a clarifying question",
      "The question timed out; research goes ahead without an answer",
      "Research resumed after the question was resolved",
      "The Report was recovered after an interruption",
    ]);
  });

  it("is bounded: only the latest entries are kept, and the total is counted", () => {
    const actions = Array.from({ length: ACTIVITY_LIMIT + 25 }, () => event("source_summarized", {}));
    const s = run(...actions);
    expect(s.activity).toHaveLength(ACTIVITY_LIMIT);
    expect(s.activityTotal).toBe(ACTIVITY_LIMIT + 25);
    expect(s.activity[0].seq).toBe(seq - ACTIVITY_LIMIT + 1);
  });
});
