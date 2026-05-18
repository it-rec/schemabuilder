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
  const onHover = vi.fn();
  render(
    <FieldsPanel extraction={mockExtraction} onHoverField={onHover} loading={false} />
  );
  fireEvent.mouseEnter(screen.getByTestId("field-invoice_id"));
  expect(onHover).toHaveBeenCalledWith(mockExtraction.fields[0]);
});

test("calls onHoverField with null on mouse leave", () => {
  const onHover = vi.fn();
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

test("array item header is hoverable and emits the item's row payload", () => {
  // line_items with one matched item carrying row-level geometry. The
  // expanded item header should be focusable, hover the row payload, and
  // (visually, but checked via class) light up when the item's matched
  // entry id is the highlighted one — exercising the row-level rung of
  // the three-level hover UX (table / row / cell).
  const rowBbox = { l: 10, t: 100, r: 200, b: 80, coord_origin: "BOTTOMLEFT" };
  const arrayExtraction = {
    document_type: "Invoice",
    document_description: "",
    fields: [
      {
        name: "line_items",
        type: "array",
        description: "",
        examples: [],
        matched_entry_id: "array:line_items",
        page: 1,
        bbox: rowBbox,
        items: [
          {
            matched_entry_id: 5,
            page: 1,
            bbox: rowBbox,
            fields: [
              {
                name: "amount",
                extracted_value: "10.72 EUR",
                confidence: 0.85,
                matched_entry_id: "cell:5:amount",
                page: 1,
                bbox: { l: 150, t: 100, r: 200, b: 80 },
                match_reason: "column_header",
              },
            ],
          },
        ],
      },
    ],
    target_tables: [],
  };
  const onHover = vi.fn();
  render(
    <FieldsPanel
      extraction={arrayExtraction}
      onHoverField={onHover}
      loading={false}
    />,
  );
  // Expand the array field so items render.
  fireEvent.click(screen.getByTestId("field-line_items"));

  const header = screen.getByTestId("array-item-header-line_items-0");
  fireEvent.mouseEnter(header);
  // The payload sent up is the row-level one — matched_entry_id is the
  // integer row id, not the cell id.
  expect(onHover).toHaveBeenCalledWith(
    expect.objectContaining({ matched_entry_id: 5, bbox: rowBbox }),
  );
});


test("renders refresh button when onRefresh is provided and fires it", () => {
  const onRefresh = vi.fn();
  render(
    <FieldsPanel
      extraction={mockExtraction}
      onHoverField={() => {}}
      onRefresh={onRefresh}
      loading={false}
    />,
  );
  const btn = screen.getByTestId("fields-panel-refresh");
  fireEvent.click(btn);
  expect(onRefresh).toHaveBeenCalledTimes(1);
});

test("hides refresh button when onRefresh is not provided", () => {
  render(
    <FieldsPanel
      extraction={mockExtraction}
      onHoverField={() => {}}
      loading={false}
    />,
  );
  expect(screen.queryByTestId("fields-panel-refresh")).toBeNull();
});

test("shows generic empty state when no document is selected", () => {
  render(
    <FieldsPanel extraction={null} onHoverField={() => {}} loading={false} />
  );
  expect(screen.getByText("Select a document")).toBeInTheDocument();
  expect(
    screen.getByText(/Pick a document from the list/i),
  ).toBeInTheDocument();
  // No CTAs without a document.
  expect(screen.queryByTestId("fields-panel-auto-generate")).toBeNull();
});

test("shows auto-generate CTA when a document is selected but no definition matches", () => {
  const onAutoGenerate = vi.fn();
  const onCreateBlank = vi.fn();
  render(
    <FieldsPanel
      extraction={null}
      onHoverField={() => {}}
      loading={false}
      hasDocument
      hasDefinitions
      onAutoGenerate={onAutoGenerate}
      onCreateBlank={onCreateBlank}
      selectedDocLabel="invoice.pdf"
    />,
  );
  expect(screen.getByText("No matching definition")).toBeInTheDocument();
  expect(screen.getByText(/Source: invoice.pdf/)).toBeInTheDocument();

  const autoGen = screen.getByTestId("fields-panel-auto-generate");
  autoGen.click();
  expect(onAutoGenerate).toHaveBeenCalledTimes(1);

  const blank = screen.getByTestId("fields-panel-create-blank");
  blank.click();
  expect(onCreateBlank).toHaveBeenCalledTimes(1);
});

test("frames empty state as 'no definitions yet' when the library is empty", () => {
  render(
    <FieldsPanel
      extraction={null}
      onHoverField={() => {}}
      loading={false}
      hasDocument
      hasDefinitions={false}
      onAutoGenerate={() => {}}
      onCreateBlank={() => {}}
    />,
  );
  expect(screen.getByText("No definitions yet")).toBeInTheDocument();
  // Both CTAs are present so the user can pick the fastest path.
  expect(screen.getByTestId("fields-panel-auto-generate")).toBeInTheDocument();
  expect(screen.getByTestId("fields-panel-create-blank")).toBeInTheDocument();
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
  const onExport = vi.fn();
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
