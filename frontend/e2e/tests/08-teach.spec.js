import { test, expect } from "@playwright/test";
import {
  resetBackendStateToSeed,
  waitForAppReady,
  selectSampleDocument,
  selectSeedDefinition,
  API_URL,
  SEED_DEF_ID,
} from "../helpers.js";

test.beforeEach(() => {
  resetBackendStateToSeed();
});

test.describe("Click-to-teach", () => {
  test("clicking a teach target opens the teach modal", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);
    await selectSampleDocument(page);
    await selectSeedDefinition(page);

    // Wait for the doc image to load so overlays are positioned.
    await page
      .locator("img.document-viewer__image")
      .first()
      .waitFor({ state: "visible" });

    // Wait for /extract so text_entries are in state and the overlays render.
    await page.waitForResponse(
      (r) => r.url().includes("/extract") && r.request().method() === "POST",
    );

    // Take the first teach-target overlay that materializes (sample.pdf is a
    // digital PDF; pypdfium2 always yields at least a handful of text spans).
    const target = page.locator('[data-testid^="teach-target-"]').first();
    await target.waitFor({ state: "visible" });
    await target.click();

    const modal = page.getByRole("dialog", { name: "Teach example" });
    await expect(modal).toBeVisible();
    await expect(modal.getByTestId("teach-value")).not.toBeEmpty();
  });

  test("teach modal lists the definition's fields as options", async ({
    page,
  }) => {
    await page.goto("/");
    await waitForAppReady(page);
    await selectSampleDocument(page);
    await selectSeedDefinition(page);

    await page
      .locator("img.document-viewer__image")
      .first()
      .waitFor({ state: "visible" });
    await page.waitForResponse(
      (r) => r.url().includes("/extract") && r.request().method() === "POST",
    );

    await page.locator('[data-testid^="teach-target-"]').first().click();
    const modal = page.getByRole("dialog", { name: "Teach example" });

    // Top-level scalars and dotted array sub-paths both appear.
    await expect(modal.getByLabel(/^title$/)).toBeVisible();
    await expect(modal.getByLabel(/^amount$/)).toBeVisible();
    await expect(modal.getByLabel(/line_items.*description/)).toBeVisible();
  });

  test("backend API: duplicate example returns 409", async ({ request }) => {
    // The teach modal converts this 409 into a notification; covering it via
    // the API is more stable than waiting for two extract round-trips.
    const r1 = await request.post(
      `${API_URL}/api/definitions/${SEED_DEF_ID}/fields/title/examples`,
      { data: { value: "Dup-Value-123" } },
    );
    expect(r1.ok()).toBeTruthy();

    const r2 = await request.post(
      `${API_URL}/api/definitions/${SEED_DEF_ID}/fields/title/examples`,
      { data: { value: "Dup-Value-123" } },
    );
    expect(r2.status()).toBe(409);
  });
});
