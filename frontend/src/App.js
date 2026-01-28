import React, { useEffect, useState, useCallback } from "react";
import { Theme, Header, HeaderName, Content } from "@carbon/react";
import DocumentList from "./components/DocumentList";
import DocumentViewer from "./components/DocumentViewer";
import TextEntriesPanel from "./components/TextEntriesPanel";
import { fetchDocuments, fetchDocument } from "./services/api";
import "./App.scss";

export default function App() {
  const [documents, setDocuments] = useState([]);
  const [selectedDocId, setSelectedDocId] = useState(null);
  const [documentData, setDocumentData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [highlightedEntryId, setHighlightedEntryId] = useState(null);

  // Load document list on mount
  useEffect(() => {
    fetchDocuments()
      .then((docs) => {
        setDocuments(docs);
        if (docs.length > 0) {
          setSelectedDocId(docs[0].id);
        }
      })
      .catch(console.error);
  }, []);

  // Load document data when selection changes
  useEffect(() => {
    if (!selectedDocId) return;
    setLoading(true);
    setDocumentData(null);
    setHighlightedEntryId(null);
    fetchDocument(selectedDocId)
      .then(setDocumentData)
      .catch(console.error)
      .finally(() => setLoading(false));
  }, [selectedDocId]);

  const handleSelect = useCallback((id) => {
    setSelectedDocId(id);
  }, []);

  const handleHover = useCallback((entryId) => {
    setHighlightedEntryId(entryId);
  }, []);

  return (
    <Theme theme="g10">
      <Header aria-label="Document Viewer">
        <HeaderName prefix="IBM">Document Viewer</HeaderName>
      </Header>
      <Content className="app-content">
        <div className="app-layout">
          <aside className="app-layout__sidebar" data-testid="document-list-panel">
            <DocumentList
              documents={documents}
              selectedId={selectedDocId}
              onSelect={handleSelect}
            />
          </aside>
          <main className="app-layout__main" data-testid="document-viewer-panel">
            <DocumentViewer
              docId={selectedDocId}
              documentData={documentData}
              highlightedEntryId={highlightedEntryId}
              loading={loading}
            />
          </main>
          <aside className="app-layout__panel" data-testid="text-entries-panel">
            <TextEntriesPanel
              entries={documentData?.text_entries}
              onHoverEntry={handleHover}
              loading={loading}
            />
          </aside>
        </div>
      </Content>
    </Theme>
  );
}
