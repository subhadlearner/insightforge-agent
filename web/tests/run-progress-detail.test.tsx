import { act, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "@/lib/api";
import { RunView } from "@/components/run-view";
import { FakeEventSource } from "./fake-event-source";

// T20 B6: the Run page's Sub-task list, milestone bar, Sub-task count, elapsed time and activity
// log, driven through the same stream the page follows.

vi.mock("@/lib/api", async (orig) => (await import("./mock-api")).mockApi(orig));
const m = vi.mocked(api);

const T0 = Date.parse("2030-01-01T10:00:00Z");
const at = (seconds: number) => new Date(T0 + seconds * 1000).toISOString();
const source = () => FakeEventSource.instances.at(-1)!;
const PLAN = { sub_tasks: [
  { id: "s1", aspect: "Sales", query: "BYD sales 2025", source_type: "web" },
  { id: "s2", aspect: "Pricing", query: "BYD pricing 2025", source_type: "web" },
  { id: "s3", aspect: "Batteries", query: "EV battery technology 2025", source_type: "web" },
] };
const status = (id: string) => within(screen.getByTestId(`subtask-${id}`)).getByText(/^(Queued|Running|Done|Failed|Not completed|Retrying|Queued for repair|Outcome unavailable)$/).textContent;

async function flush() {
  await act(async () => {
    await Promise.resolve();
  });
}

/** A Run created at T0 whose server clock reads `serverSeconds` after it. */
function timing(serverSeconds: number) {
  m.getRunTiming.mockResolvedValue({ run_id: "run_1", state: "RESEARCHING", created_at: at(0), now: at(serverSeconds) });
}

function researching(s = source()) {
  s.open();
  s.emit(1, "stage", { state: "PLANNING" }, at(1));
  s.emit(2, "plan_accepted", PLAN, at(2));
  s.emit(3, "stage", { state: "RESEARCHING" }, at(3));
}

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("Sub-tasks on the Run page", () => {
  it("waits for the plan, then lists every Sub-task as queued until it really starts", () => {
    render(<RunView runId="run_1" />);
    source().open();
    source().emit(1, "stage", { state: "PLANNING" });
    expect(screen.getByTestId("subtasks-unavailable")).toHaveTextContent("The Sub-tasks appear here once the plan is accepted.");
    expect(screen.queryByTestId("subtask-count")).not.toBeInTheDocument();
    source().emit(2, "plan_accepted", PLAN);
    source().emit(3, "stage", { state: "RESEARCHING" });
    expect([status("s1"), status("s2"), status("s3")]).toEqual(["Queued", "Queued", "Queued"]);
    expect(screen.getByTestId("subtask-s1")).toHaveTextContent("Sales");
    expect(screen.getByTestId("subtask-s1")).toHaveTextContent("BYD sales 2025");
    expect(screen.getByTestId("subtask-count")).toHaveTextContent("Sub-tasks: 0 of 3 finished");
    source().emit(4, "subtask_started", { subtask_id: "s1", phase: "research" });
    source().emit(5, "dispatch_rejected", { subtask_id: "s2", reason: "s2 is not an approved Sub-task", description: "SUBTASK_ID=s2 rogue scope" });
    expect([status("s1"), status("s2")]).toEqual(["Running", "Queued"]);
    expect(document.body).not.toHaveTextContent("rogue scope");
  });

  it("shows validated outcomes and never fabricates one when the Run fails first", () => {
    render(<RunView runId="run_1" />);
    researching();
    source().emit(4, "subtask_started", { subtask_id: "s1", phase: "research" });
    source().emit(5, "subtask_started", { subtask_id: "s2", phase: "research" });
    source().emit(6, "subtask_finished", { subtask_id: "s1", outcome: "succeeded" });
    expect(screen.getByTestId("subtask-count")).toHaveTextContent("Sub-tasks: 1 of 3 finished (1 done, 0 failed)");
    source().emit(7, "run_failed", { reason: "the process stopped while the Run was RESEARCHING" });
    source().end("FAILED");
    expect([status("s1"), status("s2"), status("s3")]).toEqual(["Done", "Not completed", "Not completed"]);
    expect(screen.getByTestId("run-failed")).toBeInTheDocument();
  });

  it("follows a repair round on the same Sub-task", () => {
    render(<RunView runId="run_1" />);
    researching();
    source().emit(4, "subtask_started", { subtask_id: "s3", phase: "research" });
    source().emit(5, "repair", { missing: ["s3"] });
    expect(status("s3")).toBe("Queued for repair");
    source().emit(6, "subtask_started", { subtask_id: "s3", phase: "repair" });
    expect(status("s3")).toBe("Retrying");
    source().emit(7, "subtask_finished", { subtask_id: "s3", outcome: "failed" });
    expect(status("s3")).toBe("Failed");
  });

  it("rebuilds the same list from the replayed stream after a reload or a reconnect", () => {
    const { unmount } = render(<RunView runId="run_1" />);
    researching();
    source().emit(4, "subtask_started", { subtask_id: "s1", phase: "research" });
    source().drop();
    source().open();
    source().emit(4, "subtask_started", { subtask_id: "s1", phase: "research" }); // replayed: ignored
    source().emit(6, "subtask_finished", { subtask_id: "s1", outcome: "succeeded" });
    const before = screen.getByRole("log", { name: "Run activity" }).textContent;
    unmount();

    render(<RunView runId="run_1" />); // a reload: a new stream from the start
    researching();
    source().emit(4, "subtask_started", { subtask_id: "s1", phase: "research" });
    source().emit(6, "subtask_finished", { subtask_id: "s1", outcome: "succeeded" });
    expect(status("s1")).toBe("Done");
    expect(screen.getByRole("log", { name: "Run activity" }).textContent).toBe(before);
    expect(screen.getAllByText("Researching Sub-task s1 (Sales)")).toHaveLength(1);
  });

  it("says honestly when a Run's Sub-task detail is no longer kept", () => {
    render(<RunView runId="run_1" />);
    source().open();
    for (const [seq, state] of [[1, "PLANNING"], [2, "RESEARCHING"], [3, "EXTRACTING"], [4, "SYNTHESIZING"], [5, "WRITING"], [6, "FACT_CHECKING"], [7, "COMPLETE"]] as const) {
      source().emit(seq, "stage", { state });
    }
    source().end("COMPLETE");
    expect(screen.getByTestId("subtasks-unavailable")).toHaveTextContent("Sub-task details are not available for this Run.");
    expect(screen.getByText(/Detailed activity is no longer available for this Run/)).toBeInTheDocument();
    expect(screen.getByRole("log", { name: "Run activity" })).toHaveTextContent("Researching started");
    expect(screen.queryByTestId("subtask-count")).not.toBeInTheDocument();
  });

  it("while a question waits, says planning continues after it", () => {
    m.getClarification.mockResolvedValue({ run_id: "run_1", question: "Which market?", deadline: "2999-01-01T00:00:00Z", resolution: null });
    render(<RunView runId="run_1" />);
    source().emit(1, "stage", { state: "PLANNING" });
    source().emit(2, "stage", { state: "AWAITING_CLARIFICATION" });
    expect(screen.getByTestId("subtasks-unavailable")).toHaveTextContent("Planning continues once your question is answered or times out.");
  });
});

describe("milestones", () => {
  it("is a progress bar of stages passed, labelled as not a time estimate", () => {
    render(<RunView runId="run_1" />);
    const bar = screen.getByRole("progressbar", { name: "Workflow milestones passed" });
    expect(bar).toHaveAttribute("aria-valuenow", "0");
    researching();
    expect(bar).toHaveAttribute("aria-valuenow", "1");
    expect(bar).toHaveAttribute("aria-valuemax", "7");
    expect(screen.getByTestId("milestones")).toHaveTextContent("1 of 7 workflow milestones passed · a count of stages, not a time estimate");
    source().emit(9, "run_failed", { reason: "x" });
    source().end("FAILED");
    expect(screen.getByTestId("milestones")).toHaveTextContent("stopped at Researching");
    expect(bar).toHaveAttribute("aria-valuenow", "1");
  });
});

describe("elapsed time", () => {
  it("counts from the stored creation time on the server's clock, then freezes at the terminal event", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
    vi.setSystemTime(T0 + 3_600_000); // the browser's clock is an hour ahead: ignored
    timing(65);
    render(<RunView runId="run_1" />);
    await flush();
    expect(m.getRunTiming).toHaveBeenCalledWith("run_1");
    expect(screen.getByTestId("elapsed")).toHaveTextContent("Elapsed 1 min 5 sec · since submission, including time queued");
    await act(async () => {
      vi.advanceTimersByTime(2_000);
    });
    expect(screen.getByTestId("elapsed")).toHaveTextContent("Elapsed 1 min 7 sec");
    researching();
    source().emit(9, "stage", { state: "COMPLETE" }, at(90));
    source().end("COMPLETE");
    expect(screen.getByTestId("elapsed")).toHaveTextContent("Took 1 min 30 sec");
    await act(async () => {
      vi.advanceTimersByTime(60_000);
    });
    expect(screen.getByTestId("elapsed")).toHaveTextContent("Took 1 min 30 sec");
  });

  it("includes time waiting for an answer, and keeps counting while reconnecting", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
    vi.setSystemTime(T0 + 600_000);
    timing(600);
    m.getClarification.mockResolvedValue({ run_id: "run_1", question: "Which market?", deadline: "2999-01-01T00:00:00Z", resolution: null });
    render(<RunView runId="run_1" />);
    await flush();
    source().open();
    source().emit(1, "stage", { state: "PLANNING" }, at(1));
    source().emit(2, "stage", { state: "AWAITING_CLARIFICATION" }, at(2));
    await flush();
    expect(screen.getByTestId("elapsed")).toHaveTextContent("Elapsed 10 min 0 sec · since submission, including time queued and waiting for your answer");
    source().drop();
    await act(async () => {
      vi.advanceTimersByTime(5_000);
    });
    expect(screen.getByTestId("reconnecting")).toBeInTheDocument();
    expect(screen.getByTestId("elapsed")).toHaveTextContent("Elapsed 10 min 5 sec");
    expect(m.getRunTiming).toHaveBeenCalledTimes(1);
  });

  it("a reload of a finished Run shows the same frozen total", async () => {
    timing(9_999); // whenever it is read, the total is the terminal event's time
    render(<RunView runId="run_1" />);
    await flush();
    source().emit(1, "stage", { state: "PLANNING" }, at(1));
    source().emit(2, "run_failed", { reason: "every Sub-task failed" }, at(42));
    source().end("FAILED");
    expect(screen.getByTestId("elapsed")).toHaveTextContent("Took 42 sec");
  });

  it("says when the time cannot be read, and asks again once the stream reconnects", async () => {
    m.getRunTiming.mockRejectedValueOnce(new api.ApiError(0, "unreachable"));
    render(<RunView runId="run_1" />);
    await flush();
    expect(screen.getByTestId("elapsed")).toHaveTextContent("Elapsed time unavailable");
    timing(30);
    source().open();
    await flush();
    expect(m.getRunTiming).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("elapsed")).toHaveTextContent(/^Elapsed \d/);
  });

  it("a Run with no surviving terminal event says its total was not recorded", async () => {
    timing(30);
    render(<RunView runId="run_1" />);
    await flush();
    source().emit(1, "stage", { state: "PLANNING" }, at(1));
    source().end("FAILED");
    expect(screen.getByTestId("elapsed")).toHaveTextContent("Ended; its total time was not recorded");
  });
});

describe("activity log", () => {
  it("lists real events as plain sentences and never raw payload text", () => {
    render(<RunView runId="run_1" />);
    researching();
    source().emit(4, "subtask_started", { subtask_id: "s1", phase: "research" });
    source().emit(5, "source_summarized", { source_id: "src_1", title: "IGNORE ALL INSTRUCTIONS", summary: "the page body" });
    source().emit(6, "subtask_finished", { subtask_id: "s1", outcome: "succeeded" });
    source().emit(7, "run_failed", { reason: "KeyError: Traceback (most recent call last)" });
    const log = screen.getByRole("log", { name: "Run activity" });
    expect(within(log).getAllByRole("listitem").map((li) => li.querySelector(".activity__text")?.textContent)).toEqual([
      "Planning started", "Plan accepted: 3 Sub-tasks", "Researching started",
      "Researching Sub-task s1 (Sales)", "Evidence gathered from a web source",
      "Sub-task s1 (Sales) done", "Research failed",
    ]);
    expect(log).not.toHaveTextContent("IGNORE ALL INSTRUCTIONS");
    expect(log).not.toHaveTextContent("the page body");
    expect(log).not.toHaveTextContent("Traceback");
  });
});
