import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "@/lib/api";
import { ReportView } from "@/components/report-view";
import { SharedReportView } from "@/components/shared-report-view";
import { sectionAnchors } from "@/lib/toc";
import { citations, reportOut } from "./fixtures";

// T20 B6: the Report's table of contents on the owner's Report and on a public share. Anchors
// are unique and stable even when headings repeat; the contents never print.

vi.mock("@/lib/api", async (orig) => (await import("./mock-api")).mockApi(orig));
const m = vi.mocked(api);

describe("sectionAnchors", () => {
  it("is stable, readable and unique even for repeated or empty headings", () => {
    const headings = ["Sales", "Pricing & margins", "Sales", "Sales", "", "Sales 2", "  ", "Ünïcode Überblick"];
    const anchors = sectionAnchors(headings);
    expect(anchors).toEqual([
      "section-sales", "section-pricing-margins", "section-sales-2", "section-sales-3",
      "section-5", "section-sales-2-2", "section-7", "section-unicode-uberblick",
    ]);
    expect(new Set(anchors).size).toBe(anchors.length);
    expect(sectionAnchors(headings)).toEqual(anchors); // the same Report, the same anchors
  });

  it("never collides with the fixed anchors", () => {
    const anchors = sectionAnchors(["Executive Summary", "Gaps & limitations", "Web Sources"]);
    for (const fixed of ["executive-summary", "gaps-and-limitations", "web-sources"]) {
      expect(anchors).not.toContain(fixed);
    }
  });
});

function contents() {
  return screen.getByRole("navigation", { name: "Report contents" });
}
const links = () => within(contents()).getAllByRole("link");

describe("the owner's Report", () => {
  beforeEach(() => {
    m.listShares.mockResolvedValue([]);
    m.getCitations.mockResolvedValue(citations());
  });

  it("links the Executive Summary, every rendered section and Gaps & limitations", async () => {
    const out = reportOut();
    m.getReport.mockResolvedValue(out);
    render(<ReportView reportId="rep_1" />);
    await screen.findByRole("heading", { level: 1 });
    const headings = screen.getAllByRole("heading", { level: 2 }).filter((h) => h.closest("article.report") && !h.closest(".report__print-sources"));
    expect(links().map((a) => a.textContent)).toEqual(headings.map((h) => h.textContent));
    for (const a of links()) {
      const id = a.getAttribute("href")!.slice(1);
      const target = document.getElementById(id);
      expect(target, id).not.toBeNull();
      expect(target!.tagName).toBe("H2");
    }
    expect(links()[0]).toHaveAttribute("href", "#executive-summary");
    expect(links().at(-1)).toHaveAttribute("href", "#gaps-and-limitations");
  });

  it("gives repeated section headings their own anchors", async () => {
    const out = reportOut();
    const draft = (out.body as { verified: { draft: { sections: { heading: string }[] } } }).verified.draft;
    draft.sections.forEach((s) => { s.heading = "Market"; });
    m.getReport.mockResolvedValue(out);
    render(<ReportView reportId="rep_1" />);
    await screen.findByRole("heading", { level: 1 });
    const hrefs = links().map((a) => a.getAttribute("href"));
    expect(new Set(hrefs).size).toBe(hrefs.length);
    expect(hrefs).toContain("#section-market");
    expect(hrefs).toContain("#section-market-2");
  });

  it("moves keyboard focus to the chosen section", async () => {
    m.getReport.mockResolvedValue(reportOut());
    render(<ReportView reportId="rep_1" />);
    await screen.findByRole("heading", { level: 1 });
    const target = links()[1];
    target.focus();
    await userEvent.keyboard("{Enter}");
    const heading = document.getElementById(target.getAttribute("href")!.slice(1))!;
    expect(heading).toHaveFocus();
    expect(heading).toHaveAttribute("tabindex", "-1");
    expect(window.location.hash).toBe(target.getAttribute("href"));
  });

  it("is kept out of print, and the print Sources are unchanged", async () => {
    m.getReport.mockResolvedValue(reportOut());
    render(<ReportView reportId="rep_1" />);
    await screen.findByRole("heading", { level: 1 });
    expect(contents()).toHaveClass("report-toc");
    expect(contents().closest("article")).toBeNull(); // not part of the printed Report
    expect(document.querySelector("article.report .report__print-sources")).not.toBeNull();
  });

  it("has no Executive Summary or Gaps entry when the Report has none", async () => {
    const out = reportOut();
    const body = out.body as { verified: { draft: { summary: string; gaps: string[] } } };
    body.verified.draft.summary = "";
    body.verified.draft.gaps = [];
    m.getReport.mockResolvedValue(out);
    render(<ReportView reportId="rep_1" />);
    await screen.findByRole("heading", { level: 1 });
    const hrefs = links().map((a) => a.getAttribute("href"));
    expect(hrefs).not.toContain("#executive-summary");
    expect(hrefs).not.toContain("#gaps-and-limitations");
  });
});

describe("the public share", () => {
  it("links its sections, Gaps & limitations and Web Sources", async () => {
    m.getSharedReport.mockResolvedValue({
      id: "rep_2", created_at: "2030-03-01T10:00:00Z", brief: "Tesla pricing",
      title: "Pricing update", summary: "Price fell.", gaps: ["Tax data unavailable."],
      fact_check: { sampled: 1, verified: 1, unverified: 0, unchecked: 0 },
      sections: [
        { heading: "Pricing", confidence: "MEDIUM", claims: [{ text: "Model 3 costs 29,000 dollars.", historical: false }] },
        { heading: "Pricing", confidence: "LOW", claims: [{ text: "A second pricing view.", historical: false }] },
      ],
      web_sources: [{ id: "src_1", title: "Price page", url: "https://example.test/price", credibility_score: 0.8 }],
    });
    render(<SharedReportView token="token_1" />);
    await screen.findByRole("heading", { name: "Pricing update" });
    expect(links().map((a) => [a.textContent, a.getAttribute("href")])).toEqual([
      ["Executive Summary", "#executive-summary"],
      ["Pricing", "#section-pricing"],
      ["Pricing", "#section-pricing-2"],
      ["Gaps & limitations", "#gaps-and-limitations"],
      ["Web Sources", "#web-sources"],
    ]);
    for (const a of links()) expect(document.getElementById(a.getAttribute("href")!.slice(1))).not.toBeNull();
    expect(contents().closest("article")).toBeNull();
  });
});
