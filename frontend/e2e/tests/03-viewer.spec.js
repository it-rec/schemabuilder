import { test, expect } from "@playwright/test";
import {
  resetBackendStateToSeed,
  waitForAppReady,
  selectSampleDocument,
} from "../helpers.js";

test.beforeEach(() => {
  resetBackendStateToSeed();
});

test.describe("Document viewer", () => {
  test("shows the page image and Page 1 of N label", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);
    await selectSampleDocument(page);

    const viewer = page.getByTestId("document-viewer-panel");
    await expect(viewer).toContainText(/Page 1 of \d+/);
    // The image src points at the API page endpoint.
    const img = viewer.locator("img.document-viewer__image");
    await expect(img).toHaveAttribute("src", /\/api\/documents\/.+\/pages\/1$/);
  });

  test("prev disabled on page 1; next enabled when multi-page", async ({
    page,
  }) => {
    await page.goto("/");
    await waitForAppReady(page);
    await selectSampleDocument(page);

    const prev = page.getByRole("button", { name: "Previous page" });
    const next = page.getByRole("button", { name: "Next page" });
    await expect(prev).toBeDisabled();
    // Sample is 2 pages — next is enabled.
    await expect(next).toBeEnabled();
  });

  test("clicking next advances pages; clicking prev returns", async ({
    page,
  }) => {
    await page.goto("/");
    await waitForAppReady(page);
    await selectSampleDocument(page);

    const viewer = page.getByTestId("document-viewer-panel");
    await page.getByRole("button", { name: "Next page" }).click();
    await expect(viewer).toContainText("Page 2 of 2");
    await page.getByRole("button", { name: "Previous page" }).click();
    await expect(viewer).toContainText("Page 1 of 2");
  });

  test("ArrowRight / ArrowLeft keys page through", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);
    await selectSampleDocument(page);

    const viewer = page.getByTestId("document-viewer-panel");
    await page.locator("body").focus();
    await page.keyboard.press("ArrowRight");
    await expect(viewer).toContainText("Page 2 of 2");
    await page.keyboard.press("ArrowLeft");
    await expect(viewer).toContainText("Page 1 of 2");
  });

  test("empty state when no document is available", async ({ page, request }) => {
    // Drop the seeded sample so the list (and viewer) start empty.
    const baseUrl = process.env.E2E_API_URL || "http://127.0.0.1:8765";
    const list = await request.get(`${baseUrl}/api/documents`);
    const body = await list.json();
    const items = Array.isArray(body) ? body : body.items;
    for (const d of items) {
      await request.delete(`${baseUrl}/api/documents/${d.id}`);
    }

    await page.goto("/");
    await waitForAppReady(page);
    await expect(
      page.getByTestId("document-viewer-panel"),
    ).toContainText(/Select a document to view\./);
  });
});
