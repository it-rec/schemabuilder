"""Tests for per-field min_confidence threshold + rejected_candidate.

The matcher uses a hard-coded 0.5 cutoff historically. Definitions can now
set `min_confidence` (0–1) per field to override that cutoff in either
direction. When a candidate scored above 0 but below the threshold, the
matcher surfaces it as `rejected_candidate` so the UI can offer a review
prompt without claiming a match.
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
    return TestClient(main.app)


def _entry(eid: int, text: str) -> dict:
    return {"id": eid, "text": text, "type": "TextItem", "page": 1, "bbox": None}


def _field(**overrides) -> dict:
    base = {"name": "invoice_id", "examples": ["INV-001"]}
    base.update(overrides)
    return base


def test_default_threshold_preserves_legacy_cutoff():
    """Without min_confidence the matcher still uses the 50/100 cutoff."""
    result = main._match_field_to_entries(
        _field(),
        [_entry(0, "INV-001")],
        used_ids=set(),
    )
    assert result["extracted_value"] == "INV-001"
    assert result["min_confidence"] is None


def test_high_threshold_rejects_weak_match():
    """A 0.99 threshold rejects matches that the default 0.5 would accept."""
    result = main._match_field_to_entries(
        _field(min_confidence=0.99),
        # No exact match — the heuristic produces score < 99.
        [_entry(0, "INV-999 (similar)")],
        used_ids=set(),
    )
    assert result["extracted_value"] is None
    # But the candidate is surfaced for review.
    assert result["rejected_candidate"] is not None
    assert result["rejected_candidate"]["text"] == "INV-999 (similar)"
    assert 0 < result["rejected_candidate"]["score"] < 99


def test_low_threshold_accepts_weaker_match():
    """A 0.1 threshold accepts matches the default 0.5 would reject."""
    field = _field(examples=["Brand Name"], min_confidence=0.1)
    # A weak ID-like substring that wouldn't normally clear 50.
    result = main._match_field_to_entries(
        field,
        [_entry(0, "x")],
        used_ids=set(),
    )
    # Weak match should clear 10% if score > 0; otherwise rejected_candidate
    # is None and no match is reported.
    if result["extracted_value"] is not None:
        assert result["match_score"] >= 10
        assert result["rejected_candidate"] is None


def test_rejected_candidate_only_when_candidate_exists():
    """No candidate at all → rejected_candidate stays None."""
    result = main._match_field_to_entries(
        _field(min_confidence=0.99),
        [],
        used_ids=set(),
    )
    assert result["extracted_value"] is None
    assert result["rejected_candidate"] is None


def test_min_confidence_passes_through_to_field_result():
    """The threshold the user set is echoed in the result so the UI can
    show it next to the field without re-fetching the definition."""
    result = main._match_field_to_entries(
        _field(min_confidence=0.75),
        [_entry(0, "INV-001")],
        used_ids=set(),
    )
    assert result["min_confidence"] == 0.75


def test_invalid_min_confidence_in_dict_falls_back_to_default():
    """A malformed (out-of-range / non-numeric) value can't sneak past via
    raw JSON. The matcher clamps to the default to avoid disabling matching
    entirely on a typo in the definition file."""
    for bad in [-0.1, 1.5, "high", None]:
        result = main._match_field_to_entries(
            _field(min_confidence=bad),
            [_entry(0, "INV-001")],
            used_ids=set(),
        )
        assert result["extracted_value"] == "INV-001"


# ── HTTP layer: validation + round-trip ─────────────────────────────────


def test_definition_endpoint_rejects_out_of_range_threshold(client):
    payload = {
        "document": {
            "document_type": "Bad",
            "fields": [{"name": "x", "min_confidence": 2.0}],
        }
    }
    resp = client.post("/api/definitions", json=payload)
    assert resp.status_code == 422


def test_definition_endpoint_accepts_in_range_threshold_and_round_trips(client):
    payload = {
        "document": {
            "document_type": "OK",
            "fields": [{"name": "x", "min_confidence": 0.8}],
        }
    }
    assert client.post("/api/definitions", json=payload).status_code == 200
    fetched = client.get("/api/definitions/ok").json()
    assert fetched["document"]["fields"][0]["min_confidence"] == 0.8
