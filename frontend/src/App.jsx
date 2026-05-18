import React, { useEffect, useState, useCallback, useMemo, useRef } from "react";
import {
  Theme,
  Header,
  HeaderName,
  HeaderGlobalBar,
  HeaderGlobalAction,
  Content,
  Dropdown,
  Button,
} from "@carbon/react";
import { Add, Asleep, Edit, Light } from "@carbon/react/icons";
import DocumentList from "./components/DocumentList";
import DocumentViewer from "./components/DocumentViewer";
import FieldsPanel from "./components/FieldsPanel";
import DefinitionEditor from "./components/DefinitionEditor";
import DefinitionHistory from "./components/DefinitionHistory";
import ExampleTeacher from "./components/ExampleTeacher";
import BatchExtractModal from "./components/BatchExtractModal";
import OfflineOverlay from "./components/OfflineOverlay";
import { useConnectionStatus } from "./hooks/useConnectionStatus";
import {
  fetchDocuments,
  fetchDocument,
  fetchDefinitions,
  extractFields,
  exportTablesJson,
  exportTableCsv,
  getPageImageUrl,
  uploadDocument,
  deleteDocument,
} from "./services/api";
import "./App.scss";

export default function App() {
  // Backend reachability. While `online` is anything other than true we
  // render the OfflineOverlay and mark the rest of the tree `inert` so no
  // network-bound interaction can happen. `reloadKey` flips on each
  // null→true / false→true transition so the initial-load effect re-runs
  // and the panels repopulate with fresh data once the backend is back.
  const { online, reloadKey } = useConnectionStatus();

  const [documents, setDocuments] = useState([]);
  const [selectedDocId, setSelectedDocId] = useState(null);
  const [documentData, setDocumentData] = useState(null);

  // Document definitions
  const [definitions, setDefinitions] = useState([]);
  const [selectedDefId, setSelectedDefId] = useState(null);
  const [extraction, setExtraction] = useState(null);

  // Highlighted field (for document overlay)
  const [highlightedField, setHighlightedField] = useState(null);

  // `loading` / `extracting` are derived from "we want this data but don't
  // have it yet" rather than tracked as separate state. The store-previous
  // blocks below null out the stale data the moment its identifying input
  // changes, so these expressions flip true immediately on selection
  // change and back to false the moment the new fetch lands.
  //
  // Trade-off: a silent fetch failure (5xx / network error not surfaced
  // via the catch block) leaves documentData null and the spinner
  // visible until the user picks a different doc. The original code's
  // .finally(setLoading(false)) hid that stuck-state. We accept the
  // regression because the broader OfflineOverlay already covers the
  // common "backend unreachable" case, and silent 4xx/5xx for a
  // specific doc id is rare in practice.
  const loading =
    online === true && !!selectedDocId && documentData == null;
  const extracting =
    online === true &&
    !!selectedDocId &&
    !!selectedDefId &&
    documentData != null &&
    extraction == null;

  // Definition editor modal — `editorMode` is null when closed, else "create" or
  // "edit". Tracking the mode separately from `open` keeps the modal's body
  // logic (hydrate-on-open) simple and lets the dialog tear down cleanly.
  const [editorMode, setEditorMode] = useState(null);
  // When the user enters create mode via the FieldsPanel empty-state CTA we
  // want the editor to kick off the LLM suggestion immediately, not require a
  // second click on the in-modal button. Flips back to false as soon as the
  // editor reads it so a manual Cancel + reopen starts fresh.
  const [editorAutoStartSuggest, setEditorAutoStartSuggest] = useState(false);
  // History modal — open when the user clicks "History" inside the editor.
  // Stored separately so it can sit on top of the editor (Carbon supports
  // stacked modals).
  const [historyOpen, setHistoryOpen] = useState(false);

  // Click-to-teach: the text entry the user clicked. Null when the teacher
  // modal is closed. Storing the entry (not just open/closed) lets the modal
  // render the chosen text without needing a second prop.
  const [teachEntry, setTeachEntry] = useState(null);

  // Theme: g10 (light) ↔ g90 (dark). Persisted to localStorage so the user's
  // choice survives reloads. Also honors the OS-level dark-mode preference
  // when no value has been stored yet.
  const [theme, setTheme] = useState(() => {
    try {
      const stored = window.localStorage.getItem("schemabuilder.theme");
      if (stored === "g10" || stored === "g90") return stored;
      if (
        window.matchMedia &&
        window.matchMedia("(prefers-color-scheme: dark)").matches
      ) {
        return "g90";
      }
    } catch (_) {
      /* localStorage may be blocked (private mode, sandboxed iframe) */
    }
    return "g10";
  });

  const toggleTheme = useCallback(() => {
    setTheme((t) => {
      const next = t === "g10" ? "g90" : "g10";
      try {
        window.localStorage.setItem("schemabuilder.theme", next);
      } catch (_) {
        /* ignore */
      }
      return next;
    });
  }, []);

  // Global keyboard navigation. j / ArrowDown → next document, k / ArrowUp →
  // previous. Skipped when focus is inside a form control / contenteditable
  // so the shortcuts don't fight with normal typing (including modal inputs).
  useEffect(() => {
    function onKeyDown(e) {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const t = e.target;
      if (
        t &&
        (t.tagName === "INPUT" ||
          t.tagName === "TEXTAREA" ||
          t.tagName === "SELECT" ||
          t.isContentEditable)
      ) {
        return;
      }
      if (!documents.length) return;
      const idx = documents.findIndex((d) => d.id === selectedDocId);
      if (e.key === "j" || e.key === "ArrowDown") {
        e.preventDefault();
        const next = idx < 0 ? 0 : Math.min(documents.length - 1, idx + 1);
        setSelectedDocId(documents[next].id);
      } else if (e.key === "k" || e.key === "ArrowUp") {
        e.preventDefault();
        const prev = idx <= 0 ? 0 : idx - 1;
        setSelectedDocId(documents[prev].id);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [documents, selectedDocId]);

  // Counter that forces the extract effect to re-run after a successful
  // teach. Bumping this is enough — the effect depends on `extractCycle`, so
  // it kicks off a fresh /extract that picks up the newly added example.
  const [extractCycle, setExtractCycle] = useState(0);
  // Set to `true` for one extract cycle to bypass the backend's SQLite cache
  // (the matcher result is keyed on (doc, definition); a code-side matcher
  // change otherwise stays invisible until the cache is invalidated). A ref,
  // not state, so consumption inside the effect doesn't itself retrigger.
  const pendingExtractRefreshRef = useRef(false);

  // Load document list and definitions whenever a connection is (re)established.
  // Gated on `online === true` so we don't fire fetches before the first probe
  // resolves or while the backend is unreachable. `reloadKey` re-triggers the
  // effect on every transition into "online" so a recovered backend repopulates
  // the panels without a page reload.
  useEffect(() => {
    if (online !== true) return;
    const ctrl = new AbortController();
    fetchDocuments({ signal: ctrl.signal })
      .then((docs) => {
        setDocuments(docs);
        setSelectedDocId((prev) => {
          if (prev && docs.some((d) => d.id === prev)) return prev;
          return docs.length > 0 ? docs[0].id : null;
        });
      })
      .catch((err) => {
        if (err?.name !== "AbortError") console.error(err);
      });

    fetchDefinitions({ signal: ctrl.signal })
      .then((defs) => {
        setDefinitions(defs);
        setSelectedDefId((prev) => {
          if (prev && defs.some((d) => d.id === prev)) return prev;
          return defs.length > 0 ? defs[0].id : null;
        });
      })
      .catch((err) => {
        if (err?.name !== "AbortError") console.error(err);
      });
    return () => ctrl.abort();
  }, [online, reloadKey]);

  // Reset doc-scoped state the moment the selection changes — done during
  // render via the store-previous-prop pattern instead of in the doc-load
  // effect, so the change rides the same commit that surfaced the new
  // selection (no flash of stale data before the effect catches up).
  const [prevSelectedDocId, setPrevSelectedDocId] = useState(selectedDocId);
  if (prevSelectedDocId !== selectedDocId) {
    setPrevSelectedDocId(selectedDocId);
    setDocumentData(null);
    setExtraction(null);
    setHighlightedField(null);
  }

  // Same pattern for the definition selection and the teach/restore
  // refetch counter — both invalidate the extraction (but not the loaded
  // document) and the highlighted field that lived inside it.
  const [prevSelectedDefId, setPrevSelectedDefId] = useState(selectedDefId);
  const [prevExtractCycle, setPrevExtractCycle] = useState(extractCycle);
  if (
    prevSelectedDefId !== selectedDefId ||
    prevExtractCycle !== extractCycle
  ) {
    setPrevSelectedDefId(selectedDefId);
    setPrevExtractCycle(extractCycle);
    setExtraction(null);
    setHighlightedField(null);
  }

  // Load document data when selection changes. AbortController kills the
  // in-flight metadata fetch when the user switches docs again before it
  // resolves — saves backend cycles vs. the prior `cancelled` flag, which
  // only suppressed the late state write.
  useEffect(() => {
    if (!selectedDocId || online !== true) return;
    const ctrl = new AbortController();

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
      });
    return () => {
      ctrl.abort();
      // Release the warm-up references so the browser can free the decoded
      // bitmaps if we abandoned this doc before the images landed.
      warmImg.src = "";
      warmImg2.src = "";
    };
  }, [selectedDocId, online, reloadKey]);

  // Extract fields when document + definition are both available. Cancels the
  // in-flight POST when inputs change so a stale extraction can't land in
  // state and (more importantly) doesn't keep the backend's concurrency slot
  // occupied longer than necessary.
  useEffect(() => {
    if (!selectedDocId || !selectedDefId || !documentData || online !== true) return;
    const ctrl = new AbortController();
    const refresh = pendingExtractRefreshRef.current;
    pendingExtractRefreshRef.current = false;
    extractFields(selectedDocId, selectedDefId, {
      signal: ctrl.signal,
      refresh,
    })
      .then((data) => {
        if (!ctrl.signal.aborted) setExtraction(data);
      })
      .catch((err) => {
        if (err?.name !== "AbortError") console.error(err);
      });
    return () => ctrl.abort();
    // extractCycle is in the dep list so a successful teach can force a
    // fresh extraction via `setExtractCycle(c => c + 1)`.
  }, [selectedDocId, selectedDefId, documentData, extractCycle, online, reloadKey]);

  const handleSelect = useCallback((id) => {
    setSelectedDocId(id);
  }, []);

  const handleHoverField = useCallback((field) => {
    setHighlightedField(field);
  }, []);

  const handleDefChange = useCallback(({ selectedItem }) => {
    setSelectedDefId(selectedItem?.id || null);
  }, []);

  // Force a fresh /extract that bypasses the backend's SQLite cache. Needed
  // when the matcher has changed on the server (deployed code / edited
  // examples) but (doc, definition) would otherwise be a cache hit and
  // return last week's result.
  const handleRefreshExtraction = useCallback(() => {
    pendingExtractRefreshRef.current = true;
    setExtractCycle((c) => c + 1);
  }, []);

  // Re-fetch the definitions list after a save/delete so newly created classes
  // appear in the dropdown immediately, and a renamed document_type is
  // reflected without a page reload.
  const refreshDefinitions = useCallback(async () => {
    try {
      const defs = await fetchDefinitions();
      setDefinitions(defs);
      return defs;
    } catch (err) {
      console.error(err);
      return null;
    }
  }, []);

  const handleTeachEntry = useCallback((entry) => {
    setTeachEntry(entry);
  }, []);

  const [uploading, setUploading] = useState(false);
  // Documents queued into the BatchExtractModal. Null when the modal is
  // closed, an array of {id, filename} when open.
  const [batchDocs, setBatchDocs] = useState(null);

  const handleRunBatch = useCallback((docs) => {
    if (!docs?.length) return;
    setBatchDocs(docs);
  }, []);

  const refreshDocuments = useCallback(async () => {
    try {
      const docs = await fetchDocuments();
      setDocuments(docs);
      return docs;
    } catch (err) {
      console.error(err);
      return null;
    }
  }, []);

  const handleUploadDocuments = useCallback(
    async (files) => {
      setUploading(true);
      let lastUploaded = null;
      try {
        // Upload sequentially: the backend caps concurrent /extract calls, but
        // serial uploads also surface per-file errors more cleanly than a
        // Promise.all bulk reject.
        for (const file of files) {
          try {
            lastUploaded = await uploadDocument(file);
          } catch (err) {
            console.error(`Upload of ${file.name} failed:`, err);
          }
        }
      } finally {
        setUploading(false);
      }
      const docs = await refreshDocuments();
      if (lastUploaded?.id && docs?.some((d) => d.id === lastUploaded.id)) {
        setSelectedDocId(lastUploaded.id);
      }
    },
    [refreshDocuments],
  );

  const handleDeleteDocument = useCallback(
    async (doc) => {
      try {
        await deleteDocument(doc.id);
      } catch (err) {
        console.error(err);
        return;
      }
      const docs = await refreshDocuments();
      if (selectedDocId === doc.id) {
        setSelectedDocId(docs && docs.length > 0 ? docs[0].id : null);
      }
    },
    [refreshDocuments, selectedDocId],
  );

  const handleTeachSaved = useCallback(() => {
    setTeachEntry(null);
    // The definition changed — re-run extraction so the newly taught
    // example takes effect immediately. The backend already invalidated the
    // definitions cache on its end.
    setExtractCycle((c) => c + 1);
  }, []);

  const handleEditorSaved = useCallback(
    async (saved) => {
      const defs = await refreshDefinitions();
      if (saved?.id && defs?.some((d) => d.id === saved.id)) {
        setSelectedDefId(saved.id);
      }
      setEditorMode(null);
      setEditorAutoStartSuggest(false);
    },
    [refreshDefinitions],
  );

  // Open the create-mode editor with the LLM suggestion pre-armed so the
  // user goes straight from "this doesn't match anything" to "here's a draft
  // schema, review it" without an interstitial click on the modal's button.
  const handleAutoGenerateFromDoc = useCallback(() => {
    if (!selectedDocId) return;
    setEditorAutoStartSuggest(true);
    setEditorMode("create");
  }, [selectedDocId]);

  // Open a blank New-definition modal. Same end state as clicking the "New"
  // button in the sidebar, exposed via the empty-state CTA in FieldsPanel so
  // users don't have to scan the UI for the button.
  const handleCreateBlankDefinition = useCallback(() => {
    setEditorAutoStartSuggest(false);
    setEditorMode("create");
  }, []);

  // Trigger a browser download for an exported table or the full JSON. We
  // resolve to a Blob (or a stringified JSON object) and synthesize an
  // anchor click so the user gets the standard "save as" UX without us
  // navigating the page. Errors are surfaced via console.error rather than
  // a toast for now — the FieldsPanel doesn't (yet) own user-facing
  // notifications.
  const handleExport = useCallback(
    async ({ format, table }) => {
      if (!selectedDocId || !selectedDefId) return;
      try {
        let blob, filename;
        if (format === "csv") {
          ({ blob, filename } = await exportTableCsv(
            selectedDocId,
            selectedDefId,
            table,
          ));
        } else {
          const payload = await exportTablesJson(selectedDocId, selectedDefId);
          blob = new Blob([JSON.stringify(payload, null, 2)], {
            type: "application/json",
          });
          filename = `${selectedDocId}-${selectedDefId}.json`;
        }
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        // Revoke on the next tick so the click has time to dispatch.
        setTimeout(() => URL.revokeObjectURL(url), 0);
      } catch (err) {
        console.error(err);
      }
    },
    [selectedDocId, selectedDefId],
  );

  const handleEditorDeleted = useCallback(
    async (deletedId) => {
      const defs = await refreshDefinitions();
      if (selectedDefId === deletedId) {
        // Fall back to the first remaining definition (or none) so the
        // extraction panel doesn't keep showing stale fields from a class
        // that no longer exists.
        setSelectedDefId(defs && defs.length > 0 ? defs[0].id : null);
      }
      setEditorMode(null);
    },
    [refreshDefinitions, selectedDefId],
  );

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

  // Flat list of every matched field with a placeable bbox. Array fields
  // collapse to one overlay per item (sub-fields of one row share the table-
  // cell bbox, so per-sub-field overlays would visually stack). `field` is the
  // payload sent back to onHoverField, so FieldsPanel can highlight the same
  // row that DocumentViewer just lit up. Scalar fields may also carry
  // `additional_bboxes` — every other place the same extracted value occurs;
  // we emit one extra overlay per occurrence, sharing the field's
  // matched_entry_id so hovering the field lights up every location at once.
  const extractedFields = useMemo(() => {
    if (!extraction?.fields) return [];
    const out = [];
    for (const f of extraction.fields) {
      if (f.matched_entry_id != null && f.bbox && f.page) {
        const baseLabel = f.name.replace(/_/g, " ");
        out.push({
          key: `field.${f.name}`,
          label: baseLabel,
          isPrimary: true,
          matched_entry_id: f.matched_entry_id,
          page: f.page,
          bbox: f.bbox,
          field: f,
        });
        if (Array.isArray(f.additional_bboxes)) {
          f.additional_bboxes.forEach((ab, i) => {
            if (!ab || !ab.bbox || !ab.page) return;
            out.push({
              key: `field.${f.name}.add.${i}`,
              label: baseLabel,
              isPrimary: false,
              matched_entry_id: f.matched_entry_id,
              page: ab.page,
              bbox: ab.bbox,
              field: f,
            });
          });
        }
      }
      if (f.type === "array" && Array.isArray(f.items)) {
        f.items.forEach((item, idx) => {
          // Prefer the item's own row-level geometry (whole line item). When
          // it's missing (older backend, or a column-routed-only item with
          // no row bbox in the entry) fall back to the first sub-field that
          // has a bbox — that's the legacy path which kept things working
          // before per-item geometry was added.
          let rowEntry = null;
          if (item.bbox && item.page && item.matched_entry_id != null) {
            rowEntry = {
              key: `array.${f.name}.${idx}`,
              label: `${f.name.replace(/_/g, " ")} #${idx + 1}`,
              matched_entry_id: item.matched_entry_id,
              page: item.page,
              bbox: item.bbox,
              field: {
                ...f,
                matched_entry_id: item.matched_entry_id,
                page: item.page,
                bbox: item.bbox,
              },
            };
          } else {
            const sub = item.fields?.find(
              (sf) => sf.bbox && sf.page && sf.matched_entry_id != null,
            );
            if (sub) {
              rowEntry = {
                key: `array.${f.name}.${idx}`,
                label: `${f.name.replace(/_/g, " ")} #${idx + 1}`,
                matched_entry_id: sub.matched_entry_id,
                page: sub.page,
                bbox: sub.bbox,
                field: sub,
              };
            }
          }
          if (rowEntry) out.push(rowEntry);

          // Cell-level overlays for sub-fields that got column-routed (their
          // matched_entry_id is a string "cell:<row>:<name>"). These are
          // invisible until directly hovered (CSS opacity:0 on --cell), so
          // they don't add visual clutter — only sub-field hover lights them.
          item.fields?.forEach((sub) => {
            if (
              sub &&
              sub.bbox &&
              sub.page &&
              typeof sub.matched_entry_id === "string" &&
              sub.matched_entry_id.startsWith("cell:")
            ) {
              out.push({
                key: `cell.${f.name}.${idx}.${sub.name}`,
                label: `${sub.name.replace(/_/g, " ")}`,
                isCell: true,
                matched_entry_id: sub.matched_entry_id,
                page: sub.page,
                bbox: sub.bbox,
                field: sub,
              });
            }
          });
        });
      }
    }
    return out;
  }, [extraction]);

  const offline = online !== true;
  // React 19 accepts `inert` as a boolean attribute, but only when the
  // prop value is strictly truthy and non-string — passing `""` is
  // treated as the empty/falsy case and the attribute is removed. Use
  // the boolean directly so the rendered HTML actually carries `inert`
  // while we're offline, keeping keyboard / pointer / screen-reader
  // interaction blocked beneath the overlay. The overlay sits outside
  // the inert subtree so its retry indicator stays operable.
  return (
    <Theme theme={theme}>
      <div className="app-shell" inert={offline || undefined}>
      <Header aria-label="Schema Builder">
        <HeaderName prefix="" href="#" onClick={(e) => e.preventDefault()}>
          Schema Builder
        </HeaderName>
        <HeaderGlobalBar>
          <HeaderGlobalAction
            aria-label={theme === "g10" ? "Switch to dark mode" : "Switch to light mode"}
            onClick={toggleTheme}
            tooltipAlignment="end"
            data-testid="theme-toggle"
          >
            {theme === "g10" ? <Asleep size={20} /> : <Light size={20} />}
          </HeaderGlobalAction>
        </HeaderGlobalBar>
      </Header>
      <Content className="app-content">
        <h1 className="cds--visually-hidden">Schema Builder</h1>
        <div className="app-layout">
          <aside
            className="app-layout__sidebar"
            aria-label="Documents"
            data-testid="document-list-panel"
          >
            <DocumentList
              documents={documents}
              selectedId={selectedDocId}
              onSelect={handleSelect}
              onUpload={handleUploadDocuments}
              onDelete={handleDeleteDocument}
              onRunBatch={selectedDefId ? handleRunBatch : null}
              uploading={uploading}
            />
          </aside>
          <main
            className="app-layout__main"
            aria-label="Document viewer"
            data-testid="document-viewer-panel"
          >
            <DocumentViewer
              docId={selectedDocId}
              documentData={viewerData}
              highlightedField={highlightedField}
              onHoverField={handleHoverField}
              onTeachEntry={selectedDefId ? handleTeachEntry : null}
              textEntries={extraction?.text_entries}
              extractedFields={extractedFields}
              loading={loading}
            />
          </main>
          <aside
            className="app-layout__panel"
            aria-label="Document class and extracted fields"
            data-testid="fields-panel"
          >
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
              <div className="definition-selector__actions">
                <Button
                  kind="ghost"
                  size="sm"
                  renderIcon={Add}
                  onClick={() => setEditorMode("create")}
                  data-testid="def-new-button"
                >
                  New
                </Button>
                <Button
                  kind="ghost"
                  size="sm"
                  renderIcon={Edit}
                  onClick={() => setEditorMode("edit")}
                  disabled={!selectedDefId}
                  data-testid="def-edit-button"
                >
                  Edit
                </Button>
              </div>
            </div>
            <FieldsPanel
              extraction={extraction}
              onHoverField={handleHoverField}
              onExport={handleExport}
              onRefresh={
                selectedDocId && selectedDefId ? handleRefreshExtraction : null
              }
              highlightedField={highlightedField}
              loading={loading || extracting}
              hasDocument={!!selectedDocId}
              hasDefinitions={definitions.length > 0}
              onAutoGenerate={selectedDocId ? handleAutoGenerateFromDoc : null}
              onCreateBlank={handleCreateBlankDefinition}
              selectedDocLabel={
                documents.find((d) => d.id === selectedDocId)?.filename
              }
            />
          </aside>
        </div>
      </Content>
      {editorMode != null && (
        <DefinitionEditor
          open
          mode={editorMode}
          definitionId={editorMode === "edit" ? selectedDefId : null}
          suggestForDocId={editorMode === "create" ? selectedDocId : null}
          suggestForDocLabel={
            editorMode === "create"
              ? documents.find((d) => d.id === selectedDocId)?.filename
              : null
          }
          autoStartSuggest={editorMode === "create" && editorAutoStartSuggest}
          onClose={() => {
            setEditorMode(null);
            setEditorAutoStartSuggest(false);
          }}
          onSaved={handleEditorSaved}
          onDeleted={handleEditorDeleted}
          onShowHistory={() => setHistoryOpen(true)}
        />
      )}
      {historyOpen && editorMode === "edit" && selectedDefId && (
        <DefinitionHistory
          // Force a fresh mount if the user switches definitions while the
          // history modal is open — useState defaults then re-run with the
          // right initial loading state for the new id.
          key={selectedDefId}
          open
          definitionId={selectedDefId}
          onClose={() => setHistoryOpen(false)}
          onRestored={() => {
            setHistoryOpen(false);
            // Force the extract effect to re-run with the restored definition.
            setExtractCycle((c) => c + 1);
          }}
        />
      )}
      {teachEntry != null && (
        <ExampleTeacher
          open
          entry={teachEntry}
          definitionId={selectedDefId}
          extraction={extraction}
          onClose={() => setTeachEntry(null)}
          onSaved={handleTeachSaved}
        />
      )}
      {batchDocs != null && (
        <BatchExtractModal
          open
          documents={batchDocs}
          definitionId={selectedDefId}
          definitionLabel={
            definitions.find((d) => d.id === selectedDefId)?.document_type || ""
          }
          onClose={() => setBatchDocs(null)}
        />
      )}
      </div>
      {offline && (
        <OfflineOverlay online={online} />
      )}
    </Theme>
  );
}
