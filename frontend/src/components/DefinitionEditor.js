import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  Button,
  Checkbox,
  ComposedModal,
  DismissibleTag,
  Dropdown,
  InlineNotification,
  ModalBody,
  ModalFooter,
  ModalHeader,
  TextArea,
  TextInput,
} from "@carbon/react";
import { Add, TrashCan } from "@carbon/react/icons";
import {
  deleteDefinition,
  fetchDefinition,
  updateDefinition,
  uploadDefinition,
} from "../services/api";

// Carbon Dropdown items for the `type` selector. `scalar` is the implicit
// default in existing JSON definitions (absent `type` key); we keep it as an
// explicit option to make the choice visible in the UI but emit `type: null`
// on save so we don't pollute the JSON with redundant keys.
const TYPE_ITEMS = [
  { id: "scalar", label: "Scalar (text / number / date)" },
  { id: "array", label: "Array (repeating items)" },
];

const EMPTY_FIELD = Object.freeze({
  name: "",
  type: "scalar",
  description: "",
  extraction_instructions: "",
  examples: [],
  available_options: [],
  affix: false,
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
    fields: Array.isArray(raw?.fields) ? raw.fields.map(hydrateField) : [],
  };
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
        </>
      )}
      {isArray && (
        <div className="definition-editor__subfields">
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
  onClose,
  onSaved, // (savedDef) => void — parent refreshes its list / selection
  onDeleted, // (deletedId) => void
}) {
  const [draft, setDraft] = useState({
    documentType: "",
    description: "",
    fields: [],
  });
  const [original, setOriginal] = useState(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState(null);
  const [loadError, setLoadError] = useState(null);

  // Hydrate the draft when the modal opens. Reset on close so reopening
  // doesn't show stale state from a previous edit.
  useEffect(() => {
    if (!open) {
      setDraft({ documentType: "", description: "", fields: [] });
      setOriginal(null);
      setError(null);
      setLoadError(null);
      return undefined;
    }
    if (mode !== "edit" || !definitionId) return undefined;
    const ctrl = new AbortController();
    setLoading(true);
    setLoadError(null);
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
  }, [open, mode, definitionId]);

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
