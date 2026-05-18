import React, { useState, useEffect, useMemo, useCallback } from "react";
import { IconButton, Loading } from "@carbon/react";
import { ChevronLeft, ChevronRight } from "@carbon/react/icons";
import { getPageImageUrl } from "../services/api";

export default function DocumentViewer({
  docId,
  documentData,
  highlightedField,
  onHoverField,
  onTeachEntry,
  textEntries,
  extractedFields,
  loading,
}) {
  const [currentPage, setCurrentPage] = useState(1);
  // Image dimensions are tagged with the page they were measured for, so a
  // late onLoad from a previously-visible page can't paint the new page's
  // overlays with stale geometry.
  const [imageDimensions, setImageDimensions] = useState(null);

  const numPages = documentData?.num_pages || 1;

  // Reset paging state when the doc itself changes — handled during render
  // via the "store the previous prop" pattern so React doesn't pay an extra
  // commit/effect cycle to do what is really derived-from-input work.
  const [prevDocId, setPrevDocId] = useState(docId);
  if (prevDocId !== docId) {
    setPrevDocId(docId);
    setCurrentPage(1);
    setImageDimensions(null);
  }

  // Jump to the page that owns the highlighted field. Tracked against the
  // previous prop so user-driven prev/next nav (which changes currentPage,
  // not highlightedField) isn't immediately overridden back.
  const [prevHighlightedField, setPrevHighlightedField] = useState(highlightedField);
  if (prevHighlightedField !== highlightedField) {
    setPrevHighlightedField(highlightedField);
    if (highlightedField?.page && highlightedField.page !== currentPage) {
      setCurrentPage(highlightedField.page);
    }
  }

  // Dimensions for any page other than the current one are stale: don't
  // hand them to projectBbox or the overlays would mis-place themselves
  // for a frame.
  const currentImageDimensions =
    imageDimensions?.pageNo === currentPage ? imageDimensions : null;

  // Pre-fetch adjacent pages so navigation is instant. The browser caches the
  // PNG, and the server memoizes the rendered bytes, so this also warms both.
  // fetchPriority="low" lets the browser keep the visible page's request at
  // the front of the queue instead of competing with prefetches.
  useEffect(() => {
    if (!docId || numPages <= 1) return;
    const adjacent = [currentPage - 1, currentPage + 1].filter(
      (p) => p >= 1 && p <= numPages && p !== currentPage,
    );
    const imgs = adjacent.map((p) => {
      const img = new Image();
      if ("fetchPriority" in img) img.fetchPriority = "low";
      img.decoding = "async";
      img.src = getPageImageUrl(docId, p);
      return img;
    });
    return () => {
      // Drop refs so the browser is free to reuse the connections.
      imgs.forEach((img) => {
        img.src = "";
      });
    };
  }, [docId, currentPage, numPages]);

  const handleImageLoad = useCallback(
    (e) => {
      setImageDimensions({
        pageNo: currentPage,
        width: e.target.naturalWidth,
        height: e.target.naturalHeight,
        displayWidth: e.target.clientWidth,
        displayHeight: e.target.clientHeight,
      });
    },
    [currentPage],
  );

  const handleImageError = useCallback(() => setImageDimensions(null), []);

  const handlePrev = useCallback(
    () => setCurrentPage((p) => Math.max(1, p - 1)),
    [],
  );
  const handleNext = useCallback(
    () => setCurrentPage((p) => Math.min(numPages, p + 1)),
    [numPages],
  );

  // Global ArrowLeft / ArrowRight scroll through pages. Skipped when focus
  // is inside a form control or contenteditable — the document list / app
  // keyboard handler has the same guard, so editing in a modal won't
  // unexpectedly flip the viewer page.
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
      if (e.key === "ArrowLeft") {
        e.preventDefault();
        handlePrev();
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        handleNext();
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [handlePrev, handleNext]);

  // Convert a Docling bbox (coordinate origin either TOPLEFT or BOTTOMLEFT,
  // measured in the page's native units) to pixel coordinates inside the
  // rendered <img>. Returns null when we don't yet know the image dimensions
  // (i.e. the PNG hasn't fired onLoad) — overlays simply don't render until
  // we can place them accurately.
  const projectBbox = useCallback(
    (bbox) => {
      if (!bbox || !currentImageDimensions) return null;
      const { displayWidth, displayHeight } = currentImageDimensions;
      const pageDims =
        documentData?.page_dimensions?.[currentPage] || currentImageDimensions;
      const pageWidth = pageDims.width;
      const pageHeight = pageDims.height;
      const scaleX = displayWidth / pageWidth;
      const scaleY = displayHeight / pageHeight;
      const isBottomOrigin =
        bbox.coord_origin === "BOTTOMLEFT" || bbox.t > bbox.b;
      const left = bbox.l * scaleX;
      const width = (bbox.r - bbox.l) * scaleX;
      let top, height;
      if (isBottomOrigin) {
        top = (pageHeight - bbox.t) * scaleY;
        height = (bbox.t - bbox.b) * scaleY;
      } else {
        top = bbox.t * scaleY;
        height = (bbox.b - bbox.t) * scaleY;
      }
      return { left, top, width, height };
    },
    [currentImageDimensions, documentData, currentPage],
  );

  // All matched fields whose bbox lands on the current page. Each one renders
  // as a low-opacity "ghost" overlay so users can see at-a-glance where
  // extractions came from, without having to hover each field one by one.
  const pageOverlays = useMemo(() => {
    if (!extractedFields?.length) return [];
    return extractedFields
      .filter((f) => f.page === currentPage && f.bbox && f.matched_entry_id != null)
      .map((f) => ({ ...f, rect: projectBbox(f.bbox) }))
      .filter((f) => f.rect);
  }, [extractedFields, currentPage, projectBbox]);

  const highlightedEntryId = highlightedField?.matched_entry_id ?? null;

  // Click-to-teach targets: every text entry on the current page that has a
  // bbox we can project. Matched entries already have a ghost overlay, but
  // the teach targets sit underneath at a lower z-index so unmatched text is
  // discoverable too. Hover reveals a dashed outline + pointer cursor.
  const teachTargets = useMemo(() => {
    if (!onTeachEntry || !textEntries?.length) return [];
    const matched = new Set(
      (extractedFields || [])
        .map((f) => f.matched_entry_id)
        .filter((id) => id != null),
    );
    return textEntries
      .filter((e) => e.page === currentPage && e.bbox)
      .map((e) => ({
        ...e,
        rect: projectBbox(e.bbox),
        alreadyMatched: matched.has(e.id),
      }))
      .filter((e) => e.rect);
  }, [onTeachEntry, textEntries, extractedFields, currentPage, projectBbox]);

  const pageImageUrl = useMemo(
    () => (docId ? getPageImageUrl(docId, currentPage) : null),
    [docId, currentPage],
  );

  if (loading) {
    return (
      <div
        className="document-viewer document-viewer--loading"
        role="status"
        aria-live="polite"
      >
        <Loading description="Loading document..." withOverlay={false} />
      </div>
    );
  }

  if (!docId || !documentData) {
    return (
      <div className="document-viewer document-viewer--empty">
        <p className="document-viewer__empty-text">
          Select a document to view.
        </p>
      </div>
    );
  }

  const filename = documentData?.filename || "document";
  const pageLabel = `Page ${currentPage} of ${numPages}`;

  return (
    <div className="document-viewer">
      <div
        className="document-viewer__toolbar"
        role="toolbar"
        aria-label="Document page navigation"
      >
        <IconButton
          label="Previous page"
          kind="ghost"
          size="sm"
          disabled={currentPage <= 1}
          onClick={handlePrev}
        >
          <ChevronLeft />
        </IconButton>
        <span
          className="document-viewer__page-info"
          aria-live="polite"
          aria-atomic="true"
        >
          {pageLabel}
        </span>
        <IconButton
          label="Next page"
          kind="ghost"
          size="sm"
          disabled={currentPage >= numPages}
          onClick={handleNext}
        >
          <ChevronRight />
        </IconButton>
      </div>
      <div className="document-viewer__canvas">
        <div className="document-viewer__image-wrapper">
          <img
            src={pageImageUrl}
            alt={`${filename} — page ${currentPage} of ${numPages}`}
            onLoad={handleImageLoad}
            onError={handleImageError}
            className="document-viewer__image"
            // The visible page is the largest contentful paint here; tell the
            // browser to schedule its bytes ahead of the prefetched neighbors.
            fetchpriority="high"
            decoding="async"
          />
          {teachTargets.map((e) => (
            <button
              key={`teach-${e.id}`}
              type="button"
              className={
                "document-viewer__teach-target" +
                (e.alreadyMatched ? " document-viewer__teach-target--matched" : "")
              }
              data-testid={`teach-target-${e.id}`}
              aria-label={`Teach "${e.text}" as an example`}
              title={`Click to teach "${e.text}" as an example for a field.`}
              style={{
                position: "absolute",
                left: `${e.rect.left}px`,
                top: `${e.rect.top}px`,
                width: `${e.rect.width}px`,
                height: `${e.rect.height}px`,
              }}
              onClick={() => onTeachEntry({ id: e.id, text: e.text, page: e.page })}
            />
          ))}
          {pageOverlays.map((f) => {
            const isActive = highlightedEntryId === f.matched_entry_id;
            return (
              <button
                key={f.key}
                type="button"
                className={
                  "document-viewer__highlight" +
                  (isActive ? " document-viewer__highlight--active" : "")
                }
                data-testid={
                  isActive ? "highlight-overlay" : `highlight-overlay-${f.key}`
                }
                aria-label={`Highlight for ${f.label}`}
                style={{
                  position: "absolute",
                  left: `${f.rect.left}px`,
                  top: `${f.rect.top}px`,
                  width: `${f.rect.width}px`,
                  height: `${f.rect.height}px`,
                }}
                onMouseEnter={() => onHoverField?.(f.field)}
                onMouseLeave={() => onHoverField?.(null)}
                onFocus={() => onHoverField?.(f.field)}
                onBlur={() => onHoverField?.(null)}
              >
                {isActive && (
                  <span className="document-viewer__highlight-label">
                    {f.label}
                  </span>
                )}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
