import { readFileSync, writeFileSync } from "node:fs";
import { expect, test, type Locator, type Page } from "@playwright/test";
import { USERS, compose, resetDemo, runScenario, signIn, waitForOpa } from "../tests/helpers";

const APP = "/?present=1"; // the app's presentation mode: larger type for video

// Narration timing. scripts/record.sh synthesises one audio clip per segment
// (macOS `say`) and passes their durations here; the walkthrough waits exactly
// that long on each segment and logs when it started, so the audio can be laid
// onto the video in sync. Without audio, durations are estimated from word count.
const NARRATION = JSON.parse(readFileSync(new URL("./narration.json", import.meta.url), "utf8")) as {
  segments: { id: string; text: string }[];
};
const DURATIONS: Record<string, number> = process.env.NARRATION_DURATIONS
  ? JSON.parse(readFileSync(process.env.NARRATION_DURATIONS, "utf8"))
  : Object.fromEntries(NARRATION.segments.map((s) => [s.id, (s.text.split(/\s+/).length / 2.7) * 1000]));
const GAP_MS = 600; // short pause after each spoken segment
const timeline: { id: string; offset_ms: number }[] = [];
let t0 = 0;

/** Caption bar drawn by the recording script only (not part of the app UI). */
async function caption(page: Page, title: string, detail = ""): Promise<void> {
  await page.evaluate(([t, d]) => {
    let bar = document.getElementById("rec-caption");
    if (!bar) {
      bar = document.createElement("div");
      bar.id = "rec-caption";
      Object.assign(bar.style, {
        position: "fixed", left: "0", right: "0", bottom: "0", zIndex: "9999", padding: "14px 28px",
        background: "rgba(17,24,39,.94)", color: "#fff", font: "600 22px/1.35 system-ui, sans-serif",
        pointerEvents: "none",
      });
      document.body.append(bar);
    }
    bar.replaceChildren();
    const strong = document.createElement("div");
    strong.textContent = t;
    const small = document.createElement("div");
    small.textContent = d;
    Object.assign(small.style, { fontWeight: "400", fontSize: "19px", opacity: ".9" });
    bar.append(strong, small);
  }, [title, detail]);
}

/** Full-screen title card for the two parts (drawn by the recording script). */
async function titleCard(page: Page, heading: string, sub: string): Promise<void> {
  await page.evaluate(([h, s]) => {
    document.getElementById("rec-card")?.remove();
    const card = document.createElement("div");
    card.id = "rec-card";
    Object.assign(card.style, {
      position: "fixed", inset: "0", zIndex: "10000", display: "grid", placeContent: "center", gap: "18px",
      background: "rgba(17,24,39,.97)", color: "#fff", textAlign: "center", padding: "0 120px",
      font: "700 52px/1.2 system-ui, sans-serif", pointerEvents: "none",
    });
    const h1 = document.createElement("div");
    h1.textContent = h;
    const p = document.createElement("div");
    p.textContent = s;
    Object.assign(p.style, { font: "400 26px/1.45 system-ui, sans-serif", opacity: ".9" });
    card.append(h1, p);
    document.body.append(card);
  }, [heading, sub]);
}

async function clearCard(page: Page): Promise<void> {
  await page.evaluate(() => document.getElementById("rec-card")?.remove());
}

/** Speak segment `id` while running `during`; total time = clip length + a short gap. */
async function narrate(page: Page, id: string, during: (budgetMs: number) => Promise<void> = async () => {}) {
  const duration = DURATIONS[id];
  if (duration === undefined) throw new Error(`no narration segment '${id}'`);
  const started = Date.now();
  timeline.push({ id, offset_ms: started - t0 });
  console.log(`[record] ${(started - t0) / 1000}s ${id}`);
  await during(duration);
  const remaining = duration + GAP_MS - (Date.now() - started);
  if (remaining > 0) await page.waitForTimeout(remaining);
}

/** Scroll a section to the middle of the screen and outline it for `ms`. */
async function spotlight(page: Page, el: Locator, ms: number): Promise<void> {
  if (!(await el.count())) return;
  await el.evaluate((node: HTMLElement) => {
    node.scrollIntoView({ block: "center" });
    Object.assign(node.style, { outline: "4px solid #f59e0b", outlineOffset: "6px", borderRadius: "6px" });
  });
  await page.waitForTimeout(ms);
  await el.evaluate((node: HTMLElement) => { node.style.outline = ""; });
}

/** Run a scenario during its narration, spotlighting outcome → input → route → decision → result. */
async function show(page: Page, scenario: string, outcome: string, segment: string, title: string, line: string) {
  await test.step(title, () => narrate(page, segment, async (budget) => {
    await caption(page, title, line);
    const started = Date.now();
    await runScenario(page, scenario);
    await expect(page.getByTestId("outcome")).toHaveAttribute("data-outcome", outcome);
    const sections = [
      page.getByTestId("outcome"),
      page.getByTestId("sql").or(page.getByTestId("code")).first(),
      page.getByTestId("route").first(),
      page.getByTestId("decision").first(),
      page.getByTestId("bypass-table").or(page.getByTestId("row-count")).or(page.getByTestId("no-data")).first(),
    ];
    const each = Math.max(1200, (budget - (Date.now() - started)) / sections.length);
    for (const section of sections) await spotlight(page, section, each);
  }));
}

test("governed agent data access - narrated demo", async ({ page }) => {
  test.setTimeout(20 * 60_000);
  t0 = Date.now();
  await resetDemo(page);
  await page.goto(APP);
  await expect(page.getByTestId("scenario-list").locator("li").first()).toBeVisible();

  // ------------------------------------------------------------------ intro
  await titleCard(page, "Governed Agent Data Access",
    "Synthetic data for two fictional companies. Every result shown comes from the live system.");
  await narrate(page, "intro", async () => {
    await page.waitForTimeout(4000);
    await clearCard(page);
    await caption(page, "Alice (Acme) signs in", "Her company comes only from her signed login.");
    await signIn(page, USERS.alice, APP);
  });

  // ------------------------------------------------------- part 1: problem
  await titleCard(page, "Part 1 · The problem", "Without enforcement, only the prompt says \"don't\".");
  await narrate(page, "p1_title");
  await clearCard(page);
  if (await page.getByTestId("run-direct_db_prompt_only").isDisabled()) {
    throw new Error("Part 1 needs the prompt-only overlay: run `make up`, not `make up-protected`.");
  }
  await show(page, "direct_db_prompt_only", "bypassed", "p1_bypass",
    "Part 1 · Unprotected: agent code with a DB password (INSECURE by design)",
    "PREPARED TEST, not AI-generated. Only the prompt said 'use the approved tool only'.");

  // ------------------------------------------------------ part 2: solution
  await titleCard(page, "Part 2 · With protections enforced",
    "No database password, no network route. Every request: identity → query check → policy → database filter.");
  await narrate(page, "p2_title");
  await clearCard(page);

  await show(page, "order_summary", "allowed", "order_summary",
    "Order summary", "Governed path; runs as Acme's own database account.");
  await show(page, "select_star", "allowed", "select_star",
    "SELECT * FROM orders (no filter)", "The database adds Acme's filter itself: 60 rows, all Acme.");
  await show(page, "cross_tenant_explicit", "denied", "cross_tenant_explicit",
    "Asks for Northstar's orders by name", "Denied by the policy engine; database never reached.");
  await show(page, "cross_tenant_rls", "allowed", "cross_tenant_rls",
    "Asks for Northstar's orders, name hidden", "Policy can't see it, so it runs, and the database returns 0 rows.");
  await show(page, "write_attempt", "denied", "write_attempt",
    "Tries to change data (UPDATE)", "Rejected by the query checker.");
  await show(page, "role_change", "denied", "role_change",
    "Tries to switch to Northstar's DB account", "Rejected by the query checker (and by the database).");
  await show(page, "python_via_mcp", "allowed", "python_via_mcp",
    "Python through the approved route", "Sandboxed code → tool server → Acme results only.");
  await show(page, "direct_db_protected", "blocked", "direct_db_protected",
    "Same break-in script, protected sandbox", "PREPARED TEST. Name unresolvable; known DB IPs unreachable; no password.");

  await narrate(page, "revoke", async () => {
    await caption(page, "Admin revokes Acme's access in the policy engine", "Agent and prompt unchanged.");
    await page.getByTestId("revoke").scrollIntoViewIfNeeded();
    await page.getByTestId("revoke").click();
    await expect(page.getByTestId("policy-state")).toContainText("REVOKED");
    await spotlight(page, page.getByTestId("policy-state"), 2500);
  });
  await show(page, "order_summary", "denied", "revoked_run",
    "The very next request", "Denied by the policy engine. No caching.");
  await page.getByTestId("restore").click();
  await expect(page.getByTestId("policy-state")).toContainText("granted");

  compose("stop", "opa");
  await waitForOpa(page, "down");
  await show(page, "opa_unavailable", "denied", "opa_down",
    "Policy engine down (container stopped for real)", "Fail closed: denied, no data.");
  compose("start", "opa");
  await waitForOpa(page, "up");

  await narrate(page, "nina", async (budget) => {
    await caption(page, "Nina (Northstar): SELECT * FROM orders", "45 rows, all Northstar. No Acme rows.");
    await signIn(page, USERS.nina, APP);
    await runScenario(page, "select_star");
    await expect(page.getByTestId("outcome")).toHaveAttribute("data-outcome", "allowed");
    await spotlight(page, page.getByTestId("row-count"), Math.max(1500, budget - 4000));
  });

  await titleCard(page, "What actually protects the data",
    "Signed identity · query checks · central policy · database row filtering · network isolation. " +
    "Database isolation is also verified directly by automated tests (see docs/TEST_REPORT.md).");
  await narrate(page, "outro");
  await page.request.post("/api/reset", { headers: { "X-Requested-With": "governed-ui" } });

  if (process.env.NARRATION_TIMELINE) writeFileSync(process.env.NARRATION_TIMELINE, JSON.stringify(timeline, null, 2));
  // scripts/record.sh copies this test's video and lays the narration onto it.
});

test.afterAll(() => {
  compose("start", "opa");
});
