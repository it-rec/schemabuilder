import { test, expect } from "@playwright/test";
import {
  resetBackendStateToSeed,
  waitForAppReady,
  selectSampleDocument,
  selectSeedDefinition,
} from "../helpers.js";

test.beforeEach(() => {
  resetBackendStateToSeed();
});

test.describe("Field extraction", () => {
  // Real Docling on a tiny digital PDF; even on a cold first call it
  // typically finishes well under 60s. The default test timeout (60s) gives
  // us margin without inflating the suite when extraction is fast.
  test("auto-extracts when doc + def are both selected", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);
    await selectSampleDocument(page);
    await selectSeedDefinition(page);

    const fields = page.getByTestId("fields-panel");
    // The title shows up once extraction is done; it's the document_type of
    // the selected definition.
    await expect(fields).toContainText("Seed Definition");
    // Tag like "0/3 found" or "1/3 found" — assert the X/Y shape.
    await expect(fields).toContainText(/\d+\/3 found/);
  });

  test("changing definition re-runs extraction", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);
    await selectSampleDocument(page);
    await selectSeedDefinition(page);

    // Create a second definition so we have something to switch to.
    await page.getByTestId("def-new-button").click();
    await page.getByLabel("Document type").fill("Switch Target");
    await page.getByRole("button", { name: "Create", exact: true }).click();
    await expect(
      page.getByRole("dialog", { name: "New document class" }),
    ).toBeHidden();

    // Switching to "Switch Target" picks it up — its field count is 0 so
    // tag should say "0/0 found".
    const fields = page.getByTestId("fields-panel");
    await expect(fields).toContainText("Switch Target");
    await expect(fields).toContainText(/0\/0 found/);
  });

  test("panel shows extraction results once /extract resolves", async ({
    page,
  }) => {
    // We can't reliably observe the transient "Extracting…" copy because
    // selectSampleDocument() may be a no-op (the seeded sample is already
    // auto-selected after page load) and a fresh extract may have completed
    // during globalSetup's warmup. The stable post-state is enough: the
    // panel ends up rendering the definition's title with a match tag.
    await page.goto("/");
    await waitForAppReady(page);
    await selectSampleDocument(page);

    const fields = page.getByTestId("fields-panel");
    await expect(fields).toContainText("Seed Definition");
    await expect(fields).toContainText(/\d+\/3 found/);
  });
});
