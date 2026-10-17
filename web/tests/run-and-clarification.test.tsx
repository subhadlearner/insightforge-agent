import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "@/lib/api";
import { ClarificationCard } from "@/components/clarification-card";
import { RunView } from "@/components/run-view";

vi.mock("@/lib/api", async (orig) => (await import("./mock-api")).mockApi(orig));
const m = vi.mocked(api);

/** A controllable stand-in for the browser's EventSource. */
class FakeEventSource {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 2;
  static instances: FakeEventSource[] = [];
  readyState = FakeEventSource.CONNECTING;
  onopen: (() => void) | null = null;
  onmessage: ((e: MessageEvent<string>) => void) | null = null;
  onerror: (() => void) | null = null;
  private named = new Map<string, (e: MessageEvent<string>) => void>();
  closed = false;

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }
  addEventListener(type: string, fn: (e: MessageEvent<string>) => void) {
    this.named.set(type, fn);
  }
  close() {
    this.closed = true;
    this.readyState = FakeEventSource.CLOSED;
  }
  // --- driving it ---
  open() {
    act(() => {
      this.readyState = FakeEventSource.OPEN;
      this.onopen?.();
    });
  }
  emit(seq: number, type: string, payload: Record<string, unknown> = {}) {
    act(() => this.onmessage?.(new MessageEvent("message", {
      data: JSON.stringify({ seq, type, payload, created_at: "2030-01-01T00:00:00Z" }),
    })));
  }
  stage(seq: number, state: string) {
    this.emit(seq, "stage", { state });
  }
  end(state: "COMPLETE" | "FAILED") {
    act(() => this.named.get("end")?.(new MessageEvent("end", { data: JSON.stringify({ run_id: "run_1", state }) })));
  }
  drop(givenUp = false) {
    act(() => {
      this.readyState = givenUp ? FakeEventSource.CLOSED : FakeEventSource.CONNECTING;
      this.onerror?.();
    });
  }
}

const source = () => FakeEventSource.instances.at(-1)!;
const railNames = () => within(screen.getByRole("list", { name: "Run progress" }))
  .getAllByRole("listitem").map((li) => li.querySelector(".rail__name")?.textContent);

beforeEach(() => {
  FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("Run progress", () => {
  it("follows the Run's same-origin stream and advances by stage", () => {
    render(<RunView runId="run_1" />);
    expect(source().url).toBe("/api/research/status?run_id=run_1");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Connecting to the Run…");
    source().open();
    source().stage(4, "PLANNING");
    source().stage(9, "RESEARCHING");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Researching…");
    expect(screen.getByText("Researching").closest("li")).toHaveAttribute("aria-current", "step");
  });

  it("shows reconnecting on a dropped stream, keeps the stage, and never shows FAILED", () => {
    render(<RunView runId="run_1" />);
    source().open();
    source().stage(1, "RESEARCHING");
    source().drop();
    expect(screen.getByTestId("reconnecting")).toHaveTextContent("Reconnecting to progress…");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Researching…");
    expect(screen.queryByTestId("run-failed")).not.toBeInTheDocument();
    expect(FakeEventSource.instances).toHaveLength(1); // the browser reconnects, not us
    source().open();
    source().stage(1, "RESEARCHING"); // replayed after reconnect: ignored
    source().stage(2, "EXTRACTING");
    expect(screen.queryByTestId("reconnecting")).not.toBeInTheDocument();
    expect(screen.getAllByText("Researching")).toHaveLength(1);
  });

  it("offers a manual retry only when the browser gives up on the stream", async () => {
    render(<RunView runId="run_1" />);
    source().drop(true);
    expect(screen.getByRole("alert")).toHaveTextContent("Progress for this Run can't be shown");
    expect(screen.queryByTestId("run-failed")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(FakeEventSource.instances).toHaveLength(2);
  });

  it("on COMPLETE finds the Run's Report and links to it", async () => {
    m.listReports.mockResolvedValue([
      { id: "rep_other", run_id: "run_0", created_at: "", title: "Old", brief: "" },
      { id: "rep_1", run_id: "run_1", created_at: "", title: "New", brief: "" },
    ]);
    render(<RunView runId="run_1" />);
    source().open();
    source().stage(1, "FACT_CHECKING");
    source().stage(2, "COMPLETE");
    source().end("COMPLETE");
    expect(source().closed).toBe(true);
    expect(await screen.findByRole("link", { name: "Read the Report" })).toHaveAttribute("href", "/reports/rep_1");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Your Report is ready");
  });

  it("on FAILED shows the reason and links to no Report", () => {
    render(<RunView runId="run_1" />);
    source().open();
    source().stage(1, "RESEARCHING");
    source().emit(5, "run_failed", { reason: "every Sub-task failed" });
    source().end("FAILED");
    expect(screen.getByTestId("run-failed")).toHaveTextContent("every Sub-task failed");
    expect(screen.queryByRole("link", { name: "Read the Report" })).not.toBeInTheDocument();
    expect(m.listReports).not.toHaveBeenCalled();
    expect(railNames()).not.toContain("Failed");
  });

  it("never draws RESUMING as a step, and shows INGESTING only for uploads", () => {
    m.getClarification.mockResolvedValue({ run_id: "run_1", question: "Which market?", deadline: "2999-01-01T00:00:00Z", resolution: null });
    render(<RunView runId="run_1" />);
    source().stage(1, "PLANNING");
    source().stage(2, "AWAITING_CLARIFICATION");
    source().stage(3, "RESUMING");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Resuming research…");
    expect(railNames()).toEqual(["Planning", "Researching", "Extracting facts", "Weighing evidence",
      "Writing the Report", "Fact-checking", "Report ready"]);
  });
});

describe("Clarification", () => {
  const open: api.ClarificationOut = { run_id: "run_1", question: "Which market and period?", deadline: "2999-01-01T00:00:00Z", resolution: null };

  it("reads the question from the API when the Run waits for an answer", async () => {
    m.getClarification.mockResolvedValue(open);
    render(<RunView runId="run_1" />);
    source().stage(1, "PLANNING");
    source().stage(2, "AWAITING_CLARIFICATION");
    expect(await screen.findByText("Which market and period?")).toBeInTheDocument();
    expect(m.getClarification).toHaveBeenCalledWith("run_1");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Waiting for your answer");
  });

  it("sends the answer and shows that research resumes", async () => {
    m.getClarification.mockResolvedValueOnce(open).mockResolvedValue({ ...open, resolution: "answered" });
    m.answerClarification.mockResolvedValue({ run_id: "run_1", state: "RESUMING" });
    render(<ClarificationCard runId="run_1" progress={{ status: "awaiting", resolution: null }} />);
    await userEvent.type(await screen.findByLabelText("Your answer"), "EU, 2025");
    await userEvent.click(screen.getByRole("button", { name: "Send answer" }));
    expect(m.answerClarification).toHaveBeenCalledWith("run_1", "EU, 2025");
    expect(await screen.findByText("Answer received. Resuming research…")).toBeInTheDocument();
  });

  it("on a late 409 re-reads the clarification, shows the timeout and does not resend", async () => {
    m.getClarification.mockResolvedValueOnce(open).mockResolvedValue({ ...open, resolution: "timed_out" });
    m.answerClarification.mockRejectedValue(new api.ApiError(409, "the clarification deadline has passed"));
    render(<ClarificationCard runId="run_1" progress={{ status: "awaiting", resolution: null }} />);
    await userEvent.type(await screen.findByLabelText("Your answer"), "EU");
    await userEvent.click(screen.getByRole("button", { name: "Send answer" }));
    expect(await screen.findByText(/arrived after the question timed out/)).toBeInTheDocument();
    expect(screen.getByText(/best-effort reading of the Brief/)).toBeInTheDocument();
    expect(m.answerClarification).toHaveBeenCalledTimes(1);
    expect(m.getClarification).toHaveBeenCalledTimes(2);
    expect(screen.queryByText(/failed/i)).not.toBeInTheDocument();
  });

  it("on a 409 with another answer accepted says so", async () => {
    m.getClarification.mockResolvedValueOnce(open).mockResolvedValue({ ...open, resolution: "answered" });
    m.answerClarification.mockRejectedValue(new api.ApiError(409, "not awaiting an answer"));
    render(<ClarificationCard runId="run_1" progress={{ status: "awaiting", resolution: null }} />);
    await userEvent.type(await screen.findByLabelText("Your answer"), "EU");
    await userEvent.click(screen.getByRole("button", { name: "Send answer" }));
    expect(await screen.findByText(/Another answer was accepted first/)).toBeInTheDocument();
  });

  it("at the visual deadline asks the server instead of deciding the timeout itself", async () => {
    const soon = new Date(Date.now() + 1500).toISOString();
    m.getClarification.mockResolvedValue({ ...open, deadline: soon });
    render(<ClarificationCard runId="run_1" progress={{ status: "awaiting", resolution: null }} />);
    expect(await screen.findByText(/Answer within/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText(/The server decides/)).toBeInTheDocument(), { timeout: 8000 });
    expect(m.getClarification.mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(screen.getByLabelText("Your answer")).toBeInTheDocument(); // not declared timed out
    expect(screen.queryByText(/timed out/)).not.toBeInTheDocument();
  });

  it("after the Run ends, keeps a record of a timed-out question and no answer form", async () => {
    m.getClarification.mockResolvedValue({ ...open, resolution: "timed_out" });
    m.listReports.mockResolvedValue([{ id: "rep_1", run_id: "run_1", created_at: "", title: "R", brief: "" }]);
    render(<RunView runId="run_1" />);
    source().stage(1, "PLANNING");
    source().stage(2, "AWAITING_CLARIFICATION");
    source().emit(3, "clarification_resolved", { resolution: "timed_out" });
    source().stage(4, "RESUMING");
    source().stage(5, "COMPLETE");
    source().end("COMPLETE");
    expect(await screen.findByText(/Research went ahead on a best-effort reading of the Brief/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Your answer")).not.toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Read the Report" })).toBeInTheDocument();
  });

  it("a timeout reported by the stream continues the Run and is not a failure", async () => {
    m.getClarification.mockResolvedValueOnce(open).mockResolvedValue({ ...open, resolution: "timed_out" });
    render(<RunView runId="run_1" />);
    source().stage(1, "PLANNING");
    source().stage(2, "AWAITING_CLARIFICATION");
    await screen.findByText("Which market and period?");
    source().emit(3, "clarification_resolved", { resolution: "timed_out" });
    source().stage(4, "RESUMING");
    expect(await screen.findByText(/The question timed out/)).toBeInTheDocument();
    expect(screen.queryByTestId("run-failed")).not.toBeInTheDocument();
    source().stage(5, "PLANNING");
    source().stage(6, "RESEARCHING");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Researching…");
  });
});
