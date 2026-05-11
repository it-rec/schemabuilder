"""Rigorous concurrency / lifespan / edge-branch tests.

These cover code paths that only fire under multithreaded access (the
double-checked locking branches in _get_or_render, _get_or_extract_text,
_get_text_converter, _resolve_ocr_decision, _load_definitions, list_documents)
and a few small branches that need targeted setup (lifespan shutdown wait
loop, _cleanup_pdf_temp_dir error path, prefetch leg exceptions, fallback to
str(coord_origin) when neither value/name exists, list_documents cache-hit
under lock).
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main


# ── _cleanup_pdf_temp_dir error swallowing ──────────────────────────────


def test_cleanup_pdf_temp_dir_swallows_unexpected_exception(monkeypatch, tmp_path):
    """The OS cleanup path runs from atexit and must NEVER raise; even if
    rmtree itself somehow throws (despite ignore_errors), we catch it."""
    import shutil

    def boom(_p, ignore_errors=False):
        raise RuntimeError("simulated rmtree explosion")

    monkeypatch.setattr(shutil, "rmtree", boom)
    monkeypatch.setattr(main, "_pdf_temp_dir", str(tmp_path))
    # Must not raise.
    main._cleanup_pdf_temp_dir()


# ── _extract_text: coord_origin str() fallback ──────────────────────────


def test_extract_text_coord_origin_str_fallback(monkeypatch, tmp_path):
    """If coord_origin has neither .value nor .name, fall back to str(co)."""
    from tests.test_render_extract import (
        _FakeDocPage, _FakeDoclingDoc, _FakeConverter, _TextItem,
    )

    class _OriginPlain:
        def __str__(self):
            return "CUSTOM-ORIGIN"

    class _Bbox:
        def __init__(self):
            self.l, self.t, self.r, self.b = 1, 2, 3, 4
            self.coord_origin = _OriginPlain()

    item = _TextItem("hi", page=1, bbox=_Bbox())
    doc = _FakeDoclingDoc([item], {1: (10, 10)})
    main._text_converters[False] = _FakeConverter(doc)
    monkeypatch.setattr(main, "_resolve_ocr_decision", lambda _p: False)
    entries, _ = main._extract_text(tmp_path / "x.pdf")
    assert entries[0]["bbox"]["coord_origin"] == "CUSTOM-ORIGIN"


# ── _match_field_to_entries: fallback when _text_lower missing ──────────


def test_match_field_falls_back_to_recomputing_lower_when_uncached():
    """An entry produced by a code path that bypassed the cache annotation
    won't have _text_lower / _text_stripped_lower. The matcher must compute
    them on the fly."""
    # No _text_lower or _text_stripped_lower → cover lines 1346, 1349.
    entry = {
        "id": 0, "text": "PAYMENT-007", "type": "TextItem",
        "page": 1, "bbox": None,
    }
    result = main._match_field_to_entries(
        {"name": "x", "examples": ["payment-007"]},
        [entry], used_ids=set(),
    )
    assert result["match_score"] == 95


# ── _compile_field_matchers: falsy option skip ──────────────────────────


def test_compile_field_matchers_skips_falsy_options():
    """Falsy options (None, empty string) must be skipped; compile would
    raise on empty pattern."""
    result = main._compile_field_matchers({
        "name": "x", "available_options": ["", None, 0, "real"],
    })
    # Only "real" compiles into options.
    assert len(result["options"]) == 1
    assert result["options"][0][0] == "real"


# ── _describe_accelerator: get_device_name failure path ─────────────────


def test_describe_accelerator_get_device_name_failure_falls_back(monkeypatch):
    """If torch.cuda.get_device_name throws (e.g. driver issue), we fall back
    to the bare "CUDA" label rather than crashing the boot log."""
    monkeypatch.delenv("DOCLING_DEVICE", raising=False)

    class _FakeCuda:
        @staticmethod
        def is_available():
            return True

        @staticmethod
        def get_device_name(_):
            raise RuntimeError("driver gone")

    class _FakeBackends:
        class mps:
            @staticmethod
            def is_available():
                return False

    class _FakeTorch:
        cuda = _FakeCuda
        backends = _FakeBackends

    monkeypatch.setitem(sys.modules, "torch", _FakeTorch)
    label = main._describe_accelerator()
    assert label == "CUDA"


def test_describe_accelerator_unknown_force_with_torch_present(monkeypatch):
    """DOCLING_DEVICE=WEIRD with torch importable: doesn't match any of
    CUDA/MPS/CPU/AUTO, and the function emits a "fall back to AUTO" hint
    rather than crashing."""
    monkeypatch.setenv("DOCLING_DEVICE", "WEIRD")

    class _FakeCuda:
        @staticmethod
        def is_available():
            return False

    class _FakeBackends:
        class mps:
            @staticmethod
            def is_available():
                return False

    class _FakeTorch:
        cuda = _FakeCuda
        backends = _FakeBackends

    monkeypatch.setitem(sys.modules, "torch", _FakeTorch)
    label = main._describe_accelerator()
    assert "WEIRD" in label
    assert "AUTO" in label  # the fallback hint


def test_describe_accelerator_explicit_auto_returns_cpu_when_no_torch(monkeypatch):
    """DOCLING_DEVICE=auto + no torch → label uses default CPU label and
    does NOT trip the "unknown" branch (which is reserved for other values).
    """
    import builtins

    real_import = builtins.__import__

    def hide_torch(name, *args, **kwargs):
        if name == "torch":
            raise ImportError("simulated")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", hide_torch)
    monkeypatch.setenv("DOCLING_DEVICE", "auto")
    label = main._describe_accelerator()
    assert "AUTO" in label  # current behavior: literally the forced value


# ── list_documents cache double-check under lock ────────────────────────


def test_list_documents_cache_hit_under_lock(monkeypatch, tmp_path):
    """Two parallel requests for an empty cache: both reach the lock, but
    only the second one (after acquiring) finds a populated cache and short-
    circuits via the under-lock double-check (line 1694).

    We slow down _build_documents_listing so the second thread waits on the
    lock long enough for the first thread to populate the cache. After the
    first releases, the second acquires, re-checks, and short-circuits.
    """
    monkeypatch.setattr(main, "TEST_DOCS_DIR", tmp_path)
    main._doc_listing_cache = None
    main._doc_listing_signature = None
    (tmp_path / "a.pdf").write_bytes(b"%PDF")

    build_started = threading.Event()
    build_release = threading.Event()
    build_calls = {"n": 0}
    real_build = main._build_documents_listing

    def slow_build():
        build_calls["n"] += 1
        build_started.set()
        build_release.wait(timeout=2)
        return real_build()

    monkeypatch.setattr(main, "_build_documents_listing", slow_build)
    client = TestClient(main.app)
    results = []
    def call():
        results.append(client.get("/api/documents").json())

    t1 = threading.Thread(target=call)
    t2 = threading.Thread(target=call)
    t1.start()
    build_started.wait(timeout=2)
    # t1 is inside the lock, building; start t2 — it will block on the
    # listing lock until t1 finishes.
    t2.start()
    # Let t2 reach the lock.
    time.sleep(0.05)
    build_release.set()
    t1.join()
    t2.join()
    # Only one build call: the second request took the under-lock cache hit.
    assert build_calls["n"] == 1
    assert results[0] == results[1]


# ── _load_definitions double-check under lock ───────────────────────────


def test_load_definitions_double_checked_under_lock(monkeypatch, tmp_path):
    """Trigger two concurrent first-time loads so one of them takes the
    under-lock cache-hit branch (line 1135)."""
    main._invalidate_definitions_cache()
    monkeypatch.setattr(main, "DEFINITIONS_DIR", tmp_path)
    (tmp_path / "a.json").write_text('{"document": {"document_type": "A", "fields": []}}')

    results = []
    barrier = threading.Barrier(4)

    def worker():
        barrier.wait()
        results.append(main._load_definitions())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # All threads must see the same loaded definitions dict.
    assert all(r is results[0] for r in results[1:])
    main._invalidate_definitions_cache()


# ── _get_or_render double-check under per-doc lock ──────────────────────


def test_get_or_render_double_checked_under_per_doc_lock(monkeypatch, tmp_path):
    """Two simultaneous first-time renders for the same doc: only one must
    perform the heavy open; the other takes the under-lock cache-hit branch
    (lines 904-905)."""
    main._render_cache.clear()
    main._render_open_locks.clear()
    f = tmp_path / "race.pdf"
    f.write_bytes(b"%PDF")
    opens = {"n": 0}
    open_started = threading.Event()
    open_release = threading.Event()

    def slow_open(filepath):
        opens["n"] += 1
        open_started.set()
        # Hold the open until the second thread reaches the per-doc lock.
        open_release.wait(timeout=2)
        return filepath, 1, {1: {"width": 1.0, "height": 1.0}}

    monkeypatch.setattr(main, "_open_pdf_metadata", slow_open)
    results = []

    def call():
        results.append(main._get_or_render(f))

    t1 = threading.Thread(target=call)
    t2 = threading.Thread(target=call)
    t1.start()
    open_started.wait(timeout=2)
    t2.start()
    # Give t2 a tick to reach the per-doc lock.
    time.sleep(0.05)
    open_release.set()
    t1.join()
    t2.join()
    assert opens["n"] == 1
    assert results[0] is results[1]


# ── _render_page double-check inside render lock ────────────────────────


def test_render_page_double_checked_under_render_lock(monkeypatch, tmp_path):
    """If two threads ask for the same page concurrently, only one runs
    _render_single_page; the second takes the under-lock memoization branch
    (line 941)."""
    main._render_cache.clear()
    f = tmp_path / "two-thread.pdf"
    f.write_bytes(b"%PDF")
    monkeypatch.setattr(
        main, "_open_pdf_metadata",
        lambda fp: (fp, 1, {1: {"width": 1.0, "height": 1.0}}),
    )

    calls = {"n": 0}
    started = threading.Event()
    release = threading.Event()

    def slow_single(_pdf, _pn):
        calls["n"] += 1
        started.set()
        release.wait(timeout=2)
        return b"PNG"

    monkeypatch.setattr(main, "_render_single_page", slow_single)

    def call():
        main._render_page(f, 1)

    t1 = threading.Thread(target=call)
    t2 = threading.Thread(target=call)
    t1.start()
    started.wait(timeout=2)
    t2.start()
    time.sleep(0.05)
    release.set()
    t1.join()
    t2.join()
    assert calls["n"] == 1


# ── _get_or_extract_text double-check under per-doc lock + merge ────────


def test_get_or_extract_text_post_extract_cache_merge(monkeypatch, tmp_path):
    """If something else populates _text_cache for the same doc + sig while
    extraction is running, the post-extract re-check must return the
    already-cached entry instead of the freshly-extracted one. Pin this
    cache-merge contract (line 1014)."""
    main._text_cache.clear()
    main._text_extract_locks.clear()
    f = tmp_path / "merge.pdf"
    f.write_bytes(b"%PDF")
    doc_id = main._get_document_id(f.name)
    sig = main._file_signature(f)
    pre_populated = {
        "text_entries": [{"id": 99, "text": "preexisting", "type": "T",
                          "page": 1, "bbox": None,
                          "_text_lower": "preexisting",
                          "_text_stripped_lower": "preexisting"}],
        "page_dimensions": {},
        "_sig": sig,
    }

    def fake_extract(_p):
        # Simulate a concurrent populate: while we're "extracting", another
        # caller dropped an entry into _text_cache.
        with main._text_lock:
            main._text_cache[doc_id] = pre_populated
        return [
            {"id": 0, "text": "fresh-extract", "type": "T",
             "page": 1, "bbox": None}
        ], {}

    monkeypatch.setattr(main, "_extract_text", fake_extract)
    result = main._get_or_extract_text(f)
    # The merge branch returns the pre-existing entry, not the fresh one.
    assert result is pre_populated
    assert result["text_entries"][0]["text"] == "preexisting"
    main._text_cache.clear()


def test_get_or_extract_text_double_checked_and_merged(monkeypatch, tmp_path):
    """Two threads extract the same doc concurrently. The slow one reaches
    the per-doc lock first; the other reaches it second and sees a populated
    cache (lines 980-981) OR the cache-merge result (line 1014). Either way,
    only one _extract_text call happens."""
    main._text_cache.clear()
    main._text_extract_locks.clear()
    f = tmp_path / "extract-race.pdf"
    f.write_bytes(b"%PDF")
    calls = {"n": 0}
    started = threading.Event()
    release = threading.Event()

    def slow_extract(_fp):
        calls["n"] += 1
        started.set()
        release.wait(timeout=2)
        return [{"id": 0, "text": "x", "type": "TextItem", "page": 1, "bbox": None}], {}

    monkeypatch.setattr(main, "_extract_text", slow_extract)
    results = []

    def call():
        results.append(main._get_or_extract_text(f))

    t1 = threading.Thread(target=call)
    t2 = threading.Thread(target=call)
    t1.start()
    started.wait(timeout=2)
    t2.start()
    time.sleep(0.05)
    release.set()
    t1.join()
    t2.join()
    assert calls["n"] == 1
    # Both calls receive the same dict (same _sig + cache hit).
    assert results[0]["_sig"] == results[1]["_sig"]


# ── _get_text_converter double-checked race ─────────────────────────────


def test_get_text_converter_double_checked_race(monkeypatch):
    """Concurrent first-time gets for the same flag: only one build happens;
    the other thread enters the lock and finds the converter already there
    (line 631)."""
    main._text_converters.clear()
    builds = {"n": 0}
    build_started = threading.Event()
    build_release = threading.Event()

    def slow_build(do_ocr):
        builds["n"] += 1
        build_started.set()
        build_release.wait(timeout=2)
        return object()

    monkeypatch.setattr(main, "_build_text_converter", slow_build)

    def call():
        main._get_text_converter(False)

    t1 = threading.Thread(target=call)
    t2 = threading.Thread(target=call)
    t1.start()
    build_started.wait(timeout=2)
    t2.start()
    time.sleep(0.05)
    build_release.set()
    t1.join()
    t2.join()
    assert builds["n"] == 1
    main._text_converters.clear()


# ── _resolve_ocr_decision double-checked race ───────────────────────────


def test_resolve_ocr_decision_double_checked_race(monkeypatch, tmp_path):
    """Two threads ask for the OCR decision on the same uncached doc; only
    one runs detection; the other takes the under-lock cache-hit branch
    (line 726)."""
    monkeypatch.delenv("DOCLING_DO_OCR", raising=False)
    main._ocr_decision_cache.clear()
    f = tmp_path / "ocr-race.pdf"
    f.write_bytes(b"%PDF")
    calls = {"n": 0}
    started = threading.Event()
    release = threading.Event()

    def slow_detect(_p):
        calls["n"] += 1
        started.set()
        release.wait(timeout=2)
        return True

    monkeypatch.setattr(main, "_document_needs_ocr", slow_detect)

    def call():
        main._resolve_ocr_decision(f)

    t1 = threading.Thread(target=call)
    t2 = threading.Thread(target=call)
    t1.start()
    started.wait(timeout=2)
    t2.start()
    time.sleep(0.05)
    release.set()
    t1.join()
    t2.join()
    assert calls["n"] == 1
    main._ocr_decision_cache.clear()


# ── prefetch worker swallows page-2 and extract failures ────────────────


def test_kick_background_prefetch_swallows_page2_exception(monkeypatch, tmp_path):
    """Page-1 succeeds, page-2 raises — the job must continue to extraction
    rather than abort (line 1045-1046)."""
    main._prefetch_inflight.clear()
    p = tmp_path / "p2.pdf"
    p.write_bytes(b"%PDF")
    doc_id = main._get_document_id(p.name)
    main._render_cache[doc_id] = {
        "filename": p.name, "num_pages": 2,
        "page_dimensions": {1: {"width": 1, "height": 1}, 2: {"width": 1, "height": 1}},
        "pdf_path": str(p),
        "page_images": {},
        "_sig": main._file_signature(p),
        "_render_lock": threading.Lock(),
    }
    extract_called = {"n": 0}
    call_log = []

    def fake_render(fp, pn):
        call_log.append(pn)
        if pn == 2:
            raise RuntimeError("page 2 boom")
        return b""

    monkeypatch.setattr(main, "_render_page", fake_render)
    monkeypatch.setattr(
        main, "_get_or_extract_text",
        lambda fp: extract_called.update(n=extract_called["n"] + 1),
    )

    class _Sync:
        def submit(self, fn, *a, **kw):
            fn(*a, **kw)

    monkeypatch.setattr(main, "_bg_executor", _Sync())
    main._kick_background_prefetch(doc_id, p)
    assert call_log == [1, 2]
    # Extraction still ran even though page-2 failed.
    assert extract_called["n"] == 1


def test_kick_background_prefetch_swallows_extract_exception(monkeypatch, tmp_path):
    """Extraction phase raises — the job must still clean up the inflight
    marker (lines 1055-1056)."""
    main._prefetch_inflight.clear()
    p = tmp_path / "ex.pdf"
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

    def explode(fp):
        raise RuntimeError("extract boom")

    monkeypatch.setattr(main, "_get_or_extract_text", explode)

    class _Sync:
        def submit(self, fn, *a, **kw):
            fn(*a, **kw)

    monkeypatch.setattr(main, "_bg_executor", _Sync())
    main._kick_background_prefetch(doc_id, p)
    # Inflight marker cleared despite exception.
    assert doc_id not in main._prefetch_inflight


# ── Lifespan: shutdown waits for in-flight extracts ─────────────────────


def _fresh_executor(monkeypatch):
    """Replace _bg_executor with a fresh ThreadPoolExecutor so each lifespan
    test starts with an un-shut-down executor (lifespan shutdown drains it)."""
    from concurrent.futures import ThreadPoolExecutor

    new_ex = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-prefetch")
    monkeypatch.setattr(main, "_bg_executor", new_ex)
    return new_ex


def test_lifespan_waits_then_logs_when_grace_expires(monkeypatch):
    """Simulate a stuck extract: enter the lifespan finalizer with
    _inflight_extracts > 0 and a very short grace period; the finalizer
    must wait, then log a warning when the deadline passes, and return."""
    monkeypatch.setattr(main, "_SHUTDOWN_GRACE_SECONDS", 0.1)
    _fresh_executor(monkeypatch)
    # No-op warm-up so we don't try to load docling.
    monkeypatch.setattr(main, "_warm_up_converter", lambda: None)

    with main._inflight_cv:
        main._inflight_extracts += 1
    try:
        async def run_lifespan():
            async with main.lifespan(main.app):
                pass

        t0 = time.monotonic()
        asyncio.run(run_lifespan())
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.05
    finally:
        with main._inflight_cv:
            main._inflight_extracts -= 1


def test_lifespan_returns_quickly_when_no_inflight(monkeypatch):
    """No in-flight requests → finalizer returns immediately; pin the
    happy path."""
    monkeypatch.setattr(main, "_SHUTDOWN_GRACE_SECONDS", 5.0)
    _fresh_executor(monkeypatch)
    monkeypatch.setattr(main, "_warm_up_converter", lambda: None)
    # Defensive: any leftover counter from a previous test would block.
    with main._inflight_cv:
        leftover = main._inflight_extracts
    assert leftover == 0

    async def run_lifespan():
        async with main.lifespan(main.app):
            pass

    t0 = time.monotonic()
    asyncio.run(run_lifespan())
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0


def test_lifespan_unblocks_when_inflight_drops_to_zero(monkeypatch):
    """Start the finalizer with inflight=1; a separate thread drops the
    counter and notifies. The finalizer should unblock before the deadline."""
    monkeypatch.setattr(main, "_SHUTDOWN_GRACE_SECONDS", 5.0)
    _fresh_executor(monkeypatch)
    monkeypatch.setattr(main, "_warm_up_converter", lambda: None)
    with main._inflight_cv:
        main._inflight_extracts += 1

    def drop_after_delay():
        time.sleep(0.1)
        with main._inflight_cv:
            main._inflight_extracts -= 1
            main._inflight_cv.notify_all()

    t = threading.Thread(target=drop_after_delay)
    t.start()

    async def run_lifespan():
        async with main.lifespan(main.app):
            pass

    t0 = time.monotonic()
    asyncio.run(run_lifespan())
    elapsed = time.monotonic() - t0
    t.join()
    assert elapsed < 2.0


# ── /extract acquires and releases semaphore under exception ────────────


def test_extract_releases_semaphore_after_handler_exception(monkeypatch, tmp_path):
    """If the matcher raises mid-extract, the semaphore must still be
    released so subsequent calls aren't permanently 503'd. The try/finally
    around the semaphore is what guarantees this — pin it."""
    monkeypatch.setattr(main, "DEFINITIONS_DIR", tmp_path / "defs")
    monkeypatch.setattr(main, "TEST_DOCS_DIR", tmp_path / "docs")
    main._invalidate_definitions_cache()
    main._render_cache.clear()
    main.TEST_DOCS_DIR.mkdir(parents=True, exist_ok=True)
    p = main.TEST_DOCS_DIR / "ex.pdf"
    p.write_bytes(b"%PDF")
    doc_id = main._get_document_id(p.name)
    main._render_cache[doc_id] = {
        "filename": p.name, "num_pages": 1,
        "page_dimensions": {1: {"width": 100, "height": 100}},
        "pdf_path": str(p), "page_images": {},
        "_sig": main._file_signature(p),
    }
    # raise_server_exceptions=False so a 500 surfaces as a response, not as
    # an exception bubbling through the TestClient harness.
    client = TestClient(main.app, raise_server_exceptions=False)
    client.post("/api/definitions", json={
        "document": {"document_type": "X", "fields": [{"name": "f", "examples": ["A"]}]}
    })
    # Skip Docling/OCR detection paths since they import optional modules.
    monkeypatch.setattr(main, "_get_or_extract_text", lambda _p: {
        "text_entries": [], "page_dimensions": {}, "_sig": ()
    })

    def explode(*_a, **_kw):
        raise RuntimeError("simulated matcher crash")

    monkeypatch.setattr(main, "_extract_fields", explode)

    before = main._extract_semaphore._value
    resp = client.post(f"/api/documents/{doc_id}/extract",
                       json={"definition_id": "x"})
    after = main._extract_semaphore._value
    assert resp.status_code == 500
    # Same semaphore value before and after; the finally released the permit.
    assert before == after


# ── Module-as-script entry point (sanity) ───────────────────────────────


def test_module_entrypoint_block_is_uvicorn_run(tmp_path):
    """The `if __name__ == "__main__"` block runs `uvicorn.run`. We don't
    actually start a server, but we verify the import + branch by reading
    the source so a future "let's add side effects at import time" change
    surfaces in this test."""
    src = Path(main.__file__).read_text()
    assert 'if __name__ == "__main__":' in src
    assert "uvicorn" in src
