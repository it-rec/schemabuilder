"""Tests for the per-field `pattern` regex slot.

When a field carries a `pattern`, any text entry whose text matches becomes
a strong candidate (matcher score 92, between example_substring=80 and
example_exact=95). The matched substring (capture group 1, falling back to
group 0) becomes extracted_value so the result is the IBAN / VAT id /
whatever the regex carved out — not the surrounding sentence.
"""
from __future__ import annotations

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
    main._signature_cache.clear()
    return TestClient(main.app)


def _entry(eid: int, text: str) -> dict:
    return {"id": eid, "text": text, "type": "TextItem", "page": 1, "bbox": None}


# ── matcher behavior ────────────────────────────────────────────────────


def test_pattern_match_returns_group_zero_when_no_groups():
    field = {"name": "iban", "pattern": r"\b[A-Z]{2}\d{20}\b"}
    result = main._match_field_to_entries(
        field,
        [_entry(0, "Please pay to DE89370400440532013000 by Friday.")],
        used_ids=set(),
    )
    assert result["extracted_value"] == "DE89370400440532013000"
    assert result["match_reason"] == "pattern_match"
    assert result["match_score"] == 92


def test_pattern_match_uses_capture_group_one():
    field = {"name": "iban", "pattern": r"IBAN[:\s]+([A-Z]{2}\d{20})"}
    result = main._match_field_to_entries(
        field,
        [_entry(0, "IBAN: DE89370400440532013000")],
        used_ids=set(),
    )
    # The capture group scopes the value to just the IBAN — no "IBAN: " prefix.
    assert result["extracted_value"] == "DE89370400440532013000"


def test_pattern_match_loses_to_example_exact():
    field = {
        "name": "code",
        "examples": ["FOO-123"],
        "pattern": r"\bFOO-\d+\b",
    }
    # Both signals fire, but example_exact (95) > pattern_match (92).
    result = main._match_field_to_entries(
        field,
        [_entry(0, "FOO-123")],
        used_ids=set(),
    )
    assert result["match_reason"] == "example_exact"
    # Without the substring extraction, we get the full entry text.
    assert result["extracted_value"] == "FOO-123"


def test_pattern_match_beats_format_heuristics():
    """A user-supplied pattern is a stronger intent signal than a date_format
    heuristic firing off an example shape."""
    field = {
        "name": "iso_date",
        "examples": ["2024-01-01"],  # triggers has_date heuristic
        "pattern": r"\b(\d{4}-\d{2}-\d{2})T",
    }
    result = main._match_field_to_entries(
        field,
        [_entry(0, "Created at 2024-02-04T10:30:00Z")],
        used_ids=set(),
    )
    assert result["match_reason"] == "pattern_match"
    assert result["extracted_value"] == "2024-02-04"


def test_pattern_field_echoes_in_result():
    """The frontend reads `pattern` off the field result to badge it."""
    field = {"name": "x", "pattern": r"\d+"}
    result = main._match_field_to_entries(
        field, [_entry(0, "abc 12 def")], used_ids=set()
    )
    assert result["pattern"] == r"\d+"


def test_no_match_when_pattern_does_not_fire():
    # Field name avoids "iban" so the label heuristic doesn't accidentally
    # match the entry text — we want to assert the pattern alone is the
    # gate.
    field = {"name": "account_number", "pattern": r"\b[A-Z]{2}\d{20}\b"}
    result = main._match_field_to_entries(
        field,
        [_entry(0, "Just some boilerplate, nothing to see.")],
        used_ids=set(),
    )
    assert result["extracted_value"] is None


def test_pattern_only_field_signature_includes_pattern(tmp_path):
    """Without examples or options, a pattern-only field would otherwise be
    pre-filtered out of the entry stream by `_entry_could_match`. Verify
    the regex is added to the signatures list."""
    definition = {
        "document": {
            "document_type": "X",
            "fields": [{"name": "iban", "pattern": r"\bDE\d{20}\b"}],
        }
    }
    sigs = main._build_field_signatures(definition)
    assert any(kind == "regex" and pat.search("DE89370400440532013000") for kind, pat in sigs)


def test_invalid_regex_in_dict_falls_back_silently():
    """Direct dict editing past Pydantic shouldn't crash the matcher."""
    field = {"name": "x", "pattern": "[unclosed"}
    result = main._match_field_to_entries(
        field, [_entry(0, "anything")], used_ids=set()
    )
    # Pattern is silently ignored; no other signal → no match.
    assert result["extracted_value"] is None


# ── HTTP layer: validation + round-trip ─────────────────────────────────


def test_definition_endpoint_rejects_invalid_regex(client):
    payload = {
        "document": {
            "document_type": "Bad",
            "fields": [{"name": "x", "pattern": "[unclosed"}],
        }
    }
    resp = client.post("/api/definitions", json=payload)
    assert resp.status_code == 422
    # Error message points at the offending field.
    assert "regular expression" in resp.text.lower()


def test_definition_endpoint_accepts_valid_regex(client):
    payload = {
        "document": {
            "document_type": "Good",
            "fields": [{"name": "x", "pattern": r"\d+"}],
        }
    }
    assert client.post("/api/definitions", json=payload).status_code == 200
    fetched = client.get("/api/definitions/good").json()
    assert fetched["document"]["fields"][0]["pattern"] == r"\d+"


def test_definition_endpoint_accepts_empty_pattern_as_none(client):
    """An empty string is normalized to None so a cleared editor field
    doesn't bake an always-matching regex into the JSON."""
    payload = {
        "document": {
            "document_type": "Empty",
            "fields": [{"name": "x", "pattern": ""}],
        }
    }
    assert client.post("/api/definitions", json=payload).status_code == 200
    fetched = client.get("/api/definitions/empty").json()
    assert fetched["document"]["fields"][0]["pattern"] is None
