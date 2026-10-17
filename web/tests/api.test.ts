import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, estimateRuntime, getSession, submitRun } from "@/lib/api";

function respond(status: number, body: unknown, headers: Record<string, string> = {}) {
  return vi.fn().mockResolvedValue(new Response(body === undefined ? null : JSON.stringify(body), {
    status, headers: { "content-type": "application/json", ...headers },
  }));
}

afterEach(() => vi.unstubAllGlobals());

describe("api", () => {
  it("calls the same-origin /api prefix with the cookie", async () => {
    const fetch = respond(200, { estimated_seconds: 135, base_seconds: 30, document_count: 2, source_types: ["web"] });
    vi.stubGlobal("fetch", fetch);
    await estimateRuntime({ web: true, documents: true, memory: false }, 2);
    const [url, init] = fetch.mock.calls[0];
    expect(url).toBe("/api/research/estimate");
    expect(init.credentials).toBe("same-origin");
    expect(JSON.parse(init.body)).toEqual({ source_toggles: { web: true, documents: true, memory: false }, document_count: 2 });
  });

  it("reads a 401 session as no persona selected", async () => {
    vi.stubGlobal("fetch", respond(401, { detail: "select a persona first" }));
    expect(await getSession()).toBeNull();
  });

  it("turns a FastAPI string detail into the error message", async () => {
    vi.stubGlobal("fetch", respond(422, { detail: "no source type is available" }));
    await expect(submitRun("b", { web: true }, [])).rejects.toMatchObject({
      status: 422, message: "no source type is available",
    });
  });

  it("joins FastAPI validation errors into one message", async () => {
    vi.stubGlobal("fetch", respond(422, { detail: [{ loc: ["body", "brief"], msg: "Field required" }] }));
    await expect(submitRun("", { web: true }, [])).rejects.toMatchObject({ message: "brief: Field required" });
  });

  it("keeps Retry-After from a 503", async () => {
    vi.stubGlobal("fetch", respond(503, { detail: "research is at capacity" }, { "retry-after": "10" }));
    const error = await submitRun("b", { web: true }, []).catch((e) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error.retryAfterSeconds).toBe(10);
  });

  it("never retries a Run submission, even after a network failure", async () => {
    const fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    vi.stubGlobal("fetch", fetch);
    await expect(submitRun("b", { web: true }, [])).rejects.toMatchObject({ status: 0 });
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});
