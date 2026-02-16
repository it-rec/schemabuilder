import React, { useState, useRef, useEffect } from "react";
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

  function handleImageLoad(e) {
    setImageDimensions({
      width: e.target.naturalWidth,
      height: e.target.naturalHeight,
      displayWidth: e.target.clientWidth,
      displayHeight: e.target.clientHeight,
    });
  }

  // Get highlight overlay for the currently hovered field
  function getHighlights() {
    if (!highlightedField || !highlightedField.bbox || !imageDimensions) {
      return null;
    }

    if (highlightedField.page !== currentPage) return null;

    const { bbox } = highlightedField;
    const { displayWidth, displayHeight } = imageDimensions;

    // Get page dimensions from document data or fall back to image natural size
    const pageDims =
      documentData?.page_dimensions?.[currentPage] || imageDimensions;
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

  // Show all matched fields as subtle overlays on the current page
  function getFieldOverlays() {
    if (!documentData || !imageDimensions) return null;

    // We don't render persistent overlays here anymore — only the hovered highlight
    return null;
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
          {getFieldOverlays()}
        </div>
      </div>
    </div>
  );
}
