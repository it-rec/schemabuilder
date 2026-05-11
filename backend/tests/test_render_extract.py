"""Rigorous tests for the render + Docling extraction layers.

The actual pypdfium2 / docling / pywin32 modules aren't loaded in CI (and
shouldn't be — they bring ML model weights). These tests install fakes via
`sys.modules` so the *control flow* through _open_pdf_metadata, _render_page,
_get_or_render, _extract_text, _get_or_extract_text, _build_text_converter,
_get_text_converter, _warm_up_converter, and the prefetch worker is exercised
end-to-end. The fakes preserve the contract those real modules expose; if the
contract changes (e.g. docling renames an attribute we read), the tests fail.
"""
from __future__ import annotations

import sys
import threading
import time
import types
from pathlib import Path

import pytest

import main


# ── pypdfium2 fakes ─────────────────────────────────────────────────────


class _FakeBitmap:
    def __init__(self, data: bytes):
        self._data = data

    def to_pil(self):
        return _FakePilImage(self._data)


class _FakePilImage:
    def __init__(self, data: bytes):
        self._data = data

    def save(self, buf, format=None):
        buf.write(self._data)


class _FakePdfPage:
    def __init__(self, width=612.0, height=792.0, bitmap=b"PNGDATA"):
        self._w = width
        self._h = height
        self._bitmap = bitmap

    def get_width(self):
        return self._w

    def get_height(self):
        return self._h

    def render(self, scale=2.0):
        return _FakeBitmap(self._bitmap)


class _FakePdfDocument:
    def __init__(self, pages):
        self._pages = pages
        self._closed = False

    def __len__(self):
        return len(self._pages)

    def __getitem__(self, i):
        if i < 0 or i >= len(self._pages):
            raise IndexError
        return self._pages[i]

    def close(self):
        self._closed = True


def _install_fake_pdfium(monkeypatch, doc):
    fake = types.SimpleNamespace(PdfDocument=lambda _path: doc)
    monkeypatch.setitem(sys.modules, "pypdfium2", fake)


# ── _open_pdf_metadata ──────────────────────────────────────────────────


def test_open_pdf_metadata_records_per_page_dimensions(monkeypatch, tmp_path):
    pages = [_FakePdfPage(612.0, 792.0), _FakePdfPage(595.0, 842.0)]
    doc = _FakePdfDocument(pages)
    _install_fake_pdfium(monkeypatch, doc)
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF")
    pdf_path, n, dims = main._open_pdf_metadata(f)
    assert pdf_path == f
    assert n == 2
    assert dims[1]["width"] == 612.0 and dims[1]["height"] == 792.0
    assert dims[2]["width"] == 595.0 and dims[2]["height"] == 842.0
    assert doc._closed  # The doc is closed on exit.


def test_open_pdf_metadata_returns_empty_on_missing_pdf(monkeypatch, tmp_path):
    """DOCX conversion that returns None (no Office) → no pdf_path → no
    metadata. The handler must not crash."""
    # The import happens at function top-level, so pdfium must be importable
    # even though we won't open any PDF.
    _install_fake_pdfium(monkeypatch, _FakePdfDocument([]))
    monkeypatch.setattr(main, "_convert_to_pdf", lambda _p: None)
    f = tmp_path / "doc.docx"
    f.write_bytes(b"PK\x03\x04")
    pdf_path, n, dims = main._open_pdf_metadata(f)
    assert pdf_path is None
    assert n == 0
    assert dims == {}


def test_open_pdf_metadata_converts_docx(monkeypatch, tmp_path):
    """For DOCX inputs, _convert_to_pdf is called and its result fed to pdfium."""
    converted = tmp_path / "converted.pdf"
    converted.write_bytes(b"%PDF")
    monkeypatch.setattr(main, "_convert_to_pdf", lambda _p: converted)
    _install_fake_pdfium(monkeypatch, _FakePdfDocument([_FakePdfPage()]))
    f = tmp_path / "doc.docx"
    f.write_bytes(b"PK")
    pdf_path, n, dims = main._open_pdf_metadata(f)
    assert pdf_path == converted
    assert n == 1


def test_open_pdf_metadata_returns_empty_when_converted_file_missing(
    monkeypatch, tmp_path
):
    """If _convert_to_pdf returns a path that doesn't exist on disk (e.g.
    Office died mid-write), the helper must not feed it to pdfium."""
    _install_fake_pdfium(monkeypatch, _FakePdfDocument([]))
    fake_path = tmp_path / "never-written.pdf"
    monkeypatch.setattr(main, "_convert_to_pdf", lambda _p: fake_path)
    f = tmp_path / "ghost.docx"
    f.write_bytes(b"PK")
    pdf_path, n, dims = main._open_pdf_metadata(f)
    assert pdf_path is None
    assert n == 0


# ── _render_single_page ─────────────────────────────────────────────────


def test_render_single_page_returns_png_bytes(monkeypatch, tmp_path):
    pages = [_FakePdfPage(bitmap=b"PAGE1PNG"), _FakePdfPage(bitmap=b"PAGE2PNG")]
    doc = _FakePdfDocument(pages)
    _install_fake_pdfium(monkeypatch, doc)
    assert main._render_single_page("/tmp/whatever.pdf", 1) == b"PAGE1PNG"
    assert main._render_single_page("/tmp/whatever.pdf", 2) == b"PAGE2PNG"


def test_render_single_page_out_of_range_returns_none(monkeypatch):
    pages = [_FakePdfPage()]
    _install_fake_pdfium(monkeypatch, _FakePdfDocument(pages))
    assert main._render_single_page("/tmp/x.pdf", 0) is None
    assert main._render_single_page("/tmp/x.pdf", 99) is None


def test_render_single_page_closes_doc_on_exception(monkeypatch):
    pages = [_FakePdfPage()]
    doc = _FakePdfDocument(pages)
    _install_fake_pdfium(monkeypatch, doc)

    def boom(_self, scale=None):
        raise RuntimeError("render kaboom")

    monkeypatch.setattr(_FakePdfPage, "render", boom)
    with pytest.raises(RuntimeError):
        main._render_single_page("/tmp/x.pdf", 1)
    # Finally clause must still close.
    assert doc._closed


# ── _get_or_render integration ──────────────────────────────────────────


def test_get_or_render_caches_first_call(monkeypatch, tmp_path):
    """A second call must hit the cache (no re-open of the PDF)."""
    main._render_cache.clear()
    f = tmp_path / "g.pdf"
    f.write_bytes(b"%PDF")
    opens = {"n": 0}

    def fake_open(filepath):
        opens["n"] += 1
        return filepath, 3, {i + 1: {"width": 100.0, "height": 100.0} for i in range(3)}

    monkeypatch.setattr(main, "_open_pdf_metadata", fake_open)
    a = main._get_or_render(f)
    b = main._get_or_render(f)
    assert a is b
    assert opens["n"] == 1
    assert a["num_pages"] == 3


def test_get_or_render_invalidates_on_file_signature_change(monkeypatch, tmp_path):
    """If the file is replaced in place, the next call must re-open it
    instead of returning stale page dimensions."""
    main._render_cache.clear()
    f = tmp_path / "g.pdf"
    f.write_bytes(b"%PDF-v1")
    opens = {"n": 0}

    def fake_open(filepath):
        opens["n"] += 1
        return filepath, opens["n"], {i + 1: {"width": 1.0, "height": 1.0}
                                       for i in range(opens["n"])}

    monkeypatch.setattr(main, "_open_pdf_metadata", fake_open)
    a = main._get_or_render(f)
    time.sleep(0.01)
    f.write_bytes(b"%PDF-v2-different")  # bumps mtime+size → new sig
    b = main._get_or_render(f)
    assert a is not b
    assert opens["n"] == 2


def test_get_or_render_minimum_one_page(monkeypatch, tmp_path):
    """If _open_pdf_metadata returns 0 pages (e.g. unreadable), num_pages
    must still be at least 1 so the UI can render *something* (the placeholder
    image path). Pin this invariant."""
    main._render_cache.clear()
    f = tmp_path / "z.pdf"
    f.write_bytes(b"%PDF")
    monkeypatch.setattr(main, "_open_pdf_metadata", lambda fp: (None, 0, {}))
    data = main._get_or_render(f)
    assert data["num_pages"] >= 1


# ── _render_page ────────────────────────────────────────────────────────


def test_render_page_memoizes_per_page(monkeypatch, tmp_path):
    """The PNG bytes for a page must be computed at most once per cache entry."""
    main._render_cache.clear()
    f = tmp_path / "m.pdf"
    f.write_bytes(b"%PDF")
    monkeypatch.setattr(
        main, "_open_pdf_metadata",
        lambda fp: (fp, 2, {1: {"width": 1.0, "height": 1.0},
                            2: {"width": 1.0, "height": 1.0}}),
    )
    calls = {"n": 0}

    def fake_single(_path, page_no):
        calls["n"] += 1
        return f"page-{page_no}".encode()

    monkeypatch.setattr(main, "_render_single_page", fake_single)
    main._render_page(f, 1)
    main._render_page(f, 1)
    main._render_page(f, 2)
    assert calls["n"] == 2


def test_render_page_returns_none_when_no_pdf_path(monkeypatch, tmp_path):
    main._render_cache.clear()
    f = tmp_path / "nopath.pdf"
    f.write_bytes(b"%PDF")
    monkeypatch.setattr(main, "_open_pdf_metadata", lambda fp: (None, 0, {}))
    assert main._render_page(f, 1) is None


def test_render_page_returns_none_when_renderer_returns_none(monkeypatch, tmp_path):
    main._render_cache.clear()
    f = tmp_path / "fail.pdf"
    f.write_bytes(b"%PDF")
    monkeypatch.setattr(
        main, "_open_pdf_metadata",
        lambda fp: (fp, 1, {1: {"width": 1.0, "height": 1.0}}),
    )
    monkeypatch.setattr(main, "_render_single_page", lambda *a, **kw: None)
    assert main._render_page(f, 1) is None


# ── _extract_text (Docling fake) ────────────────────────────────────────


class _Bbox:
    def __init__(self, l=0.0, t=0.0, r=10.0, b=10.0, origin="BOTTOMLEFT"):
        self.l, self.t, self.r, self.b = l, t, r, b
        self.coord_origin = types.SimpleNamespace(value=origin)


class _Prov:
    def __init__(self, page_no=1, bbox=None):
        self.page_no = page_no
        self.bbox = bbox


class _TextItem:
    def __init__(self, text, page=1, bbox=None):
        self.text = text
        self.prov = [_Prov(page, bbox)] if bbox else [_Prov(page)]


class _MarkdownItem:
    def __init__(self, md):
        self._md = md

    def export_to_markdown(self):
        return self._md


class _PageSize:
    def __init__(self, w, h):
        self.width, self.height = w, h


class _FakeDocPage:
    def __init__(self, w, h):
        self.size = _PageSize(w, h)


class _FakeDoclingDoc:
    def __init__(self, items, page_dims):
        self._items = items
        self.pages = {pn: _FakeDocPage(w, h) for pn, (w, h) in page_dims.items()}

    def iterate_items(self):
        for it in self._items:
            yield it


class _FakeConvertResult:
    def __init__(self, doc):
        self.document = doc


class _FakeConverter:
    def __init__(self, doc):
        self._doc = doc
        self.convert_calls = 0
        self.init_calls = 0

    def convert(self, _path):
        self.convert_calls += 1
        return _FakeConvertResult(self._doc)

    def initialize_pipeline(self, _fmt):
        self.init_calls += 1


def test_extract_text_returns_entries_and_dims(monkeypatch, tmp_path):
    bbox = _Bbox(l=10.0, t=20.0, r=30.0, b=40.0, origin="BOTTOMLEFT")
    doc = _FakeDoclingDoc(
        items=[
            _TextItem("Hello", page=1, bbox=bbox),
            _TextItem("   ", page=1),  # whitespace-only → skipped
            _TextItem("World", page=2),
            _MarkdownItem("# header"),
            _TextItem("", page=1),  # empty → skipped
        ],
        page_dims={1: (612.0, 792.0), 2: (595.0, 842.0)},
    )
    fake = _FakeConverter(doc)
    monkeypatch.setitem(main._text_converters, False, fake)
    monkeypatch.setattr(main, "_resolve_ocr_decision", lambda _p: False)

    entries, dims = main._extract_text(tmp_path / "x.pdf")

    # 3 non-empty items survive (2 TextItem + 1 MarkdownItem).
    assert len(entries) == 3
    assert {e["text"] for e in entries} == {"Hello", "World", "# header"}
    # Bbox propagated only when present.
    hello = next(e for e in entries if e["text"] == "Hello")
    assert hello["bbox"] == {"l": 10.0, "t": 20.0, "r": 30.0, "b": 40.0,
                             "coord_origin": "BOTTOMLEFT"}
    # Page dims include both pages.
    assert dims == {1: {"width": 612.0, "height": 792.0},
                    2: {"width": 595.0, "height": 842.0}}


def test_extract_text_bbox_coord_origin_falls_back_to_name(monkeypatch, tmp_path):
    """If the coord_origin object has no `.value`, the helper falls back to
    `.name`. Pin both branches."""

    class _OriginName:
        name = "TOPLEFT"

    class _BboxNameOnly:
        def __init__(self):
            self.l, self.t, self.r, self.b = 1, 2, 3, 4
            self.coord_origin = _OriginName()

    item = _TextItem("text", page=1, bbox=_BboxNameOnly())
    doc = _FakeDoclingDoc([item], {1: (10, 10)})
    monkeypatch.setitem(main._text_converters, False, _FakeConverter(doc))
    monkeypatch.setattr(main, "_resolve_ocr_decision", lambda _p: False)
    entries, _ = main._extract_text(tmp_path / "n.pdf")
    assert entries[0]["bbox"]["coord_origin"] == "TOPLEFT"


def test_extract_text_skips_items_without_text_attr(monkeypatch, tmp_path):
    """Items without `text` and without `export_to_markdown` must be silently
    skipped — they're typically structural nodes (sections, etc.)."""

    class _Other:
        pass

    doc = _FakeDoclingDoc([_Other(), _TextItem("real")], {1: (10, 10)})
    monkeypatch.setitem(main._text_converters, False, _FakeConverter(doc))
    monkeypatch.setattr(main, "_resolve_ocr_decision", lambda _p: False)
    entries, _ = main._extract_text(tmp_path / "s.pdf")
    assert [e["text"] for e in entries] == ["real"]


def test_extract_text_iterate_items_can_yield_tuples(monkeypatch, tmp_path):
    """Some docling versions emit (item, level) tuples; the helper unpacks
    `element[0] if isinstance(element, tuple) else element`. Cover both."""

    class _DocTuples:
        pages = {1: _FakeDocPage(10, 10)}

        def iterate_items(self):
            yield (_TextItem("from-tuple"), 0)

    monkeypatch.setitem(main._text_converters, False, _FakeConverter(_DocTuples()))
    monkeypatch.setattr(main, "_resolve_ocr_decision", lambda _p: False)
    entries, _ = main._extract_text(tmp_path / "t.pdf")
    assert entries[0]["text"] == "from-tuple"


# ── _get_or_extract_text ────────────────────────────────────────────────


def test_get_or_extract_text_pre_lowers_entries(monkeypatch, tmp_path):
    main._text_cache.clear()
    f = tmp_path / "lower.pdf"
    f.write_bytes(b"%PDF")
    monkeypatch.setattr(
        main, "_extract_text",
        lambda _p: ([
            {"id": 0, "text": "MIXED Case", "type": "TextItem", "page": 1, "bbox": None}
        ], {}),
    )
    data = main._get_or_extract_text(f)
    e = data["text_entries"][0]
    assert e["_text_lower"] == "mixed case"
    assert e["_text_stripped_lower"] == "mixed case"


def test_get_or_extract_text_returns_cached_on_signature_match(monkeypatch, tmp_path):
    main._text_cache.clear()
    f = tmp_path / "c.pdf"
    f.write_bytes(b"%PDF")
    calls = {"n": 0}

    def fake(_p):
        calls["n"] += 1
        return [], {}

    monkeypatch.setattr(main, "_extract_text", fake)
    a = main._get_or_extract_text(f)
    b = main._get_or_extract_text(f)
    assert a is b
    assert calls["n"] == 1


def test_get_or_extract_text_re_extracts_after_file_changes(monkeypatch, tmp_path):
    main._text_cache.clear()
    f = tmp_path / "c2.pdf"
    f.write_bytes(b"%PDF-v1")
    monkeypatch.setattr(
        main, "_extract_text",
        lambda _p: ([], {}),
    )
    a = main._get_or_extract_text(f)
    time.sleep(0.01)
    f.write_bytes(b"%PDF-v2-different")
    b = main._get_or_extract_text(f)
    assert a is not b


def test_get_or_extract_text_error_returns_extraction_error_marker(monkeypatch, tmp_path):
    main._text_cache.clear()
    f = tmp_path / "err.pdf"
    f.write_bytes(b"%PDF")

    def boom(_p):
        raise RuntimeError("kaboom from docling")

    monkeypatch.setattr(main, "_extract_text", boom)
    data = main._get_or_extract_text(f)
    assert data["text_entries"] == []
    assert "extraction_error" in data
    assert "kaboom" in data["extraction_error"]
    # Error result is NOT cached: a follow-up call retries.
    monkeypatch.setattr(main, "_extract_text", lambda _p: ([], {}))
    data2 = main._get_or_extract_text(f)
    assert "extraction_error" not in data2


def test_get_or_extract_text_truncates_long_error_message(monkeypatch, tmp_path):
    """The extraction_error is capped at 300 chars to keep the response small."""
    main._text_cache.clear()
    f = tmp_path / "long.pdf"
    f.write_bytes(b"%PDF")

    def boom(_p):
        raise RuntimeError("X" * 1000)

    monkeypatch.setattr(main, "_extract_text", boom)
    data = main._get_or_extract_text(f)
    assert len(data["extraction_error"]) <= 300


# ── _get_text_converter caching ─────────────────────────────────────────


def test_get_text_converter_caches_per_ocr_flag(monkeypatch):
    """The cache is keyed by do_ocr — same flag returns the same converter."""
    main._text_converters.clear()
    builds = {"n": 0}

    class _F:
        def __init__(self): builds["n"] += 1

    monkeypatch.setattr(main, "_build_text_converter", lambda do_ocr: _F())
    a = main._get_text_converter(False)
    b = main._get_text_converter(False)
    c = main._get_text_converter(True)
    assert a is b
    assert a is not c
    assert builds["n"] == 2
    main._text_converters.clear()


# ── _warm_up_converter (graceful failure) ───────────────────────────────


def test_warm_up_converter_sets_ready_even_on_failure(monkeypatch):
    """If pipeline init throws, _warmup_done must still be set so /ready
    unblocks and the lazy path retries later."""
    main._warmup_done.clear()
    main._text_converters.clear()

    # The function imports `docling.datamodel.base_models` at runtime; install
    # a fake module so the import resolves.
    fake_base = types.SimpleNamespace(InputFormat=types.SimpleNamespace(PDF="PDF"))
    monkeypatch.setitem(sys.modules, "docling", types.SimpleNamespace(datamodel=None))
    monkeypatch.setitem(sys.modules, "docling.datamodel",
                        types.SimpleNamespace(base_models=fake_base))
    monkeypatch.setitem(sys.modules, "docling.datamodel.base_models", fake_base)

    class _BoomConverter:
        def initialize_pipeline(self, _fmt):
            raise RuntimeError("simulated init failure")

    monkeypatch.setattr(main, "_get_text_converter", lambda do_ocr: _BoomConverter())
    main._warm_up_converter()
    assert main._warmup_done.is_set()
    main._warmup_done.set()  # restore default state


def test_warm_up_converter_calls_initialize_pipeline(monkeypatch):
    """The whole point of warm-up is to materialize the pipeline. Pin that
    init is invoked exactly once before the function returns."""
    main._warmup_done.clear()
    main._text_converters.clear()

    fake_base = types.SimpleNamespace(InputFormat=types.SimpleNamespace(PDF="PDF"))
    monkeypatch.setitem(sys.modules, "docling.datamodel.base_models", fake_base)

    cv = _FakeConverter(doc=_FakeDoclingDoc([], {}))
    monkeypatch.setattr(main, "_get_text_converter", lambda do_ocr: cv)
    main._warm_up_converter()
    assert cv.init_calls == 1
    assert main._warmup_done.is_set()


# ── _build_text_converter (Docling module integration) ──────────────────


def test_build_text_converter_wires_pipeline_options(monkeypatch):
    """The builder must construct PdfPipelineOptions, set accelerator + do_ocr,
    and pass them through to a DocumentConverter."""
    # Build a minimal fake docling module tree.
    captured = {}

    class _FakeAcceleratorDevice:
        AUTO = "AUTO"
        CPU = "CPU"
        CUDA = "CUDA"
        MPS = "MPS"

    class _FakeAcceleratorOptions:
        def __init__(self, num_threads, device):
            self.num_threads = num_threads
            self.device = device

    class _FakePipelineOptions:
        def __init__(self):
            self.generate_page_images = None
            self.images_scale = None
            self.accelerator_options = None
            self.do_ocr = None

    class _FakePdfFormatOption:
        def __init__(self, pipeline_options):
            self.pipeline_options = pipeline_options

    class _FakeDocConverter:
        def __init__(self, format_options):
            captured["format_options"] = format_options

    fake_pipe_mod = types.SimpleNamespace(
        PdfPipelineOptions=_FakePipelineOptions,
        AcceleratorDevice=_FakeAcceleratorDevice,
        AcceleratorOptions=_FakeAcceleratorOptions,
    )
    fake_doc_conv = types.SimpleNamespace(
        DocumentConverter=_FakeDocConverter,
        PdfFormatOption=_FakePdfFormatOption,
    )
    fake_accel_mod = types.SimpleNamespace(
        AcceleratorDevice=_FakeAcceleratorDevice,
        AcceleratorOptions=_FakeAcceleratorOptions,
    )

    monkeypatch.setitem(sys.modules, "docling", types.SimpleNamespace())
    monkeypatch.setitem(sys.modules, "docling.datamodel", types.SimpleNamespace())
    monkeypatch.setitem(sys.modules, "docling.datamodel.pipeline_options", fake_pipe_mod)
    monkeypatch.setitem(sys.modules, "docling.document_converter", fake_doc_conv)
    monkeypatch.setitem(sys.modules, "docling.datamodel.accelerator_options",
                        fake_accel_mod)
    monkeypatch.setenv("DOCLING_NUM_THREADS", "3")
    monkeypatch.delenv("DOCLING_DEVICE", raising=False)

    converter = main._build_text_converter(do_ocr=True)
    pdf_opt = captured["format_options"]["pdf"]
    pipe = pdf_opt.pipeline_options
    assert pipe.do_ocr is True
    assert pipe.generate_page_images is False
    assert pipe.images_scale == 2.0
    assert pipe.accelerator_options.num_threads == 3


def test_build_text_converter_falls_back_to_pipeline_options_module(monkeypatch):
    """When `docling.datamodel.accelerator_options` is absent (older docling),
    AcceleratorDevice / AcceleratorOptions are imported from pipeline_options.
    Pin that fallback path."""

    class _FakeAcceleratorDevice:
        AUTO = "AUTO"
        CPU = "CPU"

    class _FakeAcceleratorOptions:
        def __init__(self, num_threads, device): pass

    class _FakePipelineOptions:
        def __init__(self):
            self.generate_page_images = False
            self.images_scale = 2.0
            self.accelerator_options = None
            self.do_ocr = False

    class _FakePdfFormatOption:
        def __init__(self, pipeline_options): self.pipeline_options = pipeline_options

    class _FakeDocConverter:
        def __init__(self, format_options): pass

    fake_pipe_mod = types.SimpleNamespace(
        PdfPipelineOptions=_FakePipelineOptions,
        AcceleratorDevice=_FakeAcceleratorDevice,
        AcceleratorOptions=_FakeAcceleratorOptions,
    )
    fake_doc_conv = types.SimpleNamespace(
        DocumentConverter=_FakeDocConverter,
        PdfFormatOption=_FakePdfFormatOption,
    )
    # Critical: do not provide docling.datamodel.accelerator_options so the
    # ImportError branch fires.
    monkeypatch.setitem(sys.modules, "docling", types.SimpleNamespace())
    monkeypatch.setitem(sys.modules, "docling.datamodel", types.SimpleNamespace())
    monkeypatch.setitem(sys.modules, "docling.datamodel.pipeline_options", fake_pipe_mod)
    monkeypatch.setitem(sys.modules, "docling.document_converter", fake_doc_conv)
    sys.modules.pop("docling.datamodel.accelerator_options", None)
    monkeypatch.delenv("DOCLING_DEVICE", raising=False)
    # Should not raise.
    main._build_text_converter(do_ocr=False)


# ── _convert_to_pdf (pywin32 / Office COM fake) ─────────────────────────


def _install_fake_win32(monkeypatch, app_call_log: list):
    """Inject minimal fake pythoncom + win32com.client modules."""

    class _Doc:
        def __init__(self, log, ext):
            self._log = log
            self._ext = ext

        def SaveAs(self, path, FileFormat=None):
            self._log.append(("save", path, FileFormat, self._ext))
            Path(path).write_bytes(b"%PDF-fake\n%%EOF\n")

        def Close(self):
            self._log.append(("close", self._ext))

    class _DocsCollection:
        def __init__(self, log, ext):
            self._log = log
            self._ext = ext

        def Open(self, path):
            self._log.append(("open", path, self._ext))
            return _Doc(self._log, self._ext)


    class _PresCollection:
        def __init__(self, log):
            self._log = log

        def Open(self, path, WithWindow=False):
            self._log.append(("open-ppt", path, WithWindow))
            return _Doc(self._log, ".pptx")

    class _WordApp:
        Visible = True

        def __init__(self):
            self.Documents = _DocsCollection(app_call_log, ".docx")

        def Quit(self):
            app_call_log.append(("quit-word",))

    class _PptApp:
        def __init__(self):
            self.Presentations = _PresCollection(app_call_log)

        def Quit(self):
            app_call_log.append(("quit-ppt",))

    class _Client:
        @staticmethod
        def Dispatch(name):
            app_call_log.append(("dispatch", name))
            if name == "Word.Application":
                return _WordApp()
            if name == "PowerPoint.Application":
                return _PptApp()
            raise ValueError(name)

    fake_win32com = types.SimpleNamespace(client=_Client)
    fake_pythoncom = types.SimpleNamespace(
        CoInitialize=lambda: app_call_log.append(("co-init",)),
        CoUninitialize=lambda: app_call_log.append(("co-uninit",)),
    )
    monkeypatch.setitem(sys.modules, "win32com", fake_win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", _Client)
    monkeypatch.setitem(sys.modules, "pythoncom", fake_pythoncom)


def test_convert_to_pdf_docx_uses_word(monkeypatch, tmp_path):
    main._pdf_conversion_cache.clear()
    monkeypatch.setattr(main, "_pdf_temp_dir", str(tmp_path))
    log = []
    _install_fake_win32(monkeypatch, log)
    src = tmp_path / "report.docx"
    src.write_bytes(b"PK")
    pdf = main._convert_to_pdf(src)
    assert pdf is not None
    assert pdf.exists()
    # COM init/uninit must surround the work.
    kinds = [t[0] for t in log]
    assert kinds[0] == "co-init"
    assert kinds[-1] == "co-uninit"
    assert "dispatch" in kinds
    assert "save" in kinds
    assert ("quit-word",) in log


def test_convert_to_pdf_pptx_uses_powerpoint(monkeypatch, tmp_path):
    main._pdf_conversion_cache.clear()
    monkeypatch.setattr(main, "_pdf_temp_dir", str(tmp_path))
    log = []
    _install_fake_win32(monkeypatch, log)
    src = tmp_path / "deck.pptx"
    src.write_bytes(b"PK")
    pdf = main._convert_to_pdf(src)
    assert pdf is not None
    assert pdf.exists()
    assert ("quit-ppt",) in log


def test_convert_to_pdf_unsupported_extension_returns_none(monkeypatch, tmp_path):
    main._pdf_conversion_cache.clear()
    monkeypatch.setattr(main, "_pdf_temp_dir", str(tmp_path))
    log = []
    _install_fake_win32(monkeypatch, log)
    # Even though _open_pdf_metadata only sends docx/pptx, defense-in-depth:
    # the helper must not crash on a stranger extension.
    src = tmp_path / "weird.odt"
    src.write_bytes(b"...")
    result = main._convert_to_pdf(src)
    assert result is None
    # Still calls CoInitialize and CoUninitialize.
    assert ("co-uninit",) in log


def test_convert_to_pdf_caches_result(monkeypatch, tmp_path):
    main._pdf_conversion_cache.clear()
    monkeypatch.setattr(main, "_pdf_temp_dir", str(tmp_path))
    log = []
    _install_fake_win32(monkeypatch, log)
    src = tmp_path / "twice.docx"
    src.write_bytes(b"PK")
    first = main._convert_to_pdf(src)
    second = main._convert_to_pdf(src)
    assert first == second
    # Only one Word dispatch — second call hit the cache.
    dispatches = [t for t in log if t[0] == "dispatch"]
    assert len(dispatches) == 1


def test_convert_to_pdf_invalidates_cache_when_source_changes(monkeypatch, tmp_path):
    main._pdf_conversion_cache.clear()
    monkeypatch.setattr(main, "_pdf_temp_dir", str(tmp_path))
    log = []
    _install_fake_win32(monkeypatch, log)
    src = tmp_path / "edit.docx"
    src.write_bytes(b"v1")
    main._convert_to_pdf(src)
    time.sleep(0.01)
    src.write_bytes(b"v2 longer content here")
    main._convert_to_pdf(src)
    dispatches = [t for t in log if t[0] == "dispatch"]
    assert len(dispatches) == 2  # source changed → re-convert


def test_convert_to_pdf_recovers_from_stale_cache_pointing_at_missing_file(
    monkeypatch, tmp_path
):
    """Cached PDF path no longer exists on disk → drop the entry and rebuild."""
    main._pdf_conversion_cache.clear()
    monkeypatch.setattr(main, "_pdf_temp_dir", str(tmp_path))
    log = []
    _install_fake_win32(monkeypatch, log)
    src = tmp_path / "stale.docx"
    src.write_bytes(b"PK")
    first = main._convert_to_pdf(src)
    assert first.exists()
    first.unlink()  # simulate temp dir wiped
    second = main._convert_to_pdf(src)
    assert second.exists()


def test_convert_to_pdf_distinct_stems_per_extension(monkeypatch, tmp_path):
    """A report.docx and report.pptx must not collide on the same temp PDF
    path; the extension is mixed into the converted name."""
    main._pdf_conversion_cache.clear()
    monkeypatch.setattr(main, "_pdf_temp_dir", str(tmp_path))
    log = []
    _install_fake_win32(monkeypatch, log)
    docx = tmp_path / "report.docx"; docx.write_bytes(b"D")
    pptx = tmp_path / "report.pptx"; pptx.write_bytes(b"P")
    a = main._convert_to_pdf(docx)
    b = main._convert_to_pdf(pptx)
    assert a != b
    assert a.exists() and b.exists()


# ── _find_file caching + stale entry removal ────────────────────────────


def test_find_file_returns_match(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "TEST_DOCS_DIR", tmp_path)
    main._doc_path_cache.clear()
    p = tmp_path / "found.pdf"
    p.write_bytes(b"%PDF")
    doc_id = main._get_document_id(p.name)
    result = main._find_file(doc_id)
    assert result == p


def test_find_file_cache_hit_skips_scan(monkeypatch, tmp_path):
    main._doc_path_cache.clear()
    monkeypatch.setattr(main, "TEST_DOCS_DIR", tmp_path)
    p = tmp_path / "cached.pdf"
    p.write_bytes(b"%PDF")
    doc_id = main._get_document_id(p.name)
    main._find_file(doc_id)  # populate cache

    # Replace TEST_DOCS_DIR with an empty dir; the cached path is what counts.
    new_dir = tmp_path / "empty"
    new_dir.mkdir()
    # The cache still has the old path pointing into tmp_path; the file is
    # still there, so we should get a hit without scanning new_dir.
    monkeypatch.setattr(main, "TEST_DOCS_DIR", new_dir)
    assert main._find_file(doc_id) == p


def test_find_file_drops_cache_when_path_missing(monkeypatch, tmp_path):
    """If the cached path no longer exists (file deleted), the entry must be
    dropped and the dir rescanned."""
    main._doc_path_cache.clear()
    monkeypatch.setattr(main, "TEST_DOCS_DIR", tmp_path)
    p = tmp_path / "vanish.pdf"
    p.write_bytes(b"%PDF")
    doc_id = main._get_document_id(p.name)
    main._find_file(doc_id)  # populate cache
    p.unlink()
    # Cache holds stale path, file gone, TEST_DOCS_DIR has nothing → None.
    assert main._find_file(doc_id) is None
    assert doc_id not in main._doc_path_cache


def test_find_file_returns_none_when_dir_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "TEST_DOCS_DIR", tmp_path / "no-such")
    main._doc_path_cache.clear()
    assert main._find_file("anything") is None


def test_find_file_ignores_unsupported_extensions(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "TEST_DOCS_DIR", tmp_path)
    main._doc_path_cache.clear()
    p = tmp_path / "x.txt"
    p.write_text("ignored")
    doc_id = main._get_document_id(p.name)
    assert main._find_file(doc_id) is None


# ── prefetch job semantics ──────────────────────────────────────────────


def test_kick_background_prefetch_runs_render_and_extract(monkeypatch, tmp_path):
    """When submitted to a synchronous executor stand-in, the job calls into
    render_page (twice if num_pages>=2) and extract_text."""
    main._prefetch_inflight.clear()
    render_calls = []
    extract_calls = []

    p = tmp_path / "pf.pdf"
    p.write_bytes(b"%PDF")
    doc_id = main._get_document_id(p.name)
    # Pre-populate render cache so the meta lookup in the job has num_pages.
    main._render_cache[doc_id] = {
        "filename": p.name, "num_pages": 2,
        "page_dimensions": {1: {"width": 1, "height": 1}, 2: {"width": 1, "height": 1}},
        "pdf_path": str(p),
        "page_images": {},
        "_sig": main._file_signature(p),
        "_render_lock": threading.Lock(),
    }
    monkeypatch.setattr(main, "_render_page",
                        lambda fp, pn: render_calls.append(pn) or b"")
    monkeypatch.setattr(main, "_get_or_extract_text",
                        lambda fp: extract_calls.append(fp) or {})

    # Synchronous fake executor.
    class _Sync:
        def submit(self, fn, *a, **kw):
            fn(*a, **kw)
            return None

    monkeypatch.setattr(main, "_bg_executor", _Sync())

    main._kick_background_prefetch(doc_id, p)
    assert 1 in render_calls
    assert 2 in render_calls
    assert len(extract_calls) == 1
    # Marker cleared after job finishes.
    assert doc_id not in main._prefetch_inflight


def test_kick_background_prefetch_skips_extraction_when_semaphore_full(
    monkeypatch, tmp_path
):
    """If /extract is at capacity, the prefetch worker must NOT also run
    extraction (would double the effective concurrency vs the cap)."""
    main._prefetch_inflight.clear()
    extract_calls = []
    p = tmp_path / "skip.pdf"
    p.write_bytes(b"%PDF")
    doc_id = main._get_document_id(p.name)
    main._render_cache[doc_id] = {
        "filename": p.name, "num_pages": 1,
        "page_dimensions": {1: {"width": 1, "height": 1}},
        "pdf_path": str(p),
        "page_images": {},
        "_sig": main._file_signature(p),
        "_render_lock": threading.Lock(),
    }
    monkeypatch.setattr(main, "_render_page", lambda fp, pn: b"")
    monkeypatch.setattr(main, "_get_or_extract_text",
                        lambda fp: extract_calls.append(fp))

    class _Sync:
        def submit(self, fn, *a, **kw):
            fn(*a, **kw)
            return None

    monkeypatch.setattr(main, "_bg_executor", _Sync())

    held = []
    while main._extract_semaphore.acquire(blocking=False):
        held.append(True)
    try:
        main._kick_background_prefetch(doc_id, p)
        assert extract_calls == []
    finally:
        for _ in held:
            main._extract_semaphore.release()


def test_kick_background_prefetch_swallows_render_exception(monkeypatch, tmp_path):
    """Render exceptions inside the job must be logged but not crash the job
    (it's best-effort); the inflight marker must still be cleaned up."""
    main._prefetch_inflight.clear()
    p = tmp_path / "boom.pdf"
    p.write_bytes(b"%PDF")
    doc_id = main._get_document_id(p.name)

    def explode(_fp, _pn):
        raise RuntimeError("simulated render crash")

    monkeypatch.setattr(main, "_render_page", explode)
    monkeypatch.setattr(main, "_get_or_extract_text", lambda fp: None)

    class _Sync:
        def submit(self, fn, *a, **kw):
            fn(*a, **kw)
            return None

    monkeypatch.setattr(main, "_bg_executor", _Sync())
    # Must not raise.
    main._kick_background_prefetch(doc_id, p)
    assert doc_id not in main._prefetch_inflight


# ── _build_documents_listing OSError handling ───────────────────────────


def test_build_documents_listing_skips_files_that_stat_fails(monkeypatch, tmp_path):
    """If stat() raises on a file (e.g. permission denied), that file is
    skipped instead of crashing the whole listing."""
    monkeypatch.setattr(main, "TEST_DOCS_DIR", tmp_path)
    good = tmp_path / "good.pdf"; good.write_bytes(b"PDF")
    bad = tmp_path / "bad.pdf"; bad.write_bytes(b"PDF")

    real_stat = Path.stat

    def selective_fail(self, **kwargs):
        if self.name == "bad.pdf":
            raise OSError("simulated")
        return real_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", selective_fail)
    docs = main._build_documents_listing()
    names = [d["filename"] for d in docs]
    assert "good.pdf" in names
    assert "bad.pdf" not in names
    monkeypatch.setattr(Path, "stat", real_stat)


# ── _docs_dir_signature OSError handling ────────────────────────────────


def test_docs_dir_signature_returns_empty_on_iterdir_oserror(monkeypatch, tmp_path):
    """If iterdir raises, return () instead of bubbling."""
    monkeypatch.setattr(main, "TEST_DOCS_DIR", tmp_path)

    def boom(_self):
        raise OSError("no access")

    monkeypatch.setattr(Path, "iterdir", boom)
    assert main._docs_dir_signature() == ()


def test_docs_dir_signature_skips_files_that_stat_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "TEST_DOCS_DIR", tmp_path)
    good = tmp_path / "g.pdf"; good.write_bytes(b"PDF")
    bad = tmp_path / "b.pdf"; bad.write_bytes(b"PDF")

    real_stat = Path.stat

    def selective_fail(self, **kw):
        if self.name == "b.pdf":
            raise OSError("nope")
        return real_stat(self, **kw)

    monkeypatch.setattr(Path, "stat", selective_fail)
    sig = main._docs_dir_signature()
    names = [t[0] for t in sig]
    assert "g.pdf" in names
    assert "b.pdf" not in names
    monkeypatch.setattr(Path, "stat", real_stat)


# ── _definitions_dir_signature OSError handling ─────────────────────────


def test_definitions_dir_signature_skips_files_that_stat_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "DEFINITIONS_DIR", tmp_path)
    good = tmp_path / "good.json"; good.write_text("{}")
    bad = tmp_path / "bad.json"; bad.write_text("{}")

    real_stat = Path.stat

    def selective_fail(self, **kw):
        if self.name == "bad.json":
            raise OSError("nope")
        return real_stat(self, **kw)

    monkeypatch.setattr(Path, "stat", selective_fail)
    sig = main._definitions_dir_signature()
    names = [t[0] for t in sig]
    assert "good.json" in names
    assert "bad.json" not in names
    monkeypatch.setattr(Path, "stat", real_stat)


# ── _atomic_write_json: tmp cleanup ignores OSError ─────────────────────


def test_atomic_write_json_tmp_unlink_failure_still_propagates_original(
    monkeypatch, tmp_path
):
    """When the JSON dump fails AND unlinking the tmp fails, the helper must
    re-raise the original error (TypeError from json.dump), not the OSError."""
    monkeypatch.setattr(main, "DEFINITIONS_DIR", tmp_path)
    target = tmp_path / "out.json"

    real_unlink = __import__("os").unlink

    def fail_unlink(_p):
        raise OSError("temp also locked")

    import os as _os
    monkeypatch.setattr(_os, "unlink", fail_unlink)

    class _NotSerializable:
        pass

    with pytest.raises(TypeError):
        main._atomic_write_json(target, {"bad": _NotSerializable()})
    monkeypatch.setattr(_os, "unlink", real_unlink)
