import { test, expect } from "@playwright/test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import {
  FIXTURES_DIR,
  docRow,
  docDeleteButton,
  resetBackendStateToSeed,
  waitForAppReady,
  selectSampleDocument,
} from "../helpers.js";

test.beforeEach(() => {
  resetBackendStateToSeed();
});

test.describe("Document list", () => {
  test("renders the seeded sample.pdf with size and icon", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);

    const row = docRow(page, "sample.pdf");
    await expect(row).toBeVisible();
    await expect(row).toContainText(/KB|B/);
  });

  test("search filters the document list", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);

    // Carbon's <Search> renders both a wrapper <div role="search"> and a
    // child <input role="searchbox">; getByLabel matches both. Target the
    // input explicitly to avoid the strict-mode violation.
    const search = page.getByRole("searchbox", { name: "Search documents" });
    await search.fill("nope-no-match");
    await expect(page.getByText("No documents found.")).toBeVisible();
    await search.fill("");
    await expect(docRow(page, "sample.pdf")).toBeVisible();
  });

  test("uploads a PDF, it appears and is auto-selected", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);

    // The hidden <input type="file"> is wired directly; setInputFiles is
    // the canonical way to drive it without clicking the visible button.
    await page
      .getByTestId("upload-input")
      .setInputFiles(path.join(FIXTURES_DIR, "sample.pdf"));

    // Backend renames collisions with `-1`, `-2`, … — there's already a
    // sample.pdf seeded so the upload lands as sample-1.pdf.
    const newRow = docRow(page, "sample-1.pdf");
    await expect(newRow).toBeVisible();
    await expect(newRow).toHaveClass(/document-list__row--selected/);
  });

  test("uploads a DOCX file", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);

    await page
      .getByTestId("upload-input")
      .setInputFiles(path.join(FIXTURES_DIR, "sample.docx"));

    await expect(docRow(page, "sample.docx")).toBeVisible();
  });

  test("uploads a PPTX file", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);

    await page
      .getByTestId("upload-input")
      .setInputFiles(path.join(FIXTURES_DIR, "sample.pptx"));

    await expect(docRow(page, "sample.pptx")).toBeVisible();
  });

  test("rejects unsupported file extension on the server", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);

    // Drop a temp .txt file via the input; the backend returns 400, the
    // upload promise rejects, and the surviving doc list is unchanged.
    const tmpTxt = path.join(os.tmpdir(), "schemabuilder-e2e-bad.txt");
    fs.writeFileSync(tmpTxt, "hello");
    try {
      await page.getByTestId("upload-input").setInputFiles(tmpTxt);
      // The list should still only contain sample.pdf — no doc named bad.txt.
      await expect(docRow(page, "bad.txt")).toHaveCount(0);
    } finally {
      fs.rmSync(tmpTxt, { force: true });
    }
  });

  test("delete prompt: accepted → row is removed", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);

    // First upload an extra doc so deleting it doesn't leave the list empty
    // (the doc-viewer-empty branch is covered separately).
    await page
      .getByTestId("upload-input")
      .setInputFiles(path.join(FIXTURES_DIR, "sample.pdf"));
    const uploaded = docRow(page, "sample-1.pdf");
    await expect(uploaded).toBeVisible();

    page.once("dialog", (d) => d.accept());
    await docDeleteButton(page, "sample-1.pdf").click();
    await expect(uploaded).toHaveCount(0);
  });

  test("delete prompt: dismissed → row stays", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);

    page.once("dialog", (d) => d.dismiss());
    await docDeleteButton(page, "sample.pdf").click();
    await expect(docRow(page, "sample.pdf")).toBeVisible();
  });

  test("selecting a row updates the viewer", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);

    await selectSampleDocument(page);
    // Viewer header shows "Page 1 of 2" for the bundled sample.
    await expect(page.getByTestId("document-viewer-panel")).toContainText(
      /Page 1 of \d+/,
    );
  });

  test("j and k keyboard shortcuts navigate the list", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);

    // Need at least two docs to navigate between — upload an extra one.
    await page
      .getByTestId("upload-input")
      .setInputFiles(path.join(FIXTURES_DIR, "sample.pdf"));
    const uploaded = docRow(page, "sample-1.pdf");
    const original = docRow(page, "sample.pdf");
    await expect(uploaded).toBeVisible();
    // sample-1.pdf is alphabetically first and was auto-selected after upload.
    await expect(uploaded).toHaveClass(/document-list__row--selected/);

    // Focus the body so the global keydown handler fires (it bails out when
    // focus is inside a form control).
    await page.locator("body").focus();
    await page.keyboard.press("j");
    await expect(original).toHaveClass(/document-list__row--selected/);
    await page.keyboard.press("k");
    await expect(uploaded).toHaveClass(/document-list__row--selected/);
  });
});
