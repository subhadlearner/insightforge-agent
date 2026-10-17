import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "@/lib/api";
import { AlertsView } from "@/components/alerts-view";
import { ReportActions } from "@/components/report-actions";
import { SharedReportView } from "@/components/shared-report-view";
import { WatchlistDashboard } from "@/components/watchlist-dashboard";
import { WatchlistDiffView } from "@/components/watchlist-diff-view";
vi.mock("@/lib/api", async (orig) => (await import("./mock-api")).mockApi(orig));
const m = vi.mocked(api);

const finding: api.Finding = {
  key: "model3|price|china", subject: "Model 3", predicate: "price",
  object: "29,000 dollars", object_key: "29000 dollars",
  polarity: "affirmed", scope: "China",
  as_of_period: "2030-03-01", derived_from: "observation_date",
  conflicting: false, resolution: "vocabulary",
  provenance: {
    run_id: "run_2", report_id: "rep_2", evidence_id: "ev_2", claim: "x", statement: "x",
    primary: { observation_id: "obs_2", index: 0 },
    supporting: [{ observation_id: "obs_2", index: 0 }],
    source_ids: ["src_2"], observation_dates: { obs_2: "2030-03-01T09:00:00Z" },
  },
};

const dashboard: api.WatchlistDashboard = {
  item: {
    brief: "Tesla pricing", cadence_minutes: 15, max_runs: 4,
    source_toggles: { web: true }, id: "wl_1", owner_id: "alice",
    created_at: "2030-03-01T09:00:00Z", revision: 1, next_tick_at: "2030-03-01T09:15:00Z",
  },
  runs_started: 3, completed_runs: 2, failed_runs: 1,
  recent_failed_runs: [{
    id: "run_bad", owner_id: "alice", brief: "Tesla pricing", state: "FAILED",
    created_at: "2030-03-01T09:30:00Z", watchlist_item_id: "wl_1",
  }],
  recent_failures: [{
    owner_id: "alice", run_id: "run_bad",
    run_created_at: "2030-03-01T09:30:00Z", failed_at: "2030-03-01T09:31:00Z",
    retention_starts_at: "2030-03-01T09:31:00Z",
    stage: "RESEARCHING", reason: "provider unavailable",
    error_codes: ["all_search_providers_failed"], backfilled: false,
  }],
  unconfirmed_findings: [{ finding: { ...finding, key: "unconfirmed-feature", predicate: "feature", object: "Autopilot", polarity: "negated" }, not_reconfirmed: 3, unconfirmed: true }],
  baseline_state: "established",
};

beforeEach(() => {
  m.listWatchlists.mockResolvedValue([dashboard]);
  m.getWatchlist.mockResolvedValue(dashboard);
  m.listDiffs.mockResolvedValue([{
    owner_id: "alice", watchlist_item_id: "wl_1", run_id: "run_2", report_id: "rep_2",
    status: "succeeded", attempts: 1, error: "", alert_id: "al_2",
    updated_at: "2030-03-01T10:00:00Z", skipped: [], intervening_failed: [],
    entries: [{
      material: true,
      entry: {
        outcome: "changed", key: finding.key, finding,
        previous: { ...finding, object: "32,000 dollars", provenance: { ...finding.provenance, report_id: "rep_1" } },
        unconfirmed: false, reason: "",
      },
    }],
  }]);
  m.listAlerts.mockResolvedValue([{
    id: "al_2", owner_id: "alice", watchlist_item_id: "wl_1", run_id: "run_2", report_id: "rep_2",
    brief: "Tesla pricing", created_at: "2030-03-01T10:00:00Z", read: false, failed_diffs: [],
    changes: [{
      outcome: "changed", subject: "Model 3", predicate: "price", scope: "China",
      object: "29,000 dollars", polarity: "affirmed", as_of_period: "2030-03-01",
      derived: true, when: "Change detected on 2030-03-01", report_id: "rep_2",
      evidence_id: "ev_2", previous_object: "32,000 dollars", previous_report_id: "rep_1",
    }],
  }]);
});

describe("T15 Watchlist and Diff UI", () => {
  it("shows N of N, failure detail and unconfirmed Findings", async () => {
    render(<WatchlistDashboard />);
    expect(await screen.findByText("3 of 4 runs started", { exact: false })).toBeInTheDocument();
    expect(screen.getByText(/RESEARCHING: provider unavailable/)).toBeInTheDocument();
    expect(screen.getByText(/3 runs without reconfirmation/)).toBeInTheDocument();
    expect(screen.getByText(/Negated: Model 3.*Autopilot/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View Diff history" })).toHaveAttribute("href", "/watchlist/wl_1");
  });

  it("uses explicit materiality and honest derived-period wording", async () => {
    render(<WatchlistDiffView itemId="wl_1" />);
    expect(await screen.findByText(/changed · material change/i)).toBeInTheDocument();
    expect(screen.getAllByText("As of 2030-03-01 (derived from source observation date)")).toHaveLength(2);
    expect(screen.queryByText(/Changed on 2030-03-01/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Change detected on 2030-03-01/)).not.toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "Report" })[1]).toHaveAttribute("href", "/reports/rep_1");
    expect(screen.getAllByRole("link", { name: "Report" })[2]).toHaveAttribute("href", "/reports/rep_2");
    expect(screen.getByText("Previously established provenance:")).toBeInTheDocument();
    expect(screen.getByText("This Run provenance:")).toBeInTheDocument();
  });
});

describe("T15 Alerts", () => {
  it("links current and previous establishing Reports and marks read", async () => {
    render(<AlertsView />);
    expect(await screen.findByText(/Change detected on 2030-03-01/)).toBeInTheDocument();
    expect(screen.getByText(/This Run — Affirmed: Model 3/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open new Report" })).toHaveAttribute("href", "/reports/rep_2");
    expect(screen.getByRole("link", { name: "Previous establishing Report" })).toHaveAttribute("href", "/reports/rep_1");
  });
});

describe("T15 Report actions", () => {
  it("creates and revokes one public link without retrying mutations", async () => {
    m.createShare.mockResolvedValue({
      share_id: "share_1", path: "/share/token_1", expires_at: "2030-03-08T09:00:00Z",
    });
    m.revokeShare.mockResolvedValue(undefined);
    render(<ReportActions reportId="rep_2" />);
    await userEvent.click(screen.getByRole("button", { name: "Create share link" }));
    expect(await screen.findByText(/Public link:/)).toBeInTheDocument();
    expect(m.createShare).toHaveBeenCalledTimes(1);
    await userEvent.click(screen.getByRole("button", { name: "Revoke share link" }));
    expect(await screen.findByText(/can no longer be opened/)).toBeInTheDocument();
    expect(m.revokeShare).toHaveBeenCalledTimes(1);
  });
});

describe("T15 public share", () => {
  it("renders the constrained public Report and web Source metadata", async () => {
    m.getSharedReport.mockResolvedValue({
      id: "rep_2", created_at: "2030-03-01T10:00:00Z", brief: "Tesla pricing",
      title: "Pricing update", summary: "Price fell.", gaps: ["Tax data unavailable."],
      fact_check: { sampled: 1, verified: 1, unverified: 0, unchecked: 0 },
      sections: [{ heading: "Pricing", claims: [{ text: "Model 3 costs 29,000 dollars.", historical: false }] }],
      web_sources: [{ id: "src_1", title: "Price page", url: "https://example.test/price", credibility_score: 0.8 }],
    });
    render(<SharedReportView token="token_1" />);
    expect(await screen.findByRole("heading", { name: "Pricing update" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Price page" })).toHaveAttribute("href", "https://example.test/price");
    expect(screen.queryByText(/Run log/i)).not.toBeInTheDocument();
  });
});

describe("T15 persisted Diff semantic outcomes", () => {
  async function show(entry: api.DiffRecord["entries"][number]["entry"], material = false) {
    m.listDiffs.mockResolvedValue([{
      owner_id: "alice", watchlist_item_id: "wl_1", run_id: "run_2", report_id: "rep_2",
      status: "succeeded", attempts: 1, error: "", alert_id: null,
      updated_at: "2030-03-01T10:00:00Z", skipped: [], intervening_failed: [],
      entries: [{ entry, material }],
    }]);
    render(<WatchlistDiffView itemId="wl_1" />);
    await screen.findByText(/run run_2/i);
  }

  it("keeps affirmed and negated sides explicit for no_longer_true", async () => {
    await show({
      outcome: "no_longer_true", key: finding.key,
      previous: { ...finding, polarity: "affirmed" },
      finding: { ...finding, polarity: "negated" }, unconfirmed: false, reason: "",
    }, true);
    expect(screen.getByText(/no longer true · material change/i)).toBeInTheDocument();
    expect(screen.getByText(/Previously established:/).parentElement).toHaveTextContent("Affirmed: Model 3");
    expect(screen.getByText(/This Run:/).parentElement).toHaveTextContent("Negated: Model 3");
  });

  it("shows first-time negated added without a material-change label", async () => {
    await show({
      outcome: "added", key: finding.key, previous: null,
      finding: { ...finding, polarity: "negated" }, unconfirmed: false, reason: "",
    });
    expect(screen.getByRole("heading", { name: "added" })).toBeInTheDocument();
    expect(screen.getByText(/This Run:/).parentElement).toHaveTextContent("Negated: Model 3");
    expect(screen.queryByText(/material change/)).not.toBeInTheDocument();
  });

  for (const outcome of ["older_ignored", "ambiguous"] as const) {
    it(`labels ${outcome} incoming Finding as This Run, without replacing established state`, async () => {
      await show({
        outcome, key: finding.key, previous: { ...finding, object: "120 dollars" },
        finding: { ...finding, object: "90 dollars" },
        unconfirmed: false, reason: "older or conflicting evidence",
      });
      expect(screen.getByText(/Previously established:/).parentElement).toHaveTextContent("120 dollars");
      expect(screen.getByText(/This Run:/).parentElement).toHaveTextContent("90 dollars");
      expect(screen.queryByText(/Current provenance:/)).not.toBeInTheDocument();
      expect(screen.getByText("older or conflicting evidence")).toBeInTheDocument();
    });
  }

  it("does not fabricate a Finding when not reconfirmed", async () => {
    await show({
      outcome: "not_reconfirmed", key: finding.key, previous: finding,
      finding: null, unconfirmed: true, reason: "",
    });
    expect(screen.getByText(/Previously established:/).parentElement).toHaveTextContent("Affirmed: Model 3");
    expect(screen.getByText(/This Run:/).parentElement).toHaveTextContent("no corresponding Finding was produced");
    expect(screen.getByText(/absence is not deletion/)).toBeInTheDocument();
  });
});
