import { expect, test } from "@playwright/test";
import { USERS, compose, resetDemo, runScenario, signIn, waitForOpa } from "./helpers";

// Really stops the OPA container (no mocks), then restores it.
test.describe.configure({ mode: "serial" });

test.afterAll(async () => {
  compose("start", "opa");
});

test("OPA unavailable: requests are denied (fail closed) and the UI shows it", async ({ page }) => {
  await resetDemo(page);
  await signIn(page, USERS.alice);
  compose("stop", "opa");
  await waitForOpa(page, "down");
  await expect(page.getByTestId("status-opa")).toContainText("OPA down");
  await runScenario(page, "opa_unavailable");
  await expect(page.getByTestId("outcome")).toHaveAttribute("data-outcome", "denied");
  await expect(page.getByTestId("outcome")).toContainText("fail closed");
  await expect(page.getByTestId("result-table")).toHaveCount(0);

  compose("start", "opa");
  await waitForOpa(page, "up");
  await runScenario(page, "order_summary");
  await expect(page.getByTestId("outcome")).toHaveAttribute("data-outcome", "allowed");
});
