import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import App from "../App";
import * as api from "../services/api";

// `findBy*` queries combine retry + assert into one await and are what the
// testing-library lint plugin prefers over `await waitFor(() => getBy...)`.
// `waitFor` is reserved below for assertions that aren't element lookups
// (e.g. checking that a mocked API function was called).

jest.mock("../services/api");

const mockDocs = [
  { id: "abc123", filename: "sample.pdf", extension: ".pdf", size: 1024 },
  { id: "def456", filename: "sample.docx", extension: ".docx", size: 2048 },
];

const mockDocData = {
  id: "abc123",
  filename: "sample.pdf",
  num_pages: 1,
  page_dimensions: {},
  text_entries: [
    { id: 0, text: "Hello World", type: "TextItem", page: 1, bbox: null },
  ],
};

const mockDefinitions = [
  { id: "invoice", document_type: "Invoice", document_description: "An invoice.", field_count: 3 },
];

const mockExtraction = {
  document_id: "abc123",
  definition_id: "invoice",
  document_type: "Invoice",
  document_description: "An invoice.",
  fields: [
    { name: "invoice_id", description: "Invoice number", extracted_value: null, confidence: 0, matched_entry_id: null, page: null, bbox: null, examples: [] },
  ],
};

beforeEach(() => {
  api.fetchDocuments.mockResolvedValue(mockDocs);
  api.fetchDocument.mockResolvedValue(mockDocData);
  api.fetchDefinitions.mockResolvedValue(mockDefinitions);
  api.extractFields.mockResolvedValue(mockExtraction);
  api.getPageImageUrl.mockReturnValue("http://localhost:8000/api/documents/abc123/pages/1");
});

test("renders three panels", async () => {
  render(<App />);
  expect(await screen.findByTestId("document-list-panel")).toBeInTheDocument();
  expect(await screen.findByTestId("document-viewer-panel")).toBeInTheDocument();
  expect(await screen.findByTestId("fields-panel")).toBeInTheDocument();
});

test("loads and displays document list", async () => {
  render(<App />);
  expect(await screen.findByText("sample.pdf")).toBeInTheDocument();
  expect(await screen.findByText("sample.docx")).toBeInTheDocument();
});

test("fetches first document on load", async () => {
  render(<App />);
  // App now passes an AbortSignal-bearing options object as the second arg.
  await waitFor(() =>
    expect(api.fetchDocument).toHaveBeenCalledWith("abc123", expect.any(Object)),
  );
});

test("loads definitions and triggers extraction", async () => {
  render(<App />);
  await waitFor(() => expect(api.fetchDefinitions).toHaveBeenCalled());
  await waitFor(() =>
    expect(api.extractFields).toHaveBeenCalledWith(
      "abc123",
      "invoice",
      expect.any(Object),
    ),
  );
});

test("displays definition selector", async () => {
  render(<App />);
  expect(await screen.findByText("Document class")).toBeInTheDocument();
});

test("j / k navigate to the next / previous document", async () => {
  render(<App />);
  // Wait for the docs to load — the first doc is auto-selected.
  await screen.findByText("sample.pdf");

  // j → next document (sample.docx)
  fireEvent.keyDown(window, { key: "j" });
  await waitFor(() =>
    expect(api.fetchDocument).toHaveBeenCalledWith("def456", expect.any(Object)),
  );

  // k → back to the first
  fireEvent.keyDown(window, { key: "k" });
  await waitFor(() => {
    const calls = api.fetchDocument.mock.calls.map((c) => c[0]);
    expect(calls).toContain("abc123");
  });
});

test("keyboard nav is suppressed when typing in an input", async () => {
  render(<App />);
  await screen.findByText("sample.pdf");
  api.fetchDocument.mockClear();

  const search = screen.getByPlaceholderText(/Search documents/i);
  search.focus();
  fireEvent.keyDown(search, { key: "j" });
  // No additional fetchDocument call because the keydown was on an INPUT.
  expect(api.fetchDocument).not.toHaveBeenCalled();
});

test("theme toggle flips data attribute / updates label", async () => {
  render(<App />);
  const toggle = await screen.findByTestId("theme-toggle");
  expect(toggle).toHaveAttribute("aria-label", expect.stringMatching(/dark mode/i));
  fireEvent.click(toggle);
  expect(toggle).toHaveAttribute("aria-label", expect.stringMatching(/light mode/i));
});

test("export menu triggers JSON download via api.exportTablesJson", async () => {
  api.extractFields.mockResolvedValue({
    ...mockExtraction,
    target_tables: ["Invoice"],
  });
  api.exportTablesJson = jest.fn().mockResolvedValue({
    document_id: "abc123",
    definition_id: "invoice",
    tables: { Invoice: [{ doc_id: "abc123", invoice_id: "INV-001" }] },
  });

  render(<App />);
  // Wait until extraction has resolved and the export menu trigger has
  // mounted in the panel.
  const trigger = await screen.findByRole("button", { name: /Export options/i });
  fireEvent.click(trigger);
  fireEvent.click(await screen.findByText(/Download all tables \(JSON\)/i));

  await waitFor(() =>
    expect(api.exportTablesJson).toHaveBeenCalledWith("abc123", "invoice"),
  );
});
