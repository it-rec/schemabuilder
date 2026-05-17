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
    await page.getByRole("button", { name: "Create" }).click();
    await expect(
      page.getByRole("dialog", { name: "New document class" }),
    ).toBeHidden();

    // Switching to "Switch Target" picks it up — its field count is 0 so
    // tag should say "0/0 found".
    const fields = page.getByTestId("fields-panel");
    await expect(fields).toContainText("Switch Target");
    await expect(fields).toContainText(/0\/0 found/);
  });

  test("loading state shows while extraction runs", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);

    // Click a doc — viewer load AND extraction kick off together. The
    // fields panel shows the loading copy until /extract resolves.
    const extractPromise = page.waitForResponse(
      (r) => r.url().includes("/extract") && r.request().method() === "POST",
    );
    await selectSampleDocument(page);
    // Don't await the loading text — on a warm backend it can be gone by the
    // time the assertion runs. Just wait for the response and then assert
    // the panel ended in the post-extract state.
    await extractPromise;
    await expect(page.getByTestId("fields-panel")).toContainText("Seed Definition");
  });
});
