import React, { useEffect, useState, useCallback, useMemo } from "react";
import {
  Theme,
  Header,
  HeaderName,
  Content,
  Dropdown,
} from "@carbon/react";
import DocumentList from "./components/DocumentList";
import DocumentViewer from "./components/DocumentViewer";
import FieldsPanel from "./components/FieldsPanel";
import {
  fetchDocuments,
  fetchDocument,
  fetchDefinitions,
  extractFields,
} from "./services/api";
import "./App.scss";

export default function App() {
  const [documents, setDocuments] = useState([]);
  const [selectedDocId, setSelectedDocId] = useState(null);
  const [documentData, setDocumentData] = useState(null);
  const [loading, setLoading] = useState(false);

  // Document definitions
  const [definitions, setDefinitions] = useState([]);
  const [selectedDefId, setSelectedDefId] = useState(null);
  const [extraction, setExtraction] = useState(null);
  const [extracting, setExtracting] = useState(false);

  // Highlighted field (for document overlay)
  const [highlightedField, setHighlightedField] = useState(null);

  // Load document list and definitions on mount
  useEffect(() => {
    fetchDocuments()
      .then((docs) => {
        setDocuments(docs);
        if (docs.length > 0) {
          setSelectedDocId(docs[0].id);
        }
      })
      .catch(console.error);

    fetchDefinitions()
      .then((defs) => {
        setDefinitions(defs);
        if (defs.length > 0) {
          setSelectedDefId(defs[0].id);
        }
      })
      .catch(console.error);
  }, []);

  // Load document data when selection changes. Use a `cancelled` flag so a
  // slow response from a previously-selected document can't overwrite state
  // for the doc the user has since switched to.
  useEffect(() => {
    if (!selectedDocId) return;
    let cancelled = false;
    setLoading(true);
    setDocumentData(null);
    setExtraction(null);
    setHighlightedField(null);
    fetchDocument(selectedDocId)
      .then((data) => {
        if (!cancelled) setDocumentData(data);
      })
      .catch((err) => {
        if (!cancelled) console.error(err);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedDocId]);

  // Extract fields when document + definition are both available. Same
  // cancellation guard: rapid doc/definition switches can't surface stale
  // extraction results from a prior pair.
  useEffect(() => {
    if (!selectedDocId || !selectedDefId || !documentData) return;
    let cancelled = false;
    setExtracting(true);
    setExtraction(null);
    extractFields(selectedDocId, selectedDefId)
      .then((data) => {
        if (!cancelled) setExtraction(data);
      })
      .catch((err) => {
        if (!cancelled) console.error(err);
      })
      .finally(() => {
        if (!cancelled) setExtracting(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedDocId, selectedDefId, documentData]);

  const handleSelect = useCallback((id) => {
    setSelectedDocId(id);
  }, []);

  const handleHoverField = useCallback((field) => {
    setHighlightedField(field);
  }, []);

  const handleDefChange = useCallback(({ selectedItem }) => {
    setSelectedDefId(selectedItem?.id || null);
  }, []);

  // Bboxes returned by extraction are in Docling's coordinate space, which can
  // differ from the pypdfium2 dims used for the rendered image. Prefer Docling's
  // page_dimensions for highlight math when available.
  const viewerData = useMemo(() => {
    if (!documentData) return null;
    const extDims = extraction?.page_dimensions;
    if (extDims && Object.keys(extDims).length > 0) {
      return {
        ...documentData,
        page_dimensions: { ...documentData.page_dimensions, ...extDims },
      };
    }
    return documentData;
  }, [documentData, extraction]);

  return (
    <Theme theme="g10">
      <Header aria-label="Document Viewer">
        <HeaderName prefix="IBM">Schema Builder</HeaderName>
      </Header>
      <Content className="app-content">
        <div className="app-layout">
          <aside className="app-layout__sidebar" data-testid="document-list-panel">
            <div className="definition-selector">
              <Dropdown
                id="definition-selector"
                titleText="Document class"
                label="Select a definition..."
                items={definitions}
                itemToString={(item) => item?.document_type || ""}
                selectedItem={definitions.find((d) => d.id === selectedDefId) || null}
                onChange={handleDefChange}
                size="sm"
              />
            </div>
            <DocumentList
              documents={documents}
              selectedId={selectedDocId}
              onSelect={handleSelect}
            />
          </aside>
          <main className="app-layout__main" data-testid="document-viewer-panel">
            <DocumentViewer
              docId={selectedDocId}
              documentData={viewerData}
              highlightedField={highlightedField}
              loading={loading}
            />
          </main>
          <aside className="app-layout__panel" data-testid="fields-panel">
            <FieldsPanel
              extraction={extraction}
              onHoverField={handleHoverField}
              loading={loading || extracting}
            />
          </aside>
        </div>
      </Content>
    </Theme>
  );
}
