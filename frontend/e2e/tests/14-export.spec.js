import { test, expect } from "@playwright/test";
import {
  API_URL,
  resetBackendStateToSeed,
  waitForAppReady,
  selectSampleDocument,
  selectDefinition,
  getDocumentIdByFilename,
  SEED_DOC_FILENAME,
} from "../helpers.js";

// A definition with `target_tables` so the FieldsPanel renders its export
// OverflowMenu (the menu is hidden when no tables are declared).
// `target_tables` lives at the top of the JSON, NOT inside `document` —
// the backend reads `definition.target_tables`, not `definition.document.
// target_tables`. The matching invoice.json fixture has the same shape.
const EXPORT_DEF_ID = "export_test";
const exportDef = {
  document: {
    document_type: "Export Test",
    document_description: "Has target_tables so the export menu renders.",
    fields: [
      { name: "title", description: "Document title" },
      { name: "amount", description: "Amount", normalizer: "currency" },
    ],
  },
  target_tables: [
    {
      name: "Summary",
      columns: [
        {
          name: "doc_id",
          type: "string",
          source: { variable: "document_id" },
        },
        { name: "title", type: "string", source: { field: "title" } },
        { name: "amount", type: "number", source: { field: "amount" } },
      ],
    },
  ],
};

test.beforeEach(async ({ request }) => {
  resetBackendStateToSeed();
  // overwrite=true so a stale copy from an interrupted previous run can't
  // 409 us into a different code path than what we're testing.
  await request.post(`${API_URL}/api/definitions?overwrite=true`, {
    data: exportDef,
  });
});

test.describe("FieldsPanel export menu (UI)", () => {
  test("Download all tables (JSON) triggers a .json download", async ({
    page,
  }) => {
    await page.goto("/");
    await waitForAppReady(page);
    await selectSampleDocument(page);
    await selectDefinition(page, "Export Test");

    // Wait for extraction to populate the panel; the export menu is only
    // rendered once `extraction.target_tables` is non-empty.
    const panel = page.getByTestId("fields-panel");
    await expect(panel).toContainText("Export Test");
    const menu = panel.getByTestId("export-menu");
    await expect(menu).toBeVisible();
    await menu.click();

    const downloadPromise = page.waitForEvent("download");
    await page.getByText("Download all tables (JSON)").click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toMatch(/\.json$/);
  });

  test('Download "Summary" (CSV) triggers a .csv download', async ({
    page,
  }) => {
    await page.goto("/");
    await waitForAppReady(page);
    await selectSampleDocument(page);
    await selectDefinition(page, "Export Test");

    const panel = page.getByTestId("fields-panel");
    await expect(panel).toContainText("Export Test");
    const menu = panel.getByTestId("export-menu");
    await expect(menu).toBeVisible();
    await menu.click();

    const downloadPromise = page.waitForEvent("download");
    await page.getByText('Download "Summary" (CSV)').click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toMatch(/\.csv$/);
  });
});

test.describe("Export endpoint API contract", () => {
  test("format=json returns the full tables envelope", async ({ request }) => {
    const docId = await getDocumentIdByFilename(request, SEED_DOC_FILENAME);
    const res = await request.get(
      `${API_URL}/api/documents/${docId}/export?definition_id=${EXPORT_DEF_ID}&format=json`,
    );
    expect(res.ok()).toBeTruthy();
    const body = await res.json();
    // Backend returns `{document_id, definition_id, tables: {Summary: [...]}}`.
    expect(body).toHaveProperty("tables");
    expect(body.tables).toHaveProperty("Summary");
    expect(Array.isArray(body.tables.Summary)).toBe(true);
  });

  test("format=csv&table=Summary returns CSV with a download filename", async ({
    request,
  }) => {
    const docId = await getDocumentIdByFilename(request, SEED_DOC_FILENAME);
    const res = await request.get(
      `${API_URL}/api/documents/${docId}/export?definition_id=${EXPORT_DEF_ID}&format=csv&table=Summary`,
    );
    expect(res.ok()).toBeTruthy();
    const cd = res.headers()["content-disposition"] || "";
    expect(cd).toMatch(/filename="[^"]+\.csv"/);
    const text = await res.text();
    // At minimum a header row should be present.
    expect(text.length).toBeGreaterThan(0);
    expect(text.split("\n")[0]).toContain("doc_id");
  });

  test("format=csv without &table= returns 400", async ({ request }) => {
    const docId = await getDocumentIdByFilename(request, SEED_DOC_FILENAME);
    const res = await request.get(
      `${API_URL}/api/documents/${docId}/export?definition_id=${EXPORT_DEF_ID}&format=csv`,
    );
    expect(res.status()).toBe(400);
  });

  test("unknown format returns 400", async ({ request }) => {
    const docId = await getDocumentIdByFilename(request, SEED_DOC_FILENAME);
    const res = await request.get(
      `${API_URL}/api/documents/${docId}/export?definition_id=${EXPORT_DEF_ID}&format=bogus`,
    );
    expect(res.status()).toBe(400);
  });

  test("unknown table returns 400 or 404", async ({ request }) => {
    const docId = await getDocumentIdByFilename(request, SEED_DOC_FILENAME);
    const res = await request.get(
      `${API_URL}/api/documents/${docId}/export?definition_id=${EXPORT_DEF_ID}&format=csv&table=NoSuchTable`,
    );
    expect([400, 404]).toContain(res.status());
  });
});
