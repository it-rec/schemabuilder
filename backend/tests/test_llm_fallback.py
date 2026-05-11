"""Tests for the LLM fallback module + its integration into _extract_fields.

The Anthropic SDK is mocked end-to-end — no network calls, no SDK install
required in the test environment. Pure-module tests cover the gating logic
(env-var off / SDK missing / API key missing); integration tests verify
that fallback only fires for opted-in unmatched fields and that the result
shape lines up with the rest of the matcher output.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import llm_fallback
import main


@pytest.fixture(autouse=True)
def _reset_llm_state(monkeypatch):
    """Each test starts with a clean SDK-detection cache + no API key,
    so leaked module state from one test can't change another's gating."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(llm_fallback, "_LLM_ENABLED", "auto")
    llm_fallback.reset_for_tests()
    yield
    llm_fallback.reset_for_tests()


# ── gating: is_available() ──────────────────────────────────────────────


def test_is_available_false_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llm_fallback.is_available() is False


def test_is_available_false_when_explicitly_disabled(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(llm_fallback, "_LLM_ENABLED", "0")
    assert llm_fallback.is_available() is False


def test_is_available_true_with_key_and_sdk(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    # Pretend the SDK is importable. _detect_sdk caches the result, so
    # we just monkeypatch the cache.
    monkeypatch.setattr(llm_fallback, "_sdk_import_ok", True)
    assert llm_fallback.is_available() is True


def test_is_available_false_when_sdk_missing(monkeypatch):
    """If `anthropic` can't be imported, fallback stays disabled even with
    a key set — no AttributeError downstream."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(llm_fallback, "_sdk_import_ok", False)
    assert llm_fallback.is_available() is False


# ── extract_field — direct module tests ─────────────────────────────────


def _stub_client(monkeypatch, parsed_value, confidence=0.9):
    """Install a stub Anthropic client that returns the given parsed
    FieldExtraction without actually touching the SDK."""
    monkeypatch.setattr(llm_fallback, "_sdk_import_ok", True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    stub = MagicMock()
    stub.messages.parse.return_value = MagicMock(
        parsed_output=llm_fallback.FieldExtraction(
            value=parsed_value, confidence=confidence
        )
    )
    monkeypatch.setattr(llm_fallback, "_client", stub)
    monkeypatch.setattr(llm_fallback, "_client_key", "sk-test")
    return stub


def test_extract_field_returns_value_above_threshold(monkeypatch):
    stub = _stub_client(monkeypatch, "ACME Corp.", confidence=0.95)
    result = llm_fallback.extract_field(
        field_name="vendor",
        field_description="The company we're paying",
        examples=["Globex", "Initech"],
        document_text="Invoice from ACME Corp. for services rendered.",
    )
    assert result == {"value": "ACME Corp.", "confidence": 0.95}
    stub.messages.parse.assert_called_once()
    # Sanity-check the call shape: system prompt is a list with
    # cache_control set, output_format is FieldExtraction.
    kwargs = stub.messages.parse.call_args.kwargs
    assert kwargs["output_format"] is llm_fallback.FieldExtraction
    system_blocks = kwargs["system"]
    assert system_blocks[0]["cache_control"] == {"type": "ephemeral"}


def test_extract_field_returns_none_below_threshold(monkeypatch):
    _stub_client(monkeypatch, "Maybe ACME?", confidence=0.3)
    result = llm_fallback.extract_field(
        field_name="vendor",
        field_description="",
        examples=None,
        document_text="...",
        min_confidence=0.5,
    )
    assert result is None


def test_extract_field_returns_none_when_value_is_null(monkeypatch):
    _stub_client(monkeypatch, None, confidence=1.0)
    result = llm_fallback.extract_field(
        field_name="vendor",
        field_description="",
        examples=None,
        document_text="No vendor info in this document.",
    )
    assert result is None


def test_extract_field_swallows_sdk_exception(monkeypatch):
    """A network error or 500 from the API must NOT propagate — the
    rule-based extraction already produced the canonical result."""
    monkeypatch.setattr(llm_fallback, "_sdk_import_ok", True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    stub = MagicMock()
    stub.messages.parse.side_effect = RuntimeError("API down")
    monkeypatch.setattr(llm_fallback, "_client", stub)
    monkeypatch.setattr(llm_fallback, "_client_key", "sk-test")

    result = llm_fallback.extract_field(
        field_name="x", field_description="", examples=None, document_text="..."
    )
    assert result is None


def test_extract_field_short_circuits_when_unavailable():
    """No API key set, no SDK probed → returns None without trying to
    construct a client."""
    assert llm_fallback.is_available() is False
    result = llm_fallback.extract_field(
        field_name="x", field_description="", examples=None, document_text="x"
    )
    assert result is None


def test_extract_field_truncates_oversized_document(monkeypatch):
    """Regression: a multi-megabyte document body must NOT be forwarded
    verbatim — the wrapper truncates to _MAX_DOCUMENT_CHARS so a 500-page
    PDF can't blow Claude's context window in one bad call."""
    stub = _stub_client(monkeypatch, "x", confidence=0.9)
    monkeypatch.setattr(llm_fallback, "_MAX_DOCUMENT_CHARS", 1000)
    huge = "abcdefghij" * 5000  # 50 000 chars
    llm_fallback.extract_field(
        field_name="x", field_description="", examples=None, document_text=huge
    )
    user_msg = stub.messages.parse.call_args.kwargs["messages"][0]["content"]
    assert len(user_msg) < 1500  # wrapper + 1000-char body + marker
    assert "[truncated to 1000 chars]" in user_msg


def test_is_available_rechecks_api_key_each_call(monkeypatch):
    """Regression: previously the env-key check was memoized; rotating
    or unsetting ANTHROPIC_API_KEY required a process restart. The fix
    re-checks the env on every call (only the SDK-import probe is memoized,
    and that result can't actually change at runtime)."""
    monkeypatch.setattr(llm_fallback, "_sdk_import_ok", True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-on")
    assert llm_fallback.is_available() is True

    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert llm_fallback.is_available() is False  # was stuck-on before the fix


# ── integration: _extract_fields invokes the fallback ────────────────────


def test_extract_fields_calls_fallback_for_unmatched_opted_in_field(monkeypatch):
    """Field has use_llm_fallback=True and the matcher couldn't find it
    → the fallback fills extracted_value with match_reason=llm_fallback."""
    monkeypatch.setattr(llm_fallback, "_sdk_import_ok", True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    stub = MagicMock()
    stub.messages.parse.return_value = MagicMock(
        parsed_output=llm_fallback.FieldExtraction(
            value="ACME Corp.", confidence=0.9
        )
    )
    monkeypatch.setattr(llm_fallback, "_client", stub)
    monkeypatch.setattr(llm_fallback, "_client_key", "sk-test")

    definition = {
        "document": {
            "document_type": "Invoice",
            "fields": [
                {
                    "name": "vendor",
                    "description": "Company name",
                    "use_llm_fallback": True,
                }
            ],
        }
    }
    text_entries = [
        {"id": 0, "text": "Invoice from ACME Corp.", "type": "TextItem", "page": 1, "bbox": None}
    ]
    [vendor] = main._extract_fields(definition, text_entries)
    assert vendor["extracted_value"] == "ACME Corp."
    assert vendor["match_reason"] == "llm_fallback"
    assert vendor["match_score"] == 90
    # The LLM can't point at a specific entry; we don't fake it.
    assert vendor["matched_entry_id"] is None


def test_extract_fields_does_not_call_fallback_when_matcher_found_value(monkeypatch):
    """The matcher already nailed it — we MUST NOT pay for an LLM call."""
    monkeypatch.setattr(llm_fallback, "_sdk_import_ok", True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    stub = MagicMock()
    stub.messages.parse.return_value = MagicMock(
        parsed_output=llm_fallback.FieldExtraction(value="OTHER", confidence=1.0)
    )
    monkeypatch.setattr(llm_fallback, "_client", stub)
    monkeypatch.setattr(llm_fallback, "_client_key", "sk-test")

    definition = {
        "document": {
            "document_type": "Invoice",
            "fields": [
                {
                    "name": "invoice_id",
                    "examples": ["INV-001"],
                    "use_llm_fallback": True,
                }
            ],
        }
    }
    text_entries = [
        {"id": 0, "text": "INV-001", "type": "TextItem", "page": 1, "bbox": None}
    ]
    [inv] = main._extract_fields(definition, text_entries)
    assert inv["extracted_value"] == "INV-001"
    assert inv["match_reason"] == "example_exact"
    stub.messages.parse.assert_not_called()


def test_extract_fields_skips_fallback_when_field_not_opted_in(monkeypatch):
    monkeypatch.setattr(llm_fallback, "_sdk_import_ok", True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    stub = MagicMock()
    monkeypatch.setattr(llm_fallback, "_client", stub)
    monkeypatch.setattr(llm_fallback, "_client_key", "sk-test")

    definition = {
        "document": {
            "document_type": "Invoice",
            "fields": [
                # Note: no use_llm_fallback — must NOT call the API.
                {"name": "vendor", "description": "Company name"}
            ],
        }
    }
    [vendor] = main._extract_fields(
        definition,
        [{"id": 0, "text": "Some text", "type": "TextItem", "page": 1, "bbox": None}],
    )
    assert vendor["extracted_value"] is None
    stub.messages.parse.assert_not_called()


def test_extract_fields_skips_fallback_when_sdk_unavailable(monkeypatch):
    """SDK not detected → opted-in field still gets the None result, not
    a crash."""
    monkeypatch.setattr(llm_fallback, "_sdk_import_ok", False)
    definition = {
        "document": {
            "document_type": "Invoice",
            "fields": [{"name": "vendor", "use_llm_fallback": True}],
        }
    }
    [vendor] = main._extract_fields(
        definition,
        [{"id": 0, "text": "...", "type": "TextItem", "page": 1, "bbox": None}],
    )
    assert vendor["extracted_value"] is None
    assert vendor["match_reason"] is None  # no fallback was called


# ── HTTP layer: round-trip use_llm_fallback through the definition API ──


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(main, "DEFINITIONS_DIR", tmp_path / "definitions")
    main._invalidate_definitions_cache()
    return TestClient(main.app)


def test_definition_endpoint_round_trips_use_llm_fallback(client):
    payload = {
        "document": {
            "document_type": "DocType",
            "fields": [
                {"name": "x", "use_llm_fallback": True},
                {"name": "y", "use_llm_fallback": False},
                {"name": "z"},  # absent — should round-trip as None
            ],
        }
    }
    assert client.post("/api/definitions", json=payload).status_code == 200
    fetched = client.get("/api/definitions/doctype").json()
    fields = fetched["document"]["fields"]
    assert fields[0]["use_llm_fallback"] is True
    assert fields[1]["use_llm_fallback"] is False
    # absent is fine — serialized as None or omitted
    assert fields[2].get("use_llm_fallback") in (None, False)
