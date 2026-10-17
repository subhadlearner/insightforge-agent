import { describe, expect, it } from "vitest";
import { attachCitations, parseReport, ReportShapeError } from "@/lib/report";
import { citations, reportOut } from "./fixtures";

describe("parseReport", () => {
  it("reads the fact-checked copy (verified.draft), never the raw draft", () => {
    const out = reportOut();
    (out.body.draft as { title: string }).title = "RAW DRAFT TITLE";
    const report = parseReport(out);
    expect(report.title).toBe("BYD and Tesla in the 2025 EV market");
    expect(report.sections[0].claims[1].text).toBe("Tesla delivered 1.79 million vehicles in 2024. [UNVERIFIED]");
  });

  it("keeps the [UNVERIFIED] mark and flags the Claim", () => {
    const [verified, unverified] = parseReport(reportOut()).sections[0].claims;
    expect(verified.unverified).toBe(false);
    expect(unverified.unverified).toBe(true);
    expect(unverified.statement).toBe("Tesla delivered 1.79 million vehicles in 2024.");
  });

  it("numbers Claims in draft order and keeps their positions", () => {
    const claims = parseReport(reportOut()).sections.flatMap((s) => s.claims);
    expect(claims.map((c) => [c.number, c.section, c.claim])).toEqual([[1, 0, 0], [2, 0, 1], [3, 1, 0]]);
  });

  it("marks Historical Claims and gives the date of the Report that first made them", () => {
    const historical = parseReport(reportOut()).sections[1].claims[0];
    expect(historical.historical).toBe(true);
    expect(historical.historicalDate).toBe("2029-06-30");
  });

  it("reads summary, gaps and the fact-check summary", () => {
    const report = parseReport(reportOut());
    expect(report.summary).toMatch(/BYD outsold Tesla/);
    expect(report.gaps).toEqual(["No Q4 2025 delivery figures were available."]);
    expect(report.factCheck).toEqual({ sampled: 3, verified: 1, unverified: 1, unchecked: 1 });
    expect(report.brief).toBe("How did BYD and Tesla compete in 2025?");
  });

  it("refuses a body without a verified draft rather than falling back to the raw draft", () => {
    const out = reportOut();
    delete (out.body as Record<string, unknown>).verified;
    expect(() => parseReport(out)).toThrow(ReportShapeError);
  });

  it("refuses malformed Claims", () => {
    const out = reportOut({ verified: { draft: { title: "t", sections: [{ heading: "h", claims: [{ text: 3 }] }] } } });
    expect(() => parseReport(out)).toThrow(ReportShapeError);
  });
});

describe("attachCitations", () => {
  it("maps each Claim to the citation at its section and Claim position", () => {
    const report = parseReport(reportOut());
    const shuffled = [...citations()].reverse();
    const byClaim = attachCitations(report, shuffled);
    const claims = report.sections.flatMap((s) => s.claims);
    expect(claims.map((c) => byClaim.get(c.key)?.source_title)).toEqual([
      "BYD annual report", "Tesla Q4 deliveries", "Seagull price list"]);
  });

  it("leaves a Claim without a citation unmapped instead of borrowing another", () => {
    const report = parseReport(reportOut());
    const byClaim = attachCitations(report, citations().slice(0, 2));
    expect(byClaim.has(report.sections[1].claims[0].key)).toBe(false);
  });

  it("does not map a citation whose evidence_id disagrees with the Claim", () => {
    const report = parseReport(reportOut());
    const wrong = citations().map((c) => (c.section === 0 && c.claim === 0 ? { ...c, evidence_id: "ev_other" } : c));
    expect(attachCitations(report, wrong).has(report.sections[0].claims[0].key)).toBe(false);
  });
});
