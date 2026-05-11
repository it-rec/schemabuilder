import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import DefinitionEditor from "../components/DefinitionEditor";
import * as api from "../services/api";

jest.mock("../services/api");

// jsdom doesn't implement window.confirm — the delete path calls it, so we
// stub a default of `true` per-test (overridden in the "cancel delete" case).
beforeEach(() => {
  jest.spyOn(window, "confirm").mockImplementation(() => true);
  api.fetchDefinition.mockReset();
  api.uploadDefinition.mockReset();
  api.updateDefinition.mockReset();
  api.deleteDefinition.mockReset();
});

afterEach(() => {
  jest.restoreAllMocks();
});

test("create flow: posts a new definition with the entered values", async () => {
  const user = userEvent.setup();
  api.uploadDefinition.mockResolvedValue({ id: "purchase_order", document_type: "Purchase Order", field_count: 1 });
  const onSaved = jest.fn();

  render(
    <DefinitionEditor
      open
      mode="create"
      onClose={() => {}}
      onSaved={onSaved}
      onDeleted={() => {}}
    />,
  );

  const typeInput = await screen.findByLabelText(/Document type/i);
  await user.type(typeInput, "Purchase Order");

  await user.click(screen.getByRole("button", { name: /Add field/i }));
  const nameInput = await screen.findByLabelText(/^Name$/i);
  await user.type(nameInput, "po_number");

  await user.click(screen.getByRole("button", { name: /Create/i }));

  await waitFor(() => expect(api.uploadDefinition).toHaveBeenCalledTimes(1));
  const payload = api.uploadDefinition.mock.calls[0][0];
  expect(payload.document.document_type).toBe("Purchase Order");
  expect(payload.document.fields).toEqual([{ name: "po_number" }]);
  expect(onSaved).toHaveBeenCalled();
});

test("create flow: blocks save when document type is empty", async () => {
  const user = userEvent.setup();
  render(
    <DefinitionEditor
      open
      mode="create"
      onClose={() => {}}
      onSaved={() => {}}
      onDeleted={() => {}}
    />,
  );

  const createButton = await screen.findByRole("button", { name: /Create/i });
  expect(createButton).toBeDisabled();
  expect(api.uploadDefinition).not.toHaveBeenCalled();

  await user.type(screen.getByLabelText(/Document type/i), "Receipt");
  await waitFor(() => expect(createButton).not.toBeDisabled());
});

test("edit flow: hydrates from fetched definition and preserves extras on save", async () => {
  const user = userEvent.setup();
  const remote = {
    document: {
      document_type: "Invoice",
      document_description: "An invoice.",
      fields: [
        { name: "invoice_id", description: "ID", examples: ["INV-001"] },
      ],
    },
    target_tables: [{ name: "Invoice", columns: [] }],
  };
  api.fetchDefinition.mockResolvedValue(remote);
  api.updateDefinition.mockResolvedValue({ id: "invoice", document_type: "Invoice", field_count: 1 });
  const onSaved = jest.fn();

  render(
    <DefinitionEditor
      open
      mode="edit"
      definitionId="invoice"
      onClose={() => {}}
      onSaved={onSaved}
      onDeleted={() => {}}
    />,
  );

  expect(await screen.findByDisplayValue("Invoice")).toBeInTheDocument();
  expect(screen.getByDisplayValue("An invoice.")).toBeInTheDocument();
  expect(screen.getByDisplayValue("invoice_id")).toBeInTheDocument();

  // Multiple "Description" labels exist (document + each field); target the
  // document-level one via its unique current value.
  const descInput = screen.getByDisplayValue("An invoice.");
  await user.clear(descInput);
  await user.type(descInput, "Updated description.");

  await user.click(screen.getByRole("button", { name: /Save changes/i }));

  await waitFor(() => expect(api.updateDefinition).toHaveBeenCalledTimes(1));
  const [defId, payload] = api.updateDefinition.mock.calls[0];
  expect(defId).toBe("invoice");
  expect(payload.document.document_description).toBe("Updated description.");
  // The editor must round-trip extras (target_tables, etc.) untouched —
  // otherwise editing the field schema destroys the downstream mapping.
  expect(payload.target_tables).toEqual(remote.target_tables);
  expect(onSaved).toHaveBeenCalled();
});

test("edit flow: delete calls API after confirmation", async () => {
  const user = userEvent.setup();
  api.fetchDefinition.mockResolvedValue({
    document: { document_type: "Invoice", fields: [] },
  });
  api.deleteDefinition.mockResolvedValue({ id: "invoice" });
  const onDeleted = jest.fn();

  render(
    <DefinitionEditor
      open
      mode="edit"
      definitionId="invoice"
      onClose={() => {}}
      onSaved={() => {}}
      onDeleted={onDeleted}
    />,
  );

  await screen.findByDisplayValue("Invoice");
  await user.click(screen.getByRole("button", { name: /danger Delete/i }));
  await waitFor(() => expect(api.deleteDefinition).toHaveBeenCalledWith("invoice"));
  expect(onDeleted).toHaveBeenCalledWith("invoice");
});

test("edit flow: cancelling the confirm dialog skips deletion", async () => {
  const user = userEvent.setup();
  window.confirm.mockImplementation(() => false);
  api.fetchDefinition.mockResolvedValue({
    document: { document_type: "Invoice", fields: [] },
  });

  render(
    <DefinitionEditor
      open
      mode="edit"
      definitionId="invoice"
      onClose={() => {}}
      onSaved={() => {}}
      onDeleted={() => {}}
    />,
  );

  await screen.findByDisplayValue("Invoice");
  await user.click(screen.getByRole("button", { name: /danger Delete/i }));
  expect(api.deleteDefinition).not.toHaveBeenCalled();
});
