import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { USERS, resetDemo, runScenario, signIn } from "./helpers";

function watchBrowser(page: Page) {
  const problems: string[] = [];
  page.on("console", (msg) => { if (msg.type() === "error") problems.push(`console: ${msg.text()}`); });
  page.on("pageerror", (err) => problems.push(`pageerror: ${err.message}`));
  page.on("requestfailed", (req) => problems.push(`requestfailed: ${req.url()} ${req.failure()?.errorText}`));
  page.on("response", (res) => { if (res.status() >= 500) problems.push(`HTTP ${res.status()}: ${res.url()}`); });
  return problems;
}

test.beforeEach(async ({ page }) => {
  await resetDemo(page);
});

test("no console errors or failed requests across a full flow", async ({ page }) => {
  const problems = watchBrowser(page);
  await signIn(page, USERS.alice);
  for (const id of ["order_summary", "select_star", "cross_tenant_explicit", "write_attempt", "python_via_mcp", "direct_db_protected"]) {
    await runScenario(page, id);
  }
  await page.getByTestId("reset").click();
  await expect(page.getByTestId("empty")).toBeVisible();
  // 4xx responses are expected (denials are rendered); 5xx, failures and JS errors are not.
  expect(problems).toEqual([]);
});

test("loading state is shown while a run is in flight", async ({ page }) => {
  await signIn(page, USERS.alice);
  // Delay (not replace) the real backend call so the loading state is observable.
  await page.route("**/api/run", async (route) => {
    await new Promise((r) => setTimeout(r, 1200));
    await route.continue();
  });
  await page.getByTestId("run-order_summary").click();
  await expect(page.getByTestId("loading")).toBeVisible();
  await expect(page.getByTestId("run-view")).toHaveAttribute("aria-busy", "true");
  await expect(page.getByTestId("run-select_star")).toBeDisabled();
  await expect(page.getByTestId("outcome")).toBeVisible();
  await expect(page.getByTestId("run-select_star")).toBeEnabled();
});

test("keyboard-only: sign in, run a scenario, switch tabs", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByTestId("scenario-list").locator("li").first()).toBeVisible();
  if (await page.getByTestId("logout").isVisible()) await page.getByTestId("logout").click();
  await page.getByTestId("identity-select").focus();
  await page.keyboard.press("Tab");
  await expect(page.getByTestId("login")).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("whoami")).toBeVisible();
  await expect(page.getByTestId("logout")).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByTestId("run-order_summary")).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("outcome")).toHaveAttribute("data-outcome", "allowed");
  await page.getByRole("tab", { name: "Ask the agent" }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("tab", { name: "SQL via MCP" })).toBeFocused();
  await expect(page.getByRole("tab", { name: "SQL via MCP" })).toHaveAttribute("aria-selected", "true");
});

test("focus is visible on interactive elements", async ({ page }) => {
  await page.goto("/");
  await page.getByTestId("identity-select").focus();
  await page.keyboard.press("Tab");
  const outline = await page.getByTestId("login").evaluate((el) => getComputedStyle(el).outlineStyle);
  expect(outline).not.toBe("none");
});

test("no serious accessibility violations (axe) after a run", async ({ page }) => {
  await signIn(page, USERS.alice);
  await runScenario(page, "select_star");
  const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  const serious = results.violations.filter((v) => ["serious", "critical"].includes(v.impact ?? ""));
  expect(serious.map((v) => `${v.id}: ${v.nodes.length} node(s)`)).toEqual([]);
});

test("desktop layout uses two columns and no horizontal scroll", async ({ page }) => {
  await signIn(page, USERS.alice);
  await runScenario(page, "select_star");
  const sidebar = await page.locator(".sidebar").boundingBox();
  const results = await page.locator(".results").boundingBox();
  expect(sidebar && results && results.x > sidebar.x + sidebar.width - 1).toBeTruthy();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});

test("mobile viewport: single column, no horizontal scroll, usable controls @mobile", async ({ page }) => {
  await signIn(page, USERS.nina);
  await runScenario(page, "select_star");
  await expect(page.getByTestId("outcome")).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  const sidebar = await page.locator(".sidebar").boundingBox();
  const results = await page.locator(".results").boundingBox();
  expect(sidebar && results && results.y >= sidebar.y + sidebar.height - 1).toBeTruthy();
  const button = await page.getByTestId("run-order_summary").boundingBox();
  expect(button && button.height >= 32).toBeTruthy();
});
