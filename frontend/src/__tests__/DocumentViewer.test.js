import React from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import DocumentViewer from "../components/DocumentViewer";

jest.mock("../services/api", () => ({
  getPageImageUrl: (docId, page) =>
    `http://localhost:8000/api/documents/${docId}/pages/${page}`,
}));

const mockDocData = {
  filename: "sample.pdf",
  num_pages: 3,
  page_dimensions: { 1: { width: 612, height: 792 } },
  text_entries: [
    {
      id: 0,
      text: "Title Text",
      type: "TitleItem",
      page: 1,
      bbox: { l: 50, t: 700, r: 300, b: 680, coord_origin: "BOTTOMLEFT" },
    },
    { id: 1, text: "Body Text", type: "TextItem", page: 2, bbox: null },
  ],
};

test("shows loading state", () => {
  render(
    <DocumentViewer docId="abc" documentData={null} highlightedField={null} loading={true} />
  );
  expect(screen.getByText("Loading document...")).toBeInTheDocument();
});

test("shows empty state when no document selected", () => {
  render(
    <DocumentViewer docId={null} documentData={null} highlightedField={null} loading={false} />
  );
  expect(screen.getByText("Select a document to view.")).toBeInTheDocument();
});

test("displays page image and pagination", () => {
  render(
    <DocumentViewer
      docId="abc"
      documentData={mockDocData}
      highlightedField={null}
      loading={false}
    />
  );
  expect(screen.getByText("Page 1 of 3")).toBeInTheDocument();
  const img = screen.getByAltText(/page 1 of 3/i);
  expect(img).toHaveAttribute(
    "src",
    "http://localhost:8000/api/documents/abc/pages/1"
  );
});

test("renders without error when a field is highlighted", () => {
  const highlightedField = {
    name: "invoice_id",
    page: 1,
    matched_entry_id: 7,
    bbox: { l: 50, t: 700, r: 300, b: 680, coord_origin: "BOTTOMLEFT" },
  };
  render(
    <DocumentViewer
      docId="abc"
      documentData={mockDocData}
      highlightedField={highlightedField}
      loading={false}
    />
  );
  // Highlight won't render without image dimensions being set (needs onLoad)
  // But the component should still render without error
  expect(screen.getByText("Page 1 of 3")).toBeInTheDocument();
});

// jsdom doesn't run the actual image decode pipeline, so `img.naturalWidth`
// stays 0 and onLoad never fires. Tests that need the overlays to render must
// fire `load` manually and stub the natural/client dims that the handler reads
// off of `event.target`.
function loadImage(img, { natural, display } = {}) {
  Object.defineProperty(img, "naturalWidth", { value: natural?.w ?? 612 });
  Object.defineProperty(img, "naturalHeight", { value: natural?.h ?? 792 });
  Object.defineProperty(img, "clientWidth", { value: display?.w ?? 612 });
  Object.defineProperty(img, "clientHeight", { value: display?.h ?? 792 });
  fireEvent.load(img);
}

test("renders one overlay per matched field on current page", () => {
  const extractedFields = [
    {
      key: "field.invoice_id",
      label: "invoice id",
      matched_entry_id: 1,
      page: 1,
      bbox: { l: 50, t: 700, r: 300, b: 680, coord_origin: "BOTTOMLEFT" },
      field: { name: "invoice_id", matched_entry_id: 1 },
    },
    {
      key: "field.total",
      label: "total",
      matched_entry_id: 2,
      page: 1,
      bbox: { l: 400, t: 700, r: 550, b: 680, coord_origin: "BOTTOMLEFT" },
      field: { name: "total", matched_entry_id: 2 },
    },
    {
      key: "field.signature",
      label: "signature",
      matched_entry_id: 3,
      page: 2, // different page — must not render
      bbox: { l: 50, t: 100, r: 300, b: 80, coord_origin: "BOTTOMLEFT" },
      field: { name: "signature", matched_entry_id: 3 },
    },
  ];

  render(
    <DocumentViewer
      docId="abc"
      documentData={mockDocData}
      highlightedField={null}
      onHoverField={() => {}}
      extractedFields={extractedFields}
      loading={false}
    />,
  );

  loadImage(screen.getByAltText(/page 1 of 3/i));

  expect(screen.getByTestId("highlight-overlay-field.invoice_id")).toBeInTheDocument();
  expect(screen.getByTestId("highlight-overlay-field.total")).toBeInTheDocument();
  expect(screen.queryByTestId("highlight-overlay-field.signature")).not.toBeInTheDocument();
});

test("hovering an overlay calls onHoverField with the underlying field", () => {
  const field = { name: "invoice_id", matched_entry_id: 1 };
  const extractedFields = [
    {
      key: "field.invoice_id",
      label: "invoice id",
      matched_entry_id: 1,
      page: 1,
      bbox: { l: 50, t: 700, r: 300, b: 680, coord_origin: "BOTTOMLEFT" },
      field,
    },
  ];
  const onHoverField = jest.fn();

  render(
    <DocumentViewer
      docId="abc"
      documentData={mockDocData}
      highlightedField={null}
      onHoverField={onHoverField}
      extractedFields={extractedFields}
      loading={false}
    />,
  );
  loadImage(screen.getByAltText(/page 1 of 3/i));

  const overlay = screen.getByTestId("highlight-overlay-field.invoice_id");
  fireEvent.mouseEnter(overlay);
  expect(onHoverField).toHaveBeenLastCalledWith(field);
  fireEvent.mouseLeave(overlay);
  expect(onHoverField).toHaveBeenLastCalledWith(null);
});

test("active overlay carries the active class and label", () => {
  const field = { name: "invoice_id", matched_entry_id: 1 };
  const extractedFields = [
    {
      key: "field.invoice_id",
      label: "invoice id",
      matched_entry_id: 1,
      page: 1,
      bbox: { l: 50, t: 700, r: 300, b: 680, coord_origin: "BOTTOMLEFT" },
      field,
    },
  ];

  render(
    <DocumentViewer
      docId="abc"
      documentData={mockDocData}
      highlightedField={field}
      onHoverField={() => {}}
      extractedFields={extractedFields}
      loading={false}
    />,
  );
  loadImage(screen.getByAltText(/page 1 of 3/i));

  const active = screen.getByTestId("highlight-overlay");
  expect(active).toHaveClass("document-viewer__highlight--active");
  expect(active).toHaveTextContent("invoice id");
});
