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

test("upload button is hidden when onUpload is not provided", () => {
  render(
    <DocumentList documents={mockDocs} selectedId={null} onSelect={() => {}} />,
  );
  expect(screen.queryByTestId("upload-button")).not.toBeInTheDocument();
});

test("upload button forwards picked files to onUpload", () => {
  const onUpload = jest.fn();
  render(
    <DocumentList
      documents={mockDocs}
      selectedId={null}
      onSelect={() => {}}
      onUpload={onUpload}
    />,
  );

  const input = screen.getByTestId("upload-input");
  const fileA = new File([new Uint8Array(8)], "a.pdf", { type: "application/pdf" });
  const fileB = new File([new Uint8Array(4)], "b.docx", {
    type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  });
  fireEvent.change(input, { target: { files: [fileA, fileB] } });

  expect(onUpload).toHaveBeenCalledTimes(1);
  expect(onUpload.mock.calls[0][0].map((f) => f.name)).toEqual(["a.pdf", "b.docx"]);
});

test("delete icon prompts then calls onDelete with the doc", () => {
  jest.spyOn(window, "confirm").mockImplementation(() => true);
  const onDelete = jest.fn();
  const onSelect = jest.fn();
  render(
    <DocumentList
      documents={mockDocs}
      selectedId={null}
      onSelect={onSelect}
      onDelete={onDelete}
    />,
  );
  fireEvent.click(screen.getByTestId("doc-delete-abc"));
  expect(window.confirm).toHaveBeenCalled();
  expect(onDelete).toHaveBeenCalledWith(mockDocs[0]);
  // Click on the trash button must NOT bubble up to the row's onSelect.
  expect(onSelect).not.toHaveBeenCalled();
  window.confirm.mockRestore();
});

test("Run all forwards the visible documents to onRunBatch", async () => {
  const user = userEvent.setup();
  const onRunBatch = jest.fn();
  render(
    <DocumentList
      documents={mockDocs}
      selectedId={null}
      onSelect={() => {}}
      onRunBatch={onRunBatch}
    />,
  );
  await user.click(screen.getByTestId("batch-run-button"));
  expect(onRunBatch).toHaveBeenCalledWith(mockDocs);
});

test("Run all is hidden when onRunBatch is not provided", () => {
  render(
    <DocumentList documents={mockDocs} selectedId={null} onSelect={() => {}} />,
  );
  expect(screen.queryByTestId("batch-run-button")).not.toBeInTheDocument();
});

test("delete is skipped when the confirm dialog is cancelled", () => {
  jest.spyOn(window, "confirm").mockImplementation(() => false);
  const onDelete = jest.fn();
  render(
    <DocumentList
      documents={mockDocs}
      selectedId={null}
      onSelect={() => {}}
      onDelete={onDelete}
    />,
  );
  fireEvent.click(screen.getByTestId("doc-delete-abc"));
  expect(onDelete).not.toHaveBeenCalled();
  window.confirm.mockRestore();
});
