// Shared test data shaped like the real API responses.
import type { Citation, ReportOut } from "@/lib/api";

export function reportOut(overrides: Partial<Record<string, unknown>> = {}): ReportOut {
  const verifiedDraft = {
    title: "BYD and Tesla in the 2025 EV market",
    summary: "BYD outsold Tesla in battery EVs for the first time.",
    sections: [
      {
        heading: "Sales",
        claims: [
          { text: "BYD sold 1.76 million battery EVs in 2024.", evidence_id: "ev_a", historical: false },
          { text: "Tesla delivered 1.79 million vehicles in 2024. [UNVERIFIED]", evidence_id: "ev_b", historical: false },
        ],
      },
      {
        heading: "Pricing",
        claims: [
          { text: "BYD cut Seagull prices by 5% in 2023.", evidence_id: "hist_r0", historical: true },
        ],
      },
    ],
    gaps: ["No Q4 2025 delivery figures were available."],
  };
  const rawDraft = {
    ...verifiedDraft,
    sections: verifiedDraft.sections.map((s) => ({
      ...s,
      claims: s.claims.map((c) => ({ ...c, text: c.text.replace(" [UNVERIFIED]", "") })),
    })),
  };
  return {
    id: "rep_1",
    run_id: "run_1",
    created_at: "2030-03-01T09:00:00Z",
    body: {
      brief: "How did BYD and Tesla compete in 2025?",
      draft: rawDraft,
      verified: {
        draft: verifiedDraft,
        verdicts: [],
        summary: { claims: 3, sampled: 3, verified: 1, unverified: 1, unchecked: 1, pass_rate: 0.5 },
      },
      historical: {
        items: [{ id: "hist_r0", report_id: "rep_0", report_date: "2029-06-30", statement: "x" }],
      },
      ...overrides,
    },
  };
}

export function citations(): Citation[] {
  return [
    citation(0, 0, "ev_a", "BYD annual report", "BYD sold 1,764,992 battery EVs in 2024.", 0.82, "2025-03-25T00:00:00Z"),
    citation(0, 1, "ev_b", "Tesla Q4 deliveries", "Tesla delivered 1,789,226 vehicles.", 0.71, null),
    { ...citation(1, 0, "hist_r0", "Seagull price list", "Seagull now starts at 69,800 yuan.", 0.4, "2023-05-01T00:00:00Z"), page: 4, section_heading: "Pricing" },
  ];
}

function citation(section: number, claim: number, evidence_id: string, source_title: string,
  passage_text: string, credibility_score: number | null, published_at: string | null): Citation {
  return {
    section, claim, evidence_id, source_id: `src_${evidence_id}`, source_title, published_at,
    credibility_score, passage: { observation_id: `obs_${evidence_id}`, index: claim },
    passage_text, page: null, section_heading: null,
  };
}
