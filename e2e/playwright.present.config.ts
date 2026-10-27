import { defineConfig } from "@playwright/test";

// Silent, caption-driven presentation recording against the live /present page.
export default defineConfig({
  testDir: "./recording",
  testMatch: /present\.record\.ts/,
  timeout: 30 * 60_000,
  expect: { timeout: 60_000 },
  workers: 1,
  reporter: [["list"]],
  outputDir: "../recordings/present-raw",
  use: {
    baseURL: process.env.BASE_URL ?? "http://127.0.0.1:8088",
    browserName: "chromium",
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 1,
    colorScheme: "light",
    video: { mode: "on", size: { width: 1440, height: 900 } },
  },
});
