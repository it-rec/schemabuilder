import { test, expect } from "@playwright/test";
import { resetBackendStateToSeed, waitForAppReady } from "../helpers.js";

test.beforeEach(() => {
  resetBackendStateToSeed();
});

test.describe("App load", () => {
  test("renders the header, panels, and seeded data", async ({ page }) => {
    await page.goto("/");
    await waitForAppReady(page);

    await expect(page.getByRole("banner")).toContainText("Schema Builder");

    // Three panels visible.
    await expect(page.getByTestId("document-list-panel")).toBeVisible();
    await expect(page.getByTestId("document-viewer-panel")).toBeVisible();
    await expect(page.getByTestId("fields-panel")).toBeVisible();

    // Seeded document appears.
    await expect(
      page.getByRole("button", { name: /Select sample\.pdf/ }),
    ).toBeVisible();

    // Seeded definition appears in the dropdown (auto-selected as the only
    // entry so the title is rendered in the trigger label).
    await expect(page.locator("#definition-selector")).toContainText(
      "Seed Definition",
    );
  });

  test("title is set", async ({ page }) => {
    await page.goto("/");
    await expect(page).toHaveTitle(/Schema Builder/);
  });

  test("theme toggle flips icon and persists to localStorage", async ({
    page,
  }) => {
    await page.goto("/");
    await waitForAppReady(page);

    const initial = await page.evaluate(() =>
      window.localStorage.getItem("schemabuilder.theme"),
    );
    const toggle = page.getByTestId("theme-toggle");
    await toggle.click();

    const after = await page.evaluate(() =>
      window.localStorage.getItem("schemabuilder.theme"),
    );
    expect(after).not.toBe(initial);
    expect(["g10", "g90"]).toContain(after);

    // Toggle again and confirm it flips back.
    await toggle.click();
    const after2 = await page.evaluate(() =>
      window.localStorage.getItem("schemabuilder.theme"),
    );
    expect(after2).toBe(initial || (after === "g90" ? "g10" : "g90"));
  });

  test("first /health probe completes successfully", async ({ page }) => {
    const healthResponse = page.waitForResponse(
      (res) => res.url().endsWith("/health") && res.status() === 200,
    );
    await page.goto("/");
    const res = await healthResponse;
    expect(res.ok()).toBeTruthy();
  });
});
