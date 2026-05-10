"""HTTP-level tests using FastAPI's TestClient.

Docling text extraction is mocked so the suite doesn't load ML models. The
tests focus on routing, request validation (Pydantic 422s), and the
CRUD lifecycle for definitions.
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    """Isolated app instance: definitions dir is a tmp dir, caches start empty."""
    monkeypatch.setattr(main, "DEFINITIONS_DIR", tmp_path / "definitions")
    monkeypatch.setattr(main, "TEST_DOCS_DIR", tmp_path / "docs")
    main._invalidate_definitions_cache()
    main._render_cache.clear()
    main._text_cache.clear()
    main._doc_path_cache.clear()
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
    assert any(d["id"] == "test_type" for d in listing)


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
