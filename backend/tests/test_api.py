"""HTTP-level tests using FastAPI's TestClient.

Docling text extraction is mocked so the suite doesn't load ML models. The
tests focus on routing, request validation (Pydantic 422s), and the
CRUD lifecycle for definitions.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    """Isolated app instance: definitions dir is a tmp dir, caches start empty.

    Each test gets a fresh tmp_path so /api/definitions starts empty and
    POSTs never collide with leftover state from a previous test. We also
    null out module-level metrics counters and the OCR-decision cache so
    cross-test ordering doesn't affect assertions.
    """
    monkeypatch.setattr(main, "DEFINITIONS_DIR", tmp_path / "definitions")
    monkeypatch.setattr(main, "TEST_DOCS_DIR", tmp_path / "docs")
    main._invalidate_definitions_cache()
    main._render_cache.clear()
    main._text_cache.clear()
    main._doc_path_cache.clear()
    main._ocr_decision_cache.clear()
    with main._metrics_lock:
        for key in list(main._metrics.keys()):
            main._metrics[key] = 0
    return TestClient(main.app)


def _valid_definition() -> dict:
    return {
        "document": {
            "document_type": "Test Type",
            "document_description": "for tests",
            "fields": [
                {"name": "invoice_id", "examples": ["INV-001"]},
                {
                    "name": "line_items",
                    "type": "array",
                    "fields": [
                        {"name": "amount", "examples": ["500.00"]},
                    ],
                },
            ],
        }
    }


# ── /api/definitions ─────────────────────────────────────────────────────


def test_create_definition_persists_and_lists(client, tmp_path: Path):
    resp = client.post("/api/definitions", json=_valid_definition())
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "test_type"
    assert body["field_count"] == 2

    listing = client.get("/api/definitions").json()
    assert listing["total"] == 1
    assert any(d["id"] == "test_type" for d in listing["items"])


def test_create_definition_rejects_non_object_body(client):
    resp = client.post("/api/definitions", json=["not", "a", "dict"])
    assert resp.status_code == 422


def test_create_definition_rejects_malformed_fields(client):
    bad = {
        "document": {
            "document_type": "Bad",
            "fields": [{"description": "missing name"}],  # name is required
        }
    }
    resp = client.post("/api/definitions", json=bad)
    assert resp.status_code == 422


def test_create_definition_rejects_empty_document_type(client):
    bad = {"document": {"document_type": "", "fields": []}}
    resp = client.post("/api/definitions", json=bad)
    assert resp.status_code == 422


def test_create_definition_rejects_punctuation_only_document_type(client):
    bad = {"document": {"document_type": "???", "fields": []}}
    resp = client.post("/api/definitions", json=bad)
    # passes Pydantic (non-empty string) but slug ends up empty → 400 from us
    assert resp.status_code == 400


def test_get_definition_returns_404_for_missing(client):
    assert client.get("/api/definitions/nope").status_code == 404


def test_delete_definition_removes_file(client):
    client.post("/api/definitions", json=_valid_definition())
    assert client.get("/api/definitions/test_type").status_code == 200

    resp = client.delete("/api/definitions/test_type")
    assert resp.status_code == 200
    assert resp.json() == {"id": "test_type", "deleted": True}

    assert client.get("/api/definitions/test_type").status_code == 404


def test_delete_definition_returns_404_for_missing(client):
    assert client.delete("/api/definitions/nope").status_code == 404


def test_delete_definition_rejects_path_traversal(client):
    # Slashes / dots shouldn't ever reach the unlink path even if the
    # definition file happens to exist somewhere.
    assert client.delete("/api/definitions/..%2Fmain").status_code == 404


# ── /api/documents/{id}/extract ─────────────────────────────────────────


def test_extract_validates_definition_id(client):
    resp = client.post("/api/documents/anything/extract", json={})
    assert resp.status_code == 422  # missing definition_id


def test_extract_rejects_empty_definition_id(client):
    resp = client.post(
        "/api/documents/anything/extract",
        json={"definition_id": ""},
    )
    assert resp.status_code == 422


def test_extract_returns_404_for_unknown_document(client):
    # Skip the upstream Pydantic check by sending a real definition_id but no
    # matching doc on disk → _find_file returns None → 404.
    client.post("/api/definitions", json=_valid_definition())
    resp = client.post(
        "/api/documents/missing-doc-id/extract",
        json={"definition_id": "test_type"},
    )
    assert resp.status_code == 404


def test_extract_runs_matcher_with_mocked_docling(client, tmp_path, monkeypatch):
    """End-to-end happy path: place a fake doc on disk, mock Docling output,
    confirm the matcher's results come back through the API."""
    # 1. Put a "document" on disk under the test docs dir so _find_file
    #    resolves it. We don't render or open it; we just need the path.
    docs_dir = main.TEST_DOCS_DIR
    docs_dir.mkdir(parents=True, exist_ok=True)
    doc_path = docs_dir / "fake.pdf"
    doc_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    doc_id = main._get_document_id(doc_path.name)

    # 2. Bypass page rasterization (_get_or_render reads the PDF) by
    #    pre-populating the render cache.
    main._render_cache[doc_id] = {
        "filename": doc_path.name,
        "num_pages": 1,
        "page_dimensions": {1: {"width": 100, "height": 100}},
        "pdf_path": str(doc_path),
        "page_images": {},
        "_sig": main._file_signature(doc_path),
    }

    # 3. Mock Docling — _extract_text would otherwise load ML models.
    def fake_extract_text(_filepath):
        entries = [
            {"id": 0, "text": "INV-001", "type": "TextItem", "page": 1, "bbox": None},
            {"id": 1, "text": "boilerplate paragraph", "type": "TextItem", "page": 1, "bbox": None},
        ]
        return entries, {1: {"width": 100, "height": 100}}

    monkeypatch.setattr(main, "_extract_text", fake_extract_text)

    # 4. Upload definition and call extract.
    client.post("/api/definitions", json=_valid_definition())
    resp = client.post(
        f"/api/documents/{doc_id}/extract",
        json={"definition_id": "test_type"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["document_type"] == "Test Type"
    # invoice_id field should have matched the "INV-001" entry.
    inv_field = next(f for f in body["fields"] if f["name"] == "invoice_id")
    assert inv_field["extracted_value"] == "INV-001"
    assert inv_field["confidence"] >= 0.9
    # Match observability: a successful match must record why it scored.
    assert inv_field["match_reason"] == "example_exact"
    assert inv_field["match_score"] >= 90
    # No extraction error on the happy path.
    assert "extraction_error" not in body


# ── pagination ───────────────────────────────────────────────────────────


def test_list_documents_paginates(client, monkeypatch, tmp_path: Path):
    docs_dir = main.TEST_DOCS_DIR
    docs_dir.mkdir(parents=True, exist_ok=True)
    for i in range(5):
        (docs_dir / f"doc{i}.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    body = client.get("/api/documents?limit=2&offset=1").json()
    assert body["total"] == 5
    assert body["limit"] == 2
    assert body["offset"] == 1
    assert len(body["items"]) == 2


def test_list_documents_rejects_bad_pagination(client):
    assert client.get("/api/documents?limit=0").status_code == 422
    assert client.get("/api/documents?limit=1000").status_code == 422
    assert client.get("/api/documents?offset=-1").status_code == 422


def test_list_definitions_paginates(client):
    # Seed three definitions with distinct slugs.
    for name in ("Alpha", "Bravo", "Charlie"):
        body = _valid_definition()
        body["document"]["document_type"] = name
        client.post("/api/definitions", json=body)
    listing = client.get("/api/definitions?limit=2&offset=0").json()
    assert listing["total"] == 3
    assert len(listing["items"]) == 2


# ── POST conflict + overwrite + PATCH ────────────────────────────────────


def test_create_definition_returns_409_on_duplicate(client):
    assert client.post("/api/definitions", json=_valid_definition()).status_code == 200
    resp = client.post("/api/definitions", json=_valid_definition())
    assert resp.status_code == 409
    assert "already exists" in resp.json()["detail"].lower()


def test_create_definition_overwrites_with_query_flag(client):
    client.post("/api/definitions", json=_valid_definition())
    updated = _valid_definition()
    updated["document"]["document_description"] = "rewritten"
    resp = client.post("/api/definitions?overwrite=true", json=updated)
    assert resp.status_code == 200
    fetched = client.get("/api/definitions/test_type").json()
    assert fetched["document"]["document_description"] == "rewritten"


def test_patch_definition_updates_existing(client):
    client.post("/api/definitions", json=_valid_definition())
    updated = _valid_definition()
    updated["document"]["document_description"] = "patched"
    resp = client.patch("/api/definitions/test_type", json=updated)
    assert resp.status_code == 200
    fetched = client.get("/api/definitions/test_type").json()
    assert fetched["document"]["document_description"] == "patched"


def test_patch_definition_404_for_missing(client):
    resp = client.patch("/api/definitions/missing", json=_valid_definition())
    assert resp.status_code == 404


def test_patch_definition_rejects_bad_slug(client):
    # Slashes / non-slug characters must 404 before unlink/open is attempted.
    resp = client.patch("/api/definitions/..%2Fmain", json=_valid_definition())
    assert resp.status_code == 404


# ── /health and /metrics ────────────────────────────────────────────────


def test_health_endpoint(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "inflight_extracts" in body


def test_metrics_endpoint_shape(client):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert "counters" in body
    assert "caches" in body
    assert "render" in body["caches"]
    assert "text" in body["caches"]


# ── Request ID middleware ────────────────────────────────────────────────


def test_request_id_echoed_when_supplied(client):
    resp = client.get("/health", headers={"X-Request-ID": "abc123"})
    assert resp.headers.get("X-Request-ID") == "abc123"


def test_request_id_generated_when_missing(client):
    resp = client.get("/health")
    rid = resp.headers.get("X-Request-ID")
    assert rid and len(rid) >= 8


# ── page_no validation ──────────────────────────────────────────────────


def test_page_no_rejects_zero_and_negative(client):
    # Need a doc on disk to even reach the page-number validation? No — FastAPI
    # validates path params before the handler runs, so 422 fires immediately.
    assert client.get("/api/documents/anyid/pages/0").status_code == 422
    assert client.get("/api/documents/anyid/pages/-1").status_code == 422


def test_page_no_rejects_out_of_range(client, monkeypatch):
    # Doc exists, but page_no exceeds num_pages — 400, not 404.
    docs_dir = main.TEST_DOCS_DIR
    docs_dir.mkdir(parents=True, exist_ok=True)
    doc_path = docs_dir / "tiny.pdf"
    doc_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    doc_id = main._get_document_id(doc_path.name)
    main._render_cache[doc_id] = {
        "filename": doc_path.name,
        "num_pages": 1,
        "page_dimensions": {1: {"width": 100, "height": 100}},
        "pdf_path": str(doc_path),
        "page_images": {},
        "_sig": main._file_signature(doc_path),
    }
    resp = client.get(f"/api/documents/{doc_id}/pages/50")
    assert resp.status_code == 400
    assert "out of range" in resp.json()["detail"].lower()


# ── extraction error surfacing ──────────────────────────────────────────


def test_extraction_error_surfaces_in_response(client, monkeypatch):
    docs_dir = main.TEST_DOCS_DIR
    docs_dir.mkdir(parents=True, exist_ok=True)
    doc_path = docs_dir / "bad.pdf"
    doc_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    doc_id = main._get_document_id(doc_path.name)
    main._render_cache[doc_id] = {
        "filename": doc_path.name,
        "num_pages": 1,
        "page_dimensions": {1: {"width": 100, "height": 100}},
        "pdf_path": str(doc_path),
        "page_images": {},
        "_sig": main._file_signature(doc_path),
    }

    def boom(_filepath):
        raise RuntimeError("docling exploded")

    monkeypatch.setattr(main, "_extract_text", boom)
    client.post("/api/definitions", json=_valid_definition())
    resp = client.post(
        f"/api/documents/{doc_id}/extract",
        json={"definition_id": "test_type"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "extraction_error" in body
    assert "docling exploded" in body["extraction_error"]
    # Fields list is still present so the UI can render its empty state.
    assert isinstance(body["fields"], list)


# ── /ready endpoint ─────────────────────────────────────────────────────


def test_ready_returns_503_until_warmup_done(client):
    main._warmup_done.clear()
    try:
        resp = client.get("/ready")
        assert resp.status_code == 503
        assert resp.headers.get("Retry-After") == "5"
        assert resp.json() == {"ready": False}
    finally:
        main._warmup_done.set()


def test_ready_returns_200_after_warmup(client):
    main._warmup_done.set()
    resp = client.get("/ready")
    assert resp.status_code == 200
    assert resp.json() == {"ready": True}


def test_health_reports_ready_flag(client):
    main._warmup_done.set()
    body = client.get("/health").json()
    assert body["ready"] is True
    main._warmup_done.clear()
    try:
        body = client.get("/health").json()
        assert body["ready"] is False
        # Liveness still 200 even when not yet ready.
        assert body["status"] == "ok"
    finally:
        main._warmup_done.set()


# ── body-size limit middleware ──────────────────────────────────────────


def test_body_size_limit_rejects_oversized(client, monkeypatch):
    # Lower the cap so we can prove it fires without sending megabytes.
    monkeypatch.setattr(main, "_MAX_BODY_BYTES", 64)
    big = {"document": {"document_type": "X" * 200, "fields": []}}
    resp = client.post("/api/definitions", json=big)
    assert resp.status_code == 413
    assert "exceeds" in resp.text.lower()


def test_body_size_limit_allows_normal(client, monkeypatch):
    # Sanity: normal-sized definition still goes through.
    monkeypatch.setattr(main, "_MAX_BODY_BYTES", 1_000_000)
    resp = client.post("/api/definitions", json=_valid_definition())
    assert resp.status_code == 200


# ── /extract concurrency limiter ────────────────────────────────────────


def test_extract_returns_503_when_semaphore_saturated(client, monkeypatch):
    # Drain the semaphore so the next acquire fails. We don't actually run
    # extraction; the limiter rejects before _track_inflight is entered.
    docs_dir = main.TEST_DOCS_DIR
    docs_dir.mkdir(parents=True, exist_ok=True)
    doc_path = docs_dir / "busy.pdf"
    doc_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    doc_id = main._get_document_id(doc_path.name)
    main._render_cache[doc_id] = {
        "filename": doc_path.name,
        "num_pages": 1,
        "page_dimensions": {1: {"width": 100, "height": 100}},
        "pdf_path": str(doc_path),
        "page_images": {},
        "_sig": main._file_signature(doc_path),
    }
    client.post("/api/definitions", json=_valid_definition())

    held = []
    for _ in range(main._MAX_CONCURRENT_EXTRACTS):
        assert main._extract_semaphore.acquire(blocking=False)
        held.append(True)
    try:
        resp = client.post(
            f"/api/documents/{doc_id}/extract",
            json={"definition_id": "test_type"},
        )
        assert resp.status_code == 503
        assert resp.headers.get("Retry-After") == "5"
        # And the rejection counter ticked.
        body = client.get("/metrics").json()
        assert body["counters"]["extractions_rejected"] >= 1
    finally:
        for _ in held:
            main._extract_semaphore.release()


# ── X-Request-ID sanitization ───────────────────────────────────────────


def test_request_id_rejects_unsafe_characters(client):
    # Header injection / control chars must not be propagated; we generate a
    # fresh id instead.
    resp = client.get("/health", headers={"X-Request-ID": "bad id\r\nX-Evil: 1"})
    rid = resp.headers.get("X-Request-ID")
    assert rid is not None
    assert "\r" not in rid and "\n" not in rid and " " not in rid
    assert rid != "bad id\r\nX-Evil: 1"


def test_request_id_rejects_overlong(client):
    too_long = "a" * 65
    resp = client.get("/health", headers={"X-Request-ID": too_long})
    rid = resp.headers.get("X-Request-ID")
    assert rid != too_long
    assert len(rid) <= 64


# ── document listing caching ────────────────────────────────────────────


def test_documents_listing_cached_until_dir_changes(client, tmp_path):
    docs_dir = main.TEST_DOCS_DIR
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "a.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    body1 = client.get("/api/documents").json()
    assert body1["total"] == 1
    cached_obj = main._doc_listing_cache
    # Second call: same signature → same cached object reused.
    client.get("/api/documents")
    assert main._doc_listing_cache is cached_obj
    # New file → cache invalidated, listing rebuilt.
    (docs_dir / "b.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    body2 = client.get("/api/documents").json()
    assert body2["total"] == 2
    assert main._doc_listing_cache is not cached_obj


# ── text-entry pre-lowered annotation ───────────────────────────────────


def test_text_cache_entries_have_pre_lowered_text(client, monkeypatch):
    """Cached text entries must be annotated at extraction time so per-/extract
    matching doesn't have to re-lower every entry on every call."""
    docs_dir = main.TEST_DOCS_DIR
    docs_dir.mkdir(parents=True, exist_ok=True)
    doc_path = docs_dir / "annotated.pdf"
    doc_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    doc_id = main._get_document_id(doc_path.name)
    main._render_cache[doc_id] = {
        "filename": doc_path.name,
        "num_pages": 1,
        "page_dimensions": {1: {"width": 100, "height": 100}},
        "pdf_path": str(doc_path),
        "page_images": {},
        "_sig": main._file_signature(doc_path),
    }

    def fake_extract(_filepath):
        return [
            {"id": 0, "text": "INV-001", "type": "TextItem", "page": 1, "bbox": None},
        ], {1: {"width": 100, "height": 100}}

    monkeypatch.setattr(main, "_extract_text", fake_extract)
    client.post("/api/definitions", json=_valid_definition())
    client.post(
        f"/api/documents/{doc_id}/extract",
        json={"definition_id": "test_type"},
    )

    cached = main._text_cache.get(doc_id)
    assert cached is not None
    entry = cached["text_entries"][0]
    assert entry["_text_lower"] == "inv-001"
    assert entry["_text_stripped_lower"] == "inv-001"


def test_extraction_error_is_not_cached(client, monkeypatch):
    """A transient extraction failure must not poison the cache; the next
    call should retry rather than replaying the empty result."""
    docs_dir = main.TEST_DOCS_DIR
    docs_dir.mkdir(parents=True, exist_ok=True)
    doc_path = docs_dir / "flaky.pdf"
    doc_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    doc_id = main._get_document_id(doc_path.name)
    main._render_cache[doc_id] = {
        "filename": doc_path.name,
        "num_pages": 1,
        "page_dimensions": {1: {"width": 100, "height": 100}},
        "pdf_path": str(doc_path),
        "page_images": {},
        "_sig": main._file_signature(doc_path),
    }

    calls = {"n": 0}

    def flaky(_filepath):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first call fails")
        return ([], {})

    monkeypatch.setattr(main, "_extract_text", flaky)
    client.post("/api/definitions", json=_valid_definition())
    body1 = client.post(
        f"/api/documents/{doc_id}/extract",
        json={"definition_id": "test_type"},
    ).json()
    assert "extraction_error" in body1
    body2 = client.post(
        f"/api/documents/{doc_id}/extract",
        json={"definition_id": "test_type"},
    ).json()
    assert "extraction_error" not in body2
    assert calls["n"] == 2
