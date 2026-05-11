"""Tests for the click-to-teach endpoint:
POST /api/definitions/{def_id}/fields/{field_name}/examples

The endpoint appends a single value to a field's `examples` list, used by
the frontend "click any text block in the document to teach it as an
example" flow. Mutations route through the same definitions lock + atomic
write helpers as POST/PATCH /api/definitions.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(main, "DEFINITIONS_DIR", tmp_path / "definitions")
    monkeypatch.setattr(main, "TEST_DOCS_DIR", tmp_path / "docs")
    main._invalidate_definitions_cache()
    return TestClient(main.app)


def _make_definition() -> dict:
    return {
        "document": {
            "document_type": "Test Type",
            "fields": [
                {"name": "invoice_id", "examples": ["INV-001"]},
                {
                    "name": "line_items",
                    "type": "array",
                    "fields": [{"name": "amount", "examples": ["500.00"]}],
                },
            ],
        }
    }


def test_appends_example_to_top_level_field(client):
    client.post("/api/definitions", json=_make_definition())
    resp = client.post(
        "/api/definitions/test_type/fields/invoice_id/examples",
        json={"value": "INV-2024-77"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["field"] == "invoice_id"
    assert body["examples"] == ["INV-001", "INV-2024-77"]

    # Persisted to disk.
    on_disk = json.loads(
        (main.DEFINITIONS_DIR / "test_type.json").read_text()
    )
    inv = on_disk["document"]["fields"][0]
    assert inv["examples"] == ["INV-001", "INV-2024-77"]


def test_appends_to_array_subfield_via_dotted_path(client):
    client.post("/api/definitions", json=_make_definition())
    resp = client.post(
        "/api/definitions/test_type/fields/line_items.amount/examples",
        json={"value": "999.99"},
    )
    assert resp.status_code == 200
    assert resp.json()["examples"] == ["500.00", "999.99"]


def test_duplicate_value_returns_409(client):
    client.post("/api/definitions", json=_make_definition())
    resp = client.post(
        "/api/definitions/test_type/fields/invoice_id/examples",
        json={"value": "INV-001"},
    )
    assert resp.status_code == 409


def test_missing_definition_returns_404(client):
    resp = client.post(
        "/api/definitions/no_such/fields/invoice_id/examples",
        json={"value": "x"},
    )
    assert resp.status_code == 404


def test_missing_field_returns_404(client):
    client.post("/api/definitions", json=_make_definition())
    resp = client.post(
        "/api/definitions/test_type/fields/no_such_field/examples",
        json={"value": "x"},
    )
    assert resp.status_code == 404


def test_dotted_path_on_non_array_field_returns_400(client):
    client.post("/api/definitions", json=_make_definition())
    resp = client.post(
        "/api/definitions/test_type/fields/invoice_id.subfield/examples",
        json={"value": "x"},
    )
    assert resp.status_code == 400


def test_too_many_dots_returns_400(client):
    client.post("/api/definitions", json=_make_definition())
    resp = client.post(
        "/api/definitions/test_type/fields/a.b.c/examples",
        json={"value": "x"},
    )
    assert resp.status_code == 400


def test_empty_value_rejected_by_pydantic(client):
    client.post("/api/definitions", json=_make_definition())
    resp = client.post(
        "/api/definitions/test_type/fields/invoice_id/examples",
        json={"value": ""},
    )
    assert resp.status_code == 422


def test_extra_keys_in_body_rejected(client):
    """The endpoint uses model_config extra='forbid' so a typo like
    `values` (plural) returns 422 instead of silently no-oping."""
    client.post("/api/definitions", json=_make_definition())
    resp = client.post(
        "/api/definitions/test_type/fields/invoice_id/examples",
        json={"value": "ok", "extra": 1},
    )
    assert resp.status_code == 422


def test_subsequent_extract_includes_new_example(client, monkeypatch):
    """End-to-end smoke: after teaching, the next /extract call sees the
    enlarged examples list (definitions cache was invalidated) and produces
    an example_exact match with the highest confidence."""
    docs_dir = main.TEST_DOCS_DIR
    docs_dir.mkdir(parents=True, exist_ok=True)
    doc_path = docs_dir / "fake.pdf"
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

    def fake_extract_text(_filepath):
        entries = [
            {"id": 0, "text": "ACME Corp.", "type": "TextItem", "page": 1, "bbox": None},
        ]
        return entries, {1: {"width": 100, "height": 100}}

    monkeypatch.setattr(main, "_extract_text", fake_extract_text)

    client.post("/api/definitions", json={
        "document": {
            "document_type": "Test Type",
            "fields": [{"name": "vendor", "examples": ["Other Vendor"]}],
        }
    })

    # Teach: ACME Corp. is now an example of vendor.
    client.post(
        "/api/definitions/test_type/fields/vendor/examples",
        json={"value": "ACME Corp."},
    )

    resp = client.post(
        f"/api/documents/{doc_id}/extract",
        json={"definition_id": "test_type"},
    )
    vendor = next(f for f in resp.json()["fields"] if f["name"] == "vendor")
    # The fresh example produces an exact match, the strongest signal.
    assert vendor["extracted_value"] == "ACME Corp."
    assert vendor["match_reason"] == "example_exact"
