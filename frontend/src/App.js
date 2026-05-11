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
  getPageImageUrl,
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

  // Load document list and definitions on mount. AbortController cancels
  // in-flight fetches if the component unmounts (HMR / route change), avoiding
  // late state writes against an unmounted tree.
  useEffect(() => {
    const ctrl = new AbortController();
    fetchDocuments({ signal: ctrl.signal })
      .then((docs) => {
        setDocuments(docs);
        if (docs.length > 0) {
          setSelectedDocId(docs[0].id);
        }
      })
      .catch((err) => {
        if (err?.name !== "AbortError") console.error(err);
      });

    fetchDefinitions({ signal: ctrl.signal })
      .then((defs) => {
        setDefinitions(defs);
        if (defs.length > 0) {
          setSelectedDefId(defs[0].id);
        }
      })
      .catch((err) => {
        if (err?.name !== "AbortError") console.error(err);
      });
    return () => ctrl.abort();
  }, []);

  // Load document data when selection changes. AbortController kills the
  // in-flight metadata fetch when the user switches docs again before it
  // resolves — saves backend cycles vs. the prior `cancelled` flag, which
  // only suppressed the late state write.
  useEffect(() => {
    if (!selectedDocId) return;
    const ctrl = new AbortController();
    setLoading(true);
    setDocumentData(null);
    setExtraction(null);
    setHighlightedField(null);

    // Kick page-1 (and a low-priority page-2) image fetches off in parallel
    // with metadata. The metadata request also triggers backend prefetch
    // (page render + Docling), so by the time documentData arrives the PNG
    // is usually already in the browser disk cache and the <img> in
    // DocumentViewer hits it instantly. Page 2 follows so a click on "next"
    // is also warm without competing with the visible page's request.
    const warmImg = new Image();
    if ("fetchPriority" in warmImg) warmImg.fetchPriority = "high";
    warmImg.decoding = "async";
    warmImg.src = getPageImageUrl(selectedDocId, 1);
    const warmImg2 = new Image();
    if ("fetchPriority" in warmImg2) warmImg2.fetchPriority = "low";
    warmImg2.decoding = "async";
    warmImg2.src = getPageImageUrl(selectedDocId, 2);

    fetchDocument(selectedDocId, { signal: ctrl.signal })
      .then((data) => {
        if (!ctrl.signal.aborted) setDocumentData(data);
      })
      .catch((err) => {
        if (err?.name !== "AbortError") console.error(err);
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setLoading(false);
      });
    return () => {
      ctrl.abort();
      // Release the warm-up references so the browser can free the decoded
      // bitmaps if we abandoned this doc before the images landed.
      warmImg.src = "";
      warmImg2.src = "";
    };
  }, [selectedDocId]);

  // Extract fields when document + definition are both available. Cancels the
  // in-flight POST when inputs change so a stale extraction can't land in
  // state and (more importantly) doesn't keep the backend's concurrency slot
  // occupied longer than necessary.
  useEffect(() => {
    if (!selectedDocId || !selectedDefId || !documentData) return;
    const ctrl = new AbortController();
    setExtracting(true);
    setExtraction(null);
    // Drop any field highlight from the prior definition; its bbox refers to
    // a field object that no longer exists in the new extraction.
    setHighlightedField(null);
    extractFields(selectedDocId, selectedDefId, { signal: ctrl.signal })
      .then((data) => {
        if (!ctrl.signal.aborted) setExtraction(data);
      })
      .catch((err) => {
        if (err?.name !== "AbortError") console.error(err);
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setExtracting(false);
      });
    return () => ctrl.abort();
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
