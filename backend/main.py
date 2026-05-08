"""FastAPI backend for the Document Viewer application using Docling."""

import hashlib
import io
import json
import os
import re
import tempfile
import threading
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

# Render cache: stores PDF path + per-page dimensions; page images are rendered
# lazily on first request and memoized. Avoids rendering every page when a
# document is selected (the prior behavior blocked the first request entirely
# for large documents).
_render_cache: dict = {}
_render_lock = threading.Lock()
# Text-extraction cache: slow, populated lazily on first extraction (Docling).
_text_cache: dict = {}
_text_lock = threading.Lock()
# Cache for DOCX/PPTX → PDF conversions
_pdf_conversion_cache: dict = {}
_pdf_temp_dir = tempfile.mkdtemp(prefix="schemabuilder_")

# Cached definitions, keyed by per-file (name, mtime, size) signature so
# the dir is rescanned only when something actually changes on disk.
_definitions_cache: Optional[dict] = None
_definitions_signature: Optional[tuple] = None
_definitions_lock = threading.Lock()

# Doc-id → resolved Path cache. Avoids rescanning the docs dir on every request.
_doc_path_cache: dict = {}

# Cached signatures per-definition (id, mtime) → list of (kind, pattern). Built
# lazily and reused across extract calls for the same definition snapshot.
_signature_cache: dict = {}


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


def _open_pdf_metadata(filepath: Path) -> tuple[Optional[Path], int, dict]:
    """Open the PDF (converting if needed) and return (pdf_path, num_pages, page_dimensions).

    Page dimension queries are cheap; rendering is deferred to _render_page so
    selecting a document doesn't block on rasterizing every page.
    """
    import pypdfium2 as pdfium

    pdf_path = filepath
    if filepath.suffix.lower() in (".docx", ".pptx"):
        pdf_path = _convert_to_pdf(filepath)

    if not pdf_path or not pdf_path.exists():
        return None, 0, {}

    pdf_doc = pdfium.PdfDocument(str(pdf_path))
    try:
        num_pages = len(pdf_doc)
        page_dimensions = {}
        for i in range(num_pages):
            page = pdf_doc[i]
            page_dimensions[i + 1] = {
                "width": float(page.get_width()),
                "height": float(page.get_height()),
            }
    finally:
        pdf_doc.close()

    return pdf_path, num_pages, page_dimensions


def _render_single_page(pdf_path: str, page_no: int) -> Optional[bytes]:
    import pypdfium2 as pdfium

    pdf_doc = pdfium.PdfDocument(pdf_path)
    try:
        if page_no < 1 or page_no > len(pdf_doc):
            return None
        page = pdf_doc[page_no - 1]
        bitmap = page.render(scale=2.0)
        pil_image = bitmap.to_pil()
        buf = io.BytesIO()
        pil_image.save(buf, format="PNG")
        return buf.getvalue()
    finally:
        pdf_doc.close()


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
    """Render-only path: opens the PDF and records dimensions. Page images are
    populated on demand by _render_page.
    """
    doc_id = _get_document_id(filepath.name)
    cached = _render_cache.get(doc_id)
    if cached is not None:
        return cached

    with _render_lock:
        cached = _render_cache.get(doc_id)
        if cached is not None:
            return cached

        pdf_path, num_pages, page_dimensions = _open_pdf_metadata(filepath)
        _render_cache[doc_id] = {
            "filename": filepath.name,
            "num_pages": max(num_pages, 1),
            "page_dimensions": page_dimensions,
            "pdf_path": str(pdf_path) if pdf_path else None,
            "page_images": {},
        }
    return _render_cache[doc_id]


def _render_page(filepath: Path, page_no: int) -> Optional[bytes]:
    """Return the PNG bytes for a page, rendering and memoizing on first request."""
    data = _get_or_render(filepath)
    page_images = data["page_images"]
    if page_no in page_images:
        return page_images[page_no]
    pdf_path = data.get("pdf_path")
    if not pdf_path:
        return None

    png_bytes = _render_single_page(pdf_path, page_no)
    if png_bytes is not None:
        page_images[page_no] = png_bytes
    return png_bytes


def _get_or_extract_text(filepath: Path) -> dict:
    """Text-extraction path: Docling. Slow on first call, cached thereafter."""
    doc_id = _get_document_id(filepath.name)
    cached = _text_cache.get(doc_id)
    if cached is not None:
        return cached

    with _text_lock:
        cached = _text_cache.get(doc_id)
        if cached is not None:
            return cached
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
    cached = _doc_path_cache.get(doc_id)
    if cached is not None:
        p = Path(cached)
        if p.exists():
            return p
        _doc_path_cache.pop(doc_id, None)

    for f in TEST_DOCS_DIR.iterdir():
        if f.suffix.lower() in SUPPORTED_EXTENSIONS:
            fid = _get_document_id(f.name)
            _doc_path_cache[fid] = str(f)
            if fid == doc_id:
                return f
    return None


# ── Document definitions ──────────────────────────────────────────────


def _definitions_dir_signature() -> tuple:
    if not DEFINITIONS_DIR.exists():
        return ()
    sig = []
    for f in sorted(DEFINITIONS_DIR.iterdir()):
        if f.suffix.lower() == ".json":
            try:
                st = f.stat()
                sig.append((f.name, st.st_mtime_ns, st.st_size))
            except OSError:
                pass
    return tuple(sig)


def _load_definitions() -> dict:
    """Load all document class definitions, cached until any file changes."""
    global _definitions_cache, _definitions_signature

    if not DEFINITIONS_DIR.exists():
        DEFINITIONS_DIR.mkdir(parents=True, exist_ok=True)

    sig = _definitions_dir_signature()
    cached = _definitions_cache
    if cached is not None and _definitions_signature == sig:
        return cached

    with _definitions_lock:
        if _definitions_cache is not None and _definitions_signature == sig:
            return _definitions_cache
        defs: dict = {}
        for f in sorted(DEFINITIONS_DIR.iterdir()):
            if f.suffix.lower() == ".json":
                try:
                    with open(f) as fp:
                        defs[f.stem] = json.load(fp)
                except Exception:
                    pass
        _definitions_cache = defs
        _definitions_signature = sig
        # Stale signature entries are harmless; clear to bound memory.
        _signature_cache.clear()
    return _definitions_cache


def _invalidate_definitions_cache() -> None:
    global _definitions_cache, _definitions_signature
    with _definitions_lock:
        _definitions_cache = None
        _definitions_signature = None
        _signature_cache.clear()


# Static, module-level patterns: cheaper than recompiling on every entry.
_DATE_EXAMPLE_RE = re.compile(r'\d{4}-\d{2}-\d{2}')
_ID_EXAMPLE_RE = re.compile(r'^[A-Z]+-\d+')
_DECIMAL_EXAMPLE_FULL_RE = re.compile(r'^\d+\.\d+$')
_INT_EXAMPLE_RE = re.compile(r'^\d+$')
_DATE_DETECT_HEAD_RE = re.compile(r'\d{4}-\d{2}-\d{2}')
_DATE_DETECT_LOOSE_RE = re.compile(r'\d{4}[-/]\d{2}[-/]\d{2}')
_ID_HEAD_RE = re.compile(r'[A-Z]+-\d+')
_ID_DETECT_RE = re.compile(r'[A-Z]+-\d+', re.IGNORECASE)
_DECIMAL_DETECT_RE = re.compile(r'\d+\.\d{2}')
_DECIMAL_LOOSE_DETECT_RE = re.compile(r'\d+\.\d+')
_INT_WORD_RE = re.compile(r'\b\d+\b')
_CURRENCY_SIGN_RE = re.compile(r'[\$€£¥]')
_CURRENCY_SIGNS = ('$', '€', '£', '¥')


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
                if _DATE_EXAMPLE_RE.match(ex):
                    signatures.append(("regex", _DATE_DETECT_LOOSE_RE))
                elif _ID_EXAMPLE_RE.match(ex):
                    signatures.append(("regex", _ID_DETECT_RE))
                elif _DECIMAL_EXAMPLE_FULL_RE.match(ex):
                    signatures.append(("regex", _DECIMAL_LOOSE_DETECT_RE))
                elif _INT_EXAMPLE_RE.match(ex):
                    signatures.append(("regex", _INT_WORD_RE))
                elif ex in _CURRENCY_SIGNS:
                    signatures.append(("regex", _CURRENCY_SIGN_RE))

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


def _get_signatures_for(def_id: str, definition: dict) -> list:
    """Cache compiled signatures by (def_id, signature). Cleared when defs reload."""
    sig = _definitions_signature
    key = (def_id, sig)
    cached = _signature_cache.get(key)
    if cached is not None:
        return cached
    built = _build_field_signatures(definition)
    _signature_cache[key] = built
    return built


def _entry_could_match(entry: dict, signatures: list) -> bool:
    """Whether an entry is worth scoring against any field."""
    if entry.get("type") == "TableItem":
        return True
    if not signatures:
        return True

    text = entry.get("text", "")
    text_lower = entry.get("_text_lower")
    if text_lower is None:
        text_lower = text.lower()
    for kind, pat in signatures:
        if kind == "literal":
            if pat in text_lower:
                return True
        else:
            if pat.search(text):
                return True
    return False


def _compile_field_matchers(field: dict) -> dict:
    """Pre-compile per-field state so the entry loop avoids recompiling regexes."""
    examples = field.get("examples", []) or []
    available_options = field.get("available_options", []) or []

    example_lower = [str(ex).lower() if ex is not None else "" for ex in examples]
    example_lower_strip = [s.strip() for s in example_lower]

    has_date = False
    has_id = False
    has_decimal = False
    has_currency_sign = False
    for ex in examples:
        ex_str = str(ex) if ex is not None else ""
        if _DATE_EXAMPLE_RE.match(ex_str):
            has_date = True
        if _ID_HEAD_RE.match(ex_str):
            has_id = True
        if re.match(r'\d+\.\d{2}$', ex_str):
            has_decimal = True
        if ex_str in _CURRENCY_SIGNS:
            has_currency_sign = True

    options = []
    for opt in available_options:
        if not opt:
            continue
        opt_str = str(opt)
        options.append((
            opt_str.lower().strip(),
            re.compile(r'\b' + re.escape(opt_str) + r'\b', re.IGNORECASE),
        ))

    label = str(field.get("name", "")).replace("_", " ").lower()

    return {
        "example_lower": example_lower,
        "example_lower_strip": example_lower_strip,
        "options": options,
        "label": label,
        "has_date": has_date,
        "has_id": has_id,
        "has_decimal": has_decimal,
        "has_currency_sign": has_currency_sign,
    }


def _match_field_to_entries(field: dict, text_entries: list, used_ids: set) -> dict:
    """Try to match a single field definition to the best text entry."""
    if field.get("type") == "array":
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
            "type": "array",
            "fields": field.get("fields", []),
            "items": _match_array_field(field, text_entries, used_ids),
        }
        return result

    m = _compile_field_matchers(field)
    example_lower = m["example_lower"]
    example_lower_strip = m["example_lower_strip"]
    options = m["options"]
    label = m["label"]
    has_date = m["has_date"]
    has_id = m["has_id"]
    has_decimal = m["has_decimal"]
    has_currency_sign = m["has_currency_sign"]

    best_match = None
    best_score = 0

    for entry in text_entries:
        if entry["id"] in used_ids:
            continue

        text = entry.get("text", "")
        text_lower = entry.get("_text_lower")
        if text_lower is None:
            text_lower = text.lower()
        text_stripped_lower = entry.get("_text_stripped_lower")
        if text_stripped_lower is None:
            text_stripped_lower = text.strip().lower()
        score = 0

        # Available options: exact (90) or word match (75); break on first hit
        for opt_lower_strip, opt_pattern in options:
            if opt_lower_strip == text_stripped_lower:
                score = 90 if score < 90 else score
                break
            if opt_pattern.search(text):
                score = 75 if score < 75 else score
                break

        # Examples: exact (95) or substring (80); break on first hit
        for ex_strip, ex_lower in zip(example_lower_strip, example_lower):
            if ex_strip == text_stripped_lower:
                if score < 95:
                    score = 95
                break
            if ex_lower and ex_lower in text_lower:
                if score < 80:
                    score = 80
                break

        # Format heuristics
        if has_date and _DATE_DETECT_HEAD_RE.search(text):
            if score < 85:
                score = 85
        if has_id and _ID_DETECT_RE.search(text):
            if score < 85:
                score = 85
        if has_decimal and _DECIMAL_DETECT_RE.search(text):
            if score < 70:
                score = 70
        if has_currency_sign and any(s in text for s in _CURRENCY_SIGNS):
            if score < 80:
                score = 80

        if label and label in text_lower:
            if score < 60:
                score = 60

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

    # Pre-classify sub-field example shapes once instead of re-matching per entry.
    sub_specs = []
    for sf in sub_fields:
        kind = None
        for example in sf.get("examples", []) or []:
            ex_str = str(example) if example is not None else ""
            if re.match(r'\d+\.\d{2}$', ex_str):
                kind = "decimal"
                break
            if _ID_HEAD_RE.match(ex_str):
                kind = "id"
                break
            if _INT_EXAMPLE_RE.match(ex_str):
                kind = "int"
                break
        sub_specs.append((sf, kind))

    items = []
    for entry in text_entries:
        if entry["id"] in used_ids:
            continue
        if entry.get("type") != "TableItem":
            continue

        text = entry.get("text", "")
        item_fields = []
        for sf, kind in sub_specs:
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
            if kind == "decimal":
                match = _DECIMAL_DETECT_RE.search(text)
                if match:
                    item_field["extracted_value"] = match.group(0)
                    item_field["confidence"] = 0.6
            elif kind == "id":
                match = _ID_DETECT_RE.search(text)
                if match:
                    item_field["extracted_value"] = match.group(0)
                    item_field["confidence"] = 0.6
            elif kind == "int":
                match = _INT_WORD_RE.search(text)
                if match:
                    item_field["extracted_value"] = match.group(0)
                    item_field["confidence"] = 0.5
            item_fields.append(item_field)

        if any(f["extracted_value"] for f in item_fields):
            used_ids.add(entry["id"])
            items.append({"fields": item_fields})

    return items


def _extract_fields(definition: dict, text_entries: list, def_id: Optional[str] = None) -> list:
    """Extract fields defined in the document definition from text entries.

    Pre-filters text entries to only those that could plausibly be a field value,
    so the per-field matcher iterates over a much smaller set than the full document.
    """
    doc = definition.get("document", {})
    fields = doc.get("fields", [])

    signatures = (
        _get_signatures_for(def_id, definition)
        if def_id is not None
        else _build_field_signatures(definition)
    )

    candidates = []
    for e in text_entries:
        text = e.get("text", "")
        # Annotate with pre-lowered text once, then reuse across all fields.
        # Use a shallow copy so we don't mutate the cached text_entries list.
        annotated = e if "_text_lower" in e else {
            **e,
            "_text_lower": text.lower(),
            "_text_stripped_lower": text.strip().lower(),
        }
        if _entry_could_match(annotated, signatures):
            candidates.append(annotated)

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
            doc_id = _get_document_id(f.name)
            _doc_path_cache[doc_id] = str(f)
            docs.append(
                {
                    "id": doc_id,
                    "filename": f.name,
                    "extension": f.suffix.lower(),
                    "size": f.stat().st_size,
                }
            )
    return docs


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: str):
    """Get document metadata. Fast — does not run text extraction or page rasterization."""
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
    """Get a rendered page image as PNG. Pages are rendered on demand and memoized."""
    filepath = _find_file(doc_id)
    if not filepath:
        raise HTTPException(status_code=404, detail="Document not found")

    img_bytes = _render_page(filepath, page_no)
    if not img_bytes:
        raise HTTPException(status_code=404, detail=f"Page {page_no} image not available")

    # Tell the browser it can reuse this image; pages are content-addressed by doc_id+page_no.
    headers = {"Cache-Control": "public, max-age=3600"}
    return Response(content=img_bytes, media_type="image/png", headers=headers)


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

    _invalidate_definitions_cache()

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
    fields = _extract_fields(definition, text_data["text_entries"], def_id=def_id)

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
