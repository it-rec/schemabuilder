import React, { useState, useMemo, useCallback } from "react";
import {
  Search,
  StructuredListWrapper,
  StructuredListHead,
  StructuredListRow,
  StructuredListCell,
  StructuredListBody,
} from "@carbon/react";
import { DocumentPdf, DocumentWordProcessor, PresentationFile } from "@carbon/react/icons";

const ICON_MAP = {
  ".pdf": DocumentPdf,
  ".docx": DocumentWordProcessor,
  ".pptx": PresentationFile,
};

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function DocumentList({ documents, selectedId, onSelect }) {
  const [query, setQuery] = useState("");

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return documents;
    return documents.filter((doc) =>
      doc.filename.toLowerCase().includes(q)
    );
  }, [documents, query]);

  const handleQueryChange = useCallback((e) => setQuery(e.target.value), []);

  return (
    <section className="document-list" aria-labelledby="document-list-heading">
      <h2
        id="document-list-heading"
        className="document-list__heading"
      >
        Documents
      </h2>
      <Search
        size="md"
        placeholder="Search documents..."
        labelText="Search documents"
        value={query}
        onChange={handleQueryChange}
      />
      <StructuredListWrapper
        className="document-list__items"
        aria-label="Document selection"
      >
        <StructuredListHead>
          <StructuredListRow head>
            <StructuredListCell head>Document</StructuredListCell>
            <StructuredListCell head>Size</StructuredListCell>
          </StructuredListRow>
        </StructuredListHead>
        <StructuredListBody>
          {filtered.map((doc) => {
            const Icon = ICON_MAP[doc.extension] || DocumentPdf;
            const isSelected = doc.id === selectedId;
            const selectedClass = isSelected ? " document-list__row--selected" : "";
            return (
              <StructuredListRow
                key={doc.id}
                onClick={() => onSelect(doc.id)}
                className={`document-list__row${selectedClass}`}
                role="button"
                tabIndex={0}
                aria-pressed={isSelected}
                aria-label={`Select ${doc.filename}, ${formatSize(doc.size)}`}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    onSelect(doc.id);
                  }
                }}
                data-testid={`doc-row-${doc.id}`}
              >
                <StructuredListCell>
                  <span className="document-list__name">
                    <Icon size={20} aria-hidden="true" />
                    <span>{doc.filename}</span>
                  </span>
                </StructuredListCell>
                <StructuredListCell>{formatSize(doc.size)}</StructuredListCell>
              </StructuredListRow>
            );
          })}
          {filtered.length === 0 && (
            <StructuredListRow>
              <StructuredListCell colSpan={2}>
                No documents found.
              </StructuredListCell>
            </StructuredListRow>
          )}
        </StructuredListBody>
      </StructuredListWrapper>
    </section>
  );
}
