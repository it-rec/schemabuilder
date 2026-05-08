import React, { useState, useMemo, useCallback } from "react";
import { Tag, Tooltip } from "@carbon/react";
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
    return (
      <Tooltip label={`Confidence: ${Math.round(confidence * 100)}%`} align="left">
        <CheckmarkFilled size={16} className="fields-panel__confidence--high" />
      </Tooltip>
    );
  }
  if (confidence >= 0.5) {
    return (
      <Tooltip label={`Confidence: ${Math.round(confidence * 100)}%`} align="left">
        <WarningFilled size={16} className="fields-panel__confidence--medium" />
      </Tooltip>
    );
  }
  return (
    <Tooltip label="Not found" align="left">
      <UndefinedFilled size={16} className="fields-panel__confidence--low" />
    </Tooltip>
  );
});

const SubFieldRow = React.memo(function SubFieldRow({
  field,
  parentName,
  index,
  subField,
  onHoverField,
}) {
  const handleEnter = useCallback(() => {
    if (subField.matched_entry_id != null) onHoverField(subField);
  }, [subField, onHoverField]);
  const handleLeave = useCallback(() => onHoverField(null), [onHoverField]);

  return (
    <li
      className="fields-panel__sub-field"
      onMouseEnter={handleEnter}
      onMouseLeave={handleLeave}
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

const FieldItem = React.memo(function FieldItem({ field, onHoverField }) {
  const [expanded, setExpanded] = useState(false);
  const isArray = field.type === "array";
  const hasValue = field.extracted_value != null;
  const hasItems = isArray && field.items && field.items.length > 0;

  const fieldLabel = useMemo(() => field.name.replace(/_/g, " "), [field.name]);

  const handleEnter = useCallback(() => {
    if (field.matched_entry_id != null) onHoverField(field);
  }, [field, onHoverField]);
  const handleLeave = useCallback(() => onHoverField(null), [onHoverField]);
  const handleClick = useCallback(() => {
    if (isArray) setExpanded((e) => !e);
  }, [isArray]);

  return (
    <li className="fields-panel__field">
      <div
        className={`fields-panel__field-header ${hasValue || hasItems ? "fields-panel__field-header--matched" : ""}`}
        onMouseEnter={handleEnter}
        onMouseLeave={handleLeave}
        onClick={handleClick}
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
            <Information size={12} />
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
  loading,
}) {
  const fields = extraction?.fields;

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
      <div className="fields-panel">
        <h4 className="fields-panel__title">Document Fields</h4>
        <p className="fields-panel__empty">Extracting fields...</p>
      </div>
    );
  }

  if (!extraction) {
    return (
      <div className="fields-panel">
        <h4 className="fields-panel__title">Document Fields</h4>
        <p className="fields-panel__empty">
          Select a document definition to extract fields.
        </p>
      </div>
    );
  }

  return (
    <div className="fields-panel">
      <div className="fields-panel__header">
        <h4 className="fields-panel__title">
          {extraction.document_type}
        </h4>
        <Tag size="sm" type={matchedCount > 0 ? "green" : "gray"}>
          {matchedCount}/{fields.length} found
        </Tag>
      </div>
      {extraction.document_description && (
        <p className="fields-panel__description">
          {extraction.document_description}
        </p>
      )}
      <ul className="fields-panel__list">
        {fields.map((field) => (
          <FieldItem
            key={field.name}
            field={field}
            onHoverField={onHoverField}
          />
        ))}
      </ul>
    </div>
  );
}
