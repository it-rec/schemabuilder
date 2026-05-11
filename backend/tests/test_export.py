"""Tests for the target_tables transform engine and /export endpoint.

Unit-tests live at the top (`transforms.py` is pure Python with no HTTP /
Docling dependencies), then the HTTP integration test reuses the same
Docling-mocking pattern as test_api.py.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
from transforms import (
    TRANSFORMS,
    TransformError,
    _t_string_to_currency,
    _t_string_to_date,
    build_export,
)

# ── transform engine ────────────────────────────────────────────────────


def test_identity_is_a_passthrough():
    assert TRANSFORMS["identity"](input="abc") == "abc"
    assert TRANSFORMS["identity"](input=None) is None
    assert TRANSFORMS["identity"](input=42) == 42


def test_string_to_date_parses_cldr_format():
    assert _t_string_to_date("2024-02-04", "YYYY-MM-DD") == "2024-02-04"
    assert _t_string_to_date("04.02.24", "DD.MM.YY") == "2024-02-04"


def test_string_to_date_returns_none_for_unparseable():
    assert _t_string_to_date("not a date", "YYYY-MM-DD") is None
    assert _t_string_to_date(None, "YYYY-MM-DD") is None
    assert _t_string_to_date("", "YYYY-MM-DD") is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("$1,234.56", "1234.56"),
        ("1.234,56 EUR", "1234.56"),
        ("500.00", "500.00"),
        ("500", "500"),
        ("1,000", "1000"),     # comma-thousands, no decimals
        ("0,99", "0.99"),      # comma-decimal, two trailing digits
        ("garbage", None),
        ("", None),
        (None, None),
    ],
)
def test_string_to_currency_handles_common_locales(raw, expected):
    assert _t_string_to_currency(raw) == expected


def test_build_export_scalar_table_uses_field_values():
    definition = {
        "document": {
            "document_type": "Invoice",
            "fields": [{"name": "invoice_id"}],
        },
        "target_tables": [
            {
                "name": "Invoice",
                "columns": [
                    {"name": "doc_id", "source": {"variable": "document_id"}},
                    {"name": "invoice_id", "source": {"field": "invoice_id"}},
                ],
            }
        ],
    }
    fields = [{"name": "invoice_id", "extracted_value": "INV-7"}]
    result = build_export(definition, "abc123", fields)
    assert result == {"Invoice": [{"doc_id": "abc123", "invoice_id": "INV-7"}]}


def test_build_export_applies_default_value_when_source_is_none():
    definition = {
        "document": {"document_type": "X", "fields": [{"name": "x"}]},
        "target_tables": [
            {
                "name": "X",
                "columns": [
                    {
                        "name": "x",
                        "default_value": "fallback",
                        "source": {"field": "x"},
                    }
                ],
            }
        ],
    }
    result = build_export(definition, "doc", [{"name": "x", "extracted_value": None}])
    assert result["X"][0]["x"] == "fallback"


def test_build_export_chains_transforms_through_arguments():
    definition = {
        "document": {"document_type": "X", "fields": [{"name": "raw_date"}]},
        "target_tables": [
            {
                "name": "X",
                "columns": [
                    {
                        "name": "iso_date",
                        "source": {
                            "transform": {
                                "transform_name": "string_to_date",
                                "arguments": [
                                    {"name": "input", "value": {"field": "raw_date"}},
                                    {"name": "format", "value": {"literal": "DD.MM.YYYY"}},
                                ],
                            }
                        },
                    }
                ],
            }
        ],
    }
    fields = [{"name": "raw_date", "extracted_value": "04.02.2024"}]
    result = build_export(definition, "doc", fields)
    assert result["X"][0]["iso_date"] == "2024-02-04"


def test_build_export_array_table_emits_row_per_item():
    definition = {
        "document": {
            "document_type": "Invoice",
            "fields": [
                {
                    "name": "line_items",
                    "type": "array",
                    "fields": [{"name": "amount"}, {"name": "qty"}],
                }
            ],
        },
        "target_tables": [
            {
                "name": "line_items",
                "columns": [
                    {"name": "doc_id", "source": {"variable": "document_id"}},
                    {"name": "amount", "source": {"field": "amount"}},
                    {"name": "qty", "source": {"field": "qty"}},
                ],
            }
        ],
    }
    fields = [
        {
            "name": "line_items",
            "type": "array",
            "items": [
                {
                    "fields": [
                        {"name": "amount", "extracted_value": "10"},
                        {"name": "qty", "extracted_value": "2"},
                    ]
                },
                {
                    "fields": [
                        {"name": "amount", "extracted_value": "5"},
                        {"name": "qty", "extracted_value": "1"},
                    ]
                },
            ],
        }
    ]
    result = build_export(definition, "doc", fields)
    assert result == {
        "line_items": [
            {"doc_id": "doc", "amount": "10", "qty": "2"},
            {"doc_id": "doc", "amount": "5", "qty": "1"},
        ]
    }


def test_build_export_array_with_no_items_returns_empty_list():
    definition = {
        "document": {
            "document_type": "Invoice",
            "fields": [{"name": "line_items", "type": "array", "fields": []}],
        },
        "target_tables": [
            {"name": "line_items", "columns": [{"name": "x", "source": {"literal": 1}}]}
        ],
    }
    fields = [{"name": "line_items", "type": "array", "items": []}]
    assert build_export(definition, "doc", fields) == {"line_items": []}


def test_build_export_unknown_transform_raises():
    definition = {
        "document": {"document_type": "X", "fields": [{"name": "x"}]},
        "target_tables": [
            {
                "name": "X",
                "columns": [
                    {
                        "name": "x",
                        "source": {
                            "transform": {"transform_name": "does_not_exist", "arguments": []}
                        },
                    }
                ],
            }
        ],
    }
    with pytest.raises(TransformError):
        build_export(definition, "doc", [{"name": "x", "extracted_value": "v"}])


def test_build_export_column_without_name_raises():
    definition = {
        "document": {"document_type": "X", "fields": [{"name": "x"}]},
        "target_tables": [
            {"name": "X", "columns": [{"source": {"literal": 1}}]},
        ],
    }
    with pytest.raises(TransformError):
        build_export(definition, "doc", [])


# ── /api/documents/{id}/export endpoint ─────────────────────────────────


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
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


def _setup_fake_doc(monkeypatch) -> str:
    """Create a fake doc on disk + mock Docling, return its doc_id."""
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
            {"id": 0, "text": "INV-001", "type": "TextItem", "page": 1, "bbox": None},
            {"id": 1, "text": "2024-02-04", "type": "TextItem", "page": 1, "bbox": None},
        ]
        return entries, {1: {"width": 100, "height": 100}}

    monkeypatch.setattr(main, "_extract_text", fake_extract_text)
    return doc_id


def _export_definition() -> dict:
    return {
        "document": {
            "document_type": "Test Type",
            "fields": [
                {"name": "invoice_id", "examples": ["INV-001"]},
                {"name": "invoice_date", "examples": ["2024-02-04"]},
            ],
        },
        "target_tables": [
            {
                "name": "Invoice",
                "columns": [
                    {"name": "doc_id", "source": {"variable": "document_id"}},
                    {"name": "invoice_id", "source": {"field": "invoice_id"}},
                    {
                        "name": "invoice_date",
                        "source": {
                            "transform": {
                                "transform_name": "string_to_date",
                                "arguments": [
                                    {"name": "input", "value": {"field": "invoice_date"}},
                                    {"name": "format", "value": {"literal": "YYYY-MM-DD"}},
                                ],
                            }
                        },
                    },
                ],
            }
        ],
    }


def test_export_json_returns_all_tables(client, monkeypatch):
    doc_id = _setup_fake_doc(monkeypatch)
    client.post("/api/definitions", json=_export_definition())

    resp = client.get(
        f"/api/documents/{doc_id}/export",
        params={"definition_id": "test_type"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["document_id"] == doc_id
    assert body["definition_id"] == "test_type"
    assert "Invoice" in body["tables"]
    row = body["tables"]["Invoice"][0]
    assert row["doc_id"] == doc_id
    assert row["invoice_id"] == "INV-001"
    # The date transform fired against the matched invoice_date value.
    assert row["invoice_date"] == "2024-02-04"


def test_export_csv_returns_attachment(client, monkeypatch):
    doc_id = _setup_fake_doc(monkeypatch)
    client.post("/api/definitions", json=_export_definition())

    resp = client.get(
        f"/api/documents/{doc_id}/export",
        params={"definition_id": "test_type", "format": "csv", "table": "Invoice"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    # First line: header. Second line: row. Column order from the definition.
    lines = resp.text.strip().splitlines()
    assert lines[0] == "doc_id,invoice_id,invoice_date"
    assert lines[1].endswith(",INV-001,2024-02-04")


def test_export_csv_requires_table_param(client, monkeypatch):
    doc_id = _setup_fake_doc(monkeypatch)
    client.post("/api/definitions", json=_export_definition())
    resp = client.get(
        f"/api/documents/{doc_id}/export",
        params={"definition_id": "test_type", "format": "csv"},
    )
    assert resp.status_code == 400
    assert "table" in resp.json()["detail"].lower()


def test_export_csv_unknown_table_returns_404(client, monkeypatch):
    doc_id = _setup_fake_doc(monkeypatch)
    client.post("/api/definitions", json=_export_definition())
    resp = client.get(
        f"/api/documents/{doc_id}/export",
        params={
            "definition_id": "test_type",
            "format": "csv",
            "table": "no_such_table",
        },
    )
    assert resp.status_code == 404


def test_export_rejects_invalid_format(client, monkeypatch):
    doc_id = _setup_fake_doc(monkeypatch)
    client.post("/api/definitions", json=_export_definition())
    resp = client.get(
        f"/api/documents/{doc_id}/export",
        params={"definition_id": "test_type", "format": "xml"},
    )
    assert resp.status_code == 400


def test_export_returns_404_for_unknown_document(client):
    client.post("/api/definitions", json=_export_definition())
    resp = client.get(
        "/api/documents/no-such-doc/export",
        params={"definition_id": "test_type"},
    )
    assert resp.status_code == 404


def test_export_returns_404_for_unknown_definition(client, monkeypatch):
    doc_id = _setup_fake_doc(monkeypatch)
    resp = client.get(
        f"/api/documents/{doc_id}/export",
        params={"definition_id": "nope"},
    )
    assert resp.status_code == 404


def test_export_definition_with_bad_transform_returns_422(client, monkeypatch):
    """Unknown transforms in a definition surface as a 422 (input was bad),
    not a 500 — the client should be able to tell server bugs apart from
    bad definitions."""
    doc_id = _setup_fake_doc(monkeypatch)
    bad = _export_definition()
    bad["target_tables"][0]["columns"].append(
        {
            "name": "broken",
            "source": {
                "transform": {"transform_name": "does_not_exist", "arguments": []}
            },
        }
    )
    client.post("/api/definitions", json=bad)
    resp = client.get(
        f"/api/documents/{doc_id}/export",
        params={"definition_id": "test_type"},
    )
    assert resp.status_code == 422
    assert "does_not_exist" in resp.json()["detail"]
