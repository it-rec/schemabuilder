"""FastAPI backend for the Document Viewer application using Docling."""

import hashlib
import io
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
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
DEFINITIONS_DIR = Path(__file__).parent / "definitions"
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".pptx"}

# Render cache: fast, populated eagerly on doc selection (pypdfium2 only).
_render_cache: dict = {}
# Text-extraction cache: slow, populated lazily on first extraction (Docling).
_text_cache: dict = {}
# Cache for DOCX/PPTX → PDF conversions
_pdf_conversion_cache: dict = {}
_pdf_temp_dir = tempfile.mkdtemp(prefix="schemabuilder_")


def _convert_to_pdf(filepath: Path) -> Optional[Path]:
    """Convert DOCX/PPTX to PDF using MS Office COM automation. Results are cached."""
    cache_key = str(filepath)
    if cache_key in _pdf_conversion_cache:
        cached = Path(_pdf_conversion_cache[cache_key])
        if cached.exists():
            return cached

    import win32com.client

    ext = filepath.suffix.lower()
    abs_path = str(filepath.resolve())
    pdf_path = Path(_pdf_temp_dir) / f"{filepath.stem}.pdf"

    if ext == ".docx":
        word = win32com.client.Dispatch("Word.Application")
        word.Visible = False
        try:
            doc = word.Documents.Open(abs_path)
            doc.SaveAs(str(pdf_path), FileFormat=17)  # 17 = wdFormatPDF
            doc.Close()
        finally:
            word.Quit()
    elif ext == ".pptx":
        ppt = win32com.client.Dispatch("PowerPoint.Application")
        try:
            presentation = ppt.Presentations.Open(abs_path, WithWindow=False)
            presentation.SaveAs(str(pdf_path), FileFormat=32)  # 32 = ppSaveAsPDF
            presentation.Close()
        finally:
            ppt.Quit()
    else:
        return None

    _pdf_conversion_cache[cache_key] = str(pdf_path)
    return pdf_path


def _get_document_id(filename: str) -> str:
    return hashlib.md5(filename.encode()).hexdigest()[:12]


def _render_pages(filepath: Path) -> tuple[dict, dict]:
    """Render document pages as PNG images. Returns (page_images, page_dimensions)."""
    import pypdfium2 as pdfium

    pdf_path = filepath
    if filepath.suffix.lower() in (".docx", ".pptx"):
        pdf_path = _convert_to_pdf(filepath)

    page_images = {}
    page_dimensions = {}

    if pdf_path and pdf_path.exists():
        pdf_doc = pdfium.PdfDocument(str(pdf_path))
        for i in range(len(pdf_doc)):
            page = pdf_doc[i]
            bitmap = page.render(scale=2.0)
            pil_image = bitmap.to_pil()
            buf = io.BytesIO()
            pil_image.save(buf, format="PNG")
            page_images[i + 1] = buf.getvalue()
            page_dimensions[i + 1] = {
                "width": float(page.get_width()),
                "height": float(page.get_height()),
            }
        pdf_doc.close()

    return page_images, page_dimensions


def _extract_text(filepath: Path) -> tuple[list, dict]:
    """Extract text entries using Docling. Returns (text_entries, page_dimensions)."""
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.pipeline_options import PdfPipelineOptions

    pdf_pipeline_opts = PdfPipelineOptions()
    pdf_pipeline_opts.generate_page_images = False
    pdf_pipeline_opts.images_scale = 2.0

    converter = DocumentConverter(
        format_options={
            "pdf": PdfFormatOption(pipeline_options=pdf_pipeline_opts),
        }
    )
    result = converter.convert(str(filepath))
    doc = result.document

    text_entries = []
    page_dimensions = {}

    for page_no, page in doc.pages.items():
        if hasattr(page, "size") and page.size is not None:
            page_dimensions[page_no] = {
                "width": float(page.size.width),
                "height": float(page.size.height),
            }

    entry_id = 0
    for element in doc.iterate_items():
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

    return text_entries, page_dimensions


def _get_or_render(filepath: Path) -> dict:
    """Render-only path: pypdfium2 page images + dimensions. Fast."""
    doc_id = _get_document_id(filepath.name)
    if doc_id not in _render_cache:
        page_images, page_dimensions = _render_pages(filepath)
        _render_cache[doc_id] = {
            "filename": filepath.name,
            "num_pages": max(len(page_images), 1),
            "page_images": page_images,
            "page_dimensions": page_dimensions,
        }
    return _render_cache[doc_id]


def _get_or_extract_text(filepath: Path) -> dict:
    """Text-extraction path: Docling. Slow on first call, cached thereafter."""
    doc_id = _get_document_id(filepath.name)
    if doc_id not in _text_cache:
        try:
            text_entries, docling_dims = _extract_text(filepath)
        except Exception:
            text_entries, docling_dims = [], {}
        _text_cache[doc_id] = {
            "text_entries": text_entries,
            "page_dimensions": docling_dims,
        }
    return _text_cache[doc_id]


def _find_file(doc_id: str) -> Optional[Path]:
    for f in TEST_DOCS_DIR.iterdir():
        if f.suffix.lower() in SUPPORTED_EXTENSIONS and _get_document_id(f.name) == doc_id:
            return f
    return None


# ── Document definitions ──────────────────────────────────────────────


def _load_definitions() -> dict:
    """Load all document class definitions from the definitions directory."""
    if not DEFINITIONS_DIR.exists():
        DEFINITIONS_DIR.mkdir(parents=True, exist_ok=True)
    defs = {}
    for f in sorted(DEFINITIONS_DIR.iterdir()):
        if f.suffix.lower() == ".json":
            try:
                with open(f) as fp:
                    data = json.load(fp)
                def_id = f.stem
                defs[def_id] = data
            except Exception:
                pass
    return defs


def _match_field_to_entries(field: dict, text_entries: list, used_ids: set) -> dict:
    """Try to match a single field definition to the best text entry."""
    name = field.get("name", "")
    examples = field.get("examples", [])
    available_options = field.get("available_options", [])

    best_match = None
    best_score = 0

    for entry in text_entries:
        if entry["id"] in used_ids:
            continue

        text = entry.get("text", "")
        score = 0

        # Check against available_options (e.g., currency codes)
        if available_options:
            for opt in available_options:
                if opt.lower() == text.strip().lower():
                    score = max(score, 90)
                    break
                if re.search(r'\b' + re.escape(opt) + r'\b', text, re.IGNORECASE):
                    score = max(score, 75)
                    break

        # Check against examples
        for example in examples:
            if example.lower() == text.strip().lower():
                score = max(score, 95)
                break
            if example.lower() in text.lower():
                score = max(score, 80)
                break

        # Check if text looks like the expected format based on examples
        for example in examples:
            # Date-like patterns
            if re.match(r'\d{4}-\d{2}-\d{2}', example):
                if re.search(r'\d{4}-\d{2}-\d{2}', text):
                    score = max(score, 85)
            # ID-like patterns (e.g., INV-2024-001)
            if re.match(r'[A-Z]+-\d+', example):
                if re.search(r'[A-Z]+-\d+', text, re.IGNORECASE):
                    score = max(score, 85)
            # Currency-like patterns
            if re.match(r'\d+\.\d{2}$', example):
                if re.search(r'\d+\.\d{2}', text):
                    score = max(score, 70)
            # Currency sign patterns
            if example in ('$', '€', '£', '¥'):
                if any(s in text for s in ('$', '€', '£', '¥')):
                    score = max(score, 80)

        # Check if the field name (as a label) appears in the text
        label = name.replace("_", " ")
        if label.lower() in text.lower():
            score = max(score, 60)

        if score > best_score:
            best_score = score
            best_match = entry

    result = {
        "name": field["name"],
        "description": field.get("description", ""),
        "examples": field.get("examples", []),
        "extraction_instructions": field.get("extraction_instructions"),
        "available_options": field.get("available_options"),
        "affix": field.get("affix"),
        "extracted_value": None,
        "confidence": 0,
        "matched_entry_id": None,
        "page": None,
        "bbox": None,
    }

    if field.get("type") == "array":
        result["type"] = "array"
        result["fields"] = field.get("fields", [])
        result["items"] = _match_array_field(field, text_entries, used_ids)
        return result

    if best_match and best_score >= 50:
        used_ids.add(best_match["id"])
        result["extracted_value"] = best_match["text"]
        result["confidence"] = best_score / 100.0
        result["matched_entry_id"] = best_match["id"]
        result["page"] = best_match.get("page")
        result["bbox"] = best_match.get("bbox")

    return result


def _match_array_field(field: dict, text_entries: list, used_ids: set) -> list:
    """Try to match array field items (like line_items) from text entries."""
    sub_fields = field.get("fields", [])
    if not sub_fields:
        return []

    # Look for table-like entries or groups of entries that could be array items
    # Simple heuristic: find TableItem entries and try to parse them
    items = []
    for entry in text_entries:
        if entry["id"] in used_ids:
            continue
        if entry.get("type") == "TableItem":
            # Tables might contain array data
            item_fields = []
            for sf in sub_fields:
                item_field = {
                    "name": sf["name"],
                    "description": sf.get("description", ""),
                    "examples": sf.get("examples", []),
                    "extracted_value": None,
                    "confidence": 0,
                    "matched_entry_id": entry["id"],
                    "page": entry.get("page"),
                    "bbox": entry.get("bbox"),
                }
                # Try to find the sub-field value in the table text
                text = entry.get("text", "")
                for example in sf.get("examples", []):
                    if re.match(r'\d+\.\d{2}$', example):
                        match = re.search(r'(\d+\.\d{2})', text)
                        if match:
                            item_field["extracted_value"] = match.group(1)
                            item_field["confidence"] = 0.6
                    elif re.match(r'[A-Z]+-\d+', example):
                        match = re.search(r'([A-Z]+-\d+)', text, re.IGNORECASE)
                        if match:
                            item_field["extracted_value"] = match.group(1)
                            item_field["confidence"] = 0.6
                    elif re.match(r'^\d+$', example):
                        match = re.search(r'\b(\d+)\b', text)
                        if match:
                            item_field["extracted_value"] = match.group(1)
                            item_field["confidence"] = 0.5
                item_fields.append(item_field)

            if any(f["extracted_value"] for f in item_fields):
                used_ids.add(entry["id"])
                items.append({"fields": item_fields})

    return items


def _build_field_signatures(definition: dict) -> list:
    """Pre-compute (kind, value) pairs that any field-relevant text entry should match.

    Used to skip entries that can't possibly be a value for any field — paragraphs,
    headings, boilerplate. Always pass TableItems through (needed for array fields).
    """
    signatures: list = []

    def collect(field_list: list) -> None:
        for field in field_list or []:
            if field.get("type") == "array":
                collect(field.get("fields", []))
                continue

            for example in field.get("examples", []) or []:
                ex = str(example) if example is not None else ""
                if not ex:
                    continue
                signatures.append(("literal", ex.lower()))
                if re.match(r'\d{4}-\d{2}-\d{2}', ex):
                    signatures.append(("regex", re.compile(r'\d{4}[-/]\d{2}[-/]\d{2}')))
                elif re.match(r'^[A-Z]+-\d+', ex):
                    signatures.append(("regex", re.compile(r'[A-Z]+-\d+', re.IGNORECASE)))
                elif re.match(r'^\d+\.\d+$', ex):
                    signatures.append(("regex", re.compile(r'\d+\.\d+')))
                elif re.match(r'^\d+$', ex):
                    signatures.append(("regex", re.compile(r'\b\d+\b')))
                elif ex in ('$', '€', '£', '¥'):
                    signatures.append(("regex", re.compile(r'[\$€£¥]')))

            for opt in field.get("available_options", []) or []:
                if opt:
                    signatures.append(
                        ("regex", re.compile(r'\b' + re.escape(str(opt)) + r'\b', re.IGNORECASE))
                    )

            label = str(field.get("name", "")).replace("_", " ").lower().strip()
            if label:
                signatures.append(("literal", label))

    collect(definition.get("document", {}).get("fields", []))
    return signatures


def _entry_could_match(entry: dict, signatures: list) -> bool:
    """Whether an entry is worth scoring against any field."""
    if entry.get("type") == "TableItem":
        return True
    if not signatures:
        return True

    text = entry.get("text", "")
    text_lower = text.lower()
    for kind, pat in signatures:
        if kind == "literal":
            if pat in text_lower:
                return True
        else:
            if pat.search(text):
                return True
    return False


def _extract_fields(definition: dict, text_entries: list) -> list:
    """Extract fields defined in the document definition from text entries.

    Pre-filters text entries to only those that could plausibly be a field value,
    so the per-field matcher iterates over a much smaller set than the full document.
    """
    doc = definition.get("document", {})
    fields = doc.get("fields", [])

    signatures = _build_field_signatures(definition)
    candidates = [e for e in text_entries if _entry_could_match(e, signatures)]

    used_ids: set = set()
    results = []
    for field in fields:
        result = _match_field_to_entries(field, candidates, used_ids)
        results.append(result)
    return results


# ── API Routes ─────────────────────────────────────────────────────────


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
    """Get document metadata. Fast — does not run text extraction."""
    filepath = _find_file(doc_id)
    if not filepath:
        raise HTTPException(status_code=404, detail="Document not found")

    data = _get_or_render(filepath)
    return {
        "id": doc_id,
        "filename": data["filename"],
        "num_pages": data["num_pages"],
        "page_dimensions": data["page_dimensions"],
    }


@app.get("/api/documents/{doc_id}/pages/{page_no}")
def get_page_image(doc_id: str, page_no: int):
    """Get a rendered page image as PNG."""
    filepath = _find_file(doc_id)
    if not filepath:
        raise HTTPException(status_code=404, detail="Document not found")

    data = _get_or_render(filepath)
    img_bytes = data["page_images"].get(page_no)
    if not img_bytes:
        raise HTTPException(status_code=404, detail=f"Page {page_no} image not available")

    return Response(content=img_bytes, media_type="image/png")


@app.get("/api/definitions")
def list_definitions():
    """List all available document class definitions."""
    defs = _load_definitions()
    result = []
    for def_id, data in defs.items():
        doc = data.get("document", {})
        result.append({
            "id": def_id,
            "document_type": doc.get("document_type", "Unknown"),
            "document_description": doc.get("document_description", ""),
            "field_count": len(doc.get("fields", [])),
        })
    return result


@app.get("/api/definitions/{def_id}")
def get_definition(def_id: str):
    """Get a specific document class definition."""
    defs = _load_definitions()
    if def_id not in defs:
        raise HTTPException(status_code=404, detail="Definition not found")
    return {"id": def_id, **defs[def_id]}


@app.post("/api/definitions")
async def create_definition(request: Request):
    """Upload a new document class definition."""
    body = await request.json()
    doc = body.get("document", {})
    doc_type = doc.get("document_type", "untitled")
    def_id = re.sub(r'[^a-z0-9_]', '_', doc_type.lower()).strip('_')

    DEFINITIONS_DIR.mkdir(parents=True, exist_ok=True)
    filepath = DEFINITIONS_DIR / f"{def_id}.json"
    with open(filepath, "w") as f:
        json.dump(body, f, indent=2)

    return {
        "id": def_id,
        "document_type": doc_type,
        "field_count": len(doc.get("fields", [])),
    }


@app.post("/api/documents/{doc_id}/extract")
async def extract_fields(doc_id: str, request: Request):
    """Extract fields from a document using a definition.

    Triggers Docling text extraction lazily on first call per document.
    Returns Docling's page_dimensions so the client can render bbox overlays
    in the same coordinate space as the field bboxes.
    """
    filepath = _find_file(doc_id)
    if not filepath:
        raise HTTPException(status_code=404, detail="Document not found")

    body = await request.json()
    def_id = body.get("definition_id")
    if not def_id:
        raise HTTPException(status_code=400, detail="definition_id is required")

    defs = _load_definitions()
    if def_id not in defs:
        raise HTTPException(status_code=404, detail="Definition not found")

    text_data = _get_or_extract_text(filepath)
    definition = defs[def_id]
    fields = _extract_fields(definition, text_data["text_entries"])

    return {
        "document_id": doc_id,
        "definition_id": def_id,
        "document_type": definition.get("document", {}).get("document_type", ""),
        "document_description": definition.get("document", {}).get("document_description", ""),
        "fields": fields,
        "page_dimensions": text_data["page_dimensions"],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
