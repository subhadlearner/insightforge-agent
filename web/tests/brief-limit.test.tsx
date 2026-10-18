import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "@/lib/api";
import { BRIEF_MAX_CHARS, briefLength, fitBrief } from "@/lib/brief";
import { BriefForm } from "@/components/brief-form";
import { PersonaPicker } from "@/components/persona-picker";
import { PersonaProvider } from "@/components/persona-context";
import fixture from "../../tests/fixtures/briefs/one_shot_lengths.json";

vi.mock("@/lib/api", async (orig) => (await import("./mock-api")).mockApi(orig));
vi.mock("@/lib/navigation", () => ({ hardNavigate: vi.fn() }));

const m = vi.mocked(api);
const PERSONA: api.Persona = { id: "bob", display_name: "Bob Strategist", source_toggles: { web: true, documents: false, memory: true } };

// The same cases tests/api/test_t20_brief_limit.py sends to the server.
type Case = { name: string; prefix: string; unit: string; repeat: number; suffix: string; length: number };
const CASES: Case[] = fixture.cases;
const textOf = (c: Case) => c.prefix + c.unit.repeat(c.repeat) + c.suffix;
const hasLoneSurrogate = (s: string) => /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(s);

beforeEach(() => {
  m.getPersonas.mockResolvedValue([PERSONA]);
  m.getSession.mockResolvedValue(PERSONA);
  m.estimateRuntime.mockResolvedValue({ estimated_seconds: 60, base_seconds: 30, document_count: 0, source_types: ["web"] } as api.RuntimeEstimate);
});

async function renderDesk() {
  render(<PersonaProvider><PersonaPicker /><BriefForm /></PersonaProvider>);
  await screen.findByLabelText("Researching as");
  return screen.getByLabelText("Brief") as HTMLTextAreaElement;
}

const count = () => screen.getByTestId("brief-count");
const status = () => screen.getByTestId("brief-limit-status");

describe("Brief length counting", () => {
  it.each(CASES.map((c) => [c.name, c] as const))("counts %s as the server does", (_, c) => {
    expect(BRIEF_MAX_CHARS).toBe(500);
    expect(briefLength(textOf(c))).toBe(c.length);
  });

  it.each(CASES.map((c) => [c.name, c] as const))("fits %s to the limit at a code point", (_, c) => {
    const fitted = fitBrief("", textOf(c));
    expect(fitted.truncated).toBe(c.length > 500);
    expect(briefLength(fitted.value)).toBe(Math.min(c.length, 500));
    expect(textOf(c).startsWith(fitted.value)).toBe(true);
    expect(hasLoneSurrogate(fitted.value)).toBe(false);
  });

  it("cuts only the inserted text, keeping what follows a paste in the middle", () => {
    const fitted = fitBrief("ab", `a${"x".repeat(600)}b`);
    expect(fitted).toEqual({ value: `a${"x".repeat(498)}b`, caret: 499, truncated: true });
  });

  it("refuses a keystroke at the limit and keeps the caret where it was typed", () => {
    const full = "a".repeat(250) + "b".repeat(250);
    const fitted = fitBrief(full, `${"a".repeat(250)}Z${"b".repeat(250)}`);
    expect(fitted).toEqual({ value: full, caret: 250, truncated: true });
  });

  it("places the caret in UTF-16 code units after emoji", () => {
    const fitted = fitBrief("", "\u{1F697}".repeat(501));
    expect(fitted.caret).toBe(1000);
  });
});

describe("Brief form counter", () => {
  it("shows 0/500 from the start and describes the textarea with it", async () => {
    const input = await renderDesk();
    expect(count()).toHaveTextContent("0/500");
    expect(input.getAttribute("aria-describedby")!.split(" ")).toEqual(["brief-hint", "brief-count"]);
    expect(input).toHaveAccessibleDescription(/0 of 500 characters used$/);
    expect(status()).toHaveAttribute("role", "status");
    expect(status()).toBeEmptyDOMElement();
    expect(input).not.toHaveAttribute("maxlength"); // native maxLength counts UTF-16 units
  });

  it("updates while typing and editing, counting whitespace and newlines", async () => {
    const input = await renderDesk();
    await userEvent.type(input, " BYD{Enter}");
    expect(count()).toHaveTextContent("5/500");
    await userEvent.type(input, "{Backspace}{Backspace}");
    expect(count()).toHaveTextContent("3/500");
    expect(input).toHaveValue(" BY");
  });

  it("counts an emoji as one character", async () => {
    const input = await renderDesk();
    await userEvent.type(input, "EV \u{1F697}");
    expect(count()).toHaveTextContent("4/500");
  });

  it("cuts an over-long paste to 500 and announces it", async () => {
    const input = await renderDesk();
    input.focus();
    await userEvent.paste("a".repeat(620));
    expect(input.value).toBe("a".repeat(500));
    expect(count()).toHaveTextContent("500/500");
    expect(status()).toHaveTextContent("Text beyond the limit was not added.");
    expect(screen.getByRole("button", { name: "Start research" })).toBeEnabled();
  });

  it("cuts a paste into the middle without losing the text after it", async () => {
    const input = await renderDesk();
    fireEvent.change(input, { target: { value: "Start End" } });
    input.focus();
    input.setSelectionRange(6, 6);
    await userEvent.paste("x".repeat(600));
    expect(briefLength(input.value)).toBe(500);
    expect(input.value).toBe(`Start ${"x".repeat(491)}End`);
    expect(input.selectionStart).toBe(497);
  });

  it("refuses typing at the limit, then counts down again on deletion", async () => {
    const input = await renderDesk();
    input.focus();
    await userEvent.paste("b".repeat(500));
    expect(status()).toHaveTextContent("Character limit reached: 500 of 500.");
    await userEvent.type(input, "c");
    expect(input.value).toBe("b".repeat(500));
    expect(status()).toHaveTextContent("Text beyond the limit was not added.");
    await userEvent.type(input, "{Backspace}");
    expect(count()).toHaveTextContent("499/500");
    expect(status()).toHaveTextContent("50 or fewer characters left.");
    await userEvent.type(input, "d");
    expect(count()).toHaveTextContent("500/500");
    expect(status()).toHaveTextContent("Character limit reached: 500 of 500.");
  });

  it("keeps the status quiet until the last 50 characters", async () => {
    const input = await renderDesk();
    fireEvent.change(input, { target: { value: "a".repeat(449) } });
    expect(status()).toBeEmptyDOMElement();
    fireEvent.change(input, { target: { value: "a".repeat(450) } });
    expect(status()).toHaveTextContent("50 or fewer characters left.");
  });

  it.each(CASES.filter((c) => c.length <= 500).map((c) => [c.name, c] as const))(
    "submits %s, trimmed, as the server accepts it", async (_, c) => {
      m.submitRun.mockResolvedValue({ run_id: "run_1", state: "PLANNING", status_url: "/research/status?run_id=run_1" });
      const input = await renderDesk();
      fireEvent.change(input, { target: { value: textOf(c) } });
      expect(input.value).toBe(textOf(c));
      expect(count()).toHaveTextContent(`${c.length}/500`);
      await userEvent.click(screen.getByRole("button", { name: "Start research" }));
      expect(m.submitRun).toHaveBeenCalledWith(textOf(c).trim(), expect.anything(), []);
    });

  it.each(CASES.filter((c) => c.length > 500).map((c) => [c.name, c] as const))(
    "never holds or submits %s; the form state stops at 500", async (_, c) => {
      m.submitRun.mockResolvedValue({ run_id: "run_1", state: "PLANNING", status_url: "/research/status?run_id=run_1" });
      const input = await renderDesk();
      fireEvent.change(input, { target: { value: textOf(c) } }); // a programmatic change
      expect(briefLength(input.value)).toBe(500);
      fireEvent.submit(input.closest("form")!); // a programmatic submit
      await waitFor(() => expect(m.submitRun).toHaveBeenCalledTimes(1));
      const sent = m.submitRun.mock.calls[0][0];
      expect(briefLength(sent)).toBeLessThanOrEqual(500);
      expect(textOf(c).trim().startsWith(sent)).toBe(true);
    });

  it("keeps whitespace-only Briefs unsubmittable", async () => {
    const input = await renderDesk();
    fireEvent.change(input, { target: { value: " \n ".repeat(200) } });
    expect(count()).toHaveTextContent("500/500");
    expect(screen.getByRole("button", { name: "Start research" })).toBeDisabled();
    fireEvent.submit(input.closest("form")!);
    expect(m.submitRun).not.toHaveBeenCalled();
  });
});
