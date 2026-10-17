import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "@/lib/api";
import { BriefForm } from "@/components/brief-form";
import { PersonaPicker } from "@/components/persona-picker";
import { PersonaProvider } from "@/components/persona-context";
import { routerMock } from "./setup";

vi.mock("@/lib/api", async (orig) => (await import("./mock-api")).mockApi(orig));

const m = vi.mocked(api);
const PERSONAS: api.Persona[] = [
  { id: "alice", display_name: "Alice Analyst", source_toggles: { web: true, documents: true, memory: true } },
  { id: "bob", display_name: "Bob Strategist", source_toggles: { web: true, documents: false, memory: true } },
];
const ESTIMATE = { estimated_seconds: 135, base_seconds: 30, document_count: 2, source_types: ["web", "documents"] } as api.RuntimeEstimate;

function pdf(name: string, size = 1000): File {
  const file = new File(["%PDF-1.7"], name, { type: "application/pdf" });
  Object.defineProperty(file, "size", { value: size });
  return file;
}

function renderDesk() {
  return render(
    <PersonaProvider>
      <PersonaPicker />
      <BriefForm />
    </PersonaProvider>,
  );
}

beforeEach(() => {
  m.getPersonas.mockResolvedValue(PERSONAS);
  m.getSession.mockResolvedValue(PERSONAS[1]);
  m.estimateRuntime.mockResolvedValue(ESTIMATE);
});

describe("persona picker", () => {
  it("lists the personas and restores the selected one from the session", async () => {
    renderDesk();
    const select = await screen.findByLabelText("Researching as");
    expect(select).toHaveValue("bob");
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual(["Alice Analyst", "Bob Strategist"]);
  });

  it("selects through POST /session and shows the new persona", async () => {
    m.selectPersona.mockResolvedValue(PERSONAS[0]);
    renderDesk();
    await userEvent.selectOptions(await screen.findByLabelText("Researching as"), "alice");
    expect(m.selectPersona).toHaveBeenCalledWith("alice");
    await waitFor(() => expect(screen.getByLabelText("Researching as")).toHaveValue("alice"));
    expect(window.localStorage.length).toBe(0); // the cookie is the only record
  });

  it("asks for a persona when none is selected and keeps submit disabled", async () => {
    m.getSession.mockResolvedValue(null);
    renderDesk();
    expect(await screen.findByRole("option", { name: "Choose a persona" })).toBeInTheDocument();
    expect(screen.getByText("Choose a persona at the top to start.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start research" })).toBeDisabled();
    expect(m.estimateRuntime).not.toHaveBeenCalled();
  });

  it("explains a failure to load personas", async () => {
    m.getPersonas.mockRejectedValue(new api.ApiError(0, "The server could not be reached."));
    renderDesk();
    expect(await screen.findByRole("alert")).toHaveTextContent("Personas could not be loaded");
  });
});

describe("Brief form", () => {
  it("starts from the persona's default sources", async () => {
    renderDesk();
    await screen.findByLabelText("Researching as");
    await waitFor(() => expect(screen.getByRole("checkbox", { name: /Your documents/ })).not.toBeChecked());
    expect(screen.getByRole("checkbox", { name: /Web/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /Earlier Reports/ })).toBeChecked();
  });

  it("asks for an estimate with the toggles and the number of PDFs, never their contents", async () => {
    const arrayBuffer = vi.spyOn(File.prototype, "arrayBuffer");
    renderDesk();
    await screen.findByLabelText("Researching as");
    await userEvent.click(screen.getByRole("checkbox", { name: /Earlier Reports/ }));
    await userEvent.upload(screen.getByLabelText("Documents"), [pdf("a.pdf"), pdf("b.pdf")]);
    await waitFor(() => expect(m.estimateRuntime).toHaveBeenLastCalledWith(
      { web: true, documents: false, memory: false }, 2));
    expect(await screen.findByTestId("estimate-value")).toHaveTextContent("about 2 min 15 sec");
    expect(screen.getByText(/not a promise/)).toBeInTheDocument();
    expect(arrayBuffer).not.toHaveBeenCalled();
  });

  it("keeps the form usable when the estimate fails", async () => {
    m.estimateRuntime.mockRejectedValue(new api.ApiError(500, "boom"));
    renderDesk();
    await screen.findByLabelText("Researching as");
    await userEvent.type(screen.getByLabelText("Brief"), "BYD pricing in 2025");
    expect(await screen.findByText("No estimate right now. You can still submit.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start research" })).toBeEnabled();
  });

  it("shows file sizes, refuses what does not fit, and removes a file", async () => {
    renderDesk();
    await screen.findByLabelText("Researching as");
    const input = screen.getByLabelText("Documents");
    await userEvent.upload(input, [pdf("deck.pdf", 1536), pdf("huge.pdf", 21 * 1024 * 1024)]);
    expect(screen.getByText("deck.pdf")).toBeInTheDocument();
    expect(screen.getByText("1.5 KB")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("huge.pdf was not added: larger than 20 MB.");
    await userEvent.click(screen.getByRole("button", { name: "Remove deck.pdf" }));
    expect(screen.queryByText("deck.pdf")).not.toBeInTheDocument();
  });

  it("submits once, disables itself while submitting, then opens the Run", async () => {
    let accept!: (v: api.RunAccepted) => void;
    m.submitRun.mockReturnValue(new Promise((r) => (accept = r)));
    renderDesk();
    await screen.findByLabelText("Researching as");
    await userEvent.type(screen.getByLabelText("Brief"), "BYD pricing in 2025");
    const button = screen.getByRole("button", { name: "Start research" });
    await userEvent.click(button);
    expect(screen.getByRole("button", { name: "Starting research…" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Starting research…" }));
    expect(m.submitRun).toHaveBeenCalledTimes(1);
    expect(m.submitRun).toHaveBeenCalledWith("BYD pricing in 2025", { web: true, documents: false, memory: true }, []);
    await act(async () => accept({ run_id: "run_1", state: "PLANNING", status_url: "/research/status?run_id=run_1" }));
    expect(routerMock.push).toHaveBeenCalledWith("/runs/run_1");
  });

  it("shows a 422 even though an estimate was available, and does not retry", async () => {
    m.submitRun.mockRejectedValue(new api.ApiError(422, "no source type is available for this Brief"));
    renderDesk();
    await screen.findByTestId("estimate-value");
    await userEvent.type(screen.getByLabelText("Brief"), "Earlier work only");
    await userEvent.click(screen.getByRole("button", { name: "Start research" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("This Brief can't be researched as submitted");
    expect(alert).toHaveTextContent("no source type is available for this Brief");
    expect(m.submitRun).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("estimate-value")).toBeInTheDocument();
  });

  it("explains a 503 with its Retry-After", async () => {
    m.submitRun.mockRejectedValue(new api.ApiError(503, "research is at capacity", 10));
    renderDesk();
    await screen.findByLabelText("Researching as");
    await userEvent.type(screen.getByLabelText("Brief"), "BYD");
    await userEvent.click(screen.getByRole("button", { name: "Start research" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Try again in about 10 seconds.");
    expect(m.submitRun).toHaveBeenCalledTimes(1);
  });

  it("warns that a Run may exist after a network failure, without retrying", async () => {
    m.submitRun.mockRejectedValue(new api.ApiError(0, "unreachable"));
    renderDesk();
    await screen.findByLabelText("Researching as");
    await userEvent.type(screen.getByLabelText("Brief"), "BYD");
    await userEvent.click(screen.getByRole("button", { name: "Start research" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("may or may not have started");
    expect(m.submitRun).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText("Brief")).toHaveValue("BYD");
  });
});
