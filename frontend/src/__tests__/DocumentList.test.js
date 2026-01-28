import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import DocumentList from "../components/DocumentList";

const mockDocs = [
  { id: "abc", filename: "report.pdf", extension: ".pdf", size: 5000 },
  { id: "def", filename: "design.docx", extension: ".docx", size: 12000 },
  { id: "ghi", filename: "slides.pptx", extension: ".pptx", size: 30000 },
];

test("renders all documents", () => {
  render(
    <DocumentList documents={mockDocs} selectedId={null} onSelect={() => {}} />
  );
  expect(screen.getByText("report.pdf")).toBeInTheDocument();
  expect(screen.getByText("design.docx")).toBeInTheDocument();
  expect(screen.getByText("slides.pptx")).toBeInTheDocument();
});

test("filters documents by search query", async () => {
  const user = userEvent.setup();
  render(
    <DocumentList documents={mockDocs} selectedId={null} onSelect={() => {}} />
  );
  const search = screen.getByPlaceholderText("Search documents...");
  await user.type(search, "report");
  expect(screen.getByText("report.pdf")).toBeInTheDocument();
  expect(screen.queryByText("design.docx")).not.toBeInTheDocument();
  expect(screen.queryByText("slides.pptx")).not.toBeInTheDocument();
});

test("calls onSelect when a document is clicked", () => {
  const onSelect = jest.fn();
  render(
    <DocumentList documents={mockDocs} selectedId={null} onSelect={onSelect} />
  );
  fireEvent.click(screen.getByText("design.docx"));
  expect(onSelect).toHaveBeenCalledWith("def");
});

test("shows selected state for active document", () => {
  render(
    <DocumentList documents={mockDocs} selectedId="abc" onSelect={() => {}} />
  );
  const row = screen.getByTestId("doc-row-abc");
  expect(row).toHaveClass("document-list__row--selected");
});

test("shows empty message when no documents match search", async () => {
  const user = userEvent.setup();
  render(
    <DocumentList documents={mockDocs} selectedId={null} onSelect={() => {}} />
  );
  const search = screen.getByPlaceholderText("Search documents...");
  await user.type(search, "nonexistent");
  expect(screen.getByText("No documents found.")).toBeInTheDocument();
});
