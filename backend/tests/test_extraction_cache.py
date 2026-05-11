"""Tests for the SQLite extraction cache module + its wiring into /extract.

Two layers:
- Pure-module tests for ExtractionCache (get/put/evict/invalidate) and
  definition_hash (matcher-relevant subset, target_tables irrelevance).
- End-to-end tests through /extract that prove the cache lifts the second
  call into a zero-Docling-call return.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
from extraction_cache import (
    ExtractionCache,
    definition_hash,
    make_key,
)
from extraction_cache import (
    reset_default_cache as _reset_default_cache,
)

# ── module-level tests ──────────────────────────────────────────────────


def test_get_returns_none_when_missing(tmp_path: Path):
    cache = ExtractionCache(path=tmp_path / "c.sqlite")
    assert cache.get("nope") is None


def test_put_then_get_round_trips(tmp_path: Path):
    cache = ExtractionCache(path=tmp_path / "c.sqlite")
    cache.put("k", {"value": 42, "list": [1, 2, 3]})
    assert cache.get("k") == {"value": 42, "list": [1, 2, 3]}


def test_put_overwrites_existing_key(tmp_path: Path):
    cache = ExtractionCache(path=tmp_path / "c.sqlite")
    cache.put("k", {"v": 1})
    cache.put("k", {"v": 2})
    assert cache.get("k") == {"v": 2}


def test_eviction_caps_at_max_entries(tmp_path: Path):
    cache = ExtractionCache(path=tmp_path / "c.sqlite", max_entries=3)
    for i in range(5):
        cache.put(f"k{i}", {"i": i})
        # Tiny sleep so created_at differs between entries — Python's
        # time.time() is per-call but with sub-microsecond resolution on
        # most systems the timestamps could collide on a fast loop.
        time.sleep(0.001)
    assert cache.size() == 3
    # The two oldest should be gone.
    assert cache.get("k0") is None
    assert cache.get("k1") is None
    assert cache.get("k4") is not None


def test_invalidate_drops_the_row(tmp_path: Path):
    cache = ExtractionCache(path=tmp_path / "c.sqlite")
    cache.put("k", {"v": 1})
    cache.invalidate("k")
    assert cache.get("k") is None


def test_corrupted_row_is_self_healing(tmp_path: Path):
    """If a row contains invalid JSON, get() drops it. Defensive against
    schema migrations that might land an old encoding."""
    cache = ExtractionCache(path=tmp_path / "c.sqlite")
    # Direct write so we can plant a bad payload.
    cache._conn().execute(
        "INSERT INTO extractions(key, value, created_at) VALUES (?, ?, ?)",
        ("bad", "{not json", time.time()),
    )
    assert cache.get("bad") is None
    # And the row was cleaned up.
    assert cache.size() == 0


def test_invalidate_by_doc_signature_targets_only_matching_entries(tmp_path: Path):
    cache = ExtractionCache(path=tmp_path / "c.sqlite")
    cache.put("a", {"document_id": "doc-a", "_doc_signature": ["a.pdf", 1, 100]})
    cache.put("b", {"document_id": "doc-b", "_doc_signature": ["b.pdf", 2, 200]})
    removed = cache.invalidate_by_doc_signature(("a.pdf", 1, 100))
    assert removed == 1
    assert cache.get("a") is None
    assert cache.get("b") is not None


def test_definition_hash_ignores_target_tables_and_extras():
    """Editing target_tables should NOT invalidate cached extractions
    because target_tables don't drive matching — they're applied at export
    time. The hash must be stable across such edits."""
    base = {
        "document": {
            "document_type": "Invoice",
            "fields": [{"name": "invoice_id", "examples": ["INV-001"]}],
        }
    }
    with_tt = {
        **base,
        "target_tables": [{"name": "Invoice", "columns": []}],
        "weird_extra_key": 42,
    }
    assert definition_hash(base) == definition_hash(with_tt)


def test_definition_hash_changes_when_examples_change():
    a = {
        "document": {
            "document_type": "Invoice",
            "fields": [{"name": "id", "examples": ["X-1"]}],
        }
    }
    b = {
        "document": {
            "document_type": "Invoice",
            "fields": [{"name": "id", "examples": ["X-1", "X-2"]}],
        }
    }
    assert definition_hash(a) != definition_hash(b)


def test_make_key_changes_with_doc_signature():
    d = {"document": {"document_type": "X", "fields": []}}
    k1 = make_key(("a.pdf", 1, 100), d)
    k2 = make_key(("a.pdf", 2, 100), d)
    assert k1 != k2


# ── end-to-end tests through /extract ───────────────────────────────────


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(main, "DEFINITIONS_DIR", tmp_path / "definitions")
    monkeypatch.setattr(main, "TEST_DOCS_DIR", tmp_path / "docs")
    monkeypatch.setenv("SCHEMABUILDER_EXTRACTION_CACHE_PATH", str(tmp_path / "ext.sqlite"))
    _reset_default_cache()
    main._invalidate_definitions_cache()
    main._render_cache.clear()
    main._text_cache.clear()
    main._doc_path_cache.clear()
    main._ocr_decision_cache.clear()
    main._invalidate_doc_listing_cache()
    with main._metrics_lock:
        for k in list(main._metrics.keys()):
            main._metrics[k] = 0
    yield TestClient(main.app)
    _reset_default_cache()


def _setup_doc(monkeypatch, call_counter: dict) -> str:
    docs_dir = main.TEST_DOCS_DIR
    docs_dir.mkdir(parents=True, exist_ok=True)
    path = docs_dir / "fake.pdf"
    path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    doc_id = main._get_document_id(path.name)
    main._render_cache[doc_id] = {
        "filename": path.name,
        "num_pages": 1,
        "page_dimensions": {1: {"width": 100, "height": 100}},
        "pdf_path": str(path),
        "page_images": {},
        "_sig": main._file_signature(path),
    }

    def fake_extract_text(_filepath):
        call_counter["n"] = call_counter.get("n", 0) + 1
        return [
            {"id": 0, "text": "INV-001", "type": "TextItem", "page": 1, "bbox": None},
        ], {1: {"width": 100, "height": 100}}

    monkeypatch.setattr(main, "_extract_text", fake_extract_text)
    return doc_id


def _make_def(client):
    client.post(
        "/api/definitions",
        json={
            "document": {
                "document_type": "Test Type",
                "fields": [{"name": "invoice_id", "examples": ["INV-001"]}],
            }
        },
    )


def test_second_extract_returns_cache_hit_without_calling_extractor(
    client, monkeypatch
):
    calls = {"n": 0}
    doc_id = _setup_doc(monkeypatch, calls)
    _make_def(client)

    first = client.post(
        f"/api/documents/{doc_id}/extract",
        json={"definition_id": "test_type"},
    ).json()
    assert first["cache_hit"] is False
    assert calls["n"] == 1
    # Text cache stays warm, but the SQLite path must also serve a
    # full-response hit from the second call onwards.
    main._text_cache.clear()  # force the matcher path to be the slow one

    second = client.post(
        f"/api/documents/{doc_id}/extract",
        json={"definition_id": "test_type"},
    ).json()
    assert second["cache_hit"] is True
    # If the cache served the second call, _extract_text was NOT called
    # again (verified by the same counter).
    assert calls["n"] == 1
    # Cached response is otherwise equivalent.
    assert second["fields"] == first["fields"]


def test_refresh_query_param_bypasses_cache(client, monkeypatch):
    calls = {"n": 0}
    doc_id = _setup_doc(monkeypatch, calls)
    _make_def(client)
    client.post(f"/api/documents/{doc_id}/extract", json={"definition_id": "test_type"})
    main._text_cache.clear()

    resp = client.post(
        f"/api/documents/{doc_id}/extract?refresh=true",
        json={"definition_id": "test_type"},
    ).json()
    assert resp["cache_hit"] is False
    assert calls["n"] == 2


def test_definition_change_invalidates_cache(client, monkeypatch):
    calls = {"n": 0}
    doc_id = _setup_doc(monkeypatch, calls)
    _make_def(client)
    client.post(f"/api/documents/{doc_id}/extract", json={"definition_id": "test_type"})
    main._text_cache.clear()

    # Update the definition's examples — matcher-relevant change.
    client.patch(
        "/api/definitions/test_type",
        json={
            "document": {
                "document_type": "Test Type",
                "fields": [
                    {"name": "invoice_id", "examples": ["INV-001", "INV-002"]}
                ],
            }
        },
    )
    # Next extract is a cache miss because the def hash changed.
    resp = client.post(
        f"/api/documents/{doc_id}/extract",
        json={"definition_id": "test_type"},
    ).json()
    assert resp["cache_hit"] is False
    assert calls["n"] == 2


def test_extraction_error_is_not_cached(client, monkeypatch):
    """A failed extraction shouldn't get frozen into the cache — the next
    call should retry rather than serve up the failure."""
    docs_dir = main.TEST_DOCS_DIR
    docs_dir.mkdir(parents=True, exist_ok=True)
    path = docs_dir / "fake.pdf"
    path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    doc_id = main._get_document_id(path.name)
    main._render_cache[doc_id] = {
        "filename": path.name,
        "num_pages": 1,
        "page_dimensions": {1: {"width": 100, "height": 100}},
        "pdf_path": str(path),
        "page_images": {},
        "_sig": main._file_signature(path),
    }

    def failing_extract(_filepath):
        raise RuntimeError("docling exploded")

    monkeypatch.setattr(main, "_extract_text", failing_extract)
    _make_def(client)

    first = client.post(
        f"/api/documents/{doc_id}/extract",
        json={"definition_id": "test_type"},
    ).json()
    assert "extraction_error" in first
    # Cache must NOT have absorbed the failure.
    second = client.post(
        f"/api/documents/{doc_id}/extract",
        json={"definition_id": "test_type"},
    ).json()
    assert second["cache_hit"] is False


def test_delete_document_invalidates_cached_extraction(client, monkeypatch):
    calls = {"n": 0}
    doc_id = _setup_doc(monkeypatch, calls)
    _make_def(client)
    # Warm the cache.
    client.post(f"/api/documents/{doc_id}/extract", json={"definition_id": "test_type"})
    # Delete the document — re-uploading under the same name should not
    # see the old extraction.
    client.delete(f"/api/documents/{doc_id}")

    # Re-stage the doc fresh; the cache entry from before deletion should
    # be gone.
    docs_dir = main.TEST_DOCS_DIR
    path = docs_dir / "fake.pdf"
    path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    main._invalidate_doc_listing_cache()
    main._render_cache[doc_id] = {
        "filename": path.name,
        "num_pages": 1,
        "page_dimensions": {1: {"width": 100, "height": 100}},
        "pdf_path": str(path),
        "page_images": {},
        "_sig": main._file_signature(path),
    }
    main._text_cache.clear()
    resp = client.post(
        f"/api/documents/{doc_id}/extract",
        json={"definition_id": "test_type"},
    ).json()
    assert resp["cache_hit"] is False
