import { expect, test, type Locator, type Page } from "@playwright/test";

// Silent, caption-driven presentation recording against the live /present page.
// Follows the "Agentic AI Security" structure: intro → 7 demos (each: risk,
// prompt, unprotected vs protected probe, and live AI for two of them) → close.
const READ = Number(process.env.READ_MS ?? 4500);
const BASE = process.env.BASE_URL ?? "http://127.0.0.1:8088";

async function card(page: Page, heading: string, sub: string, ms = READ): Promise<void> {
  await page.evaluate(([h, s]) => {
    document.getElementById("rec-card")?.remove();
    const c = document.createElement("div");
    c.id = "rec-card";
    Object.assign(c.style, {
      position: "fixed", inset: "0", zIndex: "10000", display: "grid", placeContent: "center", gap: "20px",
      background: "rgba(17,24,39,.97)", color: "#fff", textAlign: "center", padding: "0 140px",
      font: "700 50px/1.2 system-ui, sans-serif", pointerEvents: "none",
    });
    const h1 = document.createElement("div"); h1.textContent = h;
    const p = document.createElement("div"); p.textContent = s;
    Object.assign(p.style, { font: "400 26px/1.5 system-ui, sans-serif", opacity: ".9" });
    c.append(h1, p); document.body.append(c);
  }, [heading, sub]);
  await page.waitForTimeout(ms);
}
async function clearCard(page: Page): Promise<void> {
  await page.evaluate(() => document.getElementById("rec-card")?.remove());
}
async function banner(page: Page, text: string): Promise<void> {
  await page.evaluate((t) => {
    let b = document.getElementById("rec-banner");
    if (!b) {
      b = document.createElement("div"); b.id = "rec-banner";
      Object.assign(b.style, {
        position: "fixed", left: "0", right: "0", bottom: "0", zIndex: "9999", padding: "14px 28px",
        background: "rgba(17,24,39,.95)", color: "#fff", font: "600 21px/1.35 system-ui, sans-serif", pointerEvents: "none",
      });
      document.body.append(b);
    }
    b.textContent = t;
  }, text);
}
async function spotlight(page: Page, el: Locator, ms = READ): Promise<void> {
  if (!(await el.count())) return;
  await el.evaluate((n: HTMLElement) => {
    n.scrollIntoView({ block: "center" });
    Object.assign(n.style, { outline: "4px solid #f59e0b", outlineOffset: "6px", borderRadius: "10px" });
  });
  await page.waitForTimeout(ms);
  await el.evaluate((n: HTMLElement) => (n.style.outline = ""));
}
const panel = (page: Page, id: string, env: string) => page.locator(`[data-panel="${id}:${env}"]`);

async function runProbe(page: Page, demo: { id: string; envs: string[] }): Promise<void> {
  await page.evaluate((id) => (window as unknown as { __runDemo: (i: string, s: string) => Promise<void> }).__runDemo(id, "probe"), demo.id);
  for (const env of demo.envs) {
    await expect(panel(page, demo.id, env).locator(".verdict").first()).toBeVisible({ timeout: 60_000 });
  }
}
async function runAgent(page: Page, id: string, envs: string[]): Promise<void> {
  await page.evaluate((i) => (window as unknown as { __runDemo: (x: string, s: string) => Promise<void> }).__runDemo(i, "agent"), id);
  for (const env of envs) {
    await expect(panel(page, id, env).locator('[data-testid="ai-result"]')).toBeVisible({ timeout: 200_000 });
  }
}

const DEMOS = [
  { id: "normal", envs: ["protected"], title: "Demo 1 · Normal access",
    sub: "Baseline: an authorized user gets correct results." },
  { id: "cross_tenant", envs: ["unprotected", "protected"], title: "Demo 2 · Cross-tenant read",
    sub: "A valid company identity must not unlock another tenant's data.", agent: true },
  { id: "viewer", envs: ["unprotected", "protected"], title: "Demo 3 · Viewer role",
    sub: "Discovering a dataset is not permission to query it." },
  { id: "billing", envs: ["unprotected", "protected"], title: "Demo 4 · Restricted billing table",
    sub: "Tenant membership does not grant access to every table." },
  { id: "write", envs: ["unprotected", "protected"], title: "Demo 5 · Update and delete",
    sub: "Unauthorized changes can be worse than disclosure." },
  { id: "injection", envs: ["unprotected", "protected"], title: "Demo 6 · Prompt injection in data",
    sub: "Retrieved data can carry instructions, but it is not trusted policy.", agent: true },
  { id: "bypass", envs: ["unprotected", "protected"], title: "Demo 7 · Direct database bypass",
    sub: "Guarding the MCP endpoint is useless if the runtime can reach the DB directly." },
];

test("agentic AI security presentation", async ({ page }) => {
  test.setTimeout(30 * 60_000);
  await page.request.post("/api/insecure-reset", { headers: { "X-Requested-With": "governed-ui" } });
  await page.goto(`${BASE}/present`);
  await expect(page.locator("#demos .demo").first()).toBeVisible();

  await card(page, "Agentic AI Security", "Why prompts are not security boundaries", 5000);
  await clearCard(page);
  await page.evaluate(() => window.scrollTo({ top: 0 }));
  await banner(page, "AI agents can query systems, run code, and change business data. Instructions alone cannot guarantee protection.");
  await spotlight(page, page.locator(".present-intro"), 6500);

  for (const demo of DEMOS) {
    await card(page, demo.title, demo.sub);
    await clearCard(page);
    const el = page.locator(`#demo-${demo.id}`);
    await el.scrollIntoViewIfNeeded();
    await banner(page, "Prompt → probe → compare unprotected vs protected → takeaway.");
    await spotlight(page, el.locator(".prompt"), 3500);
    await runProbe(page, demo);
    if (demo.envs.includes("unprotected")) {
      await banner(page, "UNPROTECTED: no identity, no validation, no policy.");
      await spotlight(page, panel(page, demo.id, "unprotected"), READ);
    }
    await banner(page, "PROTECTED: identity, tool validation, policy, tenant isolation, runtime isolation.");
    await spotlight(page, panel(page, demo.id, "protected"), READ);
    if (demo.agent && process.env.RECORD_AGENT === "1") {
      await banner(page, "Same prompt, run by the live local AI. Backend enforcement is unchanged.");
      await runAgent(page, demo.id, demo.envs);
      await spotlight(page, el.locator(".panels"), READ + 1500);
    }
    await spotlight(page, el.locator(".takeaway"), 3500);
  }

  await page.evaluate(() => document.getElementById("rec-banner")?.remove());
  await card(page, "Defense in depth",
    "Identity · MCP authorization · tool permissions · runtime isolation · database authorization · audit logging.", 6500);
  await card(page, "No unauthorized action should become an unauthorized database transaction.", "", 5000);
});
