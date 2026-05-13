"""Tests for the LLM schema generation module + its HTTP endpoint.

The Anthropic SDK is mocked end-to-end so the suite runs without a
network or API key. Mirrors the structure of `test_llm_fallback.py`.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import llm_schema_generator
import main


@pytest.fixture(autouse=True)
def _reset_llm_state(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(llm_schema_generator, "_LLM_ENABLED", "auto")
    llm_schema_generator.reset_for_tests()
    yield
    llm_schema_generator.reset_for_tests()


# ── gating ────────────────────────────────────────────────────────────


def test_is_available_false_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llm_schema_generator.is_available() is False


def test_is_available_false_when_explicitly_disabled(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(llm_schema_generator, "_LLM_ENABLED", "0")
    assert llm_schema_generator.is_available() is False


def test_is_available_true_with_key_and_sdk(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(llm_schema_generator, "_sdk_import_ok", True)
    assert llm_schema_generator.is_available() is True


# ── module-level generate_schema ──────────────────────────────────────


def _stub_client(monkeypatch, suggestion):
    """Install a stub Anthropic client that returns the given suggestion
    parsed as a SchemaSuggestion. Tests that need to inspect the call
    arguments grab the returned MagicMock."""
    monkeypatch.setattr(llm_schema_generator, "_sdk_import_ok", True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    stub = MagicMock()
    stub.messages.parse.return_value = MagicMock(parsed_output=suggestion)
    monkeypatch.setattr(llm_schema_generator, "_client", stub)
    monkeypatch.setattr(llm_schema_generator, "_client_key", "sk-test")
    return stub


def test_generate_schema_returns_normalized_document_shape(monkeypatch):
    suggestion = llm_schema_generator.SchemaSuggestion(
        document_type="Purchase Order",
        document_description="A B2B purchase order.",
        fields=[
            llm_schema_generator.SuggestedField(
                name="po_number",
                type="scalar",
                description="Identifier",
                examples=["PO-1001"],
            ),
            llm_schema_generator.SuggestedField(
                name="line_items",
                type="array",
                description="Items",
            ),
        ],
    )
    stub = _stub_client(monkeypatch, suggestion)
    out = llm_schema_generator.generate_schema(
        document_text="PO-1001 ... line items ...",
        filename_hint="po.pdf",
    )
    assert out == {
        "document_type": "Purchase Order",
        "document_description": "A B2B purchase order.",
        "fields": [
            {
                "name": "po_number",
                "description": "Identifier",
                "examples": ["PO-1001"],
            },
            {"name": "line_items", "type": "array", "description": "Items"},
        ],
    }
    # Sanity-check the SDK call shape: structured output + cached system.
    kwargs = stub.messages.parse.call_args.kwargs
    assert kwargs["output_format"] is llm_schema_generator.SchemaSuggestion
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_generate_schema_sanitizes_field_names(monkeypatch):
    suggestion = llm_schema_generator.SchemaSuggestion(
        document_type="Doc",
        fields=[
            llm_schema_generator.SuggestedField(name="Invoice # / Date"),
            llm_schema_generator.SuggestedField(name="  _Total Amount_  "),
            # Duplicate after sanitization → silently dropped.
            llm_schema_generator.SuggestedField(name="invoice___date"),
        ],
    )
    _stub_client(monkeypatch, suggestion)
    out = llm_schema_generator.generate_schema(document_text="text")
    names = [f["name"] for f in out["fields"]]
    assert names == ["invoice_date", "total_amount"]


def test_generate_schema_caps_field_count(monkeypatch):
    """Regression: a chatty LLM that proposes 60 fields gets trimmed so
    the editor isn't drowned in noise. Caller can always Add more by hand."""
    monkeypatch.setattr(llm_schema_generator, "_MAX_SUGGESTED_FIELDS", 3)
    suggestion = llm_schema_generator.SchemaSuggestion(
        document_type="Doc",
        fields=[
            llm_schema_generator.SuggestedField(name=f"field_{i}")
            for i in range(10)
        ],
    )
    _stub_client(monkeypatch, suggestion)
    out = llm_schema_generator.generate_schema(document_text="x")
    assert len(out["fields"]) == 3


def test_generate_schema_truncates_oversized_document(monkeypatch):
    suggestion = llm_schema_generator.SchemaSuggestion(
        document_type="Doc",
        fields=[llm_schema_generator.SuggestedField(name="x")],
    )
    stub = _stub_client(monkeypatch, suggestion)
    monkeypatch.setattr(llm_schema_generator, "_MAX_DOCUMENT_CHARS", 1000)
    huge = "abcdefghij" * 5000  # 50 000 chars
    llm_schema_generator.generate_schema(document_text=huge)
    user_msg = stub.messages.parse.call_args.kwargs["messages"][0]["content"]
    assert "[truncated to 1000 chars]" in user_msg
    assert len(user_msg) < 1500


def test_generate_schema_returns_none_when_unavailable():
    assert llm_schema_generator.is_available() is False
    assert llm_schema_generator.generate_schema(document_text="x") is None


def test_generate_schema_returns_none_when_text_empty(monkeypatch):
    monkeypatch.setattr(llm_schema_generator, "_sdk_import_ok", True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert llm_schema_generator.generate_schema(document_text="") is None
    assert llm_schema_generator.generate_schema(document_text="   \n  ") is None


def test_generate_schema_returns_none_when_no_fields(monkeypatch):
    """An empty `fields` list is treated as "no useful suggestion" so the
    API layer can return a clean 502 instead of asking the user to save
    a definition with zero fields."""
    suggestion = llm_schema_generator.SchemaSuggestion(
        document_type="Doc", fields=[]
    )
    _stub_client(monkeypatch, suggestion)
    assert llm_schema_generator.generate_schema(document_text="text") is None


def test_generate_schema_swallows_sdk_exception(monkeypatch):
    monkeypatch.setattr(llm_schema_generator, "_sdk_import_ok", True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    stub = MagicMock()
    stub.messages.parse.side_effect = RuntimeError("API down")
    monkeypatch.setattr(llm_schema_generator, "_client", stub)
    monkeypatch.setattr(llm_schema_generator, "_client_key", "sk-test")
    assert llm_schema_generator.generate_schema(document_text="text") is None


# ── HTTP endpoint ─────────────────────────────────────────────────────


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


def _put_fake_doc(tmp_path: Path) -> str:
    docs_dir = main.TEST_DOCS_DIR
    docs_dir.mkdir(parents=True, exist_ok=True)
    doc_path = docs_dir / "fake.pdf"
    doc_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    return main._get_document_id(doc_path.name)


def _stub_text_extraction(monkeypatch, entries):
    def fake_extract_text(_filepath):
        return entries, {1: {"width": 100, "height": 100}}
    monkeypatch.setattr(main, "_extract_text", fake_extract_text)


def test_suggest_definition_returns_404_for_unknown_doc(client):
    resp = client.post("/api/documents/nope/suggest-definition")
    assert resp.status_code == 404


def test_suggest_definition_returns_503_when_llm_unavailable(
    client, tmp_path, monkeypatch
):
    """Without ANTHROPIC_API_KEY the endpoint must not invoke Docling at
    all — surface 503 immediately so the frontend can show a clear
    "configure your key" message."""
    doc_id = _put_fake_doc(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    resp = client.post(f"/api/documents/{doc_id}/suggest-definition")
    assert resp.status_code == 503
    assert "ANTHROPIC_API_KEY" in resp.json()["detail"]


def test_suggest_definition_returns_502_when_llm_returns_nothing(
    client, tmp_path, monkeypatch
):
    doc_id = _put_fake_doc(tmp_path)
    _stub_text_extraction(
        monkeypatch,
        [{"id": 0, "text": "Some text", "type": "TextItem", "page": 1, "bbox": None}],
    )
    monkeypatch.setattr(llm_schema_generator, "_sdk_import_ok", True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    stub = MagicMock()
    stub.messages.parse.return_value = MagicMock(parsed_output=None)
    monkeypatch.setattr(llm_schema_generator, "_client", stub)
    monkeypatch.setattr(llm_schema_generator, "_client_key", "sk-test")
    resp = client.post(f"/api/documents/{doc_id}/suggest-definition")
    assert resp.status_code == 502


def test_suggest_definition_happy_path(client, tmp_path, monkeypatch):
    doc_id = _put_fake_doc(tmp_path)
    _stub_text_extraction(
        monkeypatch,
        [
            {"id": 0, "text": "Invoice INV-42", "type": "TextItem", "page": 1, "bbox": None},
            {"id": 1, "text": "Date: 2025-01-15", "type": "TextItem", "page": 1, "bbox": None},
        ],
    )
    suggestion = llm_schema_generator.SchemaSuggestion(
        document_type="Invoice",
        document_description="A bill issued by a vendor.",
        fields=[
            llm_schema_generator.SuggestedField(
                name="invoice_number", type="scalar", examples=["INV-42"]
            ),
            llm_schema_generator.SuggestedField(name="issue_date", type="scalar"),
        ],
    )
    _stub_client(monkeypatch, suggestion)

    resp = client.post(f"/api/documents/{doc_id}/suggest-definition")
    assert resp.status_code == 200
    body = resp.json()
    assert body["document_id"] == doc_id
    doc = body["document"]
    assert doc["document_type"] == "Invoice"
    assert doc["document_description"] == "A bill issued by a vendor."
    names = [f["name"] for f in doc["fields"]]
    assert names == ["invoice_number", "issue_date"]

    # The returned envelope must be POSTable to /api/definitions
    # unchanged — that's the whole point of the feature: the editor
    # hydrates from this shape and the user clicks Save.
    create_resp = client.post(
        "/api/definitions", json={"document": doc}
    )
    assert create_resp.status_code == 200
    assert create_resp.json()["id"] == "invoice"


def test_suggest_definition_returns_422_when_extraction_fails(
    client, tmp_path, monkeypatch
):
    doc_id = _put_fake_doc(tmp_path)
    monkeypatch.setattr(llm_schema_generator, "_sdk_import_ok", True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

    def boom(_filepath):
        raise RuntimeError("Docling exploded")
    monkeypatch.setattr(main, "_extract_text", boom)
    resp = client.post(f"/api/documents/{doc_id}/suggest-definition")
    assert resp.status_code == 422
    assert "Docling exploded" in resp.json()["detail"]


def test_suggest_definition_returns_422_when_text_is_empty(
    client, tmp_path, monkeypatch
):
    doc_id = _put_fake_doc(tmp_path)
    monkeypatch.setattr(llm_schema_generator, "_sdk_import_ok", True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _stub_text_extraction(monkeypatch, [])
    resp = client.post(f"/api/documents/{doc_id}/suggest-definition")
    assert resp.status_code == 422


def test_suggest_definition_increments_metric(client, tmp_path, monkeypatch):
    doc_id = _put_fake_doc(tmp_path)
    _stub_text_extraction(
        monkeypatch,
        [{"id": 0, "text": "data", "type": "TextItem", "page": 1, "bbox": None}],
    )
    _stub_client(
        monkeypatch,
        llm_schema_generator.SchemaSuggestion(
            document_type="X",
            fields=[llm_schema_generator.SuggestedField(name="a")],
        ),
    )
    assert main._metrics["schema_suggestions"] == 0
    client.post(f"/api/documents/{doc_id}/suggest-definition")
    assert main._metrics["schema_suggestions"] == 1
