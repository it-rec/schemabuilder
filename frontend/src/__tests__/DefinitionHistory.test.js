import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import DefinitionHistory from "../components/DefinitionHistory";
import * as api from "../services/api";

jest.mock("../services/api");

beforeEach(() => {
  api.fetchDefinitionVersions.mockReset();
  api.fetchDefinitionVersion.mockReset();
  api.fetchDefinition.mockReset();
  api.updateDefinition.mockReset();
  jest.spyOn(window, "confirm").mockImplementation(() => true);
});
afterEach(() => {
  jest.restoreAllMocks();
});

test("shows an empty-state message when no versions are archived", async () => {
  api.fetchDefinitionVersions.mockResolvedValue({ items: [] });
  api.fetchDefinition.mockResolvedValue({
    document: { document_type: "Inv", fields: [] },
  });

  render(
    <DefinitionHistory
      open
      definitionId="inv"
      onClose={() => {}}
      onRestored={() => {}}
    />,
  );
  expect(await screen.findByText(/No archived versions/i)).toBeInTheDocument();
});

test("clicking a version loads its content and renders a diff", async () => {
  api.fetchDefinitionVersions.mockResolvedValue({
    items: [{ id: "1700000000000-patch", timestamp_ms: 1700000000000, action: "patch", size: 100 }],
  });
  api.fetchDefinition.mockResolvedValue({
    document: { document_type: "Inv", fields: [{ name: "vendor", examples: ["ACME"] }] },
  });
  api.fetchDefinitionVersion.mockResolvedValue({
    document: { document_type: "Inv", fields: [{ name: "vendor", examples: ["OLD"] }] },
  });

  render(
    <DefinitionHistory
      open
      definitionId="inv"
      onClose={() => {}}
      onRestored={() => {}}
    />,
  );

  fireEvent.click(await screen.findByTestId("def-version-1700000000000-patch"));

  const diff = await screen.findByTestId("def-history-diff");
  // The diff text contains both the archived value (OLD) and the current
  // one (ACME); the unified-diff view renders both.
  expect(diff.textContent).toContain("OLD");
  expect(diff.textContent).toContain("ACME");
});

test("Restore prompts then PATCHes with the archived content", async () => {
  api.fetchDefinitionVersions.mockResolvedValue({
    items: [{ id: "1700000000000-patch", timestamp_ms: 1700000000000, action: "patch", size: 100 }],
  });
  api.fetchDefinition.mockResolvedValue({
    document: { document_type: "Inv", fields: [{ name: "vendor", examples: ["BROKEN"] }] },
  });
  const restored = {
    document: { document_type: "Inv", fields: [{ name: "vendor", examples: ["GOOD"] }] },
  };
  api.fetchDefinitionVersion.mockResolvedValue(restored);
  api.updateDefinition.mockResolvedValue({ id: "inv" });
  const onRestored = jest.fn();

  render(
    <DefinitionHistory
      open
      definitionId="inv"
      onClose={() => {}}
      onRestored={onRestored}
    />,
  );

  fireEvent.click(await screen.findByTestId("def-version-1700000000000-patch"));
  await screen.findByTestId("def-history-diff");
  fireEvent.click(screen.getByTestId("def-restore-button"));

  await waitFor(() =>
    expect(api.updateDefinition).toHaveBeenCalledWith("inv", restored),
  );
  expect(onRestored).toHaveBeenCalledWith("inv");
});

test("Restore is skipped when the confirm dialog is cancelled", async () => {
  window.confirm.mockImplementation(() => false);
  api.fetchDefinitionVersions.mockResolvedValue({
    items: [{ id: "1700000000000-patch", timestamp_ms: 1700000000000, action: "patch", size: 100 }],
  });
  api.fetchDefinition.mockResolvedValue({
    document: { document_type: "Inv", fields: [] },
  });
  api.fetchDefinitionVersion.mockResolvedValue({
    document: { document_type: "Inv", fields: [{ name: "x" }] },
  });

  render(
    <DefinitionHistory
      open
      definitionId="inv"
      onClose={() => {}}
      onRestored={() => {}}
    />,
  );
  fireEvent.click(await screen.findByTestId("def-version-1700000000000-patch"));
  await screen.findByTestId("def-history-diff");
  fireEvent.click(screen.getByTestId("def-restore-button"));

  expect(api.updateDefinition).not.toHaveBeenCalled();
});
