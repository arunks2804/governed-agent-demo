#!/usr/bin/env node
// Capture each slide as a 1920×1080 PNG using Playwright.
// Run from e2e/ directory: node ../scripts/capture-slides.js '<json>'
// JSON: [{url: "file:///abs/path/presentation.html?n=1", output: "/abs/out.png"}, ...]

const path = require('path');
const { chromium } = require(path.resolve(__dirname, '../e2e/node_modules/playwright'));

(async () => {
  const slides = JSON.parse(process.argv[2]);
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.setViewportSize({ width: 1920, height: 1080 });

  for (const { url, output } of slides) {
    await page.goto(url);
    await page.waitForLoadState('networkidle');
    await new Promise(r => setTimeout(r, 300));   // let fonts/CSS settle
    await page.screenshot({ path: output, type: 'png' });
    console.log('captured', output);
  }

  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
