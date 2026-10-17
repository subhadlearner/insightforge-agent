import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "@/lib/api";
import * as navigation from "@/lib/navigation";
import { BriefForm } from "@/components/brief-form";
import { PersonaPicker } from "@/components/persona-picker";
import { PersonaProvider } from "@/components/persona-context";
import { togglesFor } from "@/components/source-toggles";

vi.mock("@/lib/api", async (orig) => (await import("./mock-api")).mockApi(orig));
vi.mock("@/lib/navigation", () => ({ hardNavigate: vi.fn() }));

const m = vi.mocked(api);
const ALICE: api.Persona = { id: "alice", display_name: "Alice Analyst", source_toggles: { web: true, documents: true, memory: true } };
const BOB: api.Persona = { id: "bob", display_name: "Bob Strategist", source_toggles: { web: true, documents: false, memory: true } };
const ESTIMATE = { estimated_seconds: 90, base_seconds: 30, document_count: 0, source_types: ["web"] } as api.RuntimeEstimate;

function pdf(name: string): File {
  return new File(["%PDF-1.7"], name, { type: "application/pdf" });
}

function renderDesk() {
  return render(<PersonaProvider><PersonaPicker /><BriefForm /></PersonaProvider>);
}

beforeEach(() => {
  m.getPersonas.mockResolvedValue([ALICE, BOB]);
  m.estimateRuntime.mockResolvedValue(ESTIMATE);
});

describe("switching persona is a hard boundary", () => {
  it("reloads at the start page after a successful switch, dropping the Brief, files and any Run", async () => {
    m.getSession.mockResolvedValue(ALICE);
    m.selectPersona.mockResolvedValue(BOB);
    renderDesk();
    await userEvent.selectOptions(await screen.findByLabelText("Researching as"), "bob");
    await waitFor(() => expect(navigation.hardNavigate).toHaveBeenCalledWith("/"));
    expect(m.selectPersona).toHaveBeenCalledWith("bob");
  });

  it("does not navigate when the switch is refused, and keeps the current persona", async () => {
    m.getSession.mockResolvedValue(ALICE);
    m.selectPersona.mockRejectedValue(new api.ApiError(422, "unknown persona 'bob'"));
    renderDesk();
    await userEvent.selectOptions(await screen.findByLabelText("Researching as"), "bob");
    expect(await screen.findByText("unknown persona 'bob'")).toBeInTheDocument();
    expect(navigation.hardNavigate).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Researching as")).toHaveValue("alice");
  });

  it("choosing a first persona keeps the page (nothing belonged to anyone yet)", async () => {
    m.getSession.mockResolvedValue(null);
    m.selectPersona.mockResolvedValue(BOB);
    renderDesk();
    await screen.findByRole("option", { name: "Choose a persona" });
    await userEvent.type(screen.getByLabelText("Brief"), "Drafted before choosing");
    await userEvent.selectOptions(screen.getByLabelText("Researching as"), "bob");
    await waitFor(() => expect(screen.getByLabelText("Researching as")).toHaveValue("bob"));
    expect(navigation.hardNavigate).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Brief")).toHaveValue("Drafted before choosing");
  });
});

describe("attached PDFs are never silently ignored", () => {
  beforeEach(() => m.getSession.mockResolvedValue(BOB)); // Bob's default has documents off

  it("attaching a PDF turns on Your documents and locks it while files remain", async () => {
    renderDesk();
    await screen.findByLabelText("Researching as");
    const documents = screen.getByRole("checkbox", { name: /Your documents/ });
    await waitFor(() => expect(documents).not.toBeChecked());

    await userEvent.upload(screen.getByLabelText("Documents"), [pdf("deck.pdf")]);
    expect(documents).toBeChecked();
    expect(documents).toBeDisabled();
    expect(screen.getByText("On while PDFs are attached. Remove them to turn this off.")).toBeInTheDocument();
    await waitFor(() => expect(m.estimateRuntime).toHaveBeenLastCalledWith(
      { web: true, documents: true, memory: true }, 1));

    await userEvent.click(screen.getByRole("button", { name: "Remove deck.pdf" }));
    expect(documents).not.toBeChecked(); // back to the User's own setting
    expect(documents).toBeEnabled();
    await waitFor(() => expect(m.estimateRuntime).toHaveBeenLastCalledWith(
      { web: true, documents: false, memory: true }, 0));
  });

  it("submits the PDFs with Your documents on", async () => {
    m.submitRun.mockReturnValue(new Promise(() => {}));
    renderDesk();
    await screen.findByLabelText("Researching as");
    await userEvent.type(screen.getByLabelText("Brief"), "What does the deck say about pricing?");
    await userEvent.upload(screen.getByLabelText("Documents"), [pdf("deck.pdf")]);
    await userEvent.click(screen.getByRole("button", { name: "Start research" }));
    await waitFor(() => expect(m.submitRun).toHaveBeenCalledTimes(1));
    const [, toggles, documents] = m.submitRun.mock.calls[0];
    expect(toggles).toEqual({ web: true, documents: true, memory: true });
    expect(documents.map((d) => d.filename)).toEqual(["deck.pdf"]);
  });
});

describe("togglesFor", () => {
  it("a persona with no defaults means web only, as the backend rule says", () => {
    expect(togglesFor({})).toEqual({ web: true, documents: false, memory: false });
    expect(togglesFor(undefined)).toEqual({ web: true, documents: false, memory: false });
  });

  it("a persona's defaults are used as given, with omitted types off", () => {
    expect(togglesFor({ memory: true })).toEqual({ web: false, documents: false, memory: true });
  });
});
