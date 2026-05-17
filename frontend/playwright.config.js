import { defineConfig, devices } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// E2E state lives in playwright/.tmp/ so it doesn't pollute the checked-in
// backend/test_documents/ and backend/definitions/ fixtures. The directory
// is gitignored; globalSetup seeds it before the run.
const TMP_ROOT = path.resolve(__dirname, "playwright", ".tmp");
const TEST_DOCS_DIR = path.join(TMP_ROOT, "docs");
const DEFINITIONS_DIR = path.join(TMP_ROOT, "defs");

// Backend port deliberately picked outside the dev-server range so a developer
// running `npm run dev` against the real backend on :8000 doesn't collide
// with the E2E backend.
const BACKEND_PORT = process.env.E2E_BACKEND_PORT || "8765";
const FRONTEND_PORT = process.env.E2E_FRONTEND_PORT || "3000";
const API_URL = `http://127.0.0.1:${BACKEND_PORT}`;

export default defineConfig({
  testDir: "./e2e/tests",
  // Sequential by default — most tests mutate global backend state
  // (upload a doc, create a definition). Parallel runs would fight over
  // file names and definition ids. Tests that are truly read-only could
  // be moved to a parallel project later.
  workers: 1,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  // In CI we want three things from the reporter:
  //   - a streaming `list` so the GH Actions log shows live progress,
  //   - the `github` reporter which emits ::error annotations the PR
  //     check page surfaces inline (visible without sign-in on the
  //     workflow run page), and
  //   - an html + json report uploaded as artifacts for deep dives.
  reporter: process.env.CI
    ? [
        ["list"],
        ["github"],
        ["html", { open: "never", outputFolder: "playwright-report" }],
        ["json", { outputFile: "test-results/results.json" }],
      ]
    : "list",
  // 90s per test covers a cold-start Docling extract on CI (the first
  // /extract call rebuilds the converter; subsequent calls hit the cache).
  // Most tests finish in seconds — the budget is for the outliers, not
  // the steady state.
  timeout: 90_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL: `http://127.0.0.1:${FRONTEND_PORT}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    // Chromium in CI runs as root in the GH Actions runner; without
    // --no-sandbox the renderer process can't start. Harmless when not root.
    launchOptions: { args: ["--no-sandbox"] },
  },
  globalSetup: "./e2e/global-setup.js",
  webServer: [
    {
      // Real FastAPI backend, but pointed at isolated tmp dirs so we don't
      // pollute the checked-in fixtures. DOCLING_DO_OCR=0 keeps extraction
      // on the fast pypdfium2 text path; the bundled sample PDF is digital
      // so OCR isn't needed and disabling it cuts ~5-15s off the first
      // extract on a cold cache.
      command: `python -m uvicorn main:app --host 127.0.0.1 --port ${BACKEND_PORT} --log-level info`,
      cwd: path.resolve(__dirname, "../backend"),
      url: `${API_URL}/health`,
      // 5 min: importing main.py is fast (Docling is lazy), but on a fresh
      // CI runner uvicorn boot occasionally takes longer when pip's wheel
      // cache is still being unpacked. Generous on purpose — the timeout
      // only fires when something is genuinely wrong.
      timeout: 300_000,
      reuseExistingServer: !process.env.CI,
      stdout: "pipe",
      stderr: "pipe",
      env: {
        SCHEMABUILDER_TEST_DOCS_DIR: TEST_DOCS_DIR,
        SCHEMABUILDER_DEFINITIONS_DIR: DEFINITIONS_DIR,
        DOCLING_DO_OCR: "0",
        // Disable the LLM features so the suggest-definition endpoint
        // returns a deterministic 503 we can assert on, instead of
        // depending on whether a key happens to be present in the env.
        ANTHROPIC_API_KEY: "",
        // Loosen CORS so the dev server (different port) can talk to us
        // without a proxy.
        CORS_ALLOW_ORIGINS: `http://127.0.0.1:${FRONTEND_PORT},http://localhost:${FRONTEND_PORT}`,
        // Bump the connect-status poll interval ceiling: with the default
        // 30s online-poll the offline test would wait a long time for the
        // overlay to disappear after we drop the route mock. The hook
        // doesn't expose a knob, so the test polls /health itself via a
        // route hook instead.
      },
    },
    {
      command: `npx vite --host 127.0.0.1 --port ${FRONTEND_PORT}`,
      cwd: __dirname,
      url: `http://127.0.0.1:${FRONTEND_PORT}`,
      timeout: 120_000,
      reuseExistingServer: !process.env.CI,
      stdout: "pipe",
      stderr: "pipe",
      env: {
        VITE_API_URL: API_URL,
        // Keep the API timeout at the app default (30s). A tighter value
        // would bite extraction tests when Docling's first call is slow.
      },
    },
  ],
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});

// Re-exported for globalSetup so it agrees with us on where state lives.
export { TMP_ROOT, TEST_DOCS_DIR, DEFINITIONS_DIR, BACKEND_PORT, FRONTEND_PORT, API_URL };
