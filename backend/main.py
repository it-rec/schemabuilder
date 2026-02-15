"""FastAPI backend for the Document Viewer application using PyMuPDF."""

import asyncio
import hashlib
import io
import logging
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

logger = logging.getLogger(__name__)

TEST_DOCS_DIR = Path(__file__).parent / "test_documents"
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".pptx"}

# Thread pools
_executor = ThreadPoolExecutor(max_workers=4)
_com_executor = ThreadPoolExecutor(max_workers=1)

# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------

_pdf_metadata_cache: dict = {}  # filepath_str -> metadata dict

_page_image_cache: dict = {}  # (filepath_str, page_no) -> PNG bytes
_page_image_cache_lock = Lock()
_MAX_PAGE_CACHE_ENTRIES = 100

_extraction_cache: dict = {}  # doc_id -> list[dict]
_extraction_lock = Lock()

_pdf_conversion_cache: dict = {}  # doc_id -> Path to converted PDF
_pdf_conversion_lock = Lock()


def _get_document_id(filename: str) -> str:
    return hashlib.md5(filename.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# PDF metadata & rendering (pypdfium2)
# ---------------------------------------------------------------------------

def _get_pdf_metadata(filepath: Path) -> dict:
    key = str(filepath)
    if key in _pdf_metadata_cache:
        return _pdf_metadata_cache[key]

    import pypdfium2 as pdfium

    pdf_doc = pdfium.PdfDocument(str(filepath))
    num_pages = len(pdf_doc)
    page_dimensions = {}
    for i in range(num_pages):
        page = pdf_doc[i]
        page_dimensions[i + 1] = {
            "width": float(page.get_width()),
            "height": float(page.get_height()),
        }
    pdf_doc.close()
    result = {"num_pages": num_pages, "page_dimensions": page_dimensions}
    _pdf_metadata_cache[key] = result
    return result


def _render_pdf_page(filepath: Path, page_no: int) -> bytes:
    cache_key = (str(filepath), page_no)
    with _page_image_cache_lock:
        if cache_key in _page_image_cache:
            return _page_image_cache[cache_key]

    import pypdfium2 as pdfium

    pdf_doc = pdfium.PdfDocument(str(filepath))
    page = pdf_doc[page_no - 1]
    bitmap = page.render(scale=2.0)
    pil_image = bitmap.to_pil()
    buf = io.BytesIO()
    pil_image.save(buf, format="PNG")
    pdf_doc.close()
    img_bytes = buf.getvalue()

    with _page_image_cache_lock:
        if len(_page_image_cache) >= _MAX_PAGE_CACHE_ENTRIES:
            keys = list(_page_image_cache.keys())
            for k in keys[: len(keys) // 2]:
                del _page_image_cache[k]
        _page_image_cache[cache_key] = img_bytes

    return img_bytes


# ---------------------------------------------------------------------------
# DOCX / PPTX → PDF conversion (MS Office COM / LibreOffice fallback)
# ---------------------------------------------------------------------------

def _convert_office_to_pdf(filepath: Path) -> Path:
    doc_id = _get_document_id(filepath.name)
    with _pdf_conversion_lock:
        if doc_id in _pdf_conversion_cache:
            cached = _pdf_conversion_cache[doc_id]
            if cached.exists():
                return cached

        ext = filepath.suffix.lower()
        tmp_dir = Path(tempfile.mkdtemp(prefix="docviewer_"))
        pdf_path = tmp_dir / (filepath.stem + ".pdf")

        converted = False

        if ext == ".docx":
            converted = _convert_docx_word_com(filepath, pdf_path)
        elif ext == ".pptx":
            converted = _convert_pptx_powerpoint_com(filepath, pdf_path)

        if not converted:
            converted = _convert_libreoffice(filepath, tmp_dir)
            if converted:
                candidate = tmp_dir / (filepath.stem + ".pdf")
                if candidate.exists():
                    pdf_path = candidate
                else:
                    converted = False

        if not converted or not pdf_path.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
            raise RuntimeError(
                f"Could not convert {filepath.name} to PDF. "
                "Install MS Office or LibreOffice for DOCX/PPTX support."
            )

        _pdf_conversion_cache[doc_id] = pdf_path
        return pdf_path


def _convert_docx_word_com(src: Path, dst: Path) -> bool:
    try:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            word = win32com.client.Dispatch("Word.Application")
            word.Visible = False
            doc = word.Documents.Open(str(src.resolve()))
            doc.SaveAs(str(dst.resolve()), FileFormat=17)  # wdFormatPDF
            doc.Close(False)
            word.Quit()
            return dst.exists()
        finally:
            pythoncom.CoUninitialize()
    except Exception as exc:
        logger.warning("Word COM conversion failed: %s", exc)
        return False


def _convert_pptx_powerpoint_com(src: Path, dst: Path) -> bool:
    try:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            ppt = win32com.client.Dispatch("PowerPoint.Application")
            presentation = ppt.Presentations.Open(
                str(src.resolve()), WithWindow=False
            )
            presentation.SaveAs(str(dst.resolve()), FileFormat=32)  # ppSaveAsPDF
            presentation.Close()
            ppt.Quit()
            return dst.exists()
        finally:
            pythoncom.CoUninitialize()
    except Exception as exc:
        logger.warning("PowerPoint COM conversion failed: %s", exc)
        return False


def _convert_libreoffice(src: Path, out_dir: Path) -> bool:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        return False
    try:
        subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf",
             "--outdir", str(out_dir), str(src)],
            check=True, timeout=120, capture_output=True,
        )
        return True
    except Exception as exc:
        logger.debug("LibreOffice conversion failed: %s", exc)
        return False


async def _ensure_renderable_pdf(filepath: Path) -> Path:
    if filepath.suffix.lower() == ".pdf":
        return filepath
    doc_id = _get_document_id(filepath.name)
    with _pdf_conversion_lock:
        cached = _pdf_conversion_cache.get(doc_id)
    if cached and cached.exists():
        return cached
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_com_executor, _convert_office_to_pdf, filepath)


# ---------------------------------------------------------------------------
# Text extraction (PyMuPDF — fast, no ML models)
# ---------------------------------------------------------------------------

_LIST_BULLET_RE = re.compile(
    r"^[\u2022\u2023\u25E6\u2043\u2219\u25AA\u25AB\u25CF\-\*]\s"
)
_LIST_NUMBER_RE = re.compile(r"^\d{1,3}[.)]\s")


def _classify_block(
    block_text: str, spans: list[dict], median_size: float
) -> str:
    """Classify a text block into a type name matching the frontend tags."""
    if not spans:
        return "TextItem"

    max_size = max(s["size"] for s in spans)
    total_chars = sum(len(s["text"]) for s in spans)
    if total_chars == 0:
        return "TextItem"
    bold_chars = sum(
        len(s["text"]) for s in spans if s["flags"] & (1 << 4)  # bit 4 = bold
    )
    is_mostly_bold = bold_chars > total_chars * 0.5

    # Title: significantly larger than body text and bold
    if max_size > median_size * 1.4 and is_mostly_bold:
        return "TitleItem"

    # Section header: somewhat larger than body or short bold text
    if max_size > median_size * 1.15 and is_mostly_bold:
        return "SectionHeaderItem"
    if is_mostly_bold and len(block_text) < 120:
        return "SectionHeaderItem"

    # List item: starts with bullet or number
    stripped = block_text.strip()
    if _LIST_BULLET_RE.match(stripped) or _LIST_NUMBER_RE.match(stripped):
        return "ListItem"

    return "TextItem"


def _extract_text_sync(filepath: Path, doc_id: str) -> list[dict]:
    """Extract structured text entries from a PDF using PyMuPDF."""
    with _extraction_lock:
        if doc_id in _extraction_cache:
            return _extraction_cache[doc_id]

    import fitz  # PyMuPDF

    doc = fitz.open(str(filepath))

    # First pass: collect all font sizes to compute the median (body text size)
    all_sizes: list[float] = []
    for page in doc:
        for block in page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    if span["text"].strip():
                        all_sizes.append(span["size"])

    median_size = sorted(all_sizes)[len(all_sizes) // 2] if all_sizes else 12.0

    # Second pass: build text entries
    text_entries: list[dict] = []
    entry_id = 0

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        page_no = page_idx + 1
        page_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)

        # Detect table regions (best-effort, very fast)
        table_rects: list[tuple] = []
        try:
            for table in page.find_tables().tables:
                table_rects.append(table.bbox)
        except Exception:
            pass

        for block in page_dict["blocks"]:
            if block["type"] != 0:  # skip image blocks
                continue

            # Collect spans and assemble block text
            block_spans: list[dict] = []
            lines_text: list[str] = []
            for line in block["lines"]:
                line_parts: list[str] = []
                for span in line["spans"]:
                    if span["text"].strip():
                        block_spans.append(span)
                        line_parts.append(span["text"])
                if line_parts:
                    lines_text.append("".join(line_parts))

            block_text = "\n".join(lines_text).strip()
            if not block_text:
                continue

            bx0, by0, bx1, by1 = block["bbox"]

            # Check if the block falls inside a detected table
            is_table = any(
                bx0 >= tx0 - 2 and by0 >= ty0 - 2
                and bx1 <= tx1 + 2 and by1 <= ty1 + 2
                for tx0, ty0, tx1, ty1 in table_rects
            )

            entry_type = (
                "TableItem"
                if is_table
                else _classify_block(block_text, block_spans, median_size)
            )

            text_entries.append({
                "id": entry_id,
                "text": block_text,
                "type": entry_type,
                "page": page_no,
                "bbox": {
                    "l": float(bx0),
                    "t": float(by0),
                    "r": float(bx1),
                    "b": float(by1),
                    "coord_origin": "TOPLEFT",
                },
            })
            entry_id += 1

    doc.close()

    with _extraction_lock:
        if doc_id not in _extraction_cache:
            _extraction_cache[doc_id] = text_entries
        return _extraction_cache[doc_id]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_file(doc_id: str) -> Optional[Path]:
    for f in TEST_DOCS_DIR.iterdir():
        if (
            f.suffix.lower() in SUPPORTED_EXTENSIONS
            and _get_document_id(f.name) == doc_id
        ):
            return f
    return None


def _list_doc_files() -> list[Path]:
    if not TEST_DOCS_DIR.exists():
        return []
    return [
        f
        for f in sorted(TEST_DOCS_DIR.iterdir())
        if f.suffix.lower() in SUPPORTED_EXTENSIONS
        and not f.name.startswith("~$")
    ]


# ---------------------------------------------------------------------------
# FastAPI app & routes
# ---------------------------------------------------------------------------

app = FastAPI(title="Document Viewer API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/documents")
def list_documents():
    """List all available documents."""
    docs = []
    for f in _list_doc_files():
        docs.append({
            "id": _get_document_id(f.name),
            "filename": f.name,
            "extension": f.suffix.lower(),
            "size": f.stat().st_size,
        })
    return docs


@app.get("/api/documents/{doc_id}")
async def get_document(doc_id: str):
    """Get document metadata and extracted text entries."""
    filepath = _find_file(doc_id)
    if not filepath:
        raise HTTPException(status_code=404, detail="Document not found")

    pdf_path = await _ensure_renderable_pdf(filepath)

    loop = asyncio.get_event_loop()
    metadata = await loop.run_in_executor(_executor, _get_pdf_metadata, pdf_path)
    text_entries = await loop.run_in_executor(
        _executor, _extract_text_sync, pdf_path, doc_id
    )

    return {
        "id": doc_id,
        "filename": filepath.name,
        "num_pages": metadata["num_pages"],
        "page_dimensions": metadata["page_dimensions"],
        "text_entries": text_entries,
    }


@app.get("/api/documents/{doc_id}/pages/{page_no}")
async def get_page_image(doc_id: str, page_no: int):
    """Render a single page image on the fly."""
    filepath = _find_file(doc_id)
    if not filepath:
        raise HTTPException(status_code=404, detail="Document not found")

    pdf_path = await _ensure_renderable_pdf(filepath)

    loop = asyncio.get_event_loop()
    metadata = await loop.run_in_executor(_executor, _get_pdf_metadata, pdf_path)
    if page_no < 1 or page_no > metadata["num_pages"]:
        raise HTTPException(
            status_code=404,
            detail=f"Page {page_no} not found. Document has {metadata['num_pages']} pages.",
        )

    img_bytes = await loop.run_in_executor(
        _executor, _render_pdf_page, pdf_path, page_no
    )
    return Response(content=img_bytes, media_type="image/png")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
