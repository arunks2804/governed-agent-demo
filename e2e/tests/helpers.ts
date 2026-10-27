import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect, type Page } from "@playwright/test";

const projectDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

export const USERS = {
  alice: "alice@acme.example",
  nina: "nina@northstar.example",
  victor: "victor@acme.example",
} as const;

/** Run `docker compose` for this project (used to really stop/start OPA). */
export function compose(...args: string[]): string {
  return execFileSync("docker", ["compose", ...args], {
    cwd: projectDir,
    env: { ...process.env, COMPOSE_FILE: process.env.COMPOSE_FILE ?? "compose.yaml:compose.prompt-only.yaml" },
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
}

export async function resetDemo(page: Page): Promise<void> {
  const res = await page.request.post("/api/reset", { headers: { "X-Requested-With": "governed-ui" } });
  expect(res.ok()).toBeTruthy();
}

export async function signIn(page: Page, sub: string, path = "/"): Promise<void> {
  await page.goto(path);
  await expect(page.getByTestId("scenario-list").locator("li").first()).toBeVisible();
  if (await page.getByTestId("logout").isVisible()) await page.getByTestId("logout").click();
  await page.getByTestId("identity-select").selectOption(sub);
  await page.getByTestId("login").click();
  await expect(page.getByTestId("whoami")).toContainText(sub);
}

export async function runScenario(page: Page, id: string): Promise<void> {
  await page.getByTestId(`run-${id}`).click();
  await expect(page.getByTestId("run-view")).toHaveAttribute("aria-busy", "false");
  await expect(page.getByTestId("outcome").or(page.getByTestId("run-error"))).toBeVisible();
}

export async function waitForOpa(page: Page, state: "up" | "down"): Promise<void> {
  await expect.poll(async () => (await (await page.request.get("/api/status")).json()).opa,
    { timeout: 45_000, intervals: [500] }).toBe(state);
}
