import { test, expect } from "@playwright/test";
import { resetBackendStateToSeed, waitForAppReady } from "../helpers.js";

test.beforeEach(() => {
  resetBackendStateToSeed();
});

test.describe("Definition history", () => {
  test("empty state for a freshly seeded definition", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);

    await page.getByTestId("def-edit-button").click();
    await page.getByTestId("def-history-button").click();

    const dlg = page.getByRole("dialog", { name: "Definition history" });
    await expect(dlg).toBeVisible();
    await expect(dlg).toContainText(/No archived versions/);
    await dlg.getByRole("button", { name: "Close" }).click();
  });

  test("editing a definition creates a version that history can show", async ({
    page,
  }) => {
    await page.goto("/");
    await waitForAppReady(page);

    // Force a save (which archives the previous content).
    await page.getByTestId("def-edit-button").click();
    const editor = page.getByRole("dialog", { name: "Edit document class" });
    await editor
      .getByLabel("Description")
      .fill(`Bumped ${new Date().toISOString()}`);
    await page.getByRole("button", { name: "Save changes" }).click();
    await expect(editor).toBeHidden();

    // Re-open editor + history modal.
    await page.getByTestId("def-edit-button").click();
    await page.getByTestId("def-history-button").click();
    const dlg = page.getByRole("dialog", { name: "Definition history" });
    await expect(dlg).toBeVisible();

    // At least one row in the version list.
    const versionRow = dlg.locator('[data-testid^="def-version-"]').first();
    await expect(versionRow).toBeVisible();
    await versionRow.click();

    // Diff pane renders after selecting a version.
    await expect(dlg.getByTestId("def-history-diff")).toBeVisible();

    // Restore: button is enabled now.
    const restore = dlg.getByTestId("def-restore-button");
    await expect(restore).toBeEnabled();
    page.once("dialog", (d) => d.accept());
    await restore.click();
    // Modal closes after a successful restore.
    await expect(dlg).toBeHidden();
  });
});
