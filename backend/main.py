"""FastAPI backend for the Document Viewer application using Docling."""

import hashlib
import io
import json
import logging
import os
import re
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import Response

logger = logging.getLogger("schemabuilder")

# Background workers for prefetch (page render warm-up, text extraction warm-up,
# converter construction). Two workers is plenty: text extraction is the slow
# leg and is itself serialized on the shared converter; a second worker handles
# page rasterization in parallel so the first paint isn't blocked behind it.
_bg_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="prefetch")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Build the Docling converter eagerly in the background so the first
    # /extract request doesn't pay the model-load cost (~seconds on CPU,
    # less on GPU but still non-trivial). Failure here is non-fatal: the
    # lazy path will retry on first real call.
    _bg_executor.submit(_warm_up_converter)
    try:
        yield
    finally:
        _bg_executor.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="Document Viewer API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Compress JSON responses (extraction payloads can be tens of KB once
# definitions grow). PNGs are already compressed and skipped by min size.
app.add_middleware(GZipMiddleware, minimum_size=1024)

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

# Module-level Docling converters keyed by do_ocr. Construction loads
# layout/OCR models which is slow; reuse instances across documents. The
# no-OCR converter handles digital docs (the common case) and is warmed at
# startup; the OCR-enabled converter is built lazily the first time we see
# a doc that actually needs OCR, so users with no scanned docs never pay
# for loading the OCR models.
_text_converters: dict = {}
_text_converters_lock = threading.Lock()

# Per-document OCR decision cache. Keyed by (filename, file signature) so a
# file replaced in place is re-evaluated. Entries are bool.
_ocr_decision_cache: dict = {}
_ocr_decision_lock = threading.Lock()

# Doc IDs with a prefetch job already submitted/running. Prevents flooding the
# background pool when the user clicks rapidly through the document list.
_prefetch_inflight: set = set()
_prefetch_inflight_lock = threading.Lock()


def _file_signature(filepath: Path) -> tuple:
    """Stable identity for a file's current contents. Used as part of cache
    keys so replacing a file in place invalidates the cached render/text/PDF.
    """
    try:
        st = filepath.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return ()


def _convert_to_pdf(filepath: Path) -> Optional[Path]:
    """Convert DOCX/PPTX to PDF using MS Office COM automation. Results are cached.

    Cache key includes the source file's mtime+size so the cache is invalidated
    when the source is edited or replaced in place.
    """
    sig = _file_signature(filepath)
    cache_key = (str(filepath), sig)
    if cache_key in _pdf_conversion_cache:
        cached = Path(_pdf_conversion_cache[cache_key])
        if cached.exists():
            return cached

    import win32com.client

    ext = filepath.suffix.lower()
    abs_path = str(filepath.resolve())
    # Include the source extension in the temp PDF name so two source files
    # that share a stem (e.g. report.docx and report.pptx) don't clobber each
    # other's converted PDF and serve mixed content via the cache.
    pdf_path = Path(_pdf_temp_dir) / f"{filepath.stem}{ext}.pdf"

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


def _resolve_accelerator_device(AcceleratorDevice):
    """Pick the best accelerator: env override → CUDA → MPS → CPU.

    Docling's AUTO does similar detection internally, but we probe ourselves so
    we can log what was chosen (helpful when "why is it slow?" comes up) and
    so the env override (DOCLING_DEVICE=cpu|cuda|mps|auto) is honored exactly.
    """
    forced = (os.getenv("DOCLING_DEVICE") or "").strip().upper()
    if forced:
        chosen = getattr(AcceleratorDevice, forced, None)
        if chosen is not None:
            return chosen
        logger.warning("Unknown DOCLING_DEVICE=%s; falling back to AUTO", forced)

    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            cuda = getattr(AcceleratorDevice, "CUDA", None)
            if cuda is not None:
                return cuda
        mps_backend = getattr(getattr(torch, "backends", None), "mps", None)
        if mps_backend is not None and mps_backend.is_available():
            mps = getattr(AcceleratorDevice, "MPS", None)
            if mps is not None:
                return mps
    except Exception:
        pass

    return getattr(AcceleratorDevice, "AUTO", AcceleratorDevice.CPU)


def _build_text_converter(do_ocr: bool):
    """Build a Docling DocumentConverter with accelerator + OCR settings.

    Accelerator: auto-prefers CUDA → MPS → CPU. Override via DOCLING_DEVICE.
    Threads: defaults to CPU count; override via DOCLING_NUM_THREADS.
    OCR is decided per-document by `_resolve_ocr_decision`; this builder just
    materializes a converter for one branch of that decision. Table structure
    is left on (default) — the field-extraction code in `_match_array_field`
    relies on TableItem detection.
    """
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.pipeline_options import PdfPipelineOptions

    # AcceleratorOptions/AcceleratorDevice moved between submodules across
    # docling versions (pipeline_options on 2.14, accelerator_options later).
    try:
        from docling.datamodel.accelerator_options import (  # type: ignore
            AcceleratorOptions,
            AcceleratorDevice,
        )
    except ImportError:
        from docling.datamodel.pipeline_options import (  # type: ignore
            AcceleratorOptions,
            AcceleratorDevice,
        )

    num_threads = int(os.getenv("DOCLING_NUM_THREADS") or os.cpu_count() or 4)
    device = _resolve_accelerator_device(AcceleratorDevice)
    accelerator = AcceleratorOptions(num_threads=num_threads, device=device)

    pdf_pipeline_opts = PdfPipelineOptions()
    pdf_pipeline_opts.generate_page_images = False
    pdf_pipeline_opts.images_scale = 2.0
    pdf_pipeline_opts.accelerator_options = accelerator
    pdf_pipeline_opts.do_ocr = do_ocr

    logger.info(
        "Docling converter built: device=%s threads=%s ocr=%s",
        getattr(device, "name", device),
        num_threads,
        do_ocr,
    )

    return DocumentConverter(
        format_options={
            "pdf": PdfFormatOption(pipeline_options=pdf_pipeline_opts),
        }
    )


def _get_text_converter(do_ocr: bool):
    """Return the cached converter for the requested OCR mode, building if needed."""
    cached = _text_converters.get(do_ocr)
    if cached is not None:
        return cached
    with _text_converters_lock:
        cached = _text_converters.get(do_ocr)
        if cached is not None:
            return cached
        cv = _build_text_converter(do_ocr=do_ocr)
        _text_converters[do_ocr] = cv
        return cv


# Pages to sample when guessing whether a PDF needs OCR. Few enough to stay
# in the millisecond budget for selection-time prefetch; spread across the
# document so a cover page with a single watermark image doesn't dominate.
_OCR_DETECT_SAMPLE_PAGES = 3
# Aggregate character count across sampled pages below which we treat the
# document as image-only and route it through the OCR-enabled converter.
_OCR_DETECT_MIN_CHARS = 30
# If a single page already produces this many characters, the document is
# obviously digital and we can skip the rest of the sample.
_OCR_DETECT_FAST_EXIT_CHARS = 100


def _document_needs_ocr(filepath: Path) -> bool:
    """Heuristic: does this document have so little extractable text that OCR
    is worth the cost?

    DOCX/PPTX always carry structured text; short-circuit to False. For PDFs,
    pull text from a few sampled pages via pypdfium2 (much faster than
    standing up Docling's full pipeline) and treat near-empty results as a
    scanned document.
    """
    if filepath.suffix.lower() != ".pdf":
        return False

    import pypdfium2 as pdfium

    try:
        pdf_doc = pdfium.PdfDocument(str(filepath))
    except Exception:
        # If we can't open it for sampling we can't extract from it anyway;
        # take the fast path and let docling surface whatever error occurs.
        return False

    try:
        n_pages = len(pdf_doc)
        if n_pages == 0:
            return False

        if n_pages <= _OCR_DETECT_SAMPLE_PAGES:
            sample = list(range(n_pages))
        else:
            # First, middle, last — covers cover-only-image cases and trailing
            # appendices that may differ from the body.
            sample = sorted({0, n_pages // 2, n_pages - 1})

        total_chars = 0
        for i in sample:
            page = pdf_doc[i]
            textpage = None
            try:
                textpage = page.get_textpage()
                text = textpage.get_text_range()
            except Exception:
                text = ""
            finally:
                if textpage is not None:
                    textpage.close()
            total_chars += len((text or "").strip())
            if total_chars >= _OCR_DETECT_FAST_EXIT_CHARS:
                return False
        return total_chars < _OCR_DETECT_MIN_CHARS
    finally:
        pdf_doc.close()


def _resolve_ocr_decision(filepath: Path) -> bool:
    """Per-document OCR decision with file-signature cache.

    DOCLING_DO_OCR forces a global override (1 = on, 0 = off) for the rare
    case where the heuristic guesses wrong; otherwise the decision is made
    per-doc and cached by (name, mtime, size) so editing a file in place
    re-samples it.
    """
    forced = os.getenv("DOCLING_DO_OCR")
    if forced is not None:
        v = forced.strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off"):
            return False

    sig = _file_signature(filepath)
    key = (filepath.name, sig)
    cached = _ocr_decision_cache.get(key)
    if cached is not None:
        return cached
    with _ocr_decision_lock:
        cached = _ocr_decision_cache.get(key)
        if cached is not None:
            return cached
        # Detection should never break extraction: on any sampling error
        # default to no-OCR (fast path). A truly-scanned doc misclassified
        # this way will still extract — just with empty text — which matches
        # prior behavior, while a digital doc keeps working as expected.
        try:
            decision = _document_needs_ocr(filepath)
        except Exception:
            logger.exception(
                "OCR detection failed for %s; defaulting to no OCR", filepath.name
            )
            decision = False
        _ocr_decision_cache[key] = decision
        logger.info(
            "OCR decision for %s: %s", filepath.name, "on" if decision else "off"
        )
        return decision


def _warm_up_converter():
    """Background warm-up entrypoint; never raises.

    Only the no-OCR converter is built up front. The OCR-enabled converter
    loads heavier models and is built lazily the first time a scanned doc
    actually requests it, so users with only digital docs never pay for it.
    """
    try:
        _get_text_converter(do_ocr=False)
    except Exception:
        logger.exception("Docling converter warm-up failed; will retry lazily")


def _extract_text(filepath: Path) -> tuple[list, dict]:
    """Extract text entries using Docling. Returns (text_entries, page_dimensions).

    Picks the OCR-enabled or no-OCR converter based on a fast pypdfium2 text
    sample. The decision is cached per file signature, so subsequent calls
    pay only a dict lookup.
    """
    do_ocr = _resolve_ocr_decision(filepath)
    converter = _get_text_converter(do_ocr=do_ocr)
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
                        # docling-core's CoordOrigin is an Enum; str(member) is
                        # version-dependent ("BOTTOMLEFT" on Python 3.11+ str-
                        # based enums, "CoordOrigin.BOTTOMLEFT" on 3.10). Pull
                        # the underlying value/name so the frontend's
                        # === "BOTTOMLEFT" check is reliable.
                        co = bbox.coord_origin
                        if hasattr(co, "value"):
                            entry["bbox"]["coord_origin"] = str(co.value)
                        elif hasattr(co, "name"):
                            entry["bbox"]["coord_origin"] = co.name
                        else:
                            entry["bbox"]["coord_origin"] = str(co)

        text_entries.append(entry)

    return text_entries, page_dimensions


def _get_or_render(filepath: Path) -> dict:
    """Render-only path: opens the PDF and records dimensions. Page images are
    populated on demand by _render_page.

    Cache entries record the source file's signature; if the file changes on
    disk the entry is rebuilt instead of returning stale page dimensions or a
    pdf_path that points at an outdated converted PDF.
    """
    doc_id = _get_document_id(filepath.name)
    sig = _file_signature(filepath)
    cached = _render_cache.get(doc_id)
    if cached is not None and cached.get("_sig") == sig:
        return cached

    with _render_lock:
        cached = _render_cache.get(doc_id)
        if cached is not None and cached.get("_sig") == sig:
            return cached

        pdf_path, num_pages, page_dimensions = _open_pdf_metadata(filepath)
        _render_cache[doc_id] = {
            "filename": filepath.name,
            "num_pages": max(num_pages, 1),
            "page_dimensions": page_dimensions,
            "pdf_path": str(pdf_path) if pdf_path else None,
            "page_images": {},
            "_sig": sig,
            # Per-doc lock so two concurrent requests for the same page don't
            # both run pdfium and rasterize the same bitmap. Different docs
            # still render in parallel.
            "_render_lock": threading.Lock(),
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

    with data["_render_lock"]:
        # Re-check under the lock: another caller may have just rendered it.
        if page_no in page_images:
            return page_images[page_no]
        png_bytes = _render_single_page(pdf_path, page_no)
        if png_bytes is not None:
            page_images[page_no] = png_bytes
        return png_bytes


def _get_or_extract_text(filepath: Path) -> dict:
    """Text-extraction path: Docling. Slow on first call, cached thereafter.

    Cache key carries the source file signature so an edited document
    re-extracts instead of replaying stale text entries from a prior version.
    """
    doc_id = _get_document_id(filepath.name)
    sig = _file_signature(filepath)
    cached = _text_cache.get(doc_id)
    if cached is not None and cached.get("_sig") == sig:
        return cached

    with _text_lock:
        cached = _text_cache.get(doc_id)
        if cached is not None and cached.get("_sig") == sig:
            return cached
        try:
            text_entries, docling_dims = _extract_text(filepath)
        except Exception:
            text_entries, docling_dims = [], {}
        _text_cache[doc_id] = {
            "text_entries": text_entries,
            "page_dimensions": docling_dims,
            "_sig": sig,
        }
    return _text_cache[doc_id]


def _kick_background_prefetch(doc_id: str, filepath: Path) -> None:
    """Warm caches for a document the user just selected.

    Renders page 1 (so the first paint is instant after metadata returns) and
    runs Docling text extraction (so the subsequent /extract POST returns
    cached results). Deduped per doc_id so rapid sidebar clicks don't spawn N
    parallel extractions for the same file.
    """
    with _prefetch_inflight_lock:
        if doc_id in _prefetch_inflight:
            return
        _prefetch_inflight.add(doc_id)

    def _job():
        try:
            try:
                _render_page(filepath, 1)
            except Exception:
                logger.exception("Prefetch page-1 render failed for %s", filepath.name)
            try:
                _get_or_extract_text(filepath)
            except Exception:
                logger.exception("Prefetch text extraction failed for %s", filepath.name)
        finally:
            with _prefetch_inflight_lock:
                _prefetch_inflight.discard(doc_id)

    try:
        _bg_executor.submit(_job)
    except RuntimeError:
        # Executor already shut down (e.g. during test teardown). Drop the
        # inflight marker so a future request can re-trigger.
        with _prefetch_inflight_lock:
            _prefetch_inflight.discard(doc_id)


def _find_file(doc_id: str) -> Optional[Path]:
    cached = _doc_path_cache.get(doc_id)
    if cached is not None:
        p = Path(cached)
        if p.exists():
            return p
        _doc_path_cache.pop(doc_id, None)

    if not TEST_DOCS_DIR.exists():
        return None

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
        # Return the local `defs`: another thread could call
        # _invalidate_definitions_cache between releasing this lock and the
        # return, which would null the global and surface None to the caller.
        return defs


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

        # Available options: exact (90) is the max for this loop, so break on
        # exact only; for substring (75), upgrade and keep scanning so a later
        # exact match isn't missed (e.g. options=["AB", "ABC"], text="ABC").
        for opt_lower_strip, opt_pattern in options:
            if opt_lower_strip == text_stripped_lower:
                if score < 90:
                    score = 90
                break
            if score < 75 and opt_pattern.search(text):
                score = 75

        # Examples: exact (95) is the max; substring (80) upgrades only.
        # Same rationale: examples=["INV", "INV-001"] with text="INV-001"
        # must score 95, not 80.
        for ex_strip, ex_lower in zip(example_lower_strip, example_lower):
            if ex_strip == text_stripped_lower:
                if score < 95:
                    score = 95
                break
            if score < 80 and ex_lower and ex_lower in text_lower:
                score = 80

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
    """Get document metadata. Fast — does not run text extraction or page rasterization.

    Kicks off a background prefetch for page 1 + text extraction so by the
    time the frontend renders the viewer and POSTs to /extract, both are
    typically already in the cache.
    """
    filepath = _find_file(doc_id)
    if not filepath:
        raise HTTPException(status_code=404, detail="Document not found")

    data = _get_or_render(filepath)
    _kick_background_prefetch(doc_id, filepath)
    return {
        "id": doc_id,
        "filename": data["filename"],
        "num_pages": data["num_pages"],
        "page_dimensions": data["page_dimensions"],
    }


@app.get("/api/documents/{doc_id}/pages/{page_no}")
def get_page_image(doc_id: str, page_no: int, request: Request):
    """Get a rendered page image as PNG. Pages are rendered on demand and memoized.

    Sends an ETag tied to the file's mtime/size so the browser can revalidate
    cheaply once the max-age expires (or when the user reloads with a warm
    disk cache); a matching If-None-Match short-circuits to 304 without
    re-sending the PNG bytes.
    """
    filepath = _find_file(doc_id)
    if not filepath:
        raise HTTPException(status_code=404, detail="Document not found")

    sig = _file_signature(filepath)
    sig_token = f"{sig[0]}-{sig[1]}" if sig else "0"
    etag = f'"{doc_id}-{page_no}-{sig_token}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "public, max-age=3600"})

    img_bytes = _render_page(filepath, page_no)
    if not img_bytes:
        raise HTTPException(status_code=404, detail=f"Page {page_no} image not available")

    headers = {
        "Cache-Control": "public, max-age=3600",
        "ETag": etag,
    }
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


async def _parse_json_body(request: Request) -> dict:
    """Parse a request body as a JSON object. Returns 400 on malformed JSON or
    non-object bodies (null, lists, strings) so endpoints can rely on dict
    semantics without 500-ing on `.get` against a non-dict."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    return body


@app.post("/api/definitions")
async def create_definition(request: Request):
    """Upload a new document class definition."""
    body = await _parse_json_body(request)
    doc = body.get("document")
    if not isinstance(doc, dict):
        raise HTTPException(status_code=400, detail="`document` must be a JSON object")
    doc_type = doc.get("document_type", "untitled")
    def_id = re.sub(r'[^a-z0-9_]', '_', doc_type.lower()).strip('_')
    if not def_id:
        raise HTTPException(
            status_code=400,
            detail="document_type must contain at least one alphanumeric character",
        )

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

    body = await _parse_json_body(request)
    def_id = body.get("definition_id")
    if not isinstance(def_id, str) or not def_id:
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
