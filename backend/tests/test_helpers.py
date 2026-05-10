"""Unit tests for pure helpers in main.py.

These exercise the cache-keying, slug, and matcher logic without loading
Docling models or rendering any PDFs.
"""
import os
import time
from pathlib import Path

import pytest

import main


# ── file signature & document id ─────────────────────────────────────────


def test_document_id_is_stable_per_filename():
    assert main._get_document_id("invoice.pdf") == main._get_document_id("invoice.pdf")
    assert main._get_document_id("a.pdf") != main._get_document_id("b.pdf")


def test_file_signature_changes_after_write(tmp_path: Path):
    f = tmp_path / "sample.txt"
    f.write_text("v1")
    sig1 = main._file_signature(f)
    # mtime resolution on some filesystems is coarse; wait briefly then
    # overwrite so the (mtime_ns, size) tuple is guaranteed to differ.
    time.sleep(0.01)
    f.write_text("v2-longer")
    sig2 = main._file_signature(f)
    assert sig1 != sig2
    assert sig2 != ()


def test_file_signature_missing_file_returns_empty(tmp_path: Path):
    assert main._file_signature(tmp_path / "does-not-exist") == ()


# ── slug for definition ids ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "doc_type,expected",
    [
        ("Invoice", "invoice"),
        ("Purchase Order", "purchase_order"),
        ("  Weird---Name!!!  ", "weird___name"),
        ("123 Foo", "123_foo"),
        ("???", ""),
    ],
)
def test_slugify_document_type(doc_type, expected):
    assert main._slugify_document_type(doc_type) == expected


# ── _match_field_to_entries scoring ──────────────────────────────────────


def _entry(eid, text, etype="TextItem"):
    """Build an entry shaped like _extract_text emits."""
    return {
        "id": eid,
        "text": text,
        "type": etype,
        "page": 1,
        "bbox": None,
        "_text_lower": text.lower(),
        "_text_stripped_lower": text.strip().lower(),
    }


def test_match_field_prefers_exact_example_over_substring():
    field = {"name": "invoice_id", "examples": ["INV-2024-001"]}
    entries = [
        _entry(0, "see invoice INV-2024-001 below"),  # substring match
        _entry(1, "INV-2024-001"),                    # exact match
    ]
    result = main._match_field_to_entries(field, entries, used_ids=set())
    assert result["matched_entry_id"] == 1
    assert result["confidence"] == pytest.approx(0.95)


def test_match_field_prefers_exact_option_over_substring():
    field = {"name": "currency", "available_options": ["AB", "ABC"]}
    entries = [
        _entry(0, "ABC"),  # exact match for "ABC"
        _entry(1, "AB and other stuff"),  # exact match for "AB"
    ]
    result = main._match_field_to_entries(field, entries, used_ids=set())
    # Both score 90 (exact). The first encountered wins because score must
    # *exceed* best to swap.
    assert result["matched_entry_id"] == 0


def test_match_field_date_heuristic_kicks_in():
    field = {"name": "invoice_date", "examples": ["2024-02-04"]}
    entries = [_entry(0, "Issued on 2024-02-04 by ACME")]
    result = main._match_field_to_entries(field, entries, used_ids=set())
    # 2024-02-04 substring of example → 80; date heuristic upgrades to 85.
    assert result["matched_entry_id"] == 0
    assert result["confidence"] >= 0.85


def test_match_field_marks_used_ids():
    field = {"name": "currency", "available_options": ["USD"]}
    entries = [_entry(0, "USD")]
    used: set = set()
    main._match_field_to_entries(field, entries, used)
    assert 0 in used


def test_match_field_returns_no_match_below_threshold():
    field = {"name": "invoice_id", "examples": ["INV-2024-001"]}
    entries = [_entry(0, "totally unrelated paragraph of body text")]
    result = main._match_field_to_entries(field, entries, used_ids=set())
    assert result["matched_entry_id"] is None
    assert result["extracted_value"] is None
    assert result["confidence"] == 0


# ── definitions cache invalidation ───────────────────────────────────────


def test_definitions_dir_signature_reflects_mtime(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(main, "DEFINITIONS_DIR", tmp_path)
    sig0 = main._definitions_dir_signature()
    (tmp_path / "a.json").write_text('{"document": {"document_type": "A", "fields": []}}')
    sig1 = main._definitions_dir_signature()
    assert sig0 != sig1


# ── CORS env parsing ─────────────────────────────────────────────────────


def test_cors_origins_default(monkeypatch):
    monkeypatch.delenv("CORS_ALLOW_ORIGINS", raising=False)
    assert main._parse_cors_origins() == ["http://localhost:3000"]


def test_cors_origins_from_env(monkeypatch):
    monkeypatch.setenv(
        "CORS_ALLOW_ORIGINS",
        "http://a.example , http://b.example,, http://c.example",
    )
    assert main._parse_cors_origins() == [
        "http://a.example",
        "http://b.example",
        "http://c.example",
    ]


# ── LRU eviction ─────────────────────────────────────────────────────────


def test_lru_set_evicts_oldest():
    from collections import OrderedDict

    cache: "OrderedDict[str, int]" = OrderedDict()
    main._lru_set(cache, "a", 1, max_size=2)
    main._lru_set(cache, "b", 2, max_size=2)
    main._lru_set(cache, "c", 3, max_size=2)  # should evict "a"
    assert list(cache.keys()) == ["b", "c"]


def test_lru_get_promotes_entry():
    from collections import OrderedDict

    cache: "OrderedDict[str, int]" = OrderedDict()
    main._lru_set(cache, "a", 1, max_size=2)
    main._lru_set(cache, "b", 2, max_size=2)
    assert main._lru_get(cache, "a") == 1  # promotes "a" to most-recent
    main._lru_set(cache, "c", 3, max_size=2)  # evicts "b", not "a"
    assert "a" in cache
    assert "b" not in cache


def test_lru_eviction_callback_runs():
    from collections import OrderedDict

    evicted = []
    cache: "OrderedDict[str, int]" = OrderedDict()
    main._lru_set(cache, "a", 10, max_size=1, on_evict=lambda k, v: evicted.append((k, v)))
    main._lru_set(cache, "b", 20, max_size=1, on_evict=lambda k, v: evicted.append((k, v)))
    assert evicted == [("a", 10)]
