import { test, expect } from "@playwright/test";
import {
  resetBackendStateToSeed,
  waitForAppReady,
  selectSampleDocument,
  API_URL,
} from "../helpers.js";

test.beforeEach(() => {
  resetBackendStateToSeed();
});

test.describe("Fields panel", () => {
  test("empty state when no doc + no def offers the right copy", async ({
    page,
    request,
  }) => {
    // Delete the seed def so the panel has nothing matched.
    await request.delete(`${API_URL}/api/definitions/seed_definition`);
    await page.goto("/");
    await waitForAppReady(page);

    const panel = page.getByTestId("fields-panel");
    await expect(panel).toContainText(/Select a document|No definitions/);
  });

  test("auto-generate and create-blank CTAs render when a doc is selected", async ({
    page,
    request,
  }) => {
    // Delete the seed def so the panel falls into the "No definitions" branch.
    await request.delete(`${API_URL}/api/definitions/seed_definition`);
    await page.goto("/");
    await waitForAppReady(page);

    await selectSampleDocument(page);
    const panel = page.getByTestId("fields-panel");
    await expect(panel).toContainText(/No definitions yet/);
    await expect(panel.getByTestId("fields-panel-auto-generate")).toBeVisible();
    await expect(panel.getByTestId("fields-panel-create-blank")).toBeVisible();
  });

  test("Create blank CTA opens the definition editor", async ({
    page,
    request,
  }) => {
    await request.delete(`${API_URL}/api/definitions/seed_definition`);
    await page.goto("/");
    await waitForAppReady(page);
    await selectSampleDocument(page);

    await page.getByTestId("fields-panel-create-blank").click();
    await expect(
      page.getByRole("dialog", { name: "New document class" }),
    ).toBeVisible();
  });

  test("Auto-generate CTA opens the editor and surfaces the 503 error", async ({
    page,
    request,
  }) => {
    await request.delete(`${API_URL}/api/definitions/seed_definition`);
    await page.goto("/");
    await waitForAppReady(page);
    await selectSampleDocument(page);

    await page.getByTestId("fields-panel-auto-generate").click();
    const editor = page.getByRole("dialog", { name: "New document class" });
    await expect(editor).toBeVisible();
    // Without an ANTHROPIC_API_KEY, suggest-definition returns 503 — the
    // editor renders an error notification rather than fields.
    await expect(editor).toContainText(/ANTHROPIC_API_KEY|not configured/);
  });
});

test.describe("Batch button visibility", () => {
  test("Run-all button hidden until a definition is selected", async ({
    page,
    request,
  }) => {
    await request.delete(`${API_URL}/api/definitions/seed_definition`);
    await page.goto("/");
    await waitForAppReady(page);

    // No definition → no Run-all button.
    await expect(page.getByTestId("batch-run-button")).toHaveCount(0);
  });

  test("Run-all visible when a definition exists", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);
    await expect(page.getByTestId("batch-run-button")).toBeVisible();
  });
});
