// The lib/api functions component tests replace (the real ApiError is kept). Each test file
// calls `vi.mock("@/lib/api", mockApi)`.
export async function mockApi(importOriginal: () => Promise<unknown>) {
  const { vi } = await import("vitest");
  const real = (await importOriginal()) as typeof import("@/lib/api");
  return {
    ...real,
    getPersonas: vi.fn(), getSession: vi.fn(), selectPersona: vi.fn(),
    estimateRuntime: vi.fn(), submitRun: vi.fn(),
    getRunTiming: vi.fn(() => new Promise<never>(() => {})), // pending unless a test answers it
    getClarification: vi.fn(), answerClarification: vi.fn(),
    listReports: vi.fn(), getReport: vi.fn(), getCitations: vi.fn(),
    listWatchlists: vi.fn(), getWatchlist: vi.fn(), listDiffs: vi.fn(),
    listAlerts: vi.fn(), markAlertRead: vi.fn(),
    downloadReport: vi.fn(), listShares: vi.fn(), createShare: vi.fn(), revokeShare: vi.fn(),
    getSharedReport: vi.fn(),
  };
}
