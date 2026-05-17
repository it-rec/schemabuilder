import { test, expect } from "@playwright/test";
import path from "node:path";
import {
  FIXTURES_DIR,
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
    await expect(
      page.getByRole("button", { name: /Select sample-1\.pdf/ }),
    ).toBeVisible();

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
    await selectSeedDefinition(page);

    await page.getByTestId("batch-run-button").click();
    const modal = page.getByRole("dialog", { name: "Batch extraction" });
    await modal.getByTestId("batch-start").click();

    // Progress bar appears once status leaves "idle".
    await expect(modal.getByTestId("batch-progress")).toBeVisible();

    // Wait for the terminal state — the close-row "Close" button
    // appears once the job is no longer running.
    await expect(modal.getByRole("button", { name: "Close" })).toBeVisible({
      timeout: 120_000,
    });
    await expect(modal.getByTestId("batch-download")).toBeEnabled();
  });

  test("cancel mid-run flips to the cancelled state", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);
    await selectSeedDefinition(page);

    // Upload extras so the job takes long enough to cancel.
    for (let i = 0; i < 3; i++) {
      await page
        .getByTestId("upload-input")
        .setInputFiles(path.join(FIXTURES_DIR, "sample.pdf"));
    }
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
