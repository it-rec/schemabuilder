import { test, expect } from "@playwright/test";
import { resetBackendStateToSeed, waitForAppReady } from "../helpers.js";

test.beforeEach(() => {
  resetBackendStateToSeed();
});

test.describe("Definition templates", () => {
  test("template picker appears in create mode and populates fields", async ({
    page,
  }) => {
    await page.goto("/");
    await waitForAppReady(page);

    await page.getByTestId("def-new-button").click();
    const editor = page.getByRole("dialog", { name: "New document class" });

    const picker = editor.getByTestId("def-template-picker");
    await expect(picker).toBeVisible();

    // Open the dropdown and pick Invoice.
    await picker.click();
    await page
      .getByRole("option", { name: /Invoice \(\d+ fields\)/ })
      .click();

    // After hydration, the document type input is "Invoice".
    await expect(editor.getByLabel("Document type")).toHaveValue("Invoice");

    // Cancel without saving — list of definitions in the dropdown shouldn't
    // include "Invoice" (since we never saved).
    await editor.getByRole("button", { name: "Cancel" }).click();
    await expect(editor).toBeHidden();
  });

  test("save after template-pick creates a new definition", async ({
    page,
  }) => {
    await page.goto("/");
    await waitForAppReady(page);

    await page.getByTestId("def-new-button").click();
    const editor = page.getByRole("dialog", { name: "New document class" });

    await editor.getByTestId("def-template-picker").click();
    await page.getByRole("option", { name: /Receipt \(\d+ fields\)/ }).click();
    await expect(editor.getByLabel("Document type")).toHaveValue("Receipt");

    // Rename so we don't collide with anyone else's seed.
    await editor.getByLabel("Document type").fill("Receipt E2E");
    await page.getByRole("button", { name: "Create" }).click();
    await expect(editor).toBeHidden();

    await expect(page.locator("#definition-selector")).toContainText(
      "Receipt E2E",
    );
  });
});
