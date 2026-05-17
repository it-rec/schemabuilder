import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

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
  const seedDef = {
    document_type: "Seed Definition",
    document_description: "Seeded by E2E setup. Safe to overwrite.",
    fields: [
      {
        name: "title",
        description: "Document title",
      },
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
  };
  fs.writeFileSync(
    path.join(DEFINITIONS_DIR, "seed_definition.json"),
    JSON.stringify(seedDef, null, 2),
  );
}
