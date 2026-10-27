import { defineConfig } from "@playwright/test";

// Demo recording: one Chromium walkthrough against the live stack, recorded
// at a readable 1440x900. Uses scripted scenarios + prepared tests only, so
// the video is repeatable and matches the automated test results.
export default defineConfig({
  testDir: "./recording",
  testMatch: /demo\.record\.ts/,
  timeout: 10 * 60_000,
  expect: { timeout: 30_000 },
  workers: 1,
  reporter: [["list"]],
  outputDir: "../recordings/raw",
  use: {
    baseURL: process.env.BASE_URL ?? "http://127.0.0.1:8088",
    browserName: "chromium",
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 1,
    colorScheme: "light",
    video: { mode: "on", size: { width: 1440, height: 900 } },
  },
});
