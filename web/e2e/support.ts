import { expect, type APIRequestContext, type Page } from "@playwright/test";

export const WEB_PORT = 3200;
export const API_PORT = 8200;
export const API_ORIGIN = `http://127.0.0.1:${API_PORT}`;

// Briefs the scripted Planner understands (tests/api/support.py): a concrete one it plans
// straight away, and the Vague one it asks its clarifying question about.
export const BRIEF = "BYD battery electric vehicle sales 2025";
export const VAGUE = "Tell me about the market.";
export const QUESTION = "Which market, region and time period should the research cover?";

/** The test-only controls of the backend (never part of the app). */
export async function control(request: APIRequestContext, path: string) {
  const response = await request.post(`${API_ORIGIN}/__test__/${path}`);
  expect(response.ok(), `control ${path}`).toBeTruthy();
  return response;
}

export async function choosePersona(page: Page, persona = "alice") {
  await page.goto("/");
  const select = page.getByLabel("Researching as");
  await expect(select).toBeVisible();
  const selected = page.waitForResponse((r) => r.url().endsWith("/api/session") && r.request().method() === "POST");
  await select.selectOption(persona);
  expect((await selected).status()).toBe(200);
}

/** Submits a Brief from the start page with the given sources; returns the Run ID. */
export async function submitBrief(page: Page, brief: string, sources: Record<string, boolean> = { web: true, documents: false, memory: false }) {
  await page.getByLabel("Brief", { exact: true }).fill(brief);
  for (const [name, on] of Object.entries(sources)) {
    const label = { web: /^Web/, documents: /^Your documents/, memory: /^Earlier Reports/ }[name]!;
    await page.getByRole("checkbox", { name: label }).setChecked(on);
  }
  await expect(page.getByTestId("estimate-value")).toBeVisible();
  await page.getByRole("button", { name: "Start research" }).click();
  await page.waitForURL(/\/runs\/run_/);
  return decodeURIComponent(new URL(page.url()).pathname.split("/").pop()!);
}

export function rail(page: Page) {
  return page.getByRole("list", { name: "Run progress" });
}

export async function expectCurrentStep(page: Page, name: string) {
  await expect(rail(page).locator('[aria-current="step"]')).toContainText(name);
}
