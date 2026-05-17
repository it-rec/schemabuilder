import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { expect } from "@playwright/test";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

export const FIXTURES_DIR = path.resolve(__dirname, "fixtures");
export const TMP_ROOT = path.resolve(__dirname, "..", "playwright", ".tmp");
export const TEST_DOCS_DIR = path.join(TMP_ROOT, "docs");
export const DEFINITIONS_DIR = path.join(TMP_ROOT, "defs");

export const API_URL =
  process.env.E2E_API_URL ||
  `http://127.0.0.1:${process.env.E2E_BACKEND_PORT || "8765"}`;

export const SEED_DEF_ID = "seed_definition";
export const SEED_DOC_FILENAME = "sample.pdf";

// Drop everything except the seeded sample.pdf and seed_definition so a
// test sees the same baseline regardless of who ran before it. Faster than
// resetting via HTTP because we own the tmp dirs directly.
export function resetBackendStateToSeed() {
  for (const entry of fs.readdirSync(TEST_DOCS_DIR)) {
    if (entry !== SEED_DOC_FILENAME) {
      fs.rmSync(path.join(TEST_DOCS_DIR, entry), { recursive: true, force: true });
    }
  }
  // Re-copy the seed in case a test deleted it.
  const seedPath = path.join(TEST_DOCS_DIR, SEED_DOC_FILENAME);
  if (!fs.existsSync(seedPath)) {
    fs.copyFileSync(path.join(FIXTURES_DIR, SEED_DOC_FILENAME), seedPath);
  }
  for (const entry of fs.readdirSync(DEFINITIONS_DIR)) {
    // Keep the .versions subdir so a test that opens history doesn't fail
    // on first access; per-definition version subdirs orphaned by a delete
    // are cleaned with the parent.
    if (entry !== `${SEED_DEF_ID}.json` && entry !== ".versions") {
      fs.rmSync(path.join(DEFINITIONS_DIR, entry), { recursive: true, force: true });
    }
  }
  // Wipe versions for the seed def too, so the history test starts empty.
  const versionsDir = path.join(DEFINITIONS_DIR, ".versions");
  if (fs.existsSync(versionsDir)) {
    fs.rmSync(versionsDir, { recursive: true, force: true });
  }
  // Re-write the seed definition verbatim (in case an edit test mutated it).
  const seedDef = {
    document_type: "Seed Definition",
    document_description: "Seeded by E2E setup. Safe to overwrite.",
    fields: [
      { name: "title", description: "Document title" },
      { name: "amount", description: "Some monetary amount", normalizer: "currency" },
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
    path.join(DEFINITIONS_DIR, `${SEED_DEF_ID}.json`),
    JSON.stringify(seedDef, null, 2),
  );
}

// Wait for the app's first /health probe to flip "online" so the offline
// overlay isn't covering the UI when subsequent assertions start.
export async function waitForAppReady(page) {
  // The overlay either never appears (fast probe) or disappears within a
  // few seconds. Either way: assert it's gone before continuing.
  await expect(page.getByTestId("offline-overlay")).toBeHidden();
  // Document list panel rendered.
  await expect(page.getByTestId("document-list-panel")).toBeVisible();
}

// Convenience for selecting a definition from the Carbon Dropdown. Carbon
// renders the trigger as a <button role="combobox">; clicking the wrapper
// div doesn't open the listbox. Idempotent — if the requested item is
// already selected, returns without re-clicking.
export async function selectDefinition(page, name) {
  const trigger = page.getByRole("combobox", { name: "Document class" });
  const current = (await trigger.textContent())?.trim();
  if (current === name) return;
  await trigger.click();
  await page.getByRole("option", { name }).click();
}

export async function selectSeedDefinition(page) {
  await selectDefinition(page, "Seed Definition");
}

// Convenience for selecting the seeded sample document.
// Carbon's StructuredListRow renders as a non-button element even when we
// pass role="button" via props (the role attribute lands on the DOM but
// Playwright's accessibility tree doesn't surface it as a button — likely
// because Carbon sets a different role internally). Use the data-testid
// pattern instead, scoped by filename text so we never click the wrong
// row when multiple uploads (sample.pdf + sample-1.pdf) coexist.
export function docRow(page, filename) {
  return page
    .locator('[data-testid^="doc-row-"]')
    .filter({ hasText: filename });
}

export function docDeleteButton(page, filename) {
  // The IconButton is a real <button> so the role *does* resolve; scope to
  // within the matching row so we never delete the wrong file when several
  // rows share a filename prefix.
  return docRow(page, filename).getByRole("button", {
    name: `Delete ${filename}`,
  });
}

export async function selectDocument(page, filename) {
  await docRow(page, filename).click();
}

export async function selectSampleDocument(page) {
  await selectDocument(page, "sample.pdf");
}

// Read a known test-doc id from the API: we can't predict the hashed id
// the backend will assign to a file, so look it up by filename.
export async function getDocumentIdByFilename(request, filename) {
  const res = await request.get(`${API_URL}/api/documents`);
  expect(res.ok()).toBeTruthy();
  const body = await res.json();
  const items = Array.isArray(body) ? body : body.items;
  const match = items.find((d) => d.filename === filename);
  if (!match) throw new Error(`No document with filename ${filename}`);
  return match.id;
}
