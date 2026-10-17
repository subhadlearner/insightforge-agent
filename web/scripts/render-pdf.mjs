import { chromium } from "@playwright/test";

let input = "";
for await (const chunk of process.stdin) input += chunk;
const { url, cookieName, cookieValue } = JSON.parse(input);

const target = new URL(url);
const browser = await chromium.launch({ headless: true });
try {
  const context = await browser.newContext();
  await context.addCookies([{
    name: cookieName,
    value: cookieValue,
    url: target.origin,
    httpOnly: true,
    sameSite: "Lax",
  }]);
  const page = await context.newPage();
  page.setDefaultTimeout(30_000);
  const response = await page.goto(url, { waitUntil: "networkidle" });
  if (!response || !response.ok()) {
    throw new Error(`Report page returned ${response?.status() ?? "no response"}`);
  }
  await page.locator("article.report").waitFor({ state: "visible" });
  const pdf = await page.pdf({
    format: "A4",
    printBackground: true,
    margin: { top: "14mm", right: "14mm", bottom: "14mm", left: "14mm" },
  });
  process.stdout.write(pdf);
} finally {
  await browser.close();
}
