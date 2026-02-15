import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import App from "../App";
import * as api from "../services/api";

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

beforeEach(() => {
  api.fetchDocuments.mockResolvedValue(mockDocs);
  api.fetchDocument.mockResolvedValue(mockDocData);
  api.getPageImageUrl.mockReturnValue("http://localhost:8000/api/documents/abc123/pages/1");
});

test("renders three panels", async () => {
  render(<App />);
  await waitFor(() => {
    expect(screen.getByTestId("document-list-panel")).toBeInTheDocument();
    expect(screen.getByTestId("document-viewer-panel")).toBeInTheDocument();
    expect(screen.getByTestId("text-entries-panel")).toBeInTheDocument();
  });
});

test("loads and displays document list", async () => {
  render(<App />);
  await waitFor(() => {
    expect(screen.getByText("sample.pdf")).toBeInTheDocument();
    expect(screen.getByText("sample.docx")).toBeInTheDocument();
  });
});

test("fetches first document on load", async () => {
  render(<App />);
  await waitFor(() => {
    expect(api.fetchDocument).toHaveBeenCalledWith("abc123", expect.any(AbortSignal));
  });
});
