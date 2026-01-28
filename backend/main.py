"""FastAPI backend for the Document Viewer application using Docling."""

import hashlib
import io
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

app = FastAPI(title="Document Viewer API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TEST_DOCS_DIR = Path(__file__).parent / "test_documents"
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".pptx"}

# In-memory cache for processed documents
_document_cache: dict = {}


def _get_document_id(filename: str) -> str:
    return hashlib.md5(filename.encode()).hexdigest()[:12]


def _process_document(filepath: Path) -> dict:
    """Process a document using Docling and cache the results."""
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.pipeline_options import PdfPipelineOptions

    # Enable page image generation for PDFs
    pdf_pipeline_opts = PdfPipelineOptions()
    pdf_pipeline_opts.generate_page_images = True
    pdf_pipeline_opts.images_scale = 2.0

    converter = DocumentConverter(
        format_options={
            "pdf": PdfFormatOption(pipeline_options=pdf_pipeline_opts),
        }
    )
    result = converter.convert(str(filepath))
    doc = result.document

    # Extract text entries with page and bounding box info
    text_entries = []
    page_dimensions = {}

    # Get page dimensions from page objects
    for page_no, page in doc.pages.items():
        if hasattr(page, "size") and page.size is not None:
            page_dimensions[page_no] = {
                "width": float(page.size.width),
                "height": float(page.size.height),
            }

    entry_id = 0
    for element in doc.iterate_items():
        # iterate_items() returns (item, level) tuples
        item = element[0] if isinstance(element, tuple) else element

        text = ""
        if hasattr(item, "text"):
            text = item.text
        elif hasattr(item, "export_to_markdown"):
            text = item.export_to_markdown()

        if not text or not text.strip():
            continue

        entry = {
            "id": entry_id,
            "text": text.strip(),
            "type": type(item).__name__,
            "page": 0,
            "bbox": None,
        }
        entry_id += 1

        # Extract provenance (page number and bounding box)
        if hasattr(item, "prov") and item.prov:
            prov = item.prov[0]
            page_no = prov.page_no if hasattr(prov, "page_no") else 1
            entry["page"] = page_no

            if hasattr(prov, "bbox") and prov.bbox is not None:
                bbox = prov.bbox
                if hasattr(bbox, "l"):
                    entry["bbox"] = {
                        "l": float(bbox.l),
                        "t": float(bbox.t),
                        "r": float(bbox.r),
                        "b": float(bbox.b),
                    }
                    if hasattr(bbox, "coord_origin"):
                        entry["bbox"]["coord_origin"] = str(bbox.coord_origin)

        text_entries.append(entry)

    # Render pages as images
    page_images = {}
    for page_no, page in doc.pages.items():
        if hasattr(page, "image") and page.image is not None:
            img = page.image.pil_image
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            page_images[page_no] = buf.getvalue()
            if page_no not in page_dimensions:
                page_dimensions[page_no] = {
                    "width": float(img.width),
                    "height": float(img.height),
                }

    # Fallback: render PDF pages using pypdfium2 if Docling didn't produce images
    if not page_images and filepath.suffix.lower() == ".pdf":
        try:
            import pypdfium2 as pdfium

            pdf_doc = pdfium.PdfDocument(str(filepath))
            for i in range(len(pdf_doc)):
                page = pdf_doc[i]
                bitmap = page.render(scale=2.0)
                pil_image = bitmap.to_pil()
                buf = io.BytesIO()
                pil_image.save(buf, format="PNG")
                page_images[i + 1] = buf.getvalue()
                if (i + 1) not in page_dimensions:
                    page_dimensions[i + 1] = {
                        "width": float(page.get_width()),
                        "height": float(page.get_height()),
                    }
            pdf_doc.close()
        except Exception:
            pass

    num_pages = max(
        [e["page"] for e in text_entries if e["page"] > 0] + [len(page_images)],
        default=1,
    )

    return {
        "filename": filepath.name,
        "num_pages": num_pages,
        "text_entries": text_entries,
        "page_images": page_images,
        "page_dimensions": page_dimensions,
    }


def _get_or_process(filepath: Path) -> dict:
    doc_id = _get_document_id(filepath.name)
    if doc_id not in _document_cache:
        _document_cache[doc_id] = _process_document(filepath)
    return _document_cache[doc_id]


def _find_file(doc_id: str) -> Optional[Path]:
    for f in TEST_DOCS_DIR.iterdir():
        if f.suffix.lower() in SUPPORTED_EXTENSIONS and _get_document_id(f.name) == doc_id:
            return f
    return None


@app.get("/api/documents")
def list_documents():
    """List all available documents."""
    if not TEST_DOCS_DIR.exists():
        return []
    docs = []
    for f in sorted(TEST_DOCS_DIR.iterdir()):
        if f.suffix.lower() in SUPPORTED_EXTENSIONS:
            docs.append(
                {
                    "id": _get_document_id(f.name),
                    "filename": f.name,
                    "extension": f.suffix.lower(),
                    "size": f.stat().st_size,
                }
            )
    return docs


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: str):
    """Get document metadata and extracted text entries."""
    filepath = _find_file(doc_id)
    if not filepath:
        raise HTTPException(status_code=404, detail="Document not found")

    data = _get_or_process(filepath)
    return {
        "id": doc_id,
        "filename": data["filename"],
        "num_pages": data["num_pages"],
        "page_dimensions": data["page_dimensions"],
        "text_entries": data["text_entries"],
    }


@app.get("/api/documents/{doc_id}/pages/{page_no}")
def get_page_image(doc_id: str, page_no: int):
    """Get a rendered page image as PNG."""
    filepath = _find_file(doc_id)
    if not filepath:
        raise HTTPException(status_code=404, detail="Document not found")

    data = _get_or_process(filepath)
    img_bytes = data["page_images"].get(page_no)
    if not img_bytes:
        raise HTTPException(status_code=404, detail=f"Page {page_no} image not available")

    return Response(content=img_bytes, media_type="image/png")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
