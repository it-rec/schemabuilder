"""Tests for document upload + delete endpoints.

The upload path is bounded by a separate body-size cap (SCHEMABUILDER_MAX_DOC_BYTES,
default 50 MB) — the JSON-payload cap is far too small for PDFs. Tests verify:
- happy-path POST persists to TEST_DOCS_DIR
- filename sanitization (no directory traversal)
- collision handling (suffix appended, no overwrite)
- extension allow-list
- size cap (enforced via Content-Length + the streaming inner check)
- DELETE removes the file and purges caches
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(main, "DEFINITIONS_DIR", tmp_path / "definitions")
    monkeypatch.setattr(main, "TEST_DOCS_DIR", tmp_path / "docs")
    main._invalidate_definitions_cache()
    main._render_cache.clear()
    main._text_cache.clear()
    main._doc_path_cache.clear()
    main._ocr_decision_cache.clear()
    main._invalidate_doc_listing_cache()
    return TestClient(main.app)


# Minimal-but-valid PDF body. Docling never parses it in these tests (the
# upload endpoint just writes bytes); pypdfium2 would fail on this, but the
# upload happy-path doesn't invoke it.
_PDF_BYTES = b"%PDF-1.4\n%%EOF\n"


def test_upload_persists_pdf_to_docs_dir(client):
    resp = client.post(
        "/api/documents",
        files={"file": ("invoice.pdf", BytesIO(_PDF_BYTES), "application/pdf")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["filename"] == "invoice.pdf"
    assert body["extension"] == ".pdf"
    assert body["size"] == len(_PDF_BYTES)
    # Persisted to disk under TEST_DOCS_DIR.
    on_disk = main.TEST_DOCS_DIR / "invoice.pdf"
    assert on_disk.exists()
    assert on_disk.read_bytes() == _PDF_BYTES


def test_upload_appears_in_listing(client):
    client.post(
        "/api/documents",
        files={"file": ("invoice.pdf", BytesIO(_PDF_BYTES), "application/pdf")},
    )
    listing = client.get("/api/documents").json()
    assert any(d["filename"] == "invoice.pdf" for d in listing["items"])


def test_upload_rejects_unsupported_extension(client):
    resp = client.post(
        "/api/documents",
        files={"file": ("evil.exe", BytesIO(b"MZ"), "application/x-msdownload")},
    )
    assert resp.status_code == 415
    detail = resp.json()["detail"]
    assert ".exe" in detail
    assert not (main.TEST_DOCS_DIR / "evil.exe").exists()


def test_upload_sanitizes_traversal_attempt(client):
    """A filename with directory components must not write outside the docs
    dir. The sanitizer keeps just the basename and replaces unsafe chars."""
    resp = client.post(
        "/api/documents",
        files={
            "file": (
                "../../etc/passwd.pdf",
                BytesIO(_PDF_BYTES),
                "application/pdf",
            )
        },
    )
    assert resp.status_code == 200
    filename = resp.json()["filename"]
    # No path components survive.
    assert "/" not in filename and "\\" not in filename and ".." not in filename
    assert (main.TEST_DOCS_DIR / filename).exists()


def test_upload_collision_appends_suffix(client):
    """Re-uploading the same filename gets `-1`, `-2`… rather than
    overwriting. Each upload is preserved so users can't accidentally lose
    an in-flight investigation."""
    files_a = {"file": ("a.pdf", BytesIO(_PDF_BYTES), "application/pdf")}
    files_b = {"file": ("a.pdf", BytesIO(_PDF_BYTES + b"v2"), "application/pdf")}
    first = client.post("/api/documents", files=files_a).json()
    second = client.post("/api/documents", files=files_b).json()
    assert first["filename"] == "a.pdf"
    assert second["filename"] == "a-1.pdf"
    # Both files exist on disk with their respective contents.
    assert (main.TEST_DOCS_DIR / "a.pdf").read_bytes() == _PDF_BYTES
    assert (main.TEST_DOCS_DIR / "a-1.pdf").read_bytes().endswith(b"v2")


def test_upload_size_cap_rejects_oversized(client, monkeypatch):
    """The streaming write enforces _MAX_DOC_BYTES even when Content-Length
    is correct. We shrink the cap to test cheaply rather than constructing
    a 50 MB body."""
    monkeypatch.setattr(main, "_MAX_DOC_BYTES", 8)  # tiny on purpose
    body = b"x" * 50  # easily exceeds 8 bytes
    resp = client.post(
        "/api/documents",
        files={"file": ("big.pdf", BytesIO(body), "application/pdf")},
    )
    assert resp.status_code == 413
    # No partial file landed in the docs dir.
    assert not any(main.TEST_DOCS_DIR.glob("big*"))


def test_upload_kicks_background_prefetch(client, monkeypatch):
    """The upload handler warms render + text caches in the background so the
    user's first click on the new document doesn't wait for Docling + render.
    Verifies the prefetch is invoked with the correct doc_id + path."""
    calls = []

    def fake_kick(doc_id: str, filepath: Path) -> None:
        calls.append((doc_id, filepath))

    monkeypatch.setattr(main, "_kick_background_prefetch", fake_kick)
    resp = client.post(
        "/api/documents",
        files={"file": ("warm.pdf", BytesIO(_PDF_BYTES), "application/pdf")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(calls) == 1
    kicked_doc_id, kicked_path = calls[0]
    assert kicked_doc_id == body["id"]
    assert kicked_path == main.TEST_DOCS_DIR / "warm.pdf"


def test_upload_accepts_docx_and_pptx_extensions(client):
    for fname in ("doc.docx", "deck.pptx"):
        resp = client.post(
            "/api/documents",
            files={"file": (fname, BytesIO(b"binary"), "application/octet-stream")},
        )
        assert resp.status_code == 200, resp.text


def test_delete_removes_file_and_404s_thereafter(client):
    posted = client.post(
        "/api/documents",
        files={"file": ("kill.pdf", BytesIO(_PDF_BYTES), "application/pdf")},
    ).json()
    doc_id = posted["id"]
    assert (main.TEST_DOCS_DIR / "kill.pdf").exists()

    resp = client.delete(f"/api/documents/{doc_id}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    assert not (main.TEST_DOCS_DIR / "kill.pdf").exists()

    # The next metadata GET sees the deletion immediately (listing cache
    # was invalidated).
    assert client.get(f"/api/documents/{doc_id}").status_code == 404


def test_delete_unknown_doc_returns_404(client):
    assert client.delete("/api/documents/nope").status_code == 404


def test_upload_with_tiny_chunk_size_still_persists_full_body(client, monkeypatch):
    """The read-loop must work for any chunk size; shrink the chunk to 4 bytes
    and post a body that needs multiple loop iterations to land. Catches
    regressions where the loop is sized to read everything in one shot."""
    monkeypatch.setattr(main, "_UPLOAD_CHUNK_BYTES", 4)
    body = _PDF_BYTES + b"-extra-bytes-to-force-many-iterations"
    resp = client.post(
        "/api/documents",
        files={"file": ("chunky.pdf", BytesIO(body), "application/pdf")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["size"] == len(body)
    on_disk = main.TEST_DOCS_DIR / "chunky.pdf"
    assert on_disk.exists()
    assert on_disk.read_bytes() == body


def test_upload_invokes_posix_fadvise_after_prefetch(monkeypatch, tmp_path):
    """After the prefetch job warms the in-memory caches, the kernel page-
    cache hint must fire so the file's bytes can be reclaimed. On platforms
    without posix_fadvise the path skips silently — verified by deleting the
    attribute and confirming no AttributeError."""
    import threading as _threading

    monkeypatch.setattr(main, "TEST_DOCS_DIR", tmp_path / "docs")
    main.TEST_DOCS_DIR.mkdir(parents=True, exist_ok=True)
    main._prefetch_inflight.clear()
    p = main.TEST_DOCS_DIR / "fadv.pdf"
    p.write_bytes(b"%PDF-1.4\n%%EOF\n")
    doc_id = main._get_document_id(p.name)
    main._render_cache[doc_id] = {
        "filename": p.name,
        "num_pages": 1,
        "page_dimensions": {1: {"width": 1, "height": 1}},
        "pdf_path": str(p),
        "page_images": {},
        "_sig": main._file_signature(p),
        "_render_lock": _threading.Lock(),
    }
    monkeypatch.setattr(main, "_render_page", lambda fp, pn: b"")
    monkeypatch.setattr(main, "_get_or_extract_text", lambda fp: {})

    class _Sync:
        def submit(self, fn, *a, **kw):
            fn(*a, **kw)
            return None

    monkeypatch.setattr(main, "_bg_executor", _Sync())

    fadvise_calls = []

    def _fake_fadvise(fd, offset, length, advice):
        fadvise_calls.append((offset, length, advice))

    monkeypatch.setattr(main.os, "posix_fadvise", _fake_fadvise, raising=False)
    main.os.POSIX_FADV_DONTNEED = getattr(main.os, "POSIX_FADV_DONTNEED", 4)

    main._kick_background_prefetch(doc_id, p)
    assert len(fadvise_calls) == 1
    assert fadvise_calls[0] == (0, 0, main.os.POSIX_FADV_DONTNEED)
    assert doc_id not in main._prefetch_inflight


def test_upload_prefetch_runs_cleanly_without_posix_fadvise(monkeypatch, tmp_path):
    """macOS / Windows lack posix_fadvise; the prefetch job must still
    complete and clear its inflight marker."""
    import threading as _threading

    monkeypatch.setattr(main, "TEST_DOCS_DIR", tmp_path / "docs")
    main.TEST_DOCS_DIR.mkdir(parents=True, exist_ok=True)
    main._prefetch_inflight.clear()
    p = main.TEST_DOCS_DIR / "nofadv.pdf"
    p.write_bytes(b"%PDF-1.4\n%%EOF\n")
    doc_id = main._get_document_id(p.name)
    main._render_cache[doc_id] = {
        "filename": p.name,
        "num_pages": 1,
        "page_dimensions": {1: {"width": 1, "height": 1}},
        "pdf_path": str(p),
        "page_images": {},
        "_sig": main._file_signature(p),
        "_render_lock": _threading.Lock(),
    }
    monkeypatch.setattr(main, "_render_page", lambda fp, pn: b"")
    monkeypatch.setattr(main, "_get_or_extract_text", lambda fp: {})

    class _Sync:
        def submit(self, fn, *a, **kw):
            fn(*a, **kw)
            return None

    monkeypatch.setattr(main, "_bg_executor", _Sync())

    # Simulate a platform without posix_fadvise.
    if hasattr(main.os, "posix_fadvise"):
        monkeypatch.delattr(main.os, "posix_fadvise")

    main._kick_background_prefetch(doc_id, p)
    assert doc_id not in main._prefetch_inflight


def test_delete_purges_render_and_text_caches(client, monkeypatch):
    """A re-upload under the same filename produces the same doc_id; the
    previous extraction's text/render entries must not bleed through."""
    posted = client.post(
        "/api/documents",
        files={"file": ("rev.pdf", BytesIO(_PDF_BYTES), "application/pdf")},
    ).json()
    doc_id = posted["id"]

    main._render_cache[doc_id] = {"sentinel": True}
    main._text_cache[doc_id] = {"sentinel": True}

    client.delete(f"/api/documents/{doc_id}")
    assert doc_id not in main._render_cache
    assert doc_id not in main._text_cache
