import React, { useState, useMemo, useCallback, useRef } from "react";
import {
  Button,
  IconButton,
  Search,
  StructuredListWrapper,
  StructuredListHead,
  StructuredListRow,
  StructuredListCell,
  StructuredListBody,
} from "@carbon/react";
import {
  DocumentPdf,
  DocumentWordProcessor,
  PresentationFile,
  TrashCan,
  Upload,
} from "@carbon/react/icons";

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

export default function DocumentList({
  documents,
  selectedId,
  onSelect,
  onUpload,
  onDelete,
  uploading,
}) {
  const [query, setQuery] = useState("");
  const fileInputRef = useRef(null);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return documents;
    return documents.filter((doc) =>
      doc.filename.toLowerCase().includes(q)
    );
  }, [documents, query]);

  const handleQueryChange = useCallback((e) => setQuery(e.target.value), []);

  const handleUploadClick = useCallback(() => {
    fileInputRef.current?.click();
  }, []);

  const handleFilesPicked = useCallback(
    (e) => {
      const picked = Array.from(e.target.files || []);
      // Reset so the same filename can be re-picked after a failed upload
      // (the input only fires `change` on new selections).
      e.target.value = "";
      if (picked.length === 0 || !onUpload) return;
      onUpload(picked);
    },
    [onUpload],
  );

  const handleDelete = useCallback(
    (doc, event) => {
      event.stopPropagation();
      // eslint-disable-next-line no-alert
      if (!window.confirm(`Delete "${doc.filename}"?`)) return;
      onDelete?.(doc);
    },
    [onDelete],
  );

  return (
    <section className="document-list" aria-labelledby="document-list-heading">
      <div className="document-list__heading-row">
        <h2
          id="document-list-heading"
          className="document-list__heading"
        >
          Documents
        </h2>
        {onUpload && (
          <>
            <Button
              kind="ghost"
              size="sm"
              renderIcon={Upload}
              onClick={handleUploadClick}
              disabled={!!uploading}
              data-testid="upload-button"
            >
              {uploading ? "Uploading…" : "Upload"}
            </Button>
            <input
              ref={fileInputRef}
              type="file"
              accept=".pdf,.docx,.pptx"
              multiple
              onChange={handleFilesPicked}
              style={{ display: "none" }}
              data-testid="upload-input"
            />
          </>
        )}
      </div>
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
                <StructuredListCell>
                  <span className="document-list__size-row">
                    <span>{formatSize(doc.size)}</span>
                    {onDelete && (
                      <IconButton
                        label={`Delete ${doc.filename}`}
                        kind="ghost"
                        size="sm"
                        onClick={(e) => handleDelete(doc, e)}
                        data-testid={`doc-delete-${doc.id}`}
                      >
                        <TrashCan />
                      </IconButton>
                    )}
                  </span>
                </StructuredListCell>
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
