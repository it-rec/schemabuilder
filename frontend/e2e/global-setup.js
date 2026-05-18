import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { request } from "@playwright/test";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Mirror the constants from playwright.config.js. We can't import from the
// config file directly without a TS loader so duplicate them — the test
// asserts (in app-load.spec.js) that the seeded sample is present, which
// catches drift.
const TMP_ROOT = path.resolve(__dirname, "..", "playwright", ".tmp");
const TEST_DOCS_DIR = path.join(TMP_ROOT, "docs");
const DEFINITIONS_DIR = path.join(TMP_ROOT, "defs");
const FIXTURES_DIR = path.resolve(__dirname, "fixtures");

const BACKEND_PORT = process.env.E2E_BACKEND_PORT || "8765";
const FRONTEND_PORT = process.env.E2E_FRONTEND_PORT || "3000";
const API_URL = `http://127.0.0.1:${BACKEND_PORT}`;
const FRONTEND_URL = `http://127.0.0.1:${FRONTEND_PORT}`;

function resetDir(dir) {
  if (fs.existsSync(dir)) {
    // Remove everything inside, but keep the directory itself if other
    // processes (the backend) have it open.
    for (const entry of fs.readdirSync(dir)) {
      fs.rmSync(path.join(dir, entry), { recursive: true, force: true });
    }
  } else {
    fs.mkdirSync(dir, { recursive: true });
  }
}

export default async function globalSetup() {
  resetDir(TEST_DOCS_DIR);
  resetDir(DEFINITIONS_DIR);

  // Seed the docs dir with the small sample so tests that need a document
  // present (viewer, extraction, …) don't all have to start with an upload.
  // Use a canonical filename so tests can target it by name.
  fs.copyFileSync(
    path.join(FIXTURES_DIR, "sample.pdf"),
    path.join(TEST_DOCS_DIR, "sample.pdf"),
  );

  // Seed a known definition that matches fields in sample.pdf. Kept minimal
  // (one always-required-field, one optional, one array) so extraction tests
  // have something to display without depending on a specific PDF's content.
  // Backend stores definitions under the `document` wrapper — flat shape
  // would load with `document_type` defaulting to "Unknown".
  const seedDef = {
    document: {
      document_type: "Seed Definition",
      document_description: "Seeded by E2E setup. Safe to overwrite.",
      fields: [
        { name: "title", description: "Document title" },
        {
          name: "amount",
          description: "Some monetary amount",
          normalizer: "currency",
        },
        {
          name: "line_items",
          type: "array",
          description: "Repeating rows",
          fields: [
            { name: "description" },
            { name: "qty", normalizer: "number" },
          ],
        },
      ],
    },
  };
  fs.writeFileSync(
    path.join(DEFINITIONS_DIR, "seed_definition.json"),
    JSON.stringify(seedDef, null, 2),
  );

  // Warm both servers so the first test doesn't pay the cold-start cost:
  //   - Vite compiles the React + Carbon bundle on the first navigation
  //     (10-30s on a free CI runner); a single GET ahead of time means
  //     every test starts against a warm bundle.
  //   - Docling lazily builds its converter on the first /extract call
  //     (also 10-30s). We trigger one extract here so the per-test 90s
  //     timeout doesn't have to absorb that cost.
  // Both are best-effort: a failure here doesn't stop the suite, the
  // first real test will surface the underlying problem with better
  // diagnostics than a globalSetup throw would.
  const ctx = await request.newContext();
  try {
    await ctx
      .get(FRONTEND_URL, { timeout: 60_000 })
      .catch((err) => console.warn("frontend warmup skipped:", err.message));

    // Find the seed doc id by listing — the id is a hash of the filename
    // so we can't predict it, but the list endpoint is fast.
    const listRes = await ctx
      .get(`${API_URL}/api/documents`, { timeout: 30_000 })
      .catch(() => null);
    if (listRes && listRes.ok()) {
      const body = await listRes.json();
      const items = Array.isArray(body) ? body : body.items;
      const seed = items.find((d) => d.filename === "sample.pdf");
      if (seed) {
        // The extract call may take 30-60s on first run while Docling
        // builds its converter. Allow 180s; failure is non-fatal.
        await ctx
          .post(`${API_URL}/api/documents/${seed.id}/extract`, {
            data: { definition_id: "seed_definition" },
            timeout: 180_000,
          })
          .catch((err) =>
            console.warn("docling warmup skipped:", err.message),
          );
      }
    }
  } finally {
    await ctx.dispose();
  }
}
