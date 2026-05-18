import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Button,
  Checkbox,
  ComposedModal,
  DismissibleTag,
  Dropdown,
  InlineLoading,
  InlineNotification,
  ModalBody,
  ModalFooter,
  ModalHeader,
  NumberInput,
  OverflowMenu,
  OverflowMenuItem,
  TextArea,
  TextInput,
} from "@carbon/react";
import { Add, MagicWand, TrashCan } from "@carbon/react/icons";
import {
  deleteDefinition,
  fetchDefinition,
  fetchDefinitionCodegen,
  fetchTemplate,
  fetchTemplates,
  suggestDefinitionFromDocument,
  updateDefinition,
  uploadDefinition,
} from "../services/api";

// Codegen targets exposed by the "Export schema" overflow menu in edit mode.
// Keep in sync with `backend/codegen.py::SUPPORTED_FORMATS` — the backend
// rejects anything else with a 400, so a typo here surfaces as an error
// notification rather than a silent no-op.
const CODEGEN_FORMATS = [
  { id: "json-schema", label: "JSON Schema (.json)" },
  { id: "sql-postgres", label: "PostgreSQL DDL (.sql)" },
  { id: "sql-bigquery", label: "BigQuery DDL (.sql)" },
  { id: "typescript", label: "TypeScript types (.ts)" },
];

// Carbon Dropdown items for the `type` selector. `scalar` is the implicit
// default in existing JSON definitions (absent `type` key); we keep it as an
// explicit option to make the choice visible in the UI but emit `type: null`
// on save so we don't pollute the JSON with redundant keys.
const TYPE_ITEMS = [
  { id: "scalar", label: "Scalar (text / number / date)" },
  { id: "array", label: "Array (repeating items)" },
];

// Built-in normalizers supported by the backend. Keep this list in sync with
// `backend/normalizers.py::SUPPORTED_NORMALIZERS` — the backend's Pydantic
// validator rejects anything else, so a typo here surfaces as a save-time
// 422 with a useful message.
const NORMALIZER_ITEMS = [
  { id: "", label: "None (raw text)" },
  { id: "number", label: "Number" },
  { id: "currency", label: "Currency (locale-aware)" },
  { id: "date", label: "Date (auto-detect or yyyy-mm-dd)" },
  { id: "percent", label: "Percent (5% → 0.05)" },
  { id: "boolean", label: "Boolean (yes/no, true/false)" },
  { id: "trim", label: "Trim whitespace" },
  { id: "lowercase", label: "Lowercase" },
  { id: "uppercase", label: "Uppercase" },
];

const EMPTY_FIELD = Object.freeze({
  name: "",
  type: "scalar",
  description: "",
  extraction_instructions: "",
  examples: [],
  available_options: [],
  affix: false,
  // Per-field acceptance threshold (0–100 in the UI, 0–1 in the JSON). null
  // means "use server default (50%)" so a freshly created field doesn't
  // bake a specific value into the JSON.
  min_confidence_pct: null,
  // Optional regex applied to every text entry. Matches score 92 in the
  // backend matcher and the matched substring becomes the extracted value.
  pattern: "",
  // Opt this field into the LLM fallback. The backend only consults
  // Claude when the rule-based matcher came back empty; this flag is
  // the kill-switch that prevents fan-out across every field.
  use_llm_fallback: false,
  // Value normalizer applied after the matcher finds a value. Empty string
  // means "no normalizer" — pruned from the saved JSON.
  normalizer: "",
  // Dependency conditions. Stored as raw JSON strings so users can express
  // the full {field, equals|in|present, all|any} grammar without us
  // building a nested condition-builder UI. Empty string means "no
  // condition" — pruned from the saved JSON. The backend validates the
  // shape on save and surfaces a 422 with the offending payload.
  visible_if: "",
  required_if: "",
  // Multi-page table knobs. Only meaningful for array fields — the form
  // exposes them under the array branch.
  multi_page: false,
  header_pattern: "",
  fields: [],
});

// Strip empty-string / empty-array properties so the saved JSON stays close to
// what users hand-wrote and the backend doesn't carry around `"description": ""`
// noise for every freshly added field.
function pruneField(field) {
  const out = { name: field.name.trim() };
  if (field.type === "array") out.type = "array";
  if (field.description?.trim()) out.description = field.description.trim();
  if (field.extraction_instructions?.trim())
    out.extraction_instructions = field.extraction_instructions.trim();
  if (field.examples?.length) out.examples = field.examples.slice();
  if (field.available_options?.length)
    out.available_options = field.available_options.slice();
  if (field.affix) out.affix = true;
  if (field.pattern?.trim()) out.pattern = field.pattern.trim();
  if (field.use_llm_fallback) out.use_llm_fallback = true;
  if (field.normalizer && field.normalizer !== "")
    out.normalizer = field.normalizer;
  const parsedVisible = parseConditionInput(field.visible_if);
  if (parsedVisible !== undefined) out.visible_if = parsedVisible;
  const parsedRequired = parseConditionInput(field.required_if);
  if (parsedRequired !== undefined) out.required_if = parsedRequired;
  if (field.type === "array") {
    if (field.multi_page) out.multi_page = true;
    if (field.header_pattern?.trim())
      out.header_pattern = field.header_pattern.trim();
  }
  // Only emit min_confidence when the user actually set one. Convert from
  // the 0–100 UI value back to the 0–1 backend value, clamped to range.
  if (
    field.min_confidence_pct != null &&
    Number.isFinite(field.min_confidence_pct)
  ) {
    const clamped = Math.max(0, Math.min(100, field.min_confidence_pct));
    out.min_confidence = clamped / 100;
  }
  if (field.type === "array" && field.fields?.length)
    out.fields = field.fields.map(pruneField);
  return out;
}

// Inverse of pruneField for hydrating an existing JSON definition into the
// editor's draft state. Missing optional keys map to empty form values so the
// inputs are always controlled.
function hydrateField(raw) {
  return {
    name: raw?.name ?? "",
    type: raw?.type === "array" ? "array" : "scalar",
    description: raw?.description ?? "",
    extraction_instructions: raw?.extraction_instructions ?? "",
    examples: Array.isArray(raw?.examples) ? raw.examples.map(String) : [],
    available_options: Array.isArray(raw?.available_options)
      ? raw.available_options.map(String)
      : [],
    affix: !!raw?.affix,
    min_confidence_pct:
      typeof raw?.min_confidence === "number"
        ? Math.round(raw.min_confidence * 100)
        : null,
    pattern: typeof raw?.pattern === "string" ? raw.pattern : "",
    use_llm_fallback: !!raw?.use_llm_fallback,
    visible_if: stringifyCondition(raw?.visible_if),
    required_if: stringifyCondition(raw?.required_if),
    multi_page: !!raw?.multi_page,
    header_pattern: typeof raw?.header_pattern === "string" ? raw.header_pattern : "",
    normalizer:
      typeof raw?.normalizer === "string"
        ? raw.normalizer
        : raw?.normalizer && typeof raw.normalizer === "object" && raw.normalizer.name
          ? raw.normalizer.format
            ? `${raw.normalizer.name}:${raw.normalizer.format}`
            : raw.normalizer.name
          : "",
    fields: Array.isArray(raw?.fields) ? raw.fields.map(hydrateField) : [],
  };
}

// Cheap client-side regex check so the user gets feedback before posting.
// Distinct from the backend's authoritative validation: a passing client
// check still triggers the server check on save.
function isValidRegex(s) {
  if (!s) return true;
  try {
    new RegExp(s);
    return true;
  } catch (_) {
    return false;
  }
}

// Parse the visible_if / required_if free-text input the user typed.
// Accepts:
//   ""                          → undefined (pruned from JSON)
//   "true" / "false"            → boolean (only meaningful for required_if)
//   "field=value"               → {field, equals: value}
//   "field in a,b,c"            → {field, in: [a,b,c]}
//   "field present"             → {field, present: true}
//   "field absent"              → {field, absent: true}
//   any JSON object/array       → parsed verbatim (advanced use)
// Returns the parsed value, or `undefined` when the input is blank, or
// throws (caught by `validate()`) if the input is non-blank but
// uninterpretable.
function parseConditionInput(raw) {
  if (raw == null) return undefined;
  const trimmed = String(raw).trim();
  if (!trimmed) return undefined;
  if (trimmed === "true") return true;
  if (trimmed === "false") return false;
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    return JSON.parse(trimmed);
  }
  const presentMatch = /^(\w+)\s+present$/i.exec(trimmed);
  if (presentMatch) return { field: presentMatch[1], present: true };
  const absentMatch = /^(\w+)\s+absent$/i.exec(trimmed);
  if (absentMatch) return { field: absentMatch[1], absent: true };
  const inMatch = /^(\w+)\s+in\s+(.+)$/i.exec(trimmed);
  if (inMatch) {
    return {
      field: inMatch[1],
      in: inMatch[2].split(",").map((s) => s.trim()).filter(Boolean),
    };
  }
  const eqMatch = /^(\w+)\s*=\s*(.+)$/.exec(trimmed);
  if (eqMatch) return { field: eqMatch[1], equals: eqMatch[2].trim() };
  throw new Error(
    `Could not parse condition "${trimmed}" — try field=value, "field in a,b", "field present", or raw JSON.`,
  );
}

function stringifyCondition(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "object") {
    if (value.field) {
      if ("equals" in value) return `${value.field}=${value.equals}`;
      if (Array.isArray(value.in)) return `${value.field} in ${value.in.join(",")}`;
      if (value.present === true) return `${value.field} present`;
      if (value.absent === true) return `${value.field} absent`;
    }
    try {
      return JSON.stringify(value);
    } catch (_) {
      return "";
    }
  }
  return String(value);
}

function isValidCondition(raw) {
  try {
    parseConditionInput(raw);
    return true;
  } catch (_) {
    return false;
  }
}

// Validate the draft synchronously and return a list of human-readable errors.
// Mirrors the backend's Pydantic checks (non-empty document_type / field name)
// so the user gets immediate feedback instead of a 422 from the API.
function validate(draft) {
  const errors = [];
  if (!draft.documentType.trim()) {
    errors.push("Document type is required.");
  }
  const seen = new Set();
  const walk = (fields, path) => {
    fields.forEach((f, i) => {
      const where = `${path}[${i}]`;
      if (!f.name.trim()) {
        errors.push(`Field ${where} is missing a name.`);
      } else {
        const key = `${path}.${f.name.trim()}`;
        if (seen.has(key)) {
          errors.push(`Duplicate field name "${f.name.trim()}" in ${path || "root"}.`);
        }
        seen.add(key);
      }
      if (f.pattern && !isValidRegex(f.pattern)) {
        errors.push(`Field "${f.name || where}" has an invalid regex pattern.`);
      }
      if (f.visible_if && !isValidCondition(f.visible_if)) {
        errors.push(`Field "${f.name || where}" has an invalid visible_if condition.`);
      }
      if (f.required_if && !isValidCondition(f.required_if)) {
        errors.push(`Field "${f.name || where}" has an invalid required_if condition.`);
      }
      if (f.type === "array") walk(f.fields, `${where}.fields`);
    });
  };
  walk(draft.fields, "fields");
  return errors;
}

// ChipInput: comma- or Enter-terminated string list. Used for both `examples`
// and `available_options`. Keeping it inline avoids a fourth file for a 30-line
// component that has no other reuse site.
function ChipInput({ id, labelText, placeholder, values, onChange }) {
  const [draft, setDraft] = useState("");

  const commit = useCallback(() => {
    const trimmed = draft.trim();
    if (!trimmed) return;
    if (values.includes(trimmed)) {
      setDraft("");
      return;
    }
    onChange([...values, trimmed]);
    setDraft("");
  }, [draft, values, onChange]);

  const remove = useCallback(
    (v) => onChange(values.filter((x) => x !== v)),
    [values, onChange],
  );

  const handleKeyDown = useCallback(
    (e) => {
      if (e.key === "Enter" || e.key === ",") {
        e.preventDefault();
        commit();
      } else if (e.key === "Backspace" && !draft && values.length) {
        e.preventDefault();
        onChange(values.slice(0, -1));
      }
    },
    [commit, draft, values, onChange],
  );

  return (
    <div className="definition-editor__chip-input">
      <TextInput
        id={id}
        labelText={labelText}
        placeholder={placeholder}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={handleKeyDown}
        size="sm"
      />
      {values.length > 0 && (
        <div className="definition-editor__chips">
          {values.map((v) => (
            <DismissibleTag
              key={v}
              type="cool-gray"
              size="sm"
              text={v}
              title={`Remove ${v}`}
              onClose={() => remove(v)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function FieldEditor({ field, path, onChange, onRemove, depth = 0 }) {
  const isArray = field.type === "array";
  const update = useCallback((patch) => onChange({ ...field, ...patch }), [field, onChange]);

  const updateSubField = useCallback(
    (idx, sub) => {
      const next = field.fields.slice();
      next[idx] = sub;
      update({ fields: next });
    },
    [field.fields, update],
  );

  const removeSubField = useCallback(
    (idx) => update({ fields: field.fields.filter((_, i) => i !== idx) }),
    [field.fields, update],
  );

  const addSubField = useCallback(
    () => update({ fields: [...field.fields, { ...EMPTY_FIELD }] }),
    [field.fields, update],
  );

  // Don't allow nested arrays beyond depth 1. The backend supports it
  // recursively, but no real-world definition uses it, and surfacing the
  // option in the UI would invite unintentional structures.
  const typeItems = depth > 0 ? TYPE_ITEMS.filter((t) => t.id !== "array") : TYPE_ITEMS;

  return (
    <fieldset
      className={`definition-editor__field definition-editor__field--depth-${depth}`}
      data-testid={`def-field-${path}`}
    >
      <legend className="definition-editor__field-legend">
        {field.name.trim() || `Field ${path}`}
      </legend>
      <div className="definition-editor__field-row">
        <TextInput
          id={`def-field-name-${path}`}
          labelText="Name"
          placeholder="e.g. invoice_id"
          value={field.name}
          onChange={(e) => update({ name: e.target.value })}
          size="sm"
          required
        />
        <Dropdown
          id={`def-field-type-${path}`}
          titleText="Type"
          label="Select type"
          items={typeItems}
          itemToString={(it) => it?.label || ""}
          selectedItem={typeItems.find((t) => t.id === field.type) || typeItems[0]}
          onChange={({ selectedItem }) =>
            update({ type: selectedItem?.id || "scalar" })
          }
          size="sm"
        />
        <Button
          kind="danger--ghost"
          size="sm"
          renderIcon={TrashCan}
          iconDescription="Remove field"
          hasIconOnly
          onClick={onRemove}
          tooltipPosition="left"
        />
      </div>
      <TextArea
        id={`def-field-desc-${path}`}
        labelText="Description"
        rows={2}
        value={field.description}
        onChange={(e) => update({ description: e.target.value })}
      />
      <TextArea
        id={`def-field-instr-${path}`}
        labelText="Extraction instructions (optional)"
        helperText="Hints for where in the document this field typically lives."
        rows={2}
        value={field.extraction_instructions}
        onChange={(e) => update({ extraction_instructions: e.target.value })}
      />
      {!isArray && (
        <>
          <ChipInput
            id={`def-field-examples-${path}`}
            labelText="Examples (press Enter or comma to add)"
            placeholder="e.g. INV-2024-001"
            values={field.examples}
            onChange={(examples) => update({ examples })}
          />
          <ChipInput
            id={`def-field-options-${path}`}
            labelText="Available options (closed list of allowed values)"
            placeholder="e.g. USD"
            values={field.available_options}
            onChange={(available_options) => update({ available_options })}
          />
          <Checkbox
            id={`def-field-affix-${path}`}
            labelText="Affix (e.g. currency sign — match as prefix/suffix of nearby text)"
            checked={field.affix}
            onChange={(_, { checked }) => update({ affix: checked })}
          />
          <NumberInput
            id={`def-field-threshold-${path}`}
            label="Match threshold (%)"
            helperText="Reject matches below this score. Empty = use the default (50%)."
            min={0}
            max={100}
            step={5}
            allowEmpty
            value={field.min_confidence_pct ?? ""}
            onChange={(_e, { value }) => {
              // Carbon emits a string (or number) here; "" means cleared.
              if (value === "" || value == null) {
                update({ min_confidence_pct: null });
              } else {
                const n = Number(value);
                update({ min_confidence_pct: Number.isFinite(n) ? n : null });
              }
            }}
            size="sm"
          />
          <TextInput
            id={`def-field-pattern-${path}`}
            labelText="Regex pattern (optional)"
            helperText='e.g. \b[A-Z]{2}\d{20}\b for IBAN, or "DE\d+" for German VAT IDs. Capture group 1 (if present) becomes the extracted value.'
            placeholder="\\d{4}-\\d{2}-\\d{2}"
            value={field.pattern}
            onChange={(e) => update({ pattern: e.target.value })}
            invalid={!isValidRegex(field.pattern)}
            invalidText="Not a valid regular expression."
            size="sm"
          />
          <Checkbox
            id={`def-field-llm-${path}`}
            labelText="Use LLM fallback when the matcher comes up empty (consumes Anthropic API credits)"
            checked={field.use_llm_fallback}
            onChange={(_, { checked }) => update({ use_llm_fallback: checked })}
          />
          <Dropdown
            id={`def-field-normalizer-${path}`}
            titleText="Normalizer (optional)"
            helperText="Parse the matched text into a canonical value (e.g. 1.234,56 € → 1234.56)."
            label="Pick a normalizer…"
            items={NORMALIZER_ITEMS}
            itemToString={(it) => it?.label || ""}
            selectedItem={
              NORMALIZER_ITEMS.find((n) => n.id === (field.normalizer || "")) ||
              NORMALIZER_ITEMS[0]
            }
            onChange={({ selectedItem }) =>
              update({ normalizer: selectedItem?.id || "" })
            }
            size="sm"
          />
        </>
      )}
      <TextInput
        id={`def-field-visibleif-${path}`}
        labelText="Visible if (optional)"
        helperText='Hide this field when the condition is false. e.g. "payment_method=card", "country in DE,AT,CH", "iban present", or raw JSON.'
        placeholder="payment_method=card"
        value={field.visible_if || ""}
        onChange={(e) => update({ visible_if: e.target.value })}
        invalid={!!field.visible_if && !isValidCondition(field.visible_if)}
        invalidText="Unparseable condition."
        size="sm"
      />
      <TextInput
        id={`def-field-requiredif-${path}`}
        labelText="Required if (optional)"
        helperText='Flag the field as required when this condition is true. Use "true" to always require, or "field=value" / "field in a,b" / JSON.'
        placeholder="payment_method=card"
        value={field.required_if || ""}
        onChange={(e) => update({ required_if: e.target.value })}
        invalid={!!field.required_if && !isValidCondition(field.required_if)}
        invalidText="Unparseable condition."
        size="sm"
      />
      {isArray && (
        <div className="definition-editor__subfields">
          <Checkbox
            id={`def-field-multipage-${path}`}
            labelText="Table can span multiple pages (auto-detect repeated headers)"
            checked={!!field.multi_page}
            onChange={(_, { checked }) => update({ multi_page: checked })}
          />
          <TextInput
            id={`def-field-headerpattern-${path}`}
            labelText="Header row pattern (optional)"
            helperText="Regex matching TableItem header rows to skip — e.g. ^\\s*(SKU|Description|Qty|Amount).*$"
            placeholder="^(SKU|Description|Qty|Amount)"
            value={field.header_pattern || ""}
            onChange={(e) => update({ header_pattern: e.target.value })}
            invalid={!!field.header_pattern && !isValidRegex(field.header_pattern)}
            invalidText="Not a valid regular expression."
            size="sm"
          />
          <div className="definition-editor__subfields-header">
            <span>Item fields</span>
            <Button
              kind="ghost"
              size="sm"
              renderIcon={Add}
              onClick={addSubField}
            >
              Add item field
            </Button>
          </div>
          {field.fields.length === 0 ? (
            <p className="definition-editor__empty">
              Add at least one field that describes a single item in the array.
            </p>
          ) : (
            field.fields.map((sub, idx) => (
              <FieldEditor
                key={idx}
                field={sub}
                path={`${path}.${idx}`}
                depth={depth + 1}
                onChange={(next) => updateSubField(idx, next)}
                onRemove={() => removeSubField(idx)}
              />
            ))
          )}
        </div>
      )}
    </fieldset>
  );
}

// Build the JSON payload the backend expects. If `original` is supplied (edit
// flow), spread its top-level keys first so extras like `target_tables` and
// `source_candidates` survive a round-trip through the editor — the editor
// only owns the `document` subtree.
function buildPayload(draft, original) {
  const document = {
    document_type: draft.documentType.trim(),
    document_description: draft.description.trim() || undefined,
    fields: draft.fields.map(pruneField),
  };
  if (!document.document_description) delete document.document_description;
  const originalDoc = original?.document || {};
  const mergedDoc = { ...originalDoc, ...document };
  return { ...(original || {}), document: mergedDoc };
}

export default function DefinitionEditor({
  open,
  mode, // "create" | "edit"
  definitionId, // required when mode === "edit"
  // Optional: when set in create mode, the editor shows an
  // "Auto-generate from current document" button that asks the backend
  // to draft a schema from this document's text via the LLM. The user
  // still reviews + saves the result through the normal flow.
  suggestForDocId,
  suggestForDocLabel,
  // When true, kick off the LLM suggestion the moment the modal opens so
  // a user who clicked "Auto-generate from document" elsewhere doesn't
  // have to click a second button inside the modal. Read once on first
  // render and then ignored — the parent flips it back to false via
  // onClose.
  autoStartSuggest = false,
  onClose,
  onSaved, // (savedDef) => void — parent refreshes its list / selection
  onDeleted, // (deletedId) => void
  onShowHistory, // () => void — caller opens the history modal
}) {
  const [draft, setDraft] = useState({
    documentType: "",
    description: "",
    fields: [],
  });
  const [original, setOriginal] = useState(null);
  // Edit mode always fetches on mount, so start in the loading state to
  // avoid a synchronous setLoading(true) inside the hydrate effect.
  const [loading, setLoading] = useState(mode === "edit" && !!definitionId);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState(null);
  const [loadError, setLoadError] = useState(null);
  // Templates catalog for the "Start from template" picker. Fetched once when
  // the modal opens in create mode; ignored in edit mode (the user is already
  // working on a concrete definition).
  const [templates, setTemplates] = useState([]);
  const [templateLoading, setTemplateLoading] = useState(false);
  // Track whether an auto-generate request is in flight so the button
  // can show a loading label and stay disabled. Separate from
  // `templateLoading` because both can happen against the same draft and
  // we want each control to spin independently.
  const [suggestLoading, setSuggestLoading] = useState(false);
  // Non-error post-suggestion banner — e.g. "Generated 7 fields. Review
  // and click Create." Cleared on subsequent draft edits so it doesn't
  // linger past the moment it was useful.
  const [suggestSuccess, setSuggestSuccess] = useState(null);

  // Hydrate the draft when the modal mounts in edit mode. The parent
  // unmounts/remounts the editor on every open (editorMode flips through
  // null), so we don't need an open-prop guard or in-body state resets —
  // useState defaults above already produce the clean baseline. The dead
  // `if (!open)` reset branch went with the cleanup.
  useEffect(() => {
    if (mode !== "edit" || !definitionId) return undefined;
    const ctrl = new AbortController();
    fetchDefinition(definitionId, { signal: ctrl.signal })
      .then((data) => {
        if (ctrl.signal.aborted) return;
        const doc = data?.document || {};
        setOriginal(data);
        setDraft({
          documentType: doc.document_type ?? "",
          description: doc.document_description ?? "",
          fields: Array.isArray(doc.fields) ? doc.fields.map(hydrateField) : [],
        });
      })
      .catch((err) => {
        if (err?.name === "AbortError") return;
        setLoadError(err.message || "Failed to load definition.");
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setLoading(false);
      });
    return () => ctrl.abort();
  }, [mode, definitionId]);

  // Templates list: load once when the create-mode modal opens. Edit-mode
  // never shows the picker, so skip the fetch in that branch.
  useEffect(() => {
    if (!open || mode !== "create") return undefined;
    const ctrl = new AbortController();
    fetchTemplates({ signal: ctrl.signal })
      .then((items) => {
        if (!ctrl.signal.aborted) setTemplates(items);
      })
      .catch((err) => {
        if (err?.name !== "AbortError") {
          console.error("Failed to load templates:", err);
        }
      });
    return () => ctrl.abort();
  }, [open, mode]);

  const runAutoGenerate = useCallback(async () => {
    if (!suggestForDocId) return;
    setSuggestLoading(true);
    setError(null);
    setSuggestSuccess(null);
    try {
      const result = await suggestDefinitionFromDocument(suggestForDocId);
      const doc = result?.document || {};
      const fields = Array.isArray(doc.fields) ? doc.fields.map(hydrateField) : [];
      setDraft({
        documentType: doc.document_type ?? "",
        description: doc.document_description ?? "",
        fields,
      });
      // Don't stash the suggestion as `original` — `buildPayload` only
      // merges top-level extras from `original`, and the suggestion has
      // none. Leaving it null also matches the "fresh definition" intent
      // so the POST is a clean create rather than an upsert.
      setOriginal(null);
      const count = fields.length;
      setSuggestSuccess(
        `Generated ${count} field${count === 1 ? "" : "s"}. Review the draft below, edit anything you don't need, then click Create.`,
      );
    } catch (err) {
      setError(err.message || "Failed to generate a schema suggestion.");
    } finally {
      setSuggestLoading(false);
    }
  }, [suggestForDocId]);

  // Wrapper that adds a "are you sure?" prompt if the user has typed
  // anything into the draft. Auto-generate replaces the entire draft,
  // and silently nuking 5 minutes of typing would be a betrayal-grade UX
  // regression. The internal create flow (autoStartSuggest=true) skips
  // the prompt because by definition the draft is empty.
  const handleAutoGenerate = useCallback(
    async ({ skipConfirm = false } = {}) => {
      const hasDraftContent =
        draft.documentType.trim() !== "" ||
        draft.description.trim() !== "" ||
        draft.fields.length > 0;
      if (
        hasDraftContent &&
        !skipConfirm &&
        // eslint-disable-next-line no-alert
        !window.confirm(
          "Replace your current draft with the auto-generated schema?",
        )
      ) {
        return;
      }
      await runAutoGenerate();
    },
    [draft, runAutoGenerate],
  );

  // Auto-start on open: when the parent passes `autoStartSuggest=true`
  // (entry via the FieldsPanel CTA), kick off generation as soon as the
  // modal mounts. The parent's mount/unmount cycle already gives us a
  // fresh ref per opening, so a plain ref (not state) latches the
  // one-shot semantics without nudging us into another render.
  //
  // The dispatch is deferred to a microtask so runAutoGenerate's
  // synchronous setSuggestLoading/setError/setSuggestSuccess don't run
  // inside the effect body (the set-state-in-effect rule traces through
  // function calls). The microtask still runs before paint, so the
  // "Generating…" indicator surfaces in the very next render with no
  // user-visible delay.
  const autoStartTriggeredRef = useRef(false);
  useEffect(() => {
    if (autoStartTriggeredRef.current) return;
    if (
      mode !== "create" ||
      !autoStartSuggest ||
      !suggestForDocId ||
      suggestLoading
    ) {
      return;
    }
    autoStartTriggeredRef.current = true;
    queueMicrotask(() => runAutoGenerate());
  }, [mode, autoStartSuggest, suggestForDocId, suggestLoading, runAutoGenerate]);

  const handleApplyTemplate = useCallback(async (templateId) => {
    if (!templateId) return;
    setTemplateLoading(true);
    setError(null);
    try {
      const tpl = await fetchTemplate(templateId);
      const doc = tpl?.document || {};
      setDraft({
        documentType: doc.document_type ?? "",
        description: doc.document_description ?? "",
        fields: Array.isArray(doc.fields) ? doc.fields.map(hydrateField) : [],
      });
      // Preserve extras (e.g. `target_tables`) by stashing the template as
      // the editor's `original` — `buildPayload` merges its top-level keys
      // back into the saved JSON, exactly the way edit-mode hydration does.
      setOriginal(tpl);
    } catch (err) {
      setError(err.message || "Failed to load template.");
    } finally {
      setTemplateLoading(false);
    }
  }, []);

  const errors = useMemo(() => validate(draft), [draft]);

  const updateField = useCallback((idx, next) => {
    setDraft((d) => {
      const fields = d.fields.slice();
      fields[idx] = next;
      return { ...d, fields };
    });
  }, []);

  const removeField = useCallback((idx) => {
    setDraft((d) => ({ ...d, fields: d.fields.filter((_, i) => i !== idx) }));
  }, []);

  const addField = useCallback(() => {
    setDraft((d) => ({ ...d, fields: [...d.fields, { ...EMPTY_FIELD }] }));
  }, []);

  const handleSave = useCallback(async () => {
    if (errors.length > 0) {
      setError(errors[0]);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const payload = buildPayload(draft, original);
      const result =
        mode === "edit"
          ? await updateDefinition(definitionId, payload)
          : await uploadDefinition(payload);
      onSaved?.(result);
    } catch (err) {
      setError(err.message || "Failed to save definition.");
    } finally {
      setSaving(false);
    }
  }, [errors, draft, original, mode, definitionId, onSaved]);

  const handleExport = useCallback(
    async (format) => {
      if (mode !== "edit" || !definitionId) return;
      setError(null);
      try {
        const { blob, filename } = await fetchDefinitionCodegen(
          definitionId,
          format,
        );
        // Same download trick as BatchExtractModal: build an object URL,
        // synthesize an anchor with the server-supplied filename, click,
        // then revoke the URL so we don't pin the blob in memory.
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
      } catch (err) {
        setError(err.message || "Failed to export schema.");
      }
    },
    [mode, definitionId],
  );

  const handleDelete = useCallback(async () => {
    if (mode !== "edit" || !definitionId) return;
    // eslint-disable-next-line no-alert
    if (!window.confirm(`Delete definition "${draft.documentType}"?`)) return;
    setDeleting(true);
    setError(null);
    try {
      await deleteDefinition(definitionId);
      onDeleted?.(definitionId);
    } catch (err) {
      setError(err.message || "Failed to delete definition.");
    } finally {
      setDeleting(false);
    }
  }, [mode, definitionId, draft.documentType, onDeleted]);

  const title = mode === "edit" ? "Edit document class" : "New document class";
  const saveLabel = mode === "edit" ? "Save changes" : "Create";

  return (
    <ComposedModal
      open={open}
      onClose={() => {
        if (saving || deleting) return false;
        onClose?.();
        return undefined;
      }}
      size="lg"
      preventCloseOnClickOutside
      aria-label={title}
    >
      <ModalHeader title={title} />
      <ModalBody hasForm hasScrollingContent>
        {loadError && (
          <InlineNotification
            kind="error"
            title="Could not load definition"
            subtitle={loadError}
            hideCloseButton
            lowContrast
          />
        )}
        {!loadError && loading && (
          <p className="definition-editor__empty" role="status">
            Loading definition…
          </p>
        )}
        {!loadError && !loading && (
          <div className="definition-editor__body">
            {mode === "create" && suggestForDocId && (
              <div className="definition-editor__auto-generate">
                <div className="definition-editor__auto-generate-row">
                  <MagicWand
                    size={20}
                    aria-hidden="true"
                    className="definition-editor__auto-generate-icon"
                  />
                  <div className="definition-editor__auto-generate-text">
                    <p className="definition-editor__auto-generate-title">
                      Let the model draft this schema
                    </p>
                    <p className="definition-editor__auto-generate-hint">
                      {suggestForDocLabel ? (
                        <>
                          Reads{" "}
                          <span
                            className="definition-editor__auto-generate-filename"
                            title={suggestForDocLabel}
                          >
                            {suggestForDocLabel}
                          </span>{" "}
                          and proposes the fields a definition for this kind of
                          document should pull out. Replaces the current draft;
                          you can still edit anything before saving.
                        </>
                      ) : (
                        "Reads the selected document and proposes a starting set of fields. Replaces the current draft; you can still edit anything before saving."
                      )}
                    </p>
                  </div>
                </div>
                {suggestLoading ? (
                  <InlineLoading
                    description="Reading the document and asking the model… this can take 10–30 seconds."
                    status="active"
                    data-testid="def-auto-generate-loading"
                  />
                ) : (
                  <Button
                    kind="tertiary"
                    size="sm"
                    renderIcon={MagicWand}
                    onClick={() => handleAutoGenerate()}
                    disabled={suggestLoading}
                    data-testid="def-auto-generate-button"
                  >
                    {suggestSuccess
                      ? "Regenerate from document"
                      : "Auto-generate from document"}
                  </Button>
                )}
              </div>
            )}
            {suggestSuccess && !suggestLoading && (
              <InlineNotification
                kind="success"
                title="Schema drafted"
                subtitle={suggestSuccess}
                onCloseButtonClick={() => setSuggestSuccess(null)}
                lowContrast
                data-testid="def-auto-generate-success"
              />
            )}
            {mode === "create" && templates.length > 0 && (
              <Dropdown
                id="def-template-picker"
                titleText="Start from template (optional)"
                helperText="Replaces the current draft."
                label={templateLoading ? "Loading template…" : "Pick a template…"}
                disabled={templateLoading}
                items={templates}
                itemToString={(t) =>
                  t ? `${t.document_type} (${t.field_count} fields)` : ""
                }
                selectedItem={null}
                onChange={({ selectedItem }) => {
                  if (selectedItem?.id) handleApplyTemplate(selectedItem.id);
                }}
                size="sm"
                data-testid="def-template-picker"
              />
            )}
            <TextInput
              id="def-document-type"
              labelText="Document type"
              placeholder="e.g. Invoice"
              value={draft.documentType}
              onChange={(e) =>
                setDraft((d) => ({ ...d, documentType: e.target.value }))
              }
              required
              invalid={!draft.documentType.trim()}
              invalidText="Required."
            />
            <TextArea
              id="def-document-description"
              labelText="Description"
              helperText="Shown above the extracted fields panel."
              rows={3}
              value={draft.description}
              onChange={(e) =>
                setDraft((d) => ({ ...d, description: e.target.value }))
              }
            />
            <div className="definition-editor__fields-header">
              <h3 className="definition-editor__section-title">Fields</h3>
              <Button kind="tertiary" size="sm" renderIcon={Add} onClick={addField}>
                Add field
              </Button>
            </div>
            {draft.fields.length === 0 ? (
              <p className="definition-editor__empty">
                No fields yet. Add the first field the extractor should look for.
              </p>
            ) : (
              draft.fields.map((f, idx) => (
                <FieldEditor
                  key={idx}
                  field={f}
                  path={String(idx)}
                  onChange={(next) => updateField(idx, next)}
                  onRemove={() => removeField(idx)}
                />
              ))
            )}
            {errors.length > 0 && (
              <InlineNotification
                kind="warning"
                title="Resolve before saving"
                subtitle={errors.join(" ")}
                hideCloseButton
                lowContrast
              />
            )}
            {error && (
              <InlineNotification
                kind="error"
                title="Save failed"
                subtitle={error}
                onCloseButtonClick={() => setError(null)}
                lowContrast
              />
            )}
          </div>
        )}
      </ModalBody>
      <ModalFooter>
        {mode === "edit" && (
          <OverflowMenu
            size="sm"
            flipped
            iconDescription="Export schema"
            menuOptionsClass="definition-editor__export-menu"
            data-testid="def-export-menu"
            disabled={saving || deleting || loading}
          >
            {CODEGEN_FORMATS.map((opt) => (
              <OverflowMenuItem
                key={opt.id}
                itemText={opt.label}
                onClick={() => handleExport(opt.id)}
                data-testid={`def-export-${opt.id}`}
              />
            ))}
          </OverflowMenu>
        )}
        {mode === "edit" && onShowHistory && (
          <Button
            kind="ghost"
            onClick={onShowHistory}
            disabled={saving || deleting || loading}
            data-testid="def-history-button"
          >
            History
          </Button>
        )}
        {mode === "edit" && (
          <Button
            kind="danger--tertiary"
            onClick={handleDelete}
            disabled={saving || deleting || loading}
          >
            {deleting ? "Deleting…" : "Delete"}
          </Button>
        )}
        <Button kind="secondary" onClick={onClose} disabled={saving || deleting}>
          Cancel
        </Button>
        <Button
          kind="primary"
          onClick={handleSave}
          disabled={saving || deleting || loading || errors.length > 0}
        >
          {saving ? "Saving…" : saveLabel}
        </Button>
      </ModalFooter>
    </ComposedModal>
  );
}
