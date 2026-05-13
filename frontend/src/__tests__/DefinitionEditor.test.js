import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import DefinitionEditor from "../components/DefinitionEditor";
import * as api from "../services/api";

vi.mock("../services/api");

// jsdom doesn't implement window.confirm — the delete path calls it, so we
// stub a default of `true` per-test (overridden in the "cancel delete" case).
beforeEach(() => {
  vi.spyOn(window, "confirm").mockImplementation(() => true);
  api.fetchDefinition.mockReset();
  api.uploadDefinition.mockReset();
  api.updateDefinition.mockReset();
  api.deleteDefinition.mockReset();
  // `fetchTemplates` is fired by the create-mode template picker effect; if
  // it stays the auto-mock (returns undefined) the .then() in the effect
  // throws synchronously and pollutes every create-mode test. Default to
  // an empty list so the picker just doesn't render.
  api.fetchTemplates.mockReset();
  api.fetchTemplates.mockResolvedValue([]);
  api.fetchTemplate.mockReset();
  api.fetchDefinitionCodegen.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

test("create flow: posts a new definition with the entered values", async () => {
  const user = userEvent.setup();
  api.uploadDefinition.mockResolvedValue({ id: "purchase_order", document_type: "Purchase Order", field_count: 1 });
  const onSaved = vi.fn();

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

test("edit flow: round-trips regex pattern; invalid pattern blocks save", async () => {
  const user = userEvent.setup();
  api.fetchDefinition.mockResolvedValue({
    document: {
      document_type: "Doc",
      fields: [{ name: "iban", pattern: "\\bDE\\d{20}\\b" }],
    },
  });
  api.updateDefinition.mockResolvedValue({ id: "doc" });

  render(
    <DefinitionEditor
      open
      mode="edit"
      definitionId="doc"
      onClose={() => {}}
      onSaved={() => {}}
      onDeleted={() => {}}
    />,
  );

  // Hydrated existing pattern is in the input.
  const input = await screen.findByLabelText(/Regex pattern/i);
  expect(input).toHaveValue("\\bDE\\d{20}\\b");

  // Type an invalid regex — Save button disables. Use fireEvent.change here
  // because userEvent.type parses "[" as a keyboard modifier.
  fireEvent.change(input, { target: { value: "[unclosed" } });
  const save = screen.getByRole("button", { name: /Save changes/i });
  expect(save).toBeDisabled();

  // Fix it and save.
  fireEvent.change(input, { target: { value: "\\d+" } });
  expect(save).not.toBeDisabled();
  await user.click(save);

  await waitFor(() => expect(api.updateDefinition).toHaveBeenCalledTimes(1));
  const [, payload] = api.updateDefinition.mock.calls[0];
  expect(payload.document.fields[0].pattern).toBe("\\d+");
});

test("edit flow: round-trips use_llm_fallback checkbox", async () => {
  const user = userEvent.setup();
  api.fetchDefinition.mockResolvedValue({
    document: {
      document_type: "Doc",
      fields: [{ name: "vendor", use_llm_fallback: false }],
    },
  });
  api.updateDefinition.mockResolvedValue({ id: "doc" });

  render(
    <DefinitionEditor
      open
      mode="edit"
      definitionId="doc"
      onClose={() => {}}
      onSaved={() => {}}
      onDeleted={() => {}}
    />,
  );

  const checkbox = await screen.findByLabelText(/LLM fallback/i);
  expect(checkbox).not.toBeChecked();
  await user.click(checkbox);
  await user.click(screen.getByRole("button", { name: /Save changes/i }));

  await waitFor(() => expect(api.updateDefinition).toHaveBeenCalledTimes(1));
  const [, payload] = api.updateDefinition.mock.calls[0];
  expect(payload.document.fields[0].use_llm_fallback).toBe(true);
});

test("edit flow: hydrates min_confidence + round-trips it as 0-1 on save", async () => {
  const user = userEvent.setup();
  api.fetchDefinition.mockResolvedValue({
    document: {
      document_type: "Invoice",
      fields: [
        { name: "vendor", min_confidence: 0.75 },
      ],
    },
  });
  api.updateDefinition.mockResolvedValue({ id: "invoice" });

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

  // 0.75 should hydrate to 75 in the UI.
  const input = await screen.findByLabelText(/Match threshold/i);
  expect(input).toHaveValue(75);

  await user.clear(input);
  await user.type(input, "90");
  await user.click(screen.getByRole("button", { name: /Save changes/i }));

  await waitFor(() => expect(api.updateDefinition).toHaveBeenCalledTimes(1));
  const [, payload] = api.updateDefinition.mock.calls[0];
  expect(payload.document.fields[0].min_confidence).toBe(0.9);
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
  const onSaved = vi.fn();

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
  const onDeleted = vi.fn();

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

test("create flow: template picker hydrates draft from the chosen template", async () => {
  const user = userEvent.setup();
  api.fetchTemplates.mockResolvedValue([
    { id: "invoice", document_type: "Invoice", field_count: 2 },
  ]);
  api.fetchTemplate.mockResolvedValue({
    id: "invoice",
    document: {
      document_type: "Invoice",
      document_description: "starter",
      fields: [{ name: "invoice_number" }, { name: "total" }],
    },
  });

  render(
    <DefinitionEditor
      open
      mode="create"
      onClose={() => {}}
      onSaved={() => {}}
      onDeleted={() => {}}
    />,
  );

  // Picker shows up after fetchTemplates resolves. Carbon Dropdown
  // renders multiple "Start from template" nodes (label + listbox
  // attrs) — use the combobox role to disambiguate.
  await screen.findByText("Start from template (optional)");
  const picker = screen.getByRole("combobox", { name: /template/i });
  await user.click(picker);
  await user.click(await screen.findByText(/Invoice \(2 fields\)/));

  await waitFor(() =>
    expect(api.fetchTemplate).toHaveBeenCalledWith("invoice"),
  );
  // Document type field is now hydrated from the template.
  expect(await screen.findByDisplayValue("Invoice")).toBeInTheDocument();
  expect(screen.getByDisplayValue("invoice_number")).toBeInTheDocument();
});

test("edit flow: round-trips normalizer keyword on save", async () => {
  const user = userEvent.setup();
  api.fetchDefinition.mockResolvedValue({
    document: {
      document_type: "Doc",
      fields: [{ name: "total", normalizer: "currency" }],
    },
  });
  api.updateDefinition.mockResolvedValue({ id: "doc" });

  render(
    <DefinitionEditor
      open
      mode="edit"
      definitionId="doc"
      onClose={() => {}}
      onSaved={() => {}}
      onDeleted={() => {}}
    />,
  );

  // Hydrated normalizer dropdown shows the keyword label.
  await screen.findByDisplayValue("total");
  await user.click(screen.getByRole("button", { name: /Save changes/i }));

  await waitFor(() => expect(api.updateDefinition).toHaveBeenCalledTimes(1));
  const [, payload] = api.updateDefinition.mock.calls[0];
  expect(payload.document.fields[0].normalizer).toBe("currency");
});

test("edit flow: parses visible_if 'field=value' into a condition object", async () => {
  const user = userEvent.setup();
  api.fetchDefinition.mockResolvedValue({
    document: {
      document_type: "Doc",
      fields: [
        { name: "method" },
        { name: "iban", visible_if: { field: "method", equals: "card" } },
      ],
    },
  });
  api.updateDefinition.mockResolvedValue({ id: "doc" });

  render(
    <DefinitionEditor
      open
      mode="edit"
      definitionId="doc"
      onClose={() => {}}
      onSaved={() => {}}
      onDeleted={() => {}}
    />,
  );

  // The condition is hydrated back into the "field=value" short form.
  const inputs = await screen.findAllByLabelText(/Visible if/i);
  // Two fields → two visible_if inputs; the iban one carries the value.
  const hydrated = inputs.find((el) => el.value === "method=card");
  expect(hydrated).toBeDefined();

  await user.click(screen.getByRole("button", { name: /Save changes/i }));
  await waitFor(() => expect(api.updateDefinition).toHaveBeenCalledTimes(1));
  const [, payload] = api.updateDefinition.mock.calls[0];
  expect(payload.document.fields[1].visible_if).toEqual({
    field: "method",
    equals: "card",
  });
});

test("edit flow: export menu downloads the chosen codegen artifact", async () => {
  const user = userEvent.setup();
  api.fetchDefinition.mockResolvedValue({
    document: { document_type: "Invoice", fields: [] },
  });
  const blob = new Blob(["export interface Invoice {}\n"], {
    type: "text/plain",
  });
  api.fetchDefinitionCodegen.mockResolvedValue({
    blob,
    filename: "invoice.ts",
  });
  // jsdom doesn't implement URL.createObjectURL / revokeObjectURL — stub
  // them so the download path runs and we can assert the blob was wired
  // to the anchor before revocation.
  const createObjectURL = vi.fn(() => "blob:invoice");
  const revokeObjectURL = vi.fn();
  vi.spyOn(URL, "createObjectURL").mockImplementation(createObjectURL);
  vi.spyOn(URL, "revokeObjectURL").mockImplementation(revokeObjectURL);
  const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click")
    .mockImplementation(() => {});

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
  // Open the overflow menu, then pick TypeScript.
  await user.click(screen.getByTestId("def-export-menu"));
  await user.click(screen.getByTestId("def-export-typescript"));

  await waitFor(() =>
    expect(api.fetchDefinitionCodegen).toHaveBeenCalledWith(
      "invoice",
      "typescript",
    ),
  );
  expect(createObjectURL).toHaveBeenCalledWith(blob);
  expect(anchorClick).toHaveBeenCalledTimes(1);
  expect(revokeObjectURL).toHaveBeenCalledWith("blob:invoice");
});
