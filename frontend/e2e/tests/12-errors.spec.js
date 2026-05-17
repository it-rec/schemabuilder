import { test, expect } from "@playwright/test";
import {
  API_URL,
  resetBackendStateToSeed,
  waitForAppReady,
  SEED_DEF_ID,
  SEED_DOC_FILENAME,
  getDocumentIdByFilename,
} from "../helpers.js";

test.beforeEach(() => {
  resetBackendStateToSeed();
});

test.describe("Error paths via the API", () => {
  test("uploading an unsupported extension returns 400", async ({ request }) => {
    const res = await request.post(`${API_URL}/api/documents`, {
      multipart: {
        file: {
          name: "evil.txt",
          mimeType: "text/plain",
          buffer: Buffer.from("hello"),
        },
      },
    });
    expect(res.status()).toBe(400);
  });

  test("GETting an unknown document id returns 404", async ({ request }) => {
    const res = await request.get(
      `${API_URL}/api/documents/does-not-exist-12345`,
    );
    expect(res.status()).toBe(404);
  });

  test("suggest-definition without ANTHROPIC_API_KEY returns 503", async ({
    request,
  }) => {
    const docId = await getDocumentIdByFilename(request, SEED_DOC_FILENAME);
    const res = await request.post(
      `${API_URL}/api/documents/${docId}/suggest-definition`,
    );
    expect(res.status()).toBe(503);
    const body = await res.json();
    expect(JSON.stringify(body)).toMatch(/ANTHROPIC_API_KEY|not configured/);
  });

  test("PATCHing an unknown definition returns 404", async ({ request }) => {
    const res = await request.patch(
      `${API_URL}/api/definitions/does-not-exist`,
      {
        data: {
          document: { document_type: "Anything", fields: [] },
        },
      },
    );
    expect(res.status()).toBe(404);
  });

  test("Definition with empty document_type returns 422", async ({ request }) => {
    const res = await request.post(`${API_URL}/api/definitions`, {
      data: { document: { document_type: "", fields: [] } },
    });
    expect(res.status()).toBe(422);
  });

  test("Extraction against unknown doc id returns 404", async ({ request }) => {
    const res = await request.post(
      `${API_URL}/api/documents/does-not-exist/extract`,
      { data: { definition_id: SEED_DEF_ID } },
    );
    expect(res.status()).toBe(404);
  });

  test("Extraction against unknown def id returns 404", async ({ request }) => {
    const docId = await getDocumentIdByFilename(request, SEED_DOC_FILENAME);
    const res = await request.post(
      `${API_URL}/api/documents/${docId}/extract`,
      { data: { definition_id: "no-such-def" } },
    );
    expect(res.status()).toBe(404);
  });

  test("Batch with an empty doc list returns 422 or 400", async ({ request }) => {
    const res = await request.post(`${API_URL}/api/extract/batch`, {
      data: { document_ids: [], definition_id: SEED_DEF_ID },
    });
    // Pydantic min_items / app validation: either 400 or 422 is acceptable.
    expect([400, 422]).toContain(res.status());
  });

  test("Health endpoint returns 200 with status payload", async ({ request }) => {
    const res = await request.get(`${API_URL}/health`);
    expect(res.ok()).toBeTruthy();
    const body = await res.json();
    expect(body).toHaveProperty("status");
  });
});

test.describe("UI surfaces error states", () => {
  test("create-modal save failure shows the inline error", async ({
    page,
  }) => {
    // Intercept the create call and force a 500 response so the editor's
    // catch-block error notification renders.
    await page.route("**/api/definitions", (route) =>
      route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ detail: "Mocked failure" }),
      }),
    );

    await page.goto("/");
    await waitForAppReady(page);

    await page.getByTestId("def-new-button").click();
    await page.getByLabel("Document type").fill("Will Fail");
    await page.getByRole("button", { name: "Create", exact: true }).click();

    const dialog = page.getByRole("dialog", { name: "New document class" });
    await expect(dialog).toContainText(/Mocked failure|Save failed/);
  });
});
