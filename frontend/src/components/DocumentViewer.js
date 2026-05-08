import React, { useState, useRef, useEffect, useMemo, useCallback } from "react";
import { IconButton, Loading } from "@carbon/react";
import { ChevronLeft, ChevronRight } from "@carbon/react/icons";
import { getPageImageUrl } from "../services/api";

export default function DocumentViewer({
  docId,
  documentData,
  highlightedField,
  loading,
}) {
  const [currentPage, setCurrentPage] = useState(1);
  const [imageDimensions, setImageDimensions] = useState(null);
  const imageRef = useRef(null);
  const containerRef = useRef(null);

  const numPages = documentData?.num_pages || 1;

  useEffect(() => {
    setCurrentPage(1);
    setImageDimensions(null);
  }, [docId]);

  // Navigate to the highlighted field's page
  useEffect(() => {
    if (!highlightedField || !highlightedField.page) return;
    if (highlightedField.page !== currentPage) {
      setCurrentPage(highlightedField.page);
    }
  }, [highlightedField, currentPage]);

  // Pre-fetch adjacent pages so navigation is instant. The browser caches the
  // PNG, and the server memoizes the rendered bytes, so this also warms both.
  useEffect(() => {
    if (!docId || numPages <= 1) return;
    const adjacent = [currentPage - 1, currentPage + 1].filter(
      (p) => p >= 1 && p <= numPages && p !== currentPage,
    );
    const imgs = adjacent.map((p) => {
      const img = new Image();
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

  const handleImageLoad = useCallback((e) => {
    setImageDimensions({
      width: e.target.naturalWidth,
      height: e.target.naturalHeight,
      displayWidth: e.target.clientWidth,
      displayHeight: e.target.clientHeight,
    });
  }, []);

  const handleImageError = useCallback(() => setImageDimensions(null), []);

  const handlePrev = useCallback(
    () => setCurrentPage((p) => Math.max(1, p - 1)),
    [],
  );
  const handleNext = useCallback(
    () => setCurrentPage((p) => Math.min(numPages, p + 1)),
    [numPages],
  );

  // Highlight overlay for the currently hovered field. Memoized so an unrelated
  // state change (e.g. page hover elsewhere) doesn't recompute it.
  const highlightStyle = useMemo(() => {
    if (!highlightedField || !highlightedField.bbox || !imageDimensions) {
      return null;
    }
    if (highlightedField.page !== currentPage) return null;

    const { bbox } = highlightedField;
    const { displayWidth, displayHeight } = imageDimensions;

    const pageDims =
      documentData?.page_dimensions?.[currentPage] || imageDimensions;
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

    return {
      position: "absolute",
      left: `${left}px`,
      top: `${top}px`,
      width: `${width}px`,
      height: `${height}px`,
      backgroundColor: "rgba(15, 98, 254, 0.25)",
      border: "2px solid #0f62fe",
      borderRadius: "2px",
      pointerEvents: "none",
    };
  }, [highlightedField, imageDimensions, documentData, currentPage]);

  const pageImageUrl = useMemo(
    () => (docId ? getPageImageUrl(docId, currentPage) : null),
    [docId, currentPage],
  );

  if (loading) {
    return (
      <div className="document-viewer document-viewer--loading">
        <Loading description="Loading document..." withOverlay={false} />
      </div>
    );
  }

  if (!docId || !documentData) {
    return (
      <div className="document-viewer document-viewer--empty">
        <p>Select a document to view.</p>
      </div>
    );
  }

  return (
    <div className="document-viewer" ref={containerRef}>
      <div className="document-viewer__toolbar">
        <IconButton
          label="Previous page"
          kind="ghost"
          size="sm"
          disabled={currentPage <= 1}
          onClick={handlePrev}
        >
          <ChevronLeft />
        </IconButton>
        <span className="document-viewer__page-info">
          Page {currentPage} of {numPages}
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
            ref={imageRef}
            src={pageImageUrl}
            alt={`Page ${currentPage}`}
            onLoad={handleImageLoad}
            onError={handleImageError}
            className="document-viewer__image"
          />
          {highlightStyle && (
            <div
              className="document-viewer__highlight"
              data-testid="highlight-overlay"
              style={highlightStyle}
            />
          )}
        </div>
      </div>
    </div>
  );
}
