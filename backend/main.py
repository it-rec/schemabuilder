"""FastAPI backend for the Document Viewer application using Docling."""

import atexit
import contextvars
import csv
import hashlib
import io
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request, UploadFile
from fastapi import File as FastAPIFile
from fastapi import Path as PathParam
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from transforms import TransformError, build_export

# Per-request context variable, surfaced into log records via the filter below
# so every line emitted while handling a request carries the same id without
# every call site having to pass it through.
_request_id_ctx: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "schemabuilder_request_id", default="-"
)


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id_ctx.get()
        return True


logger = logging.getLogger("schemabuilder")
# Uvicorn only attaches handlers to its own loggers, so without this our
# logger.info(...) calls (accelerator banner, converter build details) are
# silently dropped. Match Uvicorn's default format and prefix with the request
# id so emissions during request handling can be correlated.
if not logger.handlers and not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:    %(name)s: [%(request_id)s] %(message)s",
    )
logger.setLevel(logging.INFO)
logger.addFilter(_RequestIdFilter())

# Background workers for prefetch (page render warm-up, text extraction warm-up,
# converter construction). Default to 4: the text-extraction leg is serialized
# inside Docling, but page rasterization across different docs is parallel-safe
# and a deeper queue lets a user click through several docs without later
# clicks waiting for earlier extractions to finish their warm-up. Override via
# SCHEMABUILDER_PREFETCH_WORKERS for low-memory deployments.
_PREFETCH_WORKERS = max(1, int(os.getenv("SCHEMABUILDER_PREFETCH_WORKERS") or 4))
_bg_executor = ThreadPoolExecutor(
    max_workers=_PREFETCH_WORKERS, thread_name_prefix="prefetch"
)

# Tracking for in-flight `/extract` calls so shutdown can wait for them rather
# than tearing down the converter mid-request. Use a Condition so we can sleep
# the lifespan finalizer until every active extraction has finished or until
# the grace deadline expires.
_inflight_extracts = 0
_inflight_cv = threading.Condition()
_SHUTDOWN_GRACE_SECONDS = float(os.getenv("SCHEMABUILDER_SHUTDOWN_GRACE") or 30)

# Hard cap on concurrent /extract runs. Docling holds a process-global pipeline
# and is effectively single-threaded per converter; piling on N parallel
# requests just queues them behind each other and burns memory. Reject extras
# fast with 503 + Retry-After so the load balancer can shed and clients can
# back off instead of seeing wall-clock minutes of latency.
_MAX_CONCURRENT_EXTRACTS = max(1, int(os.getenv("SCHEMABUILDER_MAX_CONCURRENT_EXTRACTS") or 4))
_extract_semaphore = threading.BoundedSemaphore(_MAX_CONCURRENT_EXTRACTS)

# Cap request body size (bytes). Definitions payloads are tiny in practice; a
# huge body is either a misconfigured client or an attempt to OOM the matcher
# by feeding it megabytes of `examples`. Enforced by middleware via
# Content-Length so we reject before streaming the body into memory.
_MAX_BODY_BYTES = max(1024, int(os.getenv("SCHEMABUILDER_MAX_BODY_BYTES") or 2_000_000))

# Separate, much larger cap for document uploads — the body is a PDF/DOCX/
# PPTX file, which routinely exceeds the JSON-payload cap. Default 50 MB.
_MAX_DOC_BYTES = max(1024, int(os.getenv("SCHEMABUILDER_MAX_DOC_BYTES") or 50_000_000))

# Strict allow-list for X-Request-ID: hex/ascii-safe so we don't propagate
# header-injection junk into log lines or downstream tracers.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")


@contextmanager
def _track_inflight():
    """Increment/decrement the in-flight extract counter under the condition."""
    global _inflight_extracts
    with _inflight_cv:
        _inflight_extracts += 1
    try:
        yield
    finally:
        with _inflight_cv:
            _inflight_extracts -= 1
            _inflight_cv.notify_all()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Surface the accelerator choice at boot so "is it actually using my GPU?"
    # is answered without waiting for the first /extract to log it.
    logger.info("Docling accelerator: %s", _describe_accelerator())
    # Build the Docling converter eagerly in the background so the first
    # /extract request doesn't pay the model-load cost (~seconds on CPU,
    # less on GPU but still non-trivial). Failure here is non-fatal: the
    # lazy path will retry on first real call.
    _bg_executor.submit(_warm_up_converter)
    try:
        yield
    finally:
        # Wait for in-flight `/extract` calls to finish so a SIGTERM mid-run
        # doesn't kill the Docling pipeline and surface a 5xx to the user.
        # Bounded by SCHEMABUILDER_SHUTDOWN_GRACE so a wedged extraction can't
        # hold the process up forever.
        deadline = time.monotonic() + _SHUTDOWN_GRACE_SECONDS
        with _inflight_cv:
            while _inflight_extracts > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "Shutdown grace expired with %d in-flight extracts",
                        _inflight_extracts,
                    )
                    break
                _inflight_cv.wait(timeout=remaining)
        # Cancel pending prefetch jobs (page warm-up, OCR detection) — these
        # are best-effort and safe to drop.
        _bg_executor.shutdown(wait=False, cancel_futures=True)


def _describe_accelerator() -> str:
    """Human-readable label for the accelerator that Docling will pick.

    Mirrors the resolution order in `_resolve_accelerator_device` (env override
    → CUDA → MPS → CPU) but returns a string so it can be logged at startup
    without importing docling's heavy AcceleratorDevice enum yet.
    """
    forced = (os.getenv("DOCLING_DEVICE") or "").strip().upper()
    try:
        import torch  # type: ignore
    except Exception:
        return f"{forced or 'CPU'} (torch not installed)"

    cuda_available = bool(getattr(torch.cuda, "is_available", lambda: False)())
    mps_backend = getattr(getattr(torch, "backends", None), "mps", None)
    mps_available = bool(mps_backend and mps_backend.is_available())

    def cuda_label() -> str:
        try:
            name = torch.cuda.get_device_name(0)
            return f"CUDA ({name})"
        except Exception:
            return "CUDA"

    if forced == "CUDA":
        return cuda_label() if cuda_available else "CUDA requested but unavailable; falling back to CPU"
    if forced == "MPS":
        return "MPS" if mps_available else "MPS requested but unavailable; falling back to CPU"
    if forced == "CPU":
        return "CPU (forced via DOCLING_DEVICE)"
    if forced and forced != "AUTO":
        return f"{forced} (unknown; will fall back to AUTO)"

    if cuda_available:
        return cuda_label()
    if mps_available:
        return "MPS"
    return "CPU"


app = FastAPI(title="Document Viewer API", lifespan=lifespan)


@app.middleware("http")
async def _request_logging(request: Request, call_next):
    """Attach a request id to every response and emit a one-line access log.

    Honors a caller-supplied `X-Request-ID` header (so an upstream proxy or
    test client can correlate logs end-to-end); otherwise generates a short
    uuid. The id is bound into a ContextVar so any logger.info(...) calls
    from inside handlers, including those dispatched to the threadpool,
    are tagged with it.
    """
    incoming = request.headers.get("x-request-id")
    rid = incoming if incoming and _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex[:12]
    token = _request_id_ctx.set(rid)
    start = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-ID"] = rid
        return response
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        # Health checks otherwise drown the access log; keep them silent.
        if request.url.path not in ("/health", "/metrics"):
            logger.info(
                "%s %s -> %d in %.1fms",
                request.method,
                request.url.path,
                status_code,
                elapsed_ms,
            )
        _request_id_ctx.reset(token)


@app.middleware("http")
async def _enforce_body_size_limit(request: Request, call_next):
    """Reject oversized request bodies before they're streamed into memory.

    Trusts Content-Length when present (Starlette's TestClient and any sane
    HTTP client send it for non-streamed bodies). Streamed/chunked uploads
    without a length header are not bounded here — Starlette's body parsers
    have their own ceilings for those.
    """
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            n = int(cl)
        except ValueError:
            return Response(status_code=400, content="Invalid Content-Length")
        # Document uploads have a separate, larger cap because PDFs are
        # routinely larger than the JSON-payload limit.
        if request.url.path == "/api/documents" and request.method == "POST":
            limit = _MAX_DOC_BYTES
        else:
            limit = _MAX_BODY_BYTES
        if n > limit:
            _metrics_inc("body_too_large")
            return Response(
                status_code=413,
                content=f"Request body exceeds {limit} bytes",
            )
    return await call_next(request)


def _parse_cors_origins() -> list[str]:
    """Resolve allowed CORS origins from the environment.

    `CORS_ALLOW_ORIGINS` is a comma-separated list. Empty or unset falls back
    to localhost:3000 (the CRA dev server) so the out-of-the-box experience
    keeps working without configuration.
    """
    raw = os.getenv("CORS_ALLOW_ORIGINS", "").strip()
    if not raw:
        return ["http://localhost:3000"]
    return [o.strip() for o in raw.split(",") if o.strip()]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_parse_cors_origins(),
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

# Hard caps on the per-doc caches so a long-running process or a directory of
# many files doesn't grow memory (and converted-PDF disk usage) without bound.
# Local single-user usage almost never hits these; they exist as a safety net.
_RENDER_CACHE_MAX = int(os.getenv("SCHEMABUILDER_RENDER_CACHE_MAX") or 64)
_TEXT_CACHE_MAX = int(os.getenv("SCHEMABUILDER_TEXT_CACHE_MAX") or 64)
_PDF_CONVERSION_CACHE_MAX = int(os.getenv("SCHEMABUILDER_PDF_CACHE_MAX") or 64)


def _lru_set(cache: OrderedDict, key, value, max_size: int, on_evict=None) -> None:
    """Insert into an OrderedDict-backed LRU and evict the oldest if over cap.

    `on_evict(key, value)` is called for each evicted entry — used by the PDF
    conversion cache to remove the corresponding file from disk so eviction
    doesn't leak temp files.
    """
    if key in cache:
        cache.move_to_end(key)
    cache[key] = value
    while len(cache) > max_size:
        evicted_key, evicted_value = cache.popitem(last=False)
        if on_evict is not None:
            try:
                on_evict(evicted_key, evicted_value)
            except Exception:
                logger.exception("LRU eviction callback failed")


def _lru_get(cache: OrderedDict, key):
    """Read-with-touch helper. Returns None when missing."""
    value = cache.get(key)
    if value is not None:
        cache.move_to_end(key)
    return value


# Render cache: stores PDF path + per-page dimensions; page images are rendered
# lazily on first request and memoized. Avoids rendering every page when a
# document is selected (the prior behavior blocked the first request entirely
# for large documents).
_render_cache: "OrderedDict[str, dict]" = OrderedDict()
_render_lock = threading.Lock()
# Text-extraction cache: slow, populated lazily on first extraction (Docling).
_text_cache: "OrderedDict[str, dict]" = OrderedDict()
_text_lock = threading.Lock()
# Cache for DOCX/PPTX → PDF conversions. Evicted entries delete the underlying
# PDF so we don't leak disk after eviction.
_pdf_conversion_cache: "OrderedDict[tuple, str]" = OrderedDict()
_pdf_conversion_lock = threading.Lock()
_pdf_temp_dir = tempfile.mkdtemp(prefix="schemabuilder_")


def _evict_pdf_file(_key, path_str: str) -> None:
    """Eviction callback for the PDF-conversion LRU: remove the on-disk PDF."""
    try:
        p = Path(path_str)
        if p.exists() and p.is_file():
            p.unlink()
    except OSError:
        logger.exception("Failed to remove evicted converted PDF %s", path_str)


def _cleanup_pdf_temp_dir() -> None:
    """Remove the converted-PDF temp dir on clean process exit.

    The OS cleans /tmp eventually, but on Windows the directory persists
    across runs and accumulates. Best-effort: don't raise from atexit.
    """
    try:
        shutil.rmtree(_pdf_temp_dir, ignore_errors=True)
    except Exception:
        pass


atexit.register(_cleanup_pdf_temp_dir)

# Cached definitions, keyed by per-file (name, mtime, size) signature so
# the dir is rescanned only when something actually changes on disk.
_definitions_cache: Optional[dict] = None
_definitions_signature: Optional[tuple] = None
_definitions_lock = threading.Lock()

# Doc-id → resolved Path cache. Avoids rescanning the docs dir on every request.
_doc_path_cache: dict = {}
_doc_path_cache_lock = threading.Lock()

# Cached envelope for /api/documents (see _build_documents_listing). Returned
# directly when the on-disk dir signature is unchanged so repeated polls from
# the frontend don't re-stat every file in the dir.
_doc_listing_cache: Optional[list] = None
_doc_listing_signature: Optional[tuple] = None
_doc_listing_lock = threading.Lock()

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

# Lightweight counters exposed via /metrics. Plain ints under a lock are good
# enough at this request volume; a real Prometheus client would be overkill.
_metrics_lock = threading.Lock()
_metrics: dict = {
    "render_cache_hits": 0,
    "render_cache_misses": 0,
    "text_cache_hits": 0,
    "text_cache_misses": 0,
    "text_extraction_errors": 0,
    "ocr_decisions_on": 0,
    "ocr_decisions_off": 0,
    "extractions_completed": 0,
    # Incremented when a request is rejected because the global concurrency
    # cap is full. A non-zero rate here is a signal to bump
    # SCHEMABUILDER_MAX_CONCURRENT_EXTRACTS or scale the process out.
    "extractions_rejected": 0,
    # Incremented when a request body is rejected for exceeding the
    # configured size limit (see _enforce_body_size_limit middleware).
    "body_too_large": 0,
}


def _metrics_inc(key: str, n: int = 1) -> None:
    with _metrics_lock:
        _metrics[key] = _metrics.get(key, 0) + n


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
    with _pdf_conversion_lock:
        cached_path = _lru_get(_pdf_conversion_cache, cache_key)
    if cached_path is not None:
        cached = Path(cached_path)
        if cached.exists():
            return cached
        # Stale entry pointing at a missing file (e.g. temp dir was cleared);
        # drop it so the convert path below repopulates the cache.
        with _pdf_conversion_lock:
            _pdf_conversion_cache.pop(cache_key, None)

    import pythoncom
    import win32com.client

    ext = filepath.suffix.lower()
    abs_path = str(filepath.resolve())
    # Include the source extension in the temp PDF name so two source files
    # that share a stem (e.g. report.docx and report.pptx) don't clobber each
    # other's converted PDF and serve mixed content via the cache.
    pdf_path = Path(_pdf_temp_dir) / f"{filepath.stem}{ext}.pdf"

    # FastAPI dispatches sync endpoints to threadpool workers that have not
    # initialized COM, so Dispatch() raises "CoInitialize was not called".
    pythoncom.CoInitialize()
    try:
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
    finally:
        pythoncom.CoUninitialize()

    with _pdf_conversion_lock:
        _lru_set(
            _pdf_conversion_cache,
            cache_key,
            str(pdf_path),
            _PDF_CONVERSION_CACHE_MAX,
            on_evict=_evict_pdf_file,
        )
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
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    # AcceleratorOptions/AcceleratorDevice moved between submodules across
    # docling versions (pipeline_options on 2.14, accelerator_options later).
    try:
        from docling.datamodel.accelerator_options import (  # type: ignore
            AcceleratorDevice,
            AcceleratorOptions,
        )
    except ImportError:
        from docling.datamodel.pipeline_options import (  # type: ignore
            AcceleratorDevice,
            AcceleratorOptions,
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
        _metrics_inc("ocr_decisions_on" if decision else "ocr_decisions_off")
        logger.info(
            "OCR decision for %s: %s", filepath.name, "on" if decision else "off"
        )
        return decision


# Set once the no-OCR Docling pipeline has finished loading. Used by /ready
# so a load balancer can hold traffic until the first /extract won't pay the
# multi-second model-load cost. Liveness (/health) stays decoupled from this:
# a process that's still warming is alive, just not yet ready to serve.
_warmup_done = threading.Event()


def _warm_up_converter():
    """Background warm-up entrypoint; never raises.

    Only the no-OCR converter is built up front. The OCR-enabled converter
    loads heavier models and is built lazily the first time a scanned doc
    actually requests it, so users with only digital docs never pay for it.

    Constructing the DocumentConverter is cheap — the expensive part is
    pipeline init (HF metadata fetch + weight load to GPU). `initialize_pipeline`
    forces that work to happen here in the background so the first /extract
    request finds the models already resident on-device.
    """
    try:
        from docling.datamodel.base_models import InputFormat

        cv = _get_text_converter(do_ocr=False)
        t0 = time.perf_counter()
        cv.initialize_pipeline(InputFormat.PDF)
        logger.info(
            "Docling pipeline pre-loaded in %.2fs (PDF, no-OCR)",
            time.perf_counter() - t0,
        )
    except Exception:
        logger.exception("Docling converter warm-up failed; will retry lazily")
    finally:
        # Signal ready even on failure: lazy fallback in _get_text_converter
        # will still serve requests. /ready stays "not ready" only until the
        # warm-up thread has at least tried.
        _warmup_done.set()


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


# Per-doc opening locks. Two concurrent requests for the same uncached doc
# would both run _open_pdf_metadata (which blocks on Office COM for DOCX/PPTX
# — minutes of work). The per-doc lock dedupes them; the global _render_lock
# is held only briefly for cache reads/writes, so different docs open in
# parallel.
_render_open_locks: dict = {}
_render_open_locks_lock = threading.Lock()

# Per-doc text-extraction locks. Two concurrent /extract requests for the same
# uncached doc would otherwise both run the multi-second Docling pipeline only
# for the second result to be discarded by the cache-merge check. The per-doc
# lock serializes the slow work for one doc while different docs still extract
# in parallel (Docling itself is serialized inside the converter anyway).
_text_extract_locks: dict = {}
_text_extract_locks_lock = threading.Lock()


def _get_or_render(filepath: Path) -> dict:
    """Render-only path: opens the PDF and records dimensions. Page images are
    populated on demand by _render_page.

    Cache entries record the source file's signature; if the file changes on
    disk the entry is rebuilt instead of returning stale page dimensions or a
    pdf_path that points at an outdated converted PDF.
    """
    doc_id = _get_document_id(filepath.name)
    sig = _file_signature(filepath)

    with _render_lock:
        cached = _lru_get(_render_cache, doc_id)
        if cached is not None and cached.get("_sig") == sig:
            _metrics_inc("render_cache_hits")
            return cached

    with _render_open_locks_lock:
        per_doc = _render_open_locks.setdefault(doc_id, threading.Lock())

    with per_doc:
        # Another caller may have populated the cache while we were waiting
        # for the per-doc lock; re-check before doing the expensive open.
        with _render_lock:
            cached = _lru_get(_render_cache, doc_id)
            if cached is not None and cached.get("_sig") == sig:
                _metrics_inc("render_cache_hits")
                return cached

        # Heavy work outside the global lock so other docs can be served in
        # parallel. Office COM conversion for DOCX/PPTX can take seconds.
        pdf_path, num_pages, page_dimensions = _open_pdf_metadata(filepath)
        entry = {
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
        with _render_lock:
            _metrics_inc("render_cache_misses")
            _lru_set(_render_cache, doc_id, entry, _RENDER_CACHE_MAX)
        return entry


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

    Errors are NOT cached: returning a dict with `extraction_error` set lets
    the caller surface the failure to the API, and the next call retries
    instead of silently replaying a stale empty result.
    """
    doc_id = _get_document_id(filepath.name)
    sig = _file_signature(filepath)
    with _text_lock:
        cached = _lru_get(_text_cache, doc_id)
        if cached is not None and cached.get("_sig") == sig:
            _metrics_inc("text_cache_hits")
            return cached
        _metrics_inc("text_cache_misses")

    # Serialize extraction for the same doc so two simultaneous /extract
    # requests don't both pay the multi-second Docling cost only for one
    # result to be discarded by the cache-merge check below. Different docs
    # are still extracted in parallel via their own per-doc locks.
    with _text_extract_locks_lock:
        per_doc = _text_extract_locks.setdefault(doc_id, threading.Lock())

    with per_doc:
        # Re-check the cache under the per-doc lock: another caller for the
        # same doc may have just populated it while we were waiting.
        with _text_lock:
            cached = _lru_get(_text_cache, doc_id)
            if cached is not None and cached.get("_sig") == sig:
                _metrics_inc("text_cache_hits")
                return cached
        # Run extraction without holding the global text lock so two different
        # docs can be in flight concurrently. Each Docling call is still
        # serialized inside the converter itself.
        try:
            text_entries, docling_dims = _extract_text(filepath)
        except Exception as exc:
            _metrics_inc("text_extraction_errors")
            logger.exception("Text extraction failed for %s", filepath.name)
            return {
                "text_entries": [],
                "page_dimensions": {},
                "_sig": sig,
                "extraction_error": f"{type(exc).__name__}: {exc}"[:300],
            }
        # Pre-lower text once at extraction time so the per-/extract matcher
        # loop doesn't recompute it for every (entry × field) pair.
        # Re-extraction on a cache miss is rare; per-extract calls are common,
        # so amortize here.
        for e in text_entries:
            text = e.get("text", "")
            e["_text_lower"] = text.lower()
            e["_text_stripped_lower"] = text.strip().lower()
        entry = {
            "text_entries": text_entries,
            "page_dimensions": docling_dims,
            "_sig": sig,
        }
        with _text_lock:
            # Another caller may have populated the cache while we were
            # extracting; prefer the freshest result for the current signature.
            existing = _lru_get(_text_cache, doc_id)
            if existing is not None and existing.get("_sig") == sig:
                return existing
            _lru_set(_text_cache, doc_id, entry, _TEXT_CACHE_MAX)
            return entry


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
            # Warm page 2 too so a click on "next page" finds the bytes
            # already memoized. Cheap: shares the open PdfDocument inside
            # _render_page and just rasterizes one extra bitmap.
            try:
                meta = _render_cache.get(doc_id)
                if meta and meta.get("num_pages", 0) >= 2:
                    _render_page(filepath, 2)
            except Exception:
                logger.exception("Prefetch page-2 render failed for %s", filepath.name)
            # Respect the global extraction cap: prefetch warm-up runs the
            # same Docling pipeline as /extract, so unconditionally firing it
            # would double the effective concurrency vs. _MAX_CONCURRENT_EXTRACTS.
            # Skip the warm-up (the real /extract call will run extraction
            # lazily) rather than queue up and starve foreground requests.
            if _extract_semaphore.acquire(blocking=False):
                try:
                    _get_or_extract_text(filepath)
                except Exception:
                    logger.exception("Prefetch text extraction failed for %s", filepath.name)
                finally:
                    _extract_semaphore.release()
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
    with _doc_path_cache_lock:
        cached = _doc_path_cache.get(doc_id)
    if cached is not None:
        p = Path(cached)
        if p.exists():
            return p
        with _doc_path_cache_lock:
            _doc_path_cache.pop(doc_id, None)

    if not TEST_DOCS_DIR.exists():
        return None

    found: Optional[Path] = None
    pending: dict = {}
    for f in TEST_DOCS_DIR.iterdir():
        if f.suffix.lower() in SUPPORTED_EXTENSIONS:
            fid = _get_document_id(f.name)
            pending[fid] = str(f)
            if fid == doc_id:
                found = f
    if pending:
        with _doc_path_cache_lock:
            _doc_path_cache.update(pending)
    return found


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
    # Read the signature first, then the cache. The writer sets cache *before*
    # signature under _definitions_lock, so this read order means a torn read
    # can only miss the fast path (and fall through to the lock) — never
    # return the old cache while believing it matches the new signature.
    cached_sig = _definitions_signature
    cached = _definitions_cache
    if cached is not None and cached_sig == sig:
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

            # User-supplied pattern: include in signatures so an entry that
            # only matches the regex (no example overlap) isn't pre-filtered.
            raw_pat = field.get("pattern")
            if isinstance(raw_pat, str) and raw_pat:
                try:
                    signatures.append(("regex", re.compile(raw_pat)))
                except re.error:
                    pass

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

    # Compile the user-supplied regex once. Validation already happened at
    # upload time (FieldSpec.field_validator); the try/except here is a belt-
    # and-suspenders against a definition that bypassed Pydantic somehow
    # (direct file edit, older format on disk).
    pattern = None
    raw_pattern = field.get("pattern")
    if isinstance(raw_pattern, str) and raw_pattern:
        try:
            pattern = re.compile(raw_pattern)
        except re.error:
            pattern = None

    return {
        "example_lower": example_lower,
        "example_lower_strip": example_lower_strip,
        "options": options,
        "label": label,
        "has_date": has_date,
        "has_id": has_id,
        "has_decimal": has_decimal,
        "has_currency_sign": has_currency_sign,
        "pattern": pattern,
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
            "match_reason": None,
            "match_score": 0,
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
    pattern = m["pattern"]

    best_match = None
    best_score = 0
    best_reason: Optional[str] = None
    # When the winning signal is `pattern_match`, store the matched substring
    # so we can return just the regex hit (e.g. the IBAN) rather than the
    # entire enclosing text entry. Cleared whenever a non-pattern signal wins.
    best_pattern_substring: Optional[str] = None

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
        reason: Optional[str] = None

        # Available options: exact (90) is the max for this loop, so break on
        # exact only; for substring (75), upgrade and keep scanning so a later
        # exact match isn't missed (e.g. options=["AB", "ABC"], text="ABC").
        for opt_lower_strip, opt_pattern in options:
            if opt_lower_strip == text_stripped_lower:
                if score < 90:
                    score = 90
                    reason = "option_exact"
                break
            if score < 75 and opt_pattern.search(text):
                score = 75
                reason = "option_substring"

        # Examples: exact (95) is the max; substring (80) upgrades only.
        # Same rationale: examples=["INV", "INV-001"] with text="INV-001"
        # must score 95, not 80.
        for ex_strip, ex_lower in zip(example_lower_strip, example_lower, strict=False):
            if ex_strip == text_stripped_lower:
                if score < 95:
                    score = 95
                    reason = "example_exact"
                break
            if score < 80 and ex_lower and ex_lower in text_lower:
                score = 80
                reason = "example_substring"

        # Format heuristics
        if has_date and _DATE_DETECT_HEAD_RE.search(text):
            if score < 85:
                score = 85
                reason = "date_format"
        if has_id and _ID_DETECT_RE.search(text):
            if score < 85:
                score = 85
                reason = "id_format"
        if has_decimal and _DECIMAL_DETECT_RE.search(text):
            if score < 70:
                score = 70
                reason = "decimal_format"
        if has_currency_sign and any(s in text for s in _CURRENCY_SIGNS):
            if score < 80:
                score = 80
                reason = "currency_sign"

        if label and label in text_lower:
            if score < 60:
                score = 60
                reason = "label"

        # User-supplied pattern: scored 92, between example_substring (80) and
        # example_exact (95). A hand-crafted regex is a strong signal of
        # intent — it should beat heuristic format detection but not an exact
        # example string.
        pattern_substring: Optional[str] = None
        if pattern is not None:
            pm = pattern.search(text)
            if pm and score < 92:
                score = 92
                reason = "pattern_match"
                pattern_substring = pm.group(1) if pm.groups() else pm.group(0)

        if score > best_score:
            best_score = score
            best_match = entry
            best_reason = reason
            best_pattern_substring = (
                pattern_substring if reason == "pattern_match" else None
            )

    # Per-field acceptance threshold (0–1). Defaults to 0.5 to preserve the
    # historical 50/100 cutoff. A definition can raise it (strict fields like
    # invoice_id) or lower it (fuzzy fields like vendor names). Anything that
    # isn't a finite number in range is clamped to the default; we never want
    # a typo in the JSON to turn off matching entirely.
    raw_threshold = field.get("min_confidence")
    if isinstance(raw_threshold, (int, float)) and 0.0 <= raw_threshold <= 1.0:
        score_cutoff = int(raw_threshold * 100)
    else:
        score_cutoff = 50

    result = {
        "name": field["name"],
        "description": field.get("description", ""),
        "examples": field.get("examples", []),
        "extraction_instructions": field.get("extraction_instructions"),
        "available_options": field.get("available_options"),
        "affix": field.get("affix"),
        "pattern": field.get("pattern") if isinstance(field.get("pattern"), str) else None,
        "min_confidence": raw_threshold
        if isinstance(raw_threshold, (int, float)) and 0.0 <= raw_threshold <= 1.0
        else None,
        "extracted_value": None,
        "confidence": 0,
        "matched_entry_id": None,
        "page": None,
        "bbox": None,
        # Why this field matched (or didn't) — useful for tuning heuristics
        # and for the frontend to show users *which* signal fired. `null`
        # when nothing scored above threshold.
        "match_reason": None,
        "match_score": 0,
        # When the best candidate scored below the field's threshold, surface
        # its text so the user can decide whether to lower the threshold or
        # teach a new example. This is observability, not a match — the
        # field's extracted_value stays None.
        "rejected_candidate": None,
    }

    if best_match and best_score >= score_cutoff:
        used_ids.add(best_match["id"])
        # Pattern matches return just the captured substring (the IBAN, the
        # VAT id, etc.) rather than the surrounding sentence. Other signals
        # return the full entry text — that's been the contract since day one.
        if best_reason == "pattern_match" and best_pattern_substring is not None:
            result["extracted_value"] = best_pattern_substring
        else:
            result["extracted_value"] = best_match["text"]
        result["confidence"] = best_score / 100.0
        result["matched_entry_id"] = best_match["id"]
        result["page"] = best_match.get("page")
        result["bbox"] = best_match.get("bbox")
        result["match_reason"] = best_reason
        result["match_score"] = best_score
    elif best_match and best_score > 0:
        # Below threshold but non-zero: surface the rejected candidate so the
        # UI can offer a "review — was this the right value?" prompt.
        result["rejected_candidate"] = {
            "text": best_match["text"],
            "score": best_score,
            "confidence": best_score / 100.0,
            "page": best_match.get("page"),
        }

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
                "match_reason": None,
                "match_score": 0,
            }
            if kind == "decimal":
                match = _DECIMAL_DETECT_RE.search(text)
                if match:
                    item_field["extracted_value"] = match.group(0)
                    item_field["confidence"] = 0.6
                    item_field["match_reason"] = "decimal_format"
                    item_field["match_score"] = 60
            elif kind == "id":
                match = _ID_DETECT_RE.search(text)
                if match:
                    item_field["extracted_value"] = match.group(0)
                    item_field["confidence"] = 0.6
                    item_field["match_reason"] = "id_format"
                    item_field["match_score"] = 60
            elif kind == "int":
                match = _INT_WORD_RE.search(text)
                if match:
                    item_field["extracted_value"] = match.group(0)
                    item_field["confidence"] = 0.5
                    item_field["match_reason"] = "int_format"
                    item_field["match_score"] = 50
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
        # Entries pulled from the text cache already carry _text_lower /
        # _text_stripped_lower (annotated at extraction time). Fall back to
        # computing here only for entries that bypassed the cache (tests).
        if "_text_lower" not in e:
            text = e.get("text", "")
            e["_text_lower"] = text.lower()
            e["_text_stripped_lower"] = text.strip().lower()
        if _entry_could_match(e, signatures):
            candidates.append(e)

    used_ids: set = set()
    results = []
    for field in fields:
        result = _match_field_to_entries(field, candidates, used_ids)
        results.append(result)
    return results


# ── API Routes ─────────────────────────────────────────────────────────


@app.get("/health")
def health():
    """Liveness probe. Cheap; does not touch Docling or the filesystem
    beyond what's already in memory. Always returns 200 if the process is up
    so an orchestrator can distinguish "stuck" from "still warming" by also
    polling /ready.
    """
    return {
        "status": "ok",
        "definitions_dir_exists": DEFINITIONS_DIR.exists(),
        "test_docs_dir_exists": TEST_DOCS_DIR.exists(),
        "converter_warmed": bool(_text_converters),
        "ready": _warmup_done.is_set(),
        "inflight_extracts": _inflight_extracts,
    }


@app.get("/ready")
def ready():
    """Readiness probe distinct from liveness.

    Returns 200 once the background warm-up has finished (or failed), so a
    Kubernetes-style readinessProbe can hold traffic away from a freshly
    started replica until the first /extract won't pay model-load latency.
    Returns 503 with `Retry-After: 5` while warming so callers back off.
    """
    if _warmup_done.is_set():
        return {"ready": True}
    return Response(
        status_code=503,
        content='{"ready": false}',
        media_type="application/json",
        headers={"Retry-After": "5"},
    )


@app.get("/metrics")
def metrics():
    """Snapshot of internal counters and cache utilization. Plain JSON so the
    frontend or a simple Prometheus exporter can scrape it; format is stable
    enough to graph but not a public contract.
    """
    with _metrics_lock:
        counters = dict(_metrics)
    return {
        "counters": counters,
        "caches": {
            "render": {"size": len(_render_cache), "max": _RENDER_CACHE_MAX},
            "text": {"size": len(_text_cache), "max": _TEXT_CACHE_MAX},
            "pdf_conversion": {
                "size": len(_pdf_conversion_cache),
                "max": _PDF_CONVERSION_CACHE_MAX,
            },
            "ocr_decisions": {"size": len(_ocr_decision_cache)},
            "definitions": {
                "size": len(_definitions_cache) if _definitions_cache else 0,
            },
        },
        "inflight_extracts": _inflight_extracts,
    }


def _paginate(items: list, limit: int, offset: int) -> dict:
    """Standard pagination envelope used by both list endpoints.

    Returns the slice plus a total so the frontend can render "N of M" without
    a second round-trip. `limit` is capped to 500 to keep responses bounded.
    """
    total = len(items)
    end = offset + limit
    return {"items": items[offset:end], "total": total, "limit": limit, "offset": offset}


def _docs_dir_signature() -> tuple:
    """Stable signature for the test docs dir contents. Used to skip rebuilds
    of the document listing when nothing has changed on disk.
    """
    if not TEST_DOCS_DIR.exists():
        return ()
    sig = []
    try:
        for f in sorted(TEST_DOCS_DIR.iterdir()):
            if f.suffix.lower() in SUPPORTED_EXTENSIONS:
                try:
                    st = f.stat()
                    sig.append((f.name, st.st_mtime_ns, st.st_size))
                except OSError:
                    pass
    except OSError:
        return ()
    return tuple(sig)


def _build_documents_listing() -> list:
    """Materialize the document listing (and refresh _doc_path_cache as a
    side-effect). Caller is responsible for caching by signature.
    """
    docs = []
    pending: dict = {}
    for f in sorted(TEST_DOCS_DIR.iterdir()):
        if f.suffix.lower() in SUPPORTED_EXTENSIONS:
            doc_id = _get_document_id(f.name)
            pending[doc_id] = str(f)
            try:
                size = f.stat().st_size
            except OSError:
                continue
            docs.append({
                "id": doc_id,
                "filename": f.name,
                "extension": f.suffix.lower(),
                "size": size,
            })
    if pending:
        with _doc_path_cache_lock:
            _doc_path_cache.update(pending)
    return docs


@app.get("/api/documents")
def list_documents(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """List documents in the test docs dir, paginated.

    The response is a `{items, total, limit, offset}` envelope rather than a
    bare array; old callers can still read `items` to get the same shape.
    Cached by directory signature so back-to-back GETs (e.g. polling) don't
    re-stat every file in the dir.
    """
    global _doc_listing_cache, _doc_listing_signature
    if not TEST_DOCS_DIR.exists():
        return _paginate([], limit, offset)

    sig = _docs_dir_signature()
    cached = _doc_listing_cache
    if cached is not None and _doc_listing_signature == sig:
        return _paginate(cached, limit, offset)

    with _doc_listing_lock:
        if _doc_listing_cache is not None and _doc_listing_signature == sig:
            return _paginate(_doc_listing_cache, limit, offset)
        docs = _build_documents_listing()
        _doc_listing_cache = docs
        _doc_listing_signature = sig
    return _paginate(docs, limit, offset)


_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_upload_filename(name: str) -> str:
    """Strip directory components and unsafe characters from an uploaded
    filename. The extension is preserved (lowercased) and validated against
    SUPPORTED_EXTENSIONS by the caller.
    """
    # Take the basename only — `Path.name` is robust against both
    # forward- and back-slash separators that a Windows client might send.
    base = Path(name).name
    stem = Path(base).stem
    ext = Path(base).suffix.lower()
    # Replace anything that isn't filename-safe with an underscore. We keep
    # dots in the stem to a single trailing one removed below.
    clean_stem = _FILENAME_SAFE_RE.sub("_", stem).strip("._-")
    return (clean_stem or "document") + ext


def _allocate_unique_path(directory: Path, filename: str) -> Path:
    """Return a path inside `directory` that doesn't collide with existing
    files; appends `-1`, `-2`, … to the stem until it's free.
    """
    stem = Path(filename).stem
    ext = Path(filename).suffix
    candidate = directory / filename
    n = 1
    while candidate.exists():
        candidate = directory / f"{stem}-{n}{ext}"
        n += 1
    return candidate


def _invalidate_doc_listing_cache() -> None:
    """Drop the document listing + path caches after a mutation. The
    signature-based cache would self-heal on the next request, but clearing
    eagerly avoids a small window where the listing is stale right after
    an upload/delete returns 200."""
    global _doc_listing_cache, _doc_listing_signature
    with _doc_listing_lock:
        _doc_listing_cache = None
        _doc_listing_signature = None
    with _doc_path_cache_lock:
        _doc_path_cache.clear()


@app.post("/api/documents")
async def upload_document(file: UploadFile = FastAPIFile(...)):
    """Accept a PDF/DOCX/PPTX upload and persist it under TEST_DOCS_DIR.

    Validates extension up-front; size is bounded by the body-size middleware
    using a separate, larger cap for this path. A filename collision gets a
    `-1`, `-2`, … suffix rather than overwriting an existing document, so an
    accidental re-upload doesn't destroy an in-flight investigation.
    """
    raw = file.filename or "upload"
    cleaned = _sanitize_upload_filename(raw)
    ext = Path(cleaned).suffix
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail=(
                f"Unsupported file type {ext!r}. "
                f"Allowed: {sorted(SUPPORTED_EXTENSIONS)}."
            ),
        )

    TEST_DOCS_DIR.mkdir(parents=True, exist_ok=True)
    target = _allocate_unique_path(TEST_DOCS_DIR, cleaned)

    # Stream to a temp file in the same dir, enforce the size cap, then
    # rename — avoids a partial file landing under the real name if the
    # client disconnects or the body exceeds the cap.
    tmp_fd, tmp_path_str = tempfile.mkstemp(
        prefix=target.stem + ".", suffix=ext + ".part", dir=str(TEST_DOCS_DIR)
    )
    tmp_path = Path(tmp_path_str)
    written = 0
    try:
        with os.fdopen(tmp_fd, "wb") as out:
            while True:
                chunk = await file.read(1 << 20)  # 1 MB
                if not chunk:
                    break
                written += len(chunk)
                if written > _MAX_DOC_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Upload exceeds {_MAX_DOC_BYTES} bytes.",
                    )
                out.write(chunk)
        os.replace(tmp_path, target)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    _invalidate_doc_listing_cache()
    doc_id = _get_document_id(target.name)
    return {
        "id": doc_id,
        "filename": target.name,
        "extension": ext,
        "size": written,
    }


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: str):
    """Remove a document from disk + flush every cache that referenced it.

    Returns 404 if no document with that id exists, mirroring the rest of
    the API. Render / text caches are pruned so a re-upload under the same
    filename (which would yield the same doc_id) won't serve stale
    rasterized pages from before the deletion.
    """
    filepath = _find_file(doc_id)
    if not filepath:
        raise HTTPException(status_code=404, detail="Document not found")
    try:
        filepath.unlink()
    except OSError as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to delete document: {e}"
        ) from e

    # Purge every cache that keyed on this doc_id; otherwise a follow-up
    # GET would either 200 from the listing cache or render a stale PNG.
    # _ocr_decision_cache is keyed by (filename, file-signature) — clear
    # whole-hog rather than scan, since deletes are infrequent and the
    # cache rebuilds itself on the next extract.
    _render_cache.pop(doc_id, None)
    _text_cache.pop(doc_id, None)
    _ocr_decision_cache.clear()
    _invalidate_doc_listing_cache()
    return {"id": doc_id, "deleted": True}


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
def get_page_image(
    doc_id: str,
    request: Request,
    page_no: int = PathParam(..., ge=1, le=10_000),
):
    """Get a rendered page image as PNG. Pages are rendered on demand and memoized.

    `page_no` is constrained to a sane range so junk like negative numbers or
    `page_no=999999` is rejected at the route boundary (422) instead of
    propagating through render and returning a confused 404. The upper bound
    is intentionally generous; any real PDF that exceeds it would already be
    a server-side performance problem.

    Sends an ETag tied to the file's mtime/size so the browser can revalidate
    cheaply once the max-age expires (or when the user reloads with a warm
    disk cache); a matching If-None-Match short-circuits to 304 without
    re-sending the PNG bytes.
    """
    filepath = _find_file(doc_id)
    if not filepath:
        raise HTTPException(status_code=404, detail="Document not found")

    # Out-of-range page (e.g. requesting p.50 on a 10-page doc) is a client
    # mistake; return 400 rather than 404 so callers don't conflate it with
    # "doc went away".
    data = _get_or_render(filepath)
    if page_no > data["num_pages"]:
        raise HTTPException(
            status_code=400,
            detail=f"page_no {page_no} out of range (document has {data['num_pages']} pages)",
        )

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
def list_definitions(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """List document class definitions, paginated.

    Same envelope as /api/documents. Sorted by id for stable pagination.
    """
    defs = _load_definitions()
    result = []
    for def_id in sorted(defs.keys()):
        data = defs[def_id]
        doc = data.get("document", {})
        result.append({
            "id": def_id,
            "document_type": doc.get("document_type", "Unknown"),
            "document_description": doc.get("document_description", ""),
            "field_count": len(doc.get("fields", [])),
        })
    return _paginate(result, limit, offset)


@app.get("/api/definitions/{def_id}")
def get_definition(def_id: str):
    """Get a specific document class definition."""
    defs = _load_definitions()
    if def_id not in defs:
        raise HTTPException(status_code=404, detail="Definition not found")
    return {"id": def_id, **defs[def_id]}


_DEF_ID_RE = re.compile(r'[a-z0-9_]+')


def _validate_def_id_shape(def_id: str) -> None:
    """Reject anything that isn't the slug shape produced by `_slugify_document_type`.

    Run before any filesystem access so `..` / slashes / null bytes never even
    reach `unlink()` or `open()`.
    """
    if not _DEF_ID_RE.fullmatch(def_id):
        raise HTTPException(status_code=404, detail="Definition not found")


@app.delete("/api/definitions/{def_id}")
def delete_definition(def_id: str):
    """Remove a document class definition by id.

    Returns 404 if no definition with that id exists. The write side of the
    definitions store (create/patch/delete) is serialized on `_definitions_lock`
    so two concurrent mutations on the same id can't interleave into a
    half-written file or torn cache.
    """
    _validate_def_id_shape(def_id)
    filepath = DEFINITIONS_DIR / f"{def_id}.json"
    with _definitions_lock:
        if not filepath.exists():
            raise HTTPException(status_code=404, detail="Definition not found")
        try:
            filepath.unlink()
        except OSError as e:
            logger.exception("Failed to delete definition %s", def_id)
            raise HTTPException(status_code=500, detail=f"Failed to delete: {e}") from e
        # Reset cache state under the same lock so a concurrent /api/definitions
        # GET can't observe stale post-delete state.
        global _definitions_cache, _definitions_signature
        _definitions_cache = None
        _definitions_signature = None
        _signature_cache.clear()
    return {"id": def_id, "deleted": True}


class FieldSpec(BaseModel):
    """Validation for a single field inside a definition's `document.fields`.

    Extra keys are permitted at every layer so future schema additions don't
    require a server update. `fields` is recursive to support `type: array`.
    """

    model_config = ConfigDict(extra="allow")

    name: str = Field(min_length=1)
    type: Optional[str] = None
    description: Optional[str] = None
    extraction_instructions: Optional[str] = None
    examples: Optional[List[Any]] = None
    available_options: Optional[List[Any]] = None
    affix: Optional[bool] = None
    # Per-field acceptance threshold for the matcher. 0–1 inclusive. When
    # None the matcher falls back to its historical 0.5 cutoff. Validated
    # here so a malformed value lands as a 422 instead of getting silently
    # ignored inside the matcher.
    min_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    # Optional regular expression. When set, any text entry whose text
    # matches becomes a strong candidate (matcher score 92, between
    # example_substring=80 and example_exact=95). The matched substring is
    # what ends up in extracted_value — use a capture group to scope it to
    # the part you want (e.g. "IBAN: (DE\\d{20})"). Validated as a
    # compilable Python regex at upload time.
    pattern: Optional[str] = None
    fields: Optional[List["FieldSpec"]] = None

    @field_validator("pattern")
    @classmethod
    def _validate_regex(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        try:
            re.compile(v)
        except re.error as e:
            raise ValueError(f"pattern is not a valid regular expression: {e}") from e
        return v


FieldSpec.model_rebuild()


class DocumentSpec(BaseModel):
    model_config = ConfigDict(extra="allow")

    document_type: str = Field(min_length=1)
    document_description: Optional[str] = None
    fields: List[FieldSpec] = Field(default_factory=list)


class DefinitionBody(BaseModel):
    model_config = ConfigDict(extra="allow")

    document: DocumentSpec


class ExtractRequest(BaseModel):
    definition_id: str = Field(min_length=1)


def _slugify_document_type(doc_type: str) -> str:
    return re.sub(r'[^a-z0-9_]', '_', doc_type.lower()).strip('_')


def _atomic_write_json(filepath: Path, payload: dict) -> None:
    """Write JSON to disk via a same-dir temp file + os.replace.

    `open(filepath, 'w')` truncates on open, so a crash mid-write leaves a
    zero-byte file that breaks the next definitions-cache load. Writing to a
    sibling temp file and renaming gives us an atomic publish on POSIX and
    "best-effort atomic" on Windows.
    """
    DEFINITIONS_DIR.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=filepath.stem + ".", suffix=".json.tmp", dir=str(filepath.parent)
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, filepath)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


@app.post("/api/definitions")
def create_definition(
    body: DefinitionBody,
    overwrite: bool = Query(False, description="Allow replacing an existing definition with the same id."),
):
    """Upload a new document class definition.

    Validated by Pydantic before this runs: missing/empty `document.document_type`
    or malformed `document.fields` returns 422 instead of crashing the matcher
    at extract time.

    If a definition with the same slugged id already exists, returns 409
    Conflict so a duplicate POST on retry doesn't silently overwrite the
    previous version. Pass `?overwrite=true` (or use PATCH) to replace.

    Write serialized on `_definitions_lock` so two concurrent POSTs can't
    race each other into a half-written file.
    """
    doc_type = body.document.document_type
    def_id = _slugify_document_type(doc_type)
    if not def_id:
        raise HTTPException(
            status_code=400,
            detail="document_type must contain at least one alphanumeric character",
        )

    # Persist the original (validated) body — `model_dump` keeps `extra` keys
    # such as target_tables, source_candidates, etc. that downstream consumers
    # rely on.
    payload = body.model_dump(exclude_none=False)
    filepath = DEFINITIONS_DIR / f"{def_id}.json"
    with _definitions_lock:
        if filepath.exists() and not overwrite:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Definition '{def_id}' already exists. "
                    f"Use PATCH /api/definitions/{def_id} or POST with ?overwrite=true."
                ),
            )
        _atomic_write_json(filepath, payload)
        global _definitions_cache, _definitions_signature
        _definitions_cache = None
        _definitions_signature = None
        _signature_cache.clear()

    return {
        "id": def_id,
        "document_type": doc_type,
        "field_count": len(body.document.fields),
    }


@app.patch("/api/definitions/{def_id}")
def patch_definition(def_id: str, body: DefinitionBody):
    """Replace an existing definition in place.

    Pydantic validates the body the same way `create_definition` does;
    semantically this is "upsert with id from the URL". Returns 404 if the
    target doesn't exist (use POST to create), avoiding the foot-gun where a
    typo in the URL silently creates a new definition under the wrong id.
    """
    _validate_def_id_shape(def_id)
    payload = body.model_dump(exclude_none=False)
    filepath = DEFINITIONS_DIR / f"{def_id}.json"
    with _definitions_lock:
        if not filepath.exists():
            raise HTTPException(status_code=404, detail="Definition not found")
        _atomic_write_json(filepath, payload)
        global _definitions_cache, _definitions_signature
        _definitions_cache = None
        _definitions_signature = None
        _signature_cache.clear()
    return {
        "id": def_id,
        "document_type": body.document.document_type,
        "field_count": len(body.document.fields),
    }


class FieldExampleBody(BaseModel):
    """Body for the click-to-teach endpoint.

    Carries just the example value to append. The field is identified in the
    URL path so a single endpoint can handle both top-level fields and dotted
    paths like `line_items.amount` without overloading the body.
    """

    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1)


def _resolve_field(fields: list, name: str) -> Optional[dict]:
    """Locate a field by name within a fields list (no recursion across
    arrays — top-level only)."""
    if not isinstance(fields, list):
        return None
    for f in fields:
        if isinstance(f, dict) and f.get("name") == name:
            return f
    return None


@app.post("/api/definitions/{def_id}/fields/{field_name}/examples")
def add_field_example(def_id: str, field_name: str, body: FieldExampleBody):
    """Append a value to a field's `examples` list (click-to-teach).

    Supports a dotted path for one level of array sub-fields, e.g.
    ``line_items.amount`` resolves to the ``amount`` sub-field of the
    ``line_items`` array. Refuses duplicates with 409 so a repeated click
    doesn't silently no-op (the caller can swallow the 409 if it prefers
    idempotent behavior).

    Write-side is serialized on `_definitions_lock` and persisted via
    `_atomic_write_json`, matching every other definition mutation.
    """
    _validate_def_id_shape(def_id)
    filepath = DEFINITIONS_DIR / f"{def_id}.json"

    parts = field_name.split(".")
    if len(parts) > 2 or any(not p for p in parts):
        raise HTTPException(
            status_code=400,
            detail="field_name must be 'name' or 'array.subname'.",
        )

    with _definitions_lock:
        if not filepath.exists():
            raise HTTPException(status_code=404, detail="Definition not found")
        with open(filepath) as f:
            definition = json.load(f)
        doc_fields = definition.get("document", {}).get("fields", [])
        field = _resolve_field(doc_fields, parts[0])
        if not field:
            raise HTTPException(
                status_code=404, detail=f"Field '{parts[0]}' not found"
            )
        if len(parts) == 2:
            if field.get("type") != "array":
                raise HTTPException(
                    status_code=400,
                    detail=f"Field '{parts[0]}' is not an array; cannot use dotted path.",
                )
            sub = _resolve_field(field.get("fields", []), parts[1])
            if not sub:
                raise HTTPException(
                    status_code=404,
                    detail=f"Sub-field '{parts[1]}' not found on '{parts[0]}'.",
                )
            field = sub

        examples = field.setdefault("examples", [])
        if body.value in examples:
            raise HTTPException(
                status_code=409,
                detail=f"Example {body.value!r} already exists for field '{field_name}'.",
            )
        examples.append(body.value)
        _atomic_write_json(filepath, definition)
        global _definitions_cache, _definitions_signature
        _definitions_cache = None
        _definitions_signature = None
        _signature_cache.clear()

    return {
        "id": def_id,
        "field": field_name,
        "examples": examples,
    }


@app.post("/api/documents/{doc_id}/extract")
def extract_fields(doc_id: str, body: ExtractRequest):
    """Extract fields from a document using a definition.

    Sync `def` (not async) so FastAPI dispatches this to the threadpool;
    Docling's `convert` blocks the calling thread, and using `async def` here
    would freeze the event loop for the duration of every other request.

    Tracked via `_track_inflight` so a SIGTERM during a long extraction
    waits for completion (bounded by SCHEMABUILDER_SHUTDOWN_GRACE) instead
    of killing the Docling pipeline mid-run.
    """
    filepath = _find_file(doc_id)
    if not filepath:
        raise HTTPException(status_code=404, detail="Document not found")

    def_id = body.definition_id
    defs = _load_definitions()
    if def_id not in defs:
        raise HTTPException(status_code=404, detail="Definition not found")

    # Bounded semaphore prevents pile-on: Docling pipelines hold large model
    # state and serialize internally, so admitting unlimited concurrent
    # extractions just queues them while burning RSS. Reject fast with 503
    # so clients (and any upstream LB) can back off / shed.
    if not _extract_semaphore.acquire(blocking=False):
        _metrics_inc("extractions_rejected")
        raise HTTPException(
            status_code=503,
            detail="Too many concurrent extractions; retry shortly.",
            headers={"Retry-After": "5"},
        )
    try:
        with _track_inflight():
            text_data = _get_or_extract_text(filepath)
            definition = defs[def_id]
            fields = _extract_fields(definition, text_data["text_entries"], def_id=def_id)
            _metrics_inc("extractions_completed")
    finally:
        _extract_semaphore.release()

    # Expose target table names (not the full schema) so the frontend can
    # render an Export menu without re-fetching the definition. Kept name-only
    # to stay cheap and avoid leaking transform internals to the client.
    target_table_names = [
        t.get("name")
        for t in (definition.get("target_tables") or [])
        if isinstance(t, dict) and t.get("name")
    ]
    response: dict = {
        "document_id": doc_id,
        "definition_id": def_id,
        "document_type": definition.get("document", {}).get("document_type", ""),
        "document_description": definition.get("document", {}).get("document_description", ""),
        "fields": fields,
        "page_dimensions": text_data["page_dimensions"],
        "target_tables": target_table_names,
        # Full text entry list so the frontend can offer "click to teach as
        # an example" against any extracted block, not just the ones we
        # already matched. Cheap to include — it's already computed and
        # cached for this call.
        "text_entries": text_data.get("text_entries", []),
    }
    # Surface a failure from the Docling pipeline so the frontend can tell
    # "no matches because nothing matched" apart from "no matches because
    # extraction errored". The fields list is still returned (empty matches)
    # so the UI can render its empty state coherently.
    if text_data.get("extraction_error"):
        response["extraction_error"] = text_data["extraction_error"]
    return response


def _csv_response(table_name: str, rows: list[dict], filename_stem: str) -> Response:
    """Serialize a single result table as CSV with stable column order.

    Column order is taken from the first row's keys, which (because
    `build_export` preserves the definition's column order via dict insertion)
    matches the definition. Empty result sets emit just the header so the
    downstream consumer can still tell schema from data.
    """
    buf = io.StringIO()
    if rows:
        fieldnames = list(rows[0].keys())
    else:
        fieldnames = []
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if v is None else v) for k, v in row.items()})
    body = buf.getvalue().encode("utf-8")
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", filename_stem).strip("_") or "export"
    safe_table = re.sub(r"[^A-Za-z0-9._-]+", "_", table_name).strip("_") or "table"
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{safe_stem}-{safe_table}.csv"'
            ),
        },
    )


@app.get("/api/documents/{doc_id}/export")
def export_document(
    doc_id: str,
    definition_id: str = Query(..., description="Definition id to apply."),
    format: str = Query("json", description="json (all tables) or csv (one table)."),
    table: Optional[str] = Query(
        None,
        description=(
            "Required when format=csv: which target table to download. "
            "Ignored for format=json (which returns all tables)."
        ),
    ),
):
    """Run extraction + apply target_tables transforms, return flat rows.

    Wraps `/extract` and the transform engine. JSON returns every target
    table at once; CSV requires `?table=<name>` because a single CSV can't
    represent multiple tables coherently. Uses the same semaphore as
    `/extract` so this endpoint can't be used to bypass the concurrency cap.
    """
    # Validate format in-band so we don't depend on Query(regex=) which has
    # moved name across FastAPI/Pydantic versions (regex → pattern).
    if format not in ("json", "csv"):
        raise HTTPException(
            status_code=400, detail="format must be 'json' or 'csv'."
        )
    filepath = _find_file(doc_id)
    if not filepath:
        raise HTTPException(status_code=404, detail="Document not found")

    defs = _load_definitions()
    if definition_id not in defs:
        raise HTTPException(status_code=404, detail="Definition not found")
    definition = defs[definition_id]

    if not _extract_semaphore.acquire(blocking=False):
        _metrics_inc("extractions_rejected")
        raise HTTPException(
            status_code=503,
            detail="Too many concurrent extractions; retry shortly.",
            headers={"Retry-After": "5"},
        )
    try:
        with _track_inflight():
            text_data = _get_or_extract_text(filepath)
            fields = _extract_fields(
                definition, text_data["text_entries"], def_id=definition_id
            )
            _metrics_inc("extractions_completed")
    finally:
        _extract_semaphore.release()

    try:
        tables = build_export(definition, doc_id, fields)
    except TransformError as e:
        # Definition-level bug (unknown transform, malformed source, etc.) —
        # surface as 422 so the client knows the input was bad, not the
        # server. The exact message is safe to leak; it points at the
        # offending definition, not user data.
        raise HTTPException(status_code=422, detail=str(e)) from e

    if format == "csv":
        if not table:
            raise HTTPException(
                status_code=400,
                detail="CSV export requires ?table=<name>; use format=json for all tables.",
            )
        if table not in tables:
            raise HTTPException(
                status_code=404,
                detail=f"Table '{table}' not found in definition '{definition_id}'.",
            )
        return _csv_response(table, tables[table], filename_stem=doc_id)

    return {
        "document_id": doc_id,
        "definition_id": definition_id,
        "tables": tables,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
