import React, { useCallback, useMemo, useState } from "react";
import {
  Button,
  ComposedModal,
  InlineNotification,
  ModalBody,
  ModalFooter,
  ModalHeader,
  RadioButton,
  RadioButtonGroup,
  Tag,
} from "@carbon/react";
import { addFieldExample } from "../services/api";

// Build the list of teachable field paths from an extraction. We expose both
// top-level scalar fields and one-level dotted paths for array sub-fields
// (`line_items.amount`) because the backend handles both. Array fields
// themselves are excluded — their examples don't drive matching directly.
function listTeachableFields(extraction) {
  const out = [];
  if (!extraction?.fields) return out;
  for (const f of extraction.fields) {
    if (f.type === "array") {
      const subs = Array.isArray(f.fields) ? f.fields : [];
      for (const sf of subs) {
        out.push({
          path: `${f.name}.${sf.name}`,
          label: `${f.name.replace(/_/g, " ")} › ${sf.name.replace(/_/g, " ")}`,
          examples: Array.isArray(sf.examples) ? sf.examples : [],
          matched: false,
        });
      }
    } else {
      out.push({
        path: f.name,
        label: f.name.replace(/_/g, " "),
        examples: Array.isArray(f.examples) ? f.examples : [],
        matched: f.matched_entry_id != null,
      });
    }
  }
  return out;
}

export default function ExampleTeacher({
  open,
  entry, // { text, page } — the clicked text entry
  definitionId,
  extraction,
  onClose,
  onSaved, // (result) => void
}) {
  const fields = useMemo(() => listTeachableFields(extraction), [extraction]);
  // Default-select the first not-yet-matched field; failing that, the
  // first field overall. Saves a click in the most common case ("nothing
  // matched, teach this text as <the first empty field>").
  const defaultPath = useMemo(() => {
    const empty = fields.find((f) => !f.matched);
    return (empty || fields[0])?.path || "";
  }, [fields]);
  const [selected, setSelected] = useState(defaultPath);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  // Reset selection whenever the modal reopens with a new entry — otherwise
  // a "stale" selection from the previous open lingers.
  React.useEffect(() => {
    if (open) {
      setSelected(defaultPath);
      setError(null);
    }
  }, [open, defaultPath, entry?.text]);

  const handleSave = useCallback(async () => {
    if (!selected || !entry?.text || !definitionId) return;
    setSaving(true);
    setError(null);
    try {
      const result = await addFieldExample(definitionId, selected, entry.text);
      onSaved?.(result);
    } catch (err) {
      setError(err.message || "Failed to add example.");
    } finally {
      setSaving(false);
    }
  }, [selected, entry, definitionId, onSaved]);

  return (
    <ComposedModal
      open={open}
      onClose={() => (saving ? false : onClose?.())}
      size="sm"
      aria-label="Teach example"
    >
      <ModalHeader title="Teach as example" />
      <ModalBody hasForm>
        <p className="example-teacher__intro">
          Add{" "}
          <strong className="example-teacher__value" data-testid="teach-value">
            {entry?.text || ""}
          </strong>{" "}
          as an example value for which field?
        </p>
        {fields.length === 0 ? (
          <p className="example-teacher__empty">
            No teachable fields. Pick a document class with at least one field.
          </p>
        ) : (
          <RadioButtonGroup
            name="teach-field"
            orientation="vertical"
            valueSelected={selected}
            onChange={setSelected}
            legendText="Field"
          >
            {fields.map((f) => (
              <RadioButton
                key={f.path}
                id={`teach-field-${f.path}`}
                labelText={
                  <span className="example-teacher__option">
                    <span className="example-teacher__option-name">{f.label}</span>
                    {f.matched && (
                      <Tag size="sm" type="green">
                        matched
                      </Tag>
                    )}
                    {f.examples.length > 0 && (
                      <span className="example-teacher__option-examples">
                        e.g. {f.examples.slice(0, 2).join(", ")}
                        {f.examples.length > 2 ? "…" : ""}
                      </span>
                    )}
                  </span>
                }
                value={f.path}
              />
            ))}
          </RadioButtonGroup>
        )}
        {error && (
          <InlineNotification
            kind="error"
            title="Save failed"
            subtitle={error}
            lowContrast
            onCloseButtonClick={() => setError(null)}
          />
        )}
      </ModalBody>
      <ModalFooter>
        <Button kind="secondary" onClick={onClose} disabled={saving}>
          Cancel
        </Button>
        <Button
          kind="primary"
          onClick={handleSave}
          disabled={saving || !selected || !entry?.text || fields.length === 0}
        >
          {saving ? "Saving…" : "Add example"}
        </Button>
      </ModalFooter>
    </ComposedModal>
  );
}
