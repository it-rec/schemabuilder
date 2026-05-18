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

  test("app shell is not interactive while offline", async ({ page }) => {
    await page.route("**/health", (route) => route.abort("failed"));
    await page.goto("/");
    await expect(page.getByTestId("offline-overlay")).toBeVisible();

    // The contract is that interaction inside the app shell is blocked
    // while the overlay is up. React 19 renders <div inert={""}> as
    // <div inert=""> in the DOM; the overlay itself is rendered outside
    // the inert subtree so its retry indicator stays operable. Assert
    // both halves of the contract: the inert attribute is on the shell
    // and the overlay is reachable.
    const shellInert = await page
      .locator(".app-shell")
      .first()
      .evaluate((el) => el.hasAttribute("inert"));
    expect(shellInert).toBe(true);
    await expect(page.getByTestId("offline-overlay-loading")).toBeVisible();
  });
});
