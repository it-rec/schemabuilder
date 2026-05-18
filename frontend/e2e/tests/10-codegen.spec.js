import { test, expect } from "@playwright/test";
import { resetBackendStateToSeed, waitForAppReady } from "../helpers.js";

test.beforeEach(() => {
  resetBackendStateToSeed();
});

test.describe("Schema codegen export", () => {
  // The OverflowMenu in the editor isn't keyboard-discoverable by name, so we
  // target it by testid then click each format's menu item and assert the
  // browser receives a download with the expected extension.

  for (const { id, label, ext } of [
    { id: "json-schema", label: "JSON Schema (.json)", ext: "json" },
    { id: "sql-postgres", label: "PostgreSQL DDL (.sql)", ext: "sql" },
    { id: "sql-bigquery", label: "BigQuery DDL (.sql)", ext: "sql" },
    { id: "typescript", label: "TypeScript types (.ts)", ext: "ts" },
  ]) {
    test(`exports ${label}`, async ({ page }) => {
      await page.goto("/");
      await waitForAppReady(page);

      // Open the editor in edit mode against the seed definition.
      await page.getByTestId("def-edit-button").click();
      await expect(
        page.getByRole("dialog", { name: "Edit document class" }),
      ).toBeVisible();

      // Open the OverflowMenu, then trigger the format-specific item.
      await page.getByTestId("def-export-menu").click();
      const downloadPromise = page.waitForEvent("download");
      await page.getByTestId(`def-export-${id}`).click();
      const download = await downloadPromise;
      const filename = download.suggestedFilename();
      expect(filename.endsWith(`.${ext}`)).toBe(true);
    });
  }
});
