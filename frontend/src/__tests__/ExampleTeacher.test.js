import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ExampleTeacher from "../components/ExampleTeacher";
import * as api from "../services/api";

jest.mock("../services/api");

beforeEach(() => {
  api.addFieldExample.mockReset();
});

const baseExtraction = {
  document_type: "Invoice",
  fields: [
    {
      name: "invoice_id",
      examples: ["INV-001"],
      extracted_value: "INV-001",
      matched_entry_id: 7,
    },
    {
      name: "customer_name",
      examples: [],
      extracted_value: null,
      matched_entry_id: null,
    },
    {
      name: "line_items",
      type: "array",
      fields: [{ name: "amount", examples: ["1.00"] }],
    },
  ],
};

test("defaults the selection to the first unmatched field", async () => {
  const user = userEvent.setup();
  api.addFieldExample.mockResolvedValue({ id: "x", field: "customer_name", examples: ["ACME"] });
  const onSaved = jest.fn();

  render(
    <ExampleTeacher
      open
      entry={{ id: 12, text: "ACME", page: 1 }}
      definitionId="invoice"
      extraction={baseExtraction}
      onClose={() => {}}
      onSaved={onSaved}
    />,
  );

  // customer_name has no match → should be preselected.
  expect(screen.getByDisplayValue("customer_name")).toBeChecked();
  await user.click(screen.getByRole("button", { name: /Add example/i }));
  await waitFor(() =>
    expect(api.addFieldExample).toHaveBeenCalledWith("invoice", "customer_name", "ACME"),
  );
  expect(onSaved).toHaveBeenCalled();
});

test("renders array sub-fields with dotted paths", () => {
  render(
    <ExampleTeacher
      open
      entry={{ id: 1, text: "999.00", page: 1 }}
      definitionId="invoice"
      extraction={baseExtraction}
      onClose={() => {}}
      onSaved={() => {}}
    />,
  );
  // Dotted-path option exposed.
  expect(screen.getByDisplayValue("line_items.amount")).toBeInTheDocument();
});

test("surfaces a save error inline without closing", async () => {
  const user = userEvent.setup();
  api.addFieldExample.mockRejectedValue(new Error("Example 'ACME' already exists"));
  const onSaved = jest.fn();

  render(
    <ExampleTeacher
      open
      entry={{ id: 12, text: "ACME", page: 1 }}
      definitionId="invoice"
      extraction={baseExtraction}
      onClose={() => {}}
      onSaved={onSaved}
    />,
  );

  await user.click(screen.getByRole("button", { name: /Add example/i }));
  expect(await screen.findByText(/already exists/)).toBeInTheDocument();
  expect(onSaved).not.toHaveBeenCalled();
});

test("user can switch the field selection before saving", async () => {
  const user = userEvent.setup();
  api.addFieldExample.mockResolvedValue({});

  render(
    <ExampleTeacher
      open
      entry={{ id: 1, text: "XYZ", page: 1 }}
      definitionId="invoice"
      extraction={baseExtraction}
      onClose={() => {}}
      onSaved={() => {}}
    />,
  );

  await user.click(screen.getByLabelText(/invoice id/i));
  await user.click(screen.getByRole("button", { name: /Add example/i }));
  expect(api.addFieldExample).toHaveBeenCalledWith("invoice", "invoice_id", "XYZ");
});
