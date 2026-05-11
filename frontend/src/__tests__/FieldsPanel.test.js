import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import FieldsPanel from "../components/FieldsPanel";

const mockExtraction = {
  document_type: "Invoice",
  document_description: "An invoice document.",
  fields: [
    {
      name: "invoice_id",
      description: "unique identifier",
      extracted_value: "INV-2024-001",
      confidence: 0.9,
      matched_entry_id: 1,
      page: 1,
      bbox: { l: 10, t: 20, r: 100, b: 30 },
      examples: ["INV-2024-001"],
    },
    {
      name: "invoice_date",
      description: "the invoice creation date",
      extracted_value: null,
      confidence: 0,
      matched_entry_id: null,
      page: null,
      bbox: null,
      examples: ["2024-02-04"],
    },
    {
      name: "line_items",
      type: "array",
      description: "Information about each product or service.",
      extracted_value: null,
      confidence: 0,
      matched_entry_id: null,
      page: null,
      bbox: null,
      examples: [],
      fields: [{ name: "amount", description: "total amount" }],
      items: [],
    },
  ],
};

test("renders document type as title", () => {
  render(
    <FieldsPanel extraction={mockExtraction} onHoverField={() => {}} loading={false} />
  );
  expect(screen.getByText("Invoice")).toBeInTheDocument();
});

test("shows field count badge", () => {
  render(
    <FieldsPanel extraction={mockExtraction} onHoverField={() => {}} loading={false} />
  );
  expect(screen.getByText("1/3 found")).toBeInTheDocument();
});

test("renders all fields", () => {
  render(
    <FieldsPanel extraction={mockExtraction} onHoverField={() => {}} loading={false} />
  );
  expect(screen.getByText("invoice id")).toBeInTheDocument();
  expect(screen.getByText("invoice date")).toBeInTheDocument();
  expect(screen.getByText("line items")).toBeInTheDocument();
});

test("shows extracted value for matched fields", () => {
  render(
    <FieldsPanel extraction={mockExtraction} onHoverField={() => {}} loading={false} />
  );
  expect(screen.getByText("INV-2024-001")).toBeInTheDocument();
});

test("shows Not found for unmatched fields", () => {
  render(
    <FieldsPanel extraction={mockExtraction} onHoverField={() => {}} loading={false} />
  );
  const notFoundElements = screen.getAllByText("Not found");
  expect(notFoundElements.length).toBeGreaterThanOrEqual(1);
});

test("calls onHoverField on mouse enter for matched field", () => {
  const onHover = jest.fn();
  render(
    <FieldsPanel extraction={mockExtraction} onHoverField={onHover} loading={false} />
  );
  fireEvent.mouseEnter(screen.getByTestId("field-invoice_id"));
  expect(onHover).toHaveBeenCalledWith(mockExtraction.fields[0]);
});

test("calls onHoverField with null on mouse leave", () => {
  const onHover = jest.fn();
  render(
    <FieldsPanel extraction={mockExtraction} onHoverField={onHover} loading={false} />
  );
  fireEvent.mouseLeave(screen.getByTestId("field-invoice_id"));
  expect(onHover).toHaveBeenCalledWith(null);
});

test("shows loading message when loading", () => {
  render(
    <FieldsPanel extraction={null} onHoverField={() => {}} loading={true} />
  );
  expect(screen.getByText("Extracting fields...")).toBeInTheDocument();
});

test("shows empty state when no extraction", () => {
  render(
    <FieldsPanel extraction={null} onHoverField={() => {}} loading={false} />
  );
  expect(screen.getByText("Select a document definition to extract fields.")).toBeInTheDocument();
});

test("shows page badge for matched field", () => {
  render(
    <FieldsPanel extraction={mockExtraction} onHoverField={() => {}} loading={false} />
  );
  expect(screen.getByText("p.1")).toBeInTheDocument();
});

test("applies highlighted class to row matching highlightedField", () => {
  render(
    <FieldsPanel
      extraction={mockExtraction}
      onHoverField={() => {}}
      highlightedField={mockExtraction.fields[0]}
      loading={false}
    />,
  );
  const row = screen.getByTestId("field-invoice_id");
  expect(row).toHaveClass("fields-panel__field-header--highlighted");
});

test("no highlighted class when highlightedField is null", () => {
  render(
    <FieldsPanel
      extraction={mockExtraction}
      onHoverField={() => {}}
      highlightedField={null}
      loading={false}
    />,
  );
  const row = screen.getByTestId("field-invoice_id");
  expect(row).not.toHaveClass("fields-panel__field-header--highlighted");
});

test("export menu is hidden when no target_tables are present", () => {
  render(
    <FieldsPanel
      extraction={mockExtraction}
      onHoverField={() => {}}
      onExport={() => {}}
      loading={false}
    />,
  );
  expect(screen.queryByTestId("export-menu")).not.toBeInTheDocument();
});

test("renders regex tag when field has a pattern", () => {
  const withPattern = {
    ...mockExtraction,
    fields: [
      { ...mockExtraction.fields[0], pattern: "\\bDE\\d{20}\\b" },
      ...mockExtraction.fields.slice(1),
    ],
  };
  render(
    <FieldsPanel extraction={withPattern} onHoverField={() => {}} loading={false} />,
  );
  expect(screen.getByTestId("field-pattern-invoice_id")).toHaveTextContent("regex");
});

test("renders LLM tag when match_reason is llm_fallback", () => {
  const withLlm = {
    ...mockExtraction,
    fields: [
      {
        ...mockExtraction.fields[0],
        extracted_value: "ACME",
        match_reason: "llm_fallback",
      },
      ...mockExtraction.fields.slice(1),
    ],
  };
  render(
    <FieldsPanel extraction={withLlm} onHoverField={() => {}} loading={false} />,
  );
  expect(screen.getByTestId("field-llm-invoice_id")).toHaveTextContent("LLM");
});

test("does not render regex tag when pattern is missing or empty", () => {
  render(
    <FieldsPanel extraction={mockExtraction} onHoverField={() => {}} loading={false} />,
  );
  expect(screen.queryByTestId("field-pattern-invoice_id")).not.toBeInTheDocument();
});

test("renders threshold tag when min_confidence is set on the field", () => {
  const withThreshold = {
    ...mockExtraction,
    fields: [
      {
        ...mockExtraction.fields[0],
        min_confidence: 0.75,
      },
      ...mockExtraction.fields.slice(1),
    ],
  };
  render(
    <FieldsPanel
      extraction={withThreshold}
      onHoverField={() => {}}
      loading={false}
    />,
  );
  expect(screen.getByTestId("field-threshold-invoice_id")).toHaveTextContent(
    "≥75%",
  );
});

test("renders rejected-candidate review hint when below threshold", () => {
  const withRejected = {
    ...mockExtraction,
    fields: [
      {
        name: "vendor",
        description: "vendor name",
        extracted_value: null,
        confidence: 0,
        matched_entry_id: null,
        page: null,
        bbox: null,
        examples: [],
        min_confidence: 0.9,
        rejected_candidate: {
          text: "ACME Corp.",
          score: 65,
          confidence: 0.65,
          page: 1,
        },
      },
    ],
  };
  render(
    <FieldsPanel
      extraction={withRejected}
      onHoverField={() => {}}
      loading={false}
    />,
  );
  const hint = screen.getByTestId("rejected-vendor");
  expect(hint).toHaveTextContent("ACME Corp.");
  expect(hint).toHaveTextContent("65%");
  expect(hint).toHaveTextContent("p.1");
});

test("does not render rejected-candidate hint when the field matched", () => {
  // invoice_id matched in the base mock — there should be no review hint.
  render(
    <FieldsPanel
      extraction={mockExtraction}
      onHoverField={() => {}}
      loading={false}
    />,
  );
  expect(screen.queryByTestId("rejected-invoice_id")).not.toBeInTheDocument();
});

test("export menu surfaces JSON + per-table CSV options and calls onExport", async () => {
  const onExport = jest.fn();
  const extractionWithTables = {
    ...mockExtraction,
    target_tables: ["Invoice", "line_items"],
  };
  render(
    <FieldsPanel
      extraction={extractionWithTables}
      onHoverField={() => {}}
      onExport={onExport}
      loading={false}
    />,
  );

  fireEvent.click(screen.getByRole("button", { name: /Export options/i }));

  // Carbon's OverflowMenu portals items into the body — find them by text.
  fireEvent.click(await screen.findByText(/Download all tables \(JSON\)/i));
  expect(onExport).toHaveBeenLastCalledWith({ format: "json" });

  fireEvent.click(screen.getByRole("button", { name: /Export options/i }));
  fireEvent.click(await screen.findByText(/Download "Invoice" \(CSV\)/i));
  expect(onExport).toHaveBeenLastCalledWith({ format: "csv", table: "Invoice" });
});
