import { test, expect } from "@playwright/test";
import { resetBackendStateToSeed } from "../helpers.js";

test.beforeEach(() => {
  resetBackendStateToSeed();
});

test.describe("Offline overlay", () => {
  test("shows the offline overlay when /health is unreachable", async ({
    page,
  }) => {
    // Intercept the health probe and fail it. The hook treats network errors
    // the same as a non-2xx — flips online to false → overlay mounts.
    await page.route("**/health", (route) => route.abort("failed"));
    await page.goto("/");

    const overlay = page.getByTestId("offline-overlay");
    await expect(overlay).toBeVisible();
    await expect(overlay).toContainText(/You're offline|Connecting/);
  });

  test("removing the route handler lets the app recover", async ({ page }) => {
    await page.route("**/health", (route) => route.abort("failed"));
    await page.goto("/");
    await expect(page.getByTestId("offline-overlay")).toBeVisible();

    // Stop intercepting — next 3s poll succeeds.
    await page.unroute("**/health");
    await expect(page.getByTestId("offline-overlay")).toBeHidden({
      timeout: 15_000,
    });
    await expect(page.getByTestId("document-list-panel")).toBeVisible();
  });

  test("app shell is marked inert while offline", async ({ page }) => {
    await page.route("**/health", (route) => route.abort("failed"));
    await page.goto("/");
    await expect(page.getByTestId("offline-overlay")).toBeVisible();

    // The app-shell wrapper carries inert when offline; the testids inside it
    // should reflect the same. Asserting on the attribute on the wrapper is
    // enough — its DOM presence is the contract.
    const hasInert = await page.evaluate(
      () => document.querySelector(".app-shell")?.hasAttribute("inert"),
    );
    expect(hasInert).toBe(true);
  });
});
