import React, { useState } from "react";
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

  const filtered = documents.filter((doc) =>
    doc.filename.toLowerCase().includes(query.toLowerCase())
  );

  return (
    <div className="document-list">
      <Search
        size="md"
        placeholder="Search documents..."
        labelText="Search documents"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      <StructuredListWrapper selection className="document-list__items">
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
            return (
              <StructuredListRow
                key={doc.id}
                onClick={() => onSelect(doc.id)}
                className={isSelected ? "document-list__row--selected" : ""}
                tabIndex={0}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") onSelect(doc.id);
                }}
                data-testid={`doc-row-${doc.id}`}
              >
                <StructuredListCell>
                  <span className="document-list__name">
                    <Icon size={20} />
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
    </div>
  );
}
