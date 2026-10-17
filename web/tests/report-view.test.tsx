import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "@/lib/api";
import { ReportView } from "@/components/report-view";
import { citations, reportOut } from "./fixtures";

vi.mock("@/lib/api", async (orig) => (await import("./mock-api")).mockApi(orig));
const m = vi.mocked(api);

beforeEach(() => {
  m.getReport.mockResolvedValue(reportOut());
  m.getCitations.mockResolvedValue(citations());
});

describe("Report viewer", () => {
  it("renders the fact-checked draft with its [UNVERIFIED] mark", async () => {
    render(<ReportView reportId="rep_1" />);
    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent("BYD and Tesla in the 2025 EV market");
    const unverified = document.querySelector('[data-claim="0:1"]')!;
    expect(unverified).toHaveTextContent("Tesla delivered 1.79 million vehicles in 2024.[UNVERIFIED]");
    expect(screen.getByText("[UNVERIFIED]")).toHaveClass("claim__unverified");
    expect(screen.getByRole("heading", { name: "Gaps & limitations" })).toBeInTheDocument();
    expect(screen.getByText("No Q4 2025 delivery figures were available.")).toBeInTheDocument();
  });

  it("sets Historical Claims apart as background with their original date", async () => {
    render(<ReportView reportId="rep_1" />);
    const historical = await screen.findByText(/Background from an earlier Report of 30 Jun 2029/);
    expect(historical.closest("p")).toHaveClass("claim--historical");
  });

  it("maps print Source numbers to Claim citation markers without Passage text", async () => {
    render(<ReportView reportId="rep_1" />);
    await screen.findByRole("button", { name: "View source for claim 3" });
    const appendix = document.querySelector(".report__print-sources") as HTMLElement;
    expect(appendix).toBeInTheDocument();
    const entries = within(appendix).getAllByRole("listitem");
    expect(entries).toHaveLength(3);
    expect(entries[0]).toHaveTextContent("Source 1: BYD annual report");
    expect(entries[1]).toHaveTextContent("Source 2: Tesla Q4 deliveries");
    expect(entries[2]).toHaveTextContent("Source 3: Seagull price list");
    expect(entries[2]).toHaveTextContent("Page 4");
    expect(entries[2]).toHaveTextContent("Section: Pricing");
    expect(appendix).not.toHaveTextContent("Seagull now starts at 69,800 yuan.");
  });

  it("gives every Claim one accessible citation marker", async () => {
    render(<ReportView reportId="rep_1" />);
    const markers = await screen.findAllByRole("button", { name: /View source for claim \d/ });
    expect(markers.map((b) => b.textContent)).toEqual(["1", "2", "3"]);
  });

  it("opens the drawer with the citation's exact Source, date, credibility and Passage", async () => {
    render(<ReportView reportId="rep_1" />);
    await userEvent.click(await screen.findByRole("button", { name: "View source for claim 3" }));
    const drawer = screen.getByRole("dialog", { name: "Source for claim 3" });
    expect(within(drawer).getByTestId("citation-source")).toHaveTextContent("Seagull price list");
    expect(within(drawer).getByTestId("citation-published")).toHaveTextContent("1 May 2023");
    expect(within(drawer).getByTestId("citation-credibility")).toHaveTextContent("0.40 of 1");
    expect(within(drawer).getByTestId("citation-passage")).toHaveTextContent("Seagull now starts at 69,800 yuan.");
    expect(within(drawer).getByText("4")).toBeInTheDocument(); // page
    expect(within(drawer).getByText("Pricing", { selector: "dd" })).toBeInTheDocument();
  });

  it("says when a source gives no publication date instead of inventing one", async () => {
    render(<ReportView reportId="rep_1" />);
    await userEvent.click(await screen.findByRole("button", { name: "View source for claim 2" }));
    expect(screen.getByTestId("citation-published")).toHaveTextContent("Not stated by the source");
  });

  it("steps between claims and returns focus to the marker on close", async () => {
    render(<ReportView reportId="rep_1" />);
    const first = await screen.findByRole("button", { name: "View source for claim 1" });
    await userEvent.click(first);
    await userEvent.click(screen.getByRole("button", { name: "Next claim" }));
    expect(screen.getByRole("dialog", { name: "Source for claim 2" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Close source" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "View source for claim 2" })).toHaveFocus();
  });

  it("keeps the Report readable when citations fail with 500, and invents no evidence", async () => {
    m.getCitations.mockRejectedValue(new api.ApiError(500, "report rep_1 has inconsistent provenance"));
    render(<ReportView reportId="rep_1" />);
    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent("BYD and Tesla");
    expect(await screen.findByTestId("citations-unavailable")).toHaveTextContent(
      "Supporting evidence is unavailable because this Report's stored citation provenance could not be resolved.");
    const markers = screen.getAllByRole("button", { name: /Source unavailable for claim/ });
    expect(markers).toHaveLength(3);
    markers.forEach((b) => expect(b).toBeDisabled());
  });

  it("shows not found for an unknown or another persona's Report", async () => {
    m.getReport.mockRejectedValue(new api.ApiError(404, "report rep_x not found"));
    render(<ReportView reportId="rep_x" />);
    expect(await screen.findByRole("heading", { name: "Report not found" })).toBeInTheDocument();
  });
});
