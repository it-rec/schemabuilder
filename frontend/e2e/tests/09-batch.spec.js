import { test, expect } from "@playwright/test";
import path from "node:path";
import {
  FIXTURES_DIR,
  docRow,
  resetBackendStateToSeed,
  waitForAppReady,
  selectSeedDefinition,
} from "../helpers.js";

test.beforeEach(() => {
  resetBackendStateToSeed();
});

test.describe("Batch extraction", () => {
  test("Run all opens the modal with the right doc count", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);
    await selectSeedDefinition(page);

    // Need at least one extra doc so we're running over 2.
    await page
      .getByTestId("upload-input")
      .setInputFiles(path.join(FIXTURES_DIR, "sample.pdf"));
    await expect(docRow(page, "sample-1.pdf")).toBeVisible();

    await page.getByTestId("batch-run-button").click();
    const modal = page.getByRole("dialog", { name: "Batch extraction" });
    await expect(modal).toBeVisible();
    await expect(modal).toContainText(/over\s*2\s*documents/);
  });

  test("starts a job, progress moves, then ends in done state", async ({
    page,
  }) => {
    await page.goto("/");
    await waitForAppReady(page);
    // The Run-all click is dropped if the doc list hasn't loaded yet
    // (filtered.length === 0 disables the button). Wait for the seeded
    // row to materialize before clicking.
    await expect(docRow(page, "sample.pdf")).toBeVisible();
    await selectSeedDefinition(page);

    await page.getByTestId("batch-run-button").click();
    const modal = page.getByRole("dialog", { name: "Batch extraction" });
    await expect(modal).toBeVisible();
    // Wait for the Start button so we don't click before the modal has
    // finished resetting its idle state.
    await expect(modal.getByTestId("batch-start")).toBeEnabled();
    await modal.getByTestId("batch-start").click();

    // The download button only enables once the job is no longer running.
    // Use that as the "done" signal; React 18+ batching means the
    // intermediate "running" status may render too briefly to assert on
    // reliably, and the footer Close label collides with the modal
    // header close-icon name.
    await expect(modal.getByTestId("batch-download")).toBeEnabled({
      timeout: 120_000,
    });
  });

  test("cancel mid-run flips to the cancelled state", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);
    await expect(docRow(page, "sample.pdf")).toBeVisible();
    await selectSeedDefinition(page);

    // Upload extras so the job takes long enough to cancel.
    for (let i = 0; i < 3; i++) {
      await page
        .getByTestId("upload-input")
        .setInputFiles(path.join(FIXTURES_DIR, "sample.pdf"));
    }
    // Wait for all three to land — without this the click can fire before
    // the docs list re-renders, leaving the cancel-test with only 1 doc.
    await expect(docRow(page, "sample-3.pdf")).toBeVisible();

    await page.getByTestId("batch-run-button").click();
    const modal = page.getByRole("dialog", { name: "Batch extraction" });
    await modal.getByTestId("batch-start").click();
    // The Cancel run button is only present while status==="running".
    const cancel = modal.getByTestId("batch-cancel");
    await expect(cancel).toBeVisible();
    await cancel.click();

    // Cancelled state surfaces the "Cancelled" helper text on the bar.
    await expect(modal).toContainText(/Cancelled/i);
  });
});
