// Acceptance suite configuration, written by bootstrap (verify-enable). Run it with `just verify <url>`.
// Inputs come from the environment: VERIFY_URL (the running app) and VERIFY_OUT (the evidence directory,
// ~/project-data/<project>/verification/<run>/). Browsers come from the shared cache bootstrap fills at
// install time; nothing is downloaded when tests run.
import { defineConfig, devices } from '@playwright/test';

const baseURL = process.env.VERIFY_URL;
const out = process.env.VERIFY_OUT;
if (!baseURL || !out) {
  throw new Error('Set VERIFY_URL and VERIFY_OUT, or run `just verify <url>`.');
}

export default defineConfig({
  testDir: '.',
  outputDir: `${out}/artifacts`,
  fullyParallel: true,
  // One run reports every test; a failure never stops the rest.
  maxFailures: 0,
  retries: 0,
  // No HTML report: the evidence is the JSON results plus traces and screenshots.
  reporter: [['list'], ['json', { outputFile: `${out}/results.json` }]],
  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    // The agent session sandbox already isolates the browser, and nested user
    // namespaces are not available inside it.
    launchOptions: { chromiumSandbox: false },
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
    { name: 'phone', use: { ...devices['Pixel 7'] } },
  ],
});
