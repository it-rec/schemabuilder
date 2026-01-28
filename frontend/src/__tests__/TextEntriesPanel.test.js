import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import TextEntriesPanel from "../components/TextEntriesPanel";

const mockEntries = [
  { id: 0, text: "Executive Summary", type: "SectionHeaderItem", page: 1, bbox: null },
  { id: 1, text: "This is a paragraph of text.", type: "TextItem", page: 1, bbox: null },
  { id: 2, text: "Revenue Table", type: "TableItem", page: 1, bbox: null },
];

test("renders all text entries", () => {
  render(
    <TextEntriesPanel entries={mockEntries} onHoverEntry={() => {}} loading={false} />
  );
  expect(screen.getByText("Executive Summary")).toBeInTheDocument();
  expect(screen.getByText("This is a paragraph of text.")).toBeInTheDocument();
  expect(screen.getByText("Revenue Table")).toBeInTheDocument();
});

test("shows entry count in title", () => {
  render(
    <TextEntriesPanel entries={mockEntries} onHoverEntry={() => {}} loading={false} />
  );
  expect(screen.getByText("Text Entries (3)")).toBeInTheDocument();
});

test("calls onHoverEntry with entry id on mouse enter", () => {
  const onHover = jest.fn();
  render(
    <TextEntriesPanel entries={mockEntries} onHoverEntry={onHover} loading={false} />
  );
  fireEvent.mouseEnter(screen.getByTestId("text-entry-0"));
  expect(onHover).toHaveBeenCalledWith(0);
});

test("calls onHoverEntry with null on mouse leave", () => {
  const onHover = jest.fn();
  render(
    <TextEntriesPanel entries={mockEntries} onHoverEntry={onHover} loading={false} />
  );
  fireEvent.mouseLeave(screen.getByTestId("text-entry-0"));
  expect(onHover).toHaveBeenCalledWith(null);
});

test("shows loading message when loading", () => {
  render(
    <TextEntriesPanel entries={null} onHoverEntry={() => {}} loading={true} />
  );
  expect(screen.getByText("Processing document...")).toBeInTheDocument();
});

test("shows empty state when no entries", () => {
  render(
    <TextEntriesPanel entries={[]} onHoverEntry={() => {}} loading={false} />
  );
  expect(screen.getByText("No text entries found.")).toBeInTheDocument();
});

test("displays type tags for entries", () => {
  render(
    <TextEntriesPanel entries={mockEntries} onHoverEntry={() => {}} loading={false} />
  );
  expect(screen.getByText("SectionHeader")).toBeInTheDocument();
  expect(screen.getByText("Text")).toBeInTheDocument();
  expect(screen.getByText("Table")).toBeInTheDocument();
});

test("displays page numbers", () => {
  render(
    <TextEntriesPanel entries={mockEntries} onHoverEntry={() => {}} loading={false} />
  );
  const pageBadges = screen.getAllByText("p.1");
  expect(pageBadges.length).toBe(3);
});
