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
