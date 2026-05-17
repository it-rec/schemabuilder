import { test, expect } from "@playwright/test";
import {
  resetBackendStateToSeed,
  waitForAppReady,
  selectSeedDefinition,
} from "../helpers.js";

test.beforeEach(() => {
  resetBackendStateToSeed();
});

test.describe("Definition CRUD", () => {
  test("seeded definition appears in dropdown", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);
    await expect(page.locator("#definition-selector")).toContainText(
      "Seed Definition",
    );
  });

  test("New button opens the create modal", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);
    await page.getByTestId("def-new-button").click();
    await expect(
      page.getByRole("dialog", { name: "New document class" }),
    ).toBeVisible();
  });

  test("Create with empty document type is blocked", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);
    await page.getByTestId("def-new-button").click();

    // "Create" button is disabled because the required field is empty.
    const save = page.getByRole("button", { name: "Create", exact: true });
    await expect(save).toBeDisabled();
  });

  test("Create and edit a definition end-to-end", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);

    await page.getByTestId("def-new-button").click();
    await page.getByLabel("Document type").fill("Custom Class");
    await page.getByLabel("Description").fill("Created in E2E");

    await page.getByRole("button", { name: "Create", exact: true }).click();
    // Modal closes, dropdown lists the new entry.
    await expect(
      page.getByRole("dialog", { name: "New document class" }),
    ).toBeHidden();
    await expect(page.locator("#definition-selector")).toContainText(
      "Custom Class",
    );

    // Edit it: open edit, change description, save.
    await page.getByTestId("def-edit-button").click();
    const editor = page.getByRole("dialog", { name: "Edit document class" });
    await expect(editor).toBeVisible();
    // Scope by id — per-field "Description" textareas would otherwise
    // trip strict mode in defs that have any fields.
    await editor.locator("#def-document-description").fill("Edited in E2E");
    await page.getByRole("button", { name: "Save changes" }).click();
    await expect(editor).toBeHidden();
  });

  test("Creating a duplicate definition surfaces a 409 message", async ({
    page,
  }) => {
    await page.goto("/");
    await waitForAppReady(page);

    // First create.
    await page.getByTestId("def-new-button").click();
    await page.getByLabel("Document type").fill("Dup Class");
    await page.getByRole("button", { name: "Create", exact: true }).click();
    await expect(
      page.getByRole("dialog", { name: "New document class" }),
    ).toBeHidden();

    // Second create with same name → server returns 409.
    await page.getByTestId("def-new-button").click();
    await page.getByLabel("Document type").fill("Dup Class");
    await page.getByRole("button", { name: "Create", exact: true }).click();
    await expect(
      page.getByRole("dialog", { name: "New document class" }),
    ).toContainText(/already exists/i);
  });

  test("Delete a definition removes it from the dropdown", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);

    // Create a throwaway so deleting won't leave the app definition-less
    // (the seed will still be there).
    await page.getByTestId("def-new-button").click();
    await page.getByLabel("Document type").fill("Delete Me");
    await page.getByRole("button", { name: "Create", exact: true }).click();
    await expect(page.locator("#definition-selector")).toContainText(
      "Delete Me",
    );

    // Open edit modal and delete.
    await page.getByTestId("def-edit-button").click();
    page.once("dialog", (d) => d.accept());
    // The trash IconButton in the document list has tooltip "Delete sample.pdf"
    // and tripped strict mode when scoped to the whole page. Scope to the
    // editor dialog to pick the danger button unambiguously.
    await page
      .getByRole("dialog", { name: "Edit document class" })
      .getByRole("button", { name: "Delete" })
      .click();

    // Wait for the modal to close, then check the dropdown no longer
    // contains "Delete Me".
    await expect(
      page.getByRole("dialog", { name: "Edit document class" }),
    ).toBeHidden();
    await expect(page.locator("#definition-selector")).not.toContainText(
      "Delete Me",
    );
  });

  test("Edit selects the right definition's data", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);
    await selectSeedDefinition(page);
    await page.getByTestId("def-edit-button").click();
    const editor = page.getByRole("dialog", { name: "Edit document class" });
    await expect(editor.getByLabel("Document type")).toHaveValue(
      "Seed Definition",
    );
  });
});
