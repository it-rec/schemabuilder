import React, { useState, useMemo, useCallback } from "react";
import {
  OverflowMenu,
  OverflowMenuItem,
  Tag,
  Tooltip,
} from "@carbon/react";
import {
  CheckmarkFilled,
  WarningFilled,
  UndefinedFilled,
  ChevronDown,
  ChevronRight,
  Information,
} from "@carbon/react/icons";

const ConfidenceIndicator = React.memo(function ConfidenceIndicator({ confidence }) {
  if (confidence >= 0.8) {
    const label = `High confidence: ${Math.round(confidence * 100)}%`;
    return (
      <Tooltip label={label} align="left">
        <button
          type="button"
          className="fields-panel__confidence-trigger"
          aria-label={label}
        >
          <CheckmarkFilled
            size={16}
            className="fields-panel__confidence--high"
            aria-hidden="true"
          />
        </button>
      </Tooltip>
    );
  }
  if (confidence >= 0.5) {
    const label = `Medium confidence: ${Math.round(confidence * 100)}%`;
    return (
      <Tooltip label={label} align="left">
        <button
          type="button"
          className="fields-panel__confidence-trigger"
          aria-label={label}
        >
          <WarningFilled
            size={16}
            className="fields-panel__confidence--medium"
            aria-hidden="true"
          />
        </button>
      </Tooltip>
    );
  }
  return (
    <Tooltip label="Not found" align="left">
      <button
        type="button"
        className="fields-panel__confidence-trigger"
        aria-label="Not found"
      >
        <UndefinedFilled
          size={16}
          className="fields-panel__confidence--low"
          aria-hidden="true"
        />
      </button>
    </Tooltip>
  );
});

const SubFieldRow = React.memo(function SubFieldRow({
  parentName,
  index,
  subField,
  onHoverField,
  highlighted,
}) {
  const isMatched = subField.matched_entry_id != null;
  const handleEnter = useCallback(() => {
    if (isMatched) onHoverField(subField);
  }, [isMatched, subField, onHoverField]);
  const handleLeave = useCallback(() => onHoverField(null), [onHoverField]);

  return (
    <li
      className={
        "fields-panel__sub-field" +
        (highlighted ? " fields-panel__sub-field--highlighted" : "")
      }
      onMouseEnter={handleEnter}
      onMouseLeave={handleLeave}
      onFocus={handleEnter}
      onBlur={handleLeave}
      data-testid={`field-${parentName}-${index}-${subField.name}`}
    >
      <div className="fields-panel__sub-field-row">
        <span className="fields-panel__sub-field-name">
          {subField.name.replace(/_/g, " ")}
        </span>
        <ConfidenceIndicator confidence={subField.confidence || 0} />
      </div>
      {subField.extracted_value ? (
        <span className="fields-panel__sub-field-value">
          {subField.extracted_value}
        </span>
      ) : (
        <span className="fields-panel__sub-field-value fields-panel__sub-field-value--empty">
          Not found
        </span>
      )}
    </li>
  );
});

const FieldItem = React.memo(function FieldItem({
  field,
  onHoverField,
  highlightedEntryId,
}) {
  const [expanded, setExpanded] = useState(false);
  const isArray = field.type === "array";
  const hasValue = field.extracted_value != null;
  const hasItems = isArray && field.items && field.items.length > 0;
  const isMatched = field.matched_entry_id != null;

  const fieldLabel = useMemo(() => field.name.replace(/_/g, " "), [field.name]);

  const handleEnter = useCallback(() => {
    if (isMatched) onHoverField(field);
  }, [isMatched, field, onHoverField]);
  const handleLeave = useCallback(() => onHoverField(null), [onHoverField]);
  const handleClick = useCallback(() => {
    if (isArray) setExpanded((e) => !e);
  }, [isArray]);
  // Mirror the click handler so keyboard users (Tab to focus, Space/Enter to
  // toggle) can expand array fields.
  const handleKeyDown = useCallback(
    (e) => {
      if (!isArray) return;
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        setExpanded((prev) => !prev);
      }
    },
    [isArray]
  );

  // Array headers are toggle buttons; non-array headers rely on the inner
  // confidence-indicator button as the keyboard focus target — its focus events
  // bubble to the parent's onFocus/onBlur, which drives the highlight overlay.
  const interactiveProps = isArray
    ? {
        onClick: handleClick,
        onKeyDown: handleKeyDown,
        role: "button",
        tabIndex: 0,
        "aria-expanded": expanded,
      }
    : {};

  const isSelfHighlighted =
    highlightedEntryId != null && field.matched_entry_id === highlightedEntryId;
  const hasHighlightedChild =
    isArray &&
    highlightedEntryId != null &&
    field.items?.some((item) =>
      item.fields?.some((sf) => sf.matched_entry_id === highlightedEntryId),
    );
  const isHighlighted = isSelfHighlighted || hasHighlightedChild;

  return (
    <li className="fields-panel__field">
      <div
        className={
          "fields-panel__field-header" +
          (hasValue || hasItems ? " fields-panel__field-header--matched" : "") +
          (isHighlighted ? " fields-panel__field-header--highlighted" : "")
        }
        onMouseEnter={handleEnter}
        onMouseLeave={handleLeave}
        onFocus={handleEnter}
        onBlur={handleLeave}
        {...interactiveProps}
        data-testid={`field-${field.name}`}
      >
        <div className="fields-panel__field-label-row">
          {isArray && (
            <span className="fields-panel__expand-icon">
              {expanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
            </span>
          )}
          <span className="fields-panel__field-name">{fieldLabel}</span>
          {!isArray && <ConfidenceIndicator confidence={field.confidence || 0} />}
          {isArray && hasItems && (
            <Tag size="sm" type="blue">
              {field.items.length} item{field.items.length !== 1 ? "s" : ""}
            </Tag>
          )}
        </div>

        <p className="fields-panel__field-description">{field.description}</p>

        {!isArray && hasValue && (
          <div className="fields-panel__field-value">
            <span className="fields-panel__field-value-text">{field.extracted_value}</span>
            {field.page && (
              <span className="fields-panel__page-badge">p.{field.page}</span>
            )}
          </div>
        )}

        {!isArray && !hasValue && (
          <div className="fields-panel__field-value fields-panel__field-value--empty">
            <span className="fields-panel__field-value-text">Not found</span>
          </div>
        )}

        {field.examples && field.examples.length > 0 && !hasValue && (
          <div className="fields-panel__field-examples">
            <Information size={12} aria-hidden="true" />
            <span>e.g. {field.examples.join(", ")}</span>
          </div>
        )}
      </div>

      {isArray && expanded && (
        <div className="fields-panel__array-items">
          {hasItems ? (
            field.items.map((item, idx) => (
              <div key={idx} className="fields-panel__array-item">
                <div className="fields-panel__array-item-header">
                  Item {idx + 1}
                </div>
                <ul className="fields-panel__array-item-fields">
                  {item.fields.map((subField) => (
                    <SubFieldRow
                      key={subField.name}
                      parentName={field.name}
                      index={idx}
                      subField={subField}
                      onHoverField={onHoverField}
                      highlighted={
                        highlightedEntryId != null &&
                        subField.matched_entry_id === highlightedEntryId
                      }
                    />
                  ))}
                </ul>
              </div>
            ))
          ) : (
            <p className="fields-panel__array-empty">No items found.</p>
          )}
        </div>
      )}
    </li>
  );
});

export default function FieldsPanel({
  extraction,
  onHoverField,
  onExport,
  highlightedField,
  loading,
}) {
  const fields = extraction?.fields;
  const highlightedEntryId = highlightedField?.matched_entry_id ?? null;
  const tableNames = useMemo(
    () => (Array.isArray(extraction?.target_tables) ? extraction.target_tables : []),
    [extraction],
  );

  const matchedCount = useMemo(() => {
    if (!fields) return 0;
    let n = 0;
    for (const f of fields) {
      if (f.extracted_value != null) {
        n++;
      } else if (f.type === "array" && f.items && f.items.length > 0) {
        n++;
      }
    }
    return n;
  }, [fields]);

  if (loading) {
    return (
      <div className="fields-panel" aria-busy="true">
        <h2 className="fields-panel__title">Document Fields</h2>
        <p className="fields-panel__empty" role="status" aria-live="polite">
          Extracting fields...
        </p>
      </div>
    );
  }

  if (!extraction) {
    return (
      <div className="fields-panel">
        <h2 className="fields-panel__title">Document Fields</h2>
        <p className="fields-panel__empty">
          Select a document definition to extract fields.
        </p>
      </div>
    );
  }

  return (
    <div className="fields-panel">
      <div className="fields-panel__header">
        <h2 className="fields-panel__title">
          {extraction.document_type}
        </h2>
        <div className="fields-panel__header-actions">
          <Tag size="sm" type={matchedCount > 0 ? "green" : "gray"}>
            {matchedCount}/{fields.length} found
          </Tag>
          {onExport && tableNames.length > 0 && (
            <OverflowMenu
              size="sm"
              flipped
              iconDescription="Export options"
              data-testid="export-menu"
            >
              <OverflowMenuItem
                itemText="Download all tables (JSON)"
                onClick={() => onExport({ format: "json" })}
              />
              {tableNames.map((t) => (
                <OverflowMenuItem
                  key={t}
                  itemText={`Download "${t}" (CSV)`}
                  onClick={() => onExport({ format: "csv", table: t })}
                />
              ))}
            </OverflowMenu>
          )}
        </div>
      </div>
      {extraction.document_description && (
        <p className="fields-panel__description">
          {extraction.document_description}
        </p>
      )}
      {extraction.extraction_error && (
        <div
          className="fields-panel__error"
          role="alert"
          data-testid="extraction-error"
        >
          <WarningFilled
            size={16}
            className="fields-panel__error-icon"
            aria-hidden="true"
          />
          <span>
            Text extraction failed: {extraction.extraction_error}. Matches below
            are based on no extracted text.
          </span>
        </div>
      )}
      <ul className="fields-panel__list">
        {fields.map((field) => (
          <FieldItem
            key={field.name}
            field={field}
            onHoverField={onHoverField}
            highlightedEntryId={highlightedEntryId}
          />
        ))}
      </ul>
    </div>
  );
}
