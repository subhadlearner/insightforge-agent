import { describe, expect, it } from "vitest";
import { formatCountdown, formatDate, formatEstimate } from "@/lib/format";

describe("format", () => {
  it("words the estimate as an approximation", () => {
    expect(formatEstimate(135)).toBe("about 2 min 15 sec");
    expect(formatEstimate(120)).toBe("about 2 min");
    expect(formatEstimate(45)).toBe("about 45 sec");
  });

  it("counts down to the deadline and stops at it", () => {
    const now = Date.parse("2030-01-01T09:00:00Z");
    expect(formatCountdown("2030-01-01T09:29:41Z", now)).toBe("29:41");
    expect(formatCountdown("2030-01-01T10:05:09Z", now)).toBe("1:05:09");
    expect(formatCountdown("2030-01-01T09:00:00Z", now)).toBeNull();
  });

  it("formats dates and calendar days without shifting them", () => {
    expect(formatDate("2025-03-25T00:00:00Z")).toBe("25 Mar 2025");
    expect(formatDate("2029-06-30")).toBe("30 Jun 2029");
    expect(formatDate(null)).toBeNull();
    expect(formatDate("not a date")).toBeNull();
  });
});
