import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.BASE_URL ?? "http://127.0.0.1:8088";

// Runs against the REAL running stack (make up). Nothing is mocked except the
// clearly-labelled error/loading tests, which intercept the browser's request.
export default defineConfig({
  testDir: "./tests",
  timeout: 90_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  workers: 1, // tests share one backend (policy state, OPA up/down)
  retries: 0,
  reporter: [["list"], ["html", { outputFolder: "../reports/playwright-html", open: "never" }],
             ["junit", { outputFile: "../reports/playwright-junit.xml" }]],
  outputDir: "../reports/playwright-artifacts",
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [
    { name: "chromium-desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } }, grepInvert: /@mobile/ },
    {
      name: "chromium-mobile",
      use: { ...devices["Pixel 7"] },
      testMatch: /quality\.spec\.ts/,
      grep: /@mobile/,
    },
  ],
});
