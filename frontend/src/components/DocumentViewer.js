import React, { useState, useRef, useEffect } from "react";
import { IconButton, Loading } from "@carbon/react";
import { ChevronLeft, ChevronRight } from "@carbon/react/icons";
import { getPageImageUrl } from "../services/api";

export default function DocumentViewer({
  docId,
  documentData,
  highlightedEntryId,
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

  // Scroll highlighted entry's page into view
  useEffect(() => {
    if (highlightedEntryId == null || !documentData) return;
    const entry = documentData.text_entries.find(
      (e) => e.id === highlightedEntryId
    );
    if (entry && entry.page > 0 && entry.page !== currentPage) {
      setCurrentPage(entry.page);
    }
  }, [highlightedEntryId, documentData, currentPage]);

  function handleImageLoad(e) {
    setImageDimensions({
      width: e.target.naturalWidth,
      height: e.target.naturalHeight,
      displayWidth: e.target.clientWidth,
      displayHeight: e.target.clientHeight,
    });
  }

  // Get highlight overlays for the current page
  function getHighlights() {
    if (
      highlightedEntryId == null ||
      !documentData ||
      !imageDimensions
    )
      return null;

    const entry = documentData.text_entries.find(
      (e) => e.id === highlightedEntryId
    );
    if (!entry || !entry.bbox || entry.page !== currentPage) return null;

    const { bbox } = entry;
    const { displayWidth, displayHeight } = imageDimensions;

    // Get page dimensions from document data or fall back to image natural size
    const pageDims =
      documentData.page_dimensions?.[currentPage] || imageDimensions;
    const pageWidth = pageDims.width;
    const pageHeight = pageDims.height;

    const scaleX = displayWidth / pageWidth;
    const scaleY = displayHeight / pageHeight;

    // Docling bbox can be bottom-left origin or top-left origin
    const isBottomOrigin =
      bbox.coord_origin === "BOTTOMLEFT" || bbox.t > bbox.b;

    let top, left, width, height;
    left = bbox.l * scaleX;
    width = (bbox.r - bbox.l) * scaleX;

    if (isBottomOrigin) {
      // Convert from bottom-left origin to top-left
      top = (pageHeight - bbox.t) * scaleY;
      height = (bbox.t - bbox.b) * scaleY;
    } else {
      top = bbox.t * scaleY;
      height = (bbox.b - bbox.t) * scaleY;
    }

    return (
      <div
        className="document-viewer__highlight"
        data-testid="highlight-overlay"
        style={{
          position: "absolute",
          left: `${left}px`,
          top: `${top}px`,
          width: `${width}px`,
          height: `${height}px`,
          backgroundColor: "rgba(15, 98, 254, 0.25)",
          border: "2px solid #0f62fe",
          borderRadius: "2px",
          pointerEvents: "none",
        }}
      />
    );
  }

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

  const pageImageUrl = getPageImageUrl(docId, currentPage);

  return (
    <div className="document-viewer" ref={containerRef}>
      <div className="document-viewer__toolbar">
        <IconButton
          align={"left"}
          label="Previous page"
          kind="ghost"
          size="sm"
          disabled={currentPage <= 1}
          onClick={() => setCurrentPage((p) => Math.max(1, p - 1))}
        >
          <ChevronLeft />
        </IconButton>
        <span className="document-viewer__page-info">
          Page {currentPage} of {numPages}
        </span>
        <IconButton
          align={"right"}
          label="Next page"
          kind="ghost"
          size="sm"
          disabled={currentPage >= numPages}
          onClick={() => setCurrentPage((p) => Math.min(numPages, p + 1))}
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
            onError={() => setImageDimensions(null)}
            className="document-viewer__image"
          />
          {getHighlights()}
        </div>
      </div>
    </div>
  );
}
