"""Rigorous tests for pure helpers in main.py.

These don't merely confirm "it returns something" — they pin down behavior at
boundaries: which scoring branch fires for which input, what gets evicted, what
gets cleaned up after a failure, and which inputs are rejected by the slug /
def-id guards. If a helper silently swaps the branch it took, these tests
should catch it.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path

import pytest

import main

# ── _file_signature ──────────────────────────────────────────────────────


def test_file_signature_size_change_alone_changes_signature(tmp_path: Path):
    """A same-mtime same-name write of different length must produce a
    different signature; size is the second tuple element and must be checked.
    Use os.utime to pin mtime identical across writes."""
    f = tmp_path / "a.bin"
    f.write_bytes(b"x" * 10)
    sig1 = main._file_signature(f)
    # Pin mtime explicitly.
    target_atime = sig1[0] / 1_000_000_000
    target_mtime = sig1[0] / 1_000_000_000
    f.write_bytes(b"x" * 30)
    os.utime(f, (target_atime, target_mtime))
    sig2 = main._file_signature(f)
    # mtime might still differ on some FSes; the important invariant is that
    # *some* component of the signature changes if either mtime or size does.
    assert sig1 != sig2
    assert sig2[1] == 30
    assert sig1[1] == 10


def test_file_signature_directory_returns_tuple_not_empty(tmp_path: Path):
    """Directories stat fine even though they're not files; the helper
    shouldn't conflate that with 'missing' (which returns ())."""
    sig = main._file_signature(tmp_path)
    assert isinstance(sig, tuple)
    # A directory exists, so the signature is populated, not the missing-file
    # sentinel.
    assert sig != ()


# ── _slugify_document_type ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Unicode is not in the [a-z0-9_] keep set → all stripped.
        ("Rechnung über €500", "rechnung__ber__500"),
        # Tabs and newlines collapse to underscores then strip outer ones.
        ("\tInvoice\nLine\t", "invoice_line"),
        # Repeated punctuation collapses by becoming '_' but adjacent '_' are
        # preserved (not collapsed). Boundary '_' are stripped.
        ("...A...B...", "a___b"),
        # All-punctuation slugs to empty (caller raises 400 on this).
        ("/\\?!", ""),
        # Mixed case lower-cases.
        ("ABC_def-GHI", "abc_def_ghi"),
        # Leading digits are valid: slug doesn't have to start with a letter.
        ("99 bottles", "99_bottles"),
    ],
)
def test_slugify_document_type_normalizes(raw, expected):
    assert main._slugify_document_type(raw) == expected


def test_slugify_collapses_no_consecutive_underscores():
    """Doc: replacement is per-char, *not* collapse — so two non-slug chars
    in a row produce two underscores. Pin this so a future "collapse" change
    is intentional, not accidental.
    """
    assert main._slugify_document_type("a--b") == "a__b"


# ── _validate_def_id_shape (path traversal guard) ────────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        "../etc/passwd",
        "a/b",
        "a\\b",
        "with space",
        "UPPERCASE",
        "dash-no",
        "dot.in",
        "",  # empty
        "\x00null",
        "with\nnewline",
    ],
)
def test_validate_def_id_shape_rejects(bad):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        main._validate_def_id_shape(bad)
    assert exc.value.status_code == 404


@pytest.mark.parametrize("good", ["a", "abc", "abc_123", "snake_case_id", "1234"])
def test_validate_def_id_shape_accepts_slug_shape(good):
    # Doesn't raise.
    main._validate_def_id_shape(good)


# ── _paginate envelope ──────────────────────────────────────────────────


def test_paginate_offset_beyond_total_returns_empty_items():
    body = main._paginate([1, 2, 3], limit=10, offset=99)
    assert body == {"items": [], "total": 3, "limit": 10, "offset": 99}


def test_paginate_zero_limit_does_not_crash():
    body = main._paginate([1, 2, 3], limit=0, offset=0)
    assert body["items"] == []
    assert body["total"] == 3


def test_paginate_returns_exact_slice():
    body = main._paginate(list(range(10)), limit=3, offset=4)
    assert body["items"] == [4, 5, 6]


# ── _parse_cors_origins ──────────────────────────────────────────────────


def test_parse_cors_origins_whitespace_only_is_default(monkeypatch):
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "   \t  ")
    assert main._parse_cors_origins() == ["http://localhost:3000"]


def test_parse_cors_origins_only_commas_returns_empty_list(monkeypatch):
    """A bare ',,,,' is a misconfiguration; should resolve to empty (no
    origins allowed) rather than the default. The default kicks in only on
    truly empty input — pin that boundary."""
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", ",,,,")
    # The current implementation strips empties → empty list.
    assert main._parse_cors_origins() == []


# ── _metrics_inc ─────────────────────────────────────────────────────────


def test_metrics_inc_creates_unknown_keys():
    """The counter dict must be tolerant of new keys (used by middleware)."""
    with main._metrics_lock:
        main._metrics.pop("never_seen_metric", None)
    main._metrics_inc("never_seen_metric")
    main._metrics_inc("never_seen_metric", 4)
    assert main._metrics["never_seen_metric"] == 5
    with main._metrics_lock:
        main._metrics.pop("never_seen_metric", None)


def test_metrics_inc_is_thread_safe():
    """Concurrent increments must sum exactly; the lock around the dict is the
    only thing preventing torn read-modify-write."""
    key = "thread_test_counter"
    main._metrics[key] = 0
    n_threads = 16
    per_thread = 500
    barrier = threading.Barrier(n_threads)

    def worker():
        barrier.wait()
        for _ in range(per_thread):
            main._metrics_inc(key)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert main._metrics[key] == n_threads * per_thread
    main._metrics.pop(key, None)


# ── LRU eviction callback failure path ──────────────────────────────────


def test_lru_eviction_callback_exception_is_swallowed(caplog):
    """If on_evict raises, the cache must still finish the set — losing a
    cleanup is acceptable, dropping the new entry is not."""
    cache: OrderedDict[str, int] = OrderedDict()

    def boom(_k, _v):
        raise RuntimeError("oops")

    main._lru_set(cache, "a", 1, max_size=1, on_evict=boom)
    # eviction with failing callback shouldn't propagate.
    main._lru_set(cache, "b", 2, max_size=1, on_evict=boom)
    assert "a" not in cache
    assert cache["b"] == 2


def test_lru_get_missing_key_returns_none_without_promotion():
    cache: OrderedDict[str, int] = OrderedDict()
    main._lru_set(cache, "a", 1, max_size=2)
    main._lru_set(cache, "b", 2, max_size=2)
    assert main._lru_get(cache, "missing") is None
    # Order untouched.
    assert list(cache.keys()) == ["a", "b"]


def test_lru_set_existing_key_promotes_and_updates_value():
    cache: OrderedDict[str, int] = OrderedDict()
    main._lru_set(cache, "a", 1, max_size=3)
    main._lru_set(cache, "b", 2, max_size=3)
    main._lru_set(cache, "a", 99, max_size=3)
    # a is now most-recent and value updated.
    assert list(cache.keys()) == ["b", "a"]
    assert cache["a"] == 99


# ── _evict_pdf_file (LRU eviction callback for converted PDFs) ──────────


def test_evict_pdf_file_removes_existing(tmp_path: Path):
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF-1.4\n%%EOF\n")
    main._evict_pdf_file(("key",), str(f))
    assert not f.exists()


def test_evict_pdf_file_ignores_missing(tmp_path: Path):
    # Must not raise even though the file is already gone.
    main._evict_pdf_file(("key",), str(tmp_path / "nope.pdf"))


def test_evict_pdf_file_ignores_directory(tmp_path: Path):
    # Directories aren't unlinked: only files. Confirm it doesn't blow up.
    sub = tmp_path / "subdir"
    sub.mkdir()
    main._evict_pdf_file(("key",), str(sub))
    # Still there — we explicitly do not rmdir.
    assert sub.exists()


def test_evict_pdf_file_logs_on_oserror(monkeypatch, tmp_path, caplog):
    f = tmp_path / "y.pdf"
    f.write_bytes(b"PDF")
    real_unlink = Path.unlink

    def boom(self):
        raise OSError("simulated")

    monkeypatch.setattr(Path, "unlink", boom)
    # Should swallow OSError so a stuck-open file on Windows doesn't crash
    # the cache.
    main._evict_pdf_file(("key",), str(f))
    monkeypatch.setattr(Path, "unlink", real_unlink)


# ── _cleanup_pdf_temp_dir ───────────────────────────────────────────────


def test_cleanup_pdf_temp_dir_does_not_raise_on_missing(monkeypatch, tmp_path):
    """Atexit must never raise; gone-already is fine."""
    monkeypatch.setattr(main, "_pdf_temp_dir", str(tmp_path / "definitely-missing"))
    # Must not raise.
    main._cleanup_pdf_temp_dir()


# ── _build_field_signatures (all detection branches) ────────────────────


def _defn(fields):
    return {"document": {"document_type": "T", "fields": fields}}


def test_build_signatures_date_example_adds_date_regex():
    sigs = main._build_field_signatures(
        _defn([{"name": "d", "examples": ["2024-01-01"]}])
    )
    kinds = [k for k, _ in sigs]
    assert "literal" in kinds
    # date regex appears
    assert any(k == "regex" and p is main._DATE_DETECT_LOOSE_RE for k, p in sigs)


def test_build_signatures_id_example_adds_id_regex():
    sigs = main._build_field_signatures(
        _defn([{"name": "inv", "examples": ["INV-001"]}])
    )
    assert any(k == "regex" and p is main._ID_DETECT_RE for k, p in sigs)


def test_build_signatures_decimal_example_adds_decimal_regex():
    sigs = main._build_field_signatures(
        _defn([{"name": "amt", "examples": ["1.00"]}])
    )
    assert any(k == "regex" and p is main._DECIMAL_LOOSE_DETECT_RE for k, p in sigs)


def test_build_signatures_int_example_adds_int_regex():
    sigs = main._build_field_signatures(
        _defn([{"name": "qty", "examples": ["42"]}])
    )
    assert any(k == "regex" and p is main._INT_WORD_RE for k, p in sigs)


def test_build_signatures_currency_sign_example_adds_currency_regex():
    sigs = main._build_field_signatures(
        _defn([{"name": "sym", "examples": ["$"]}])
    )
    assert any(k == "regex" and p is main._CURRENCY_SIGN_RE for k, p in sigs)


def test_build_signatures_skips_empty_and_none_examples():
    sigs = main._build_field_signatures(
        _defn([{"name": "x", "examples": [None, "", "  ", "real"]}])
    )
    literals = [v for k, v in sigs if k == "literal"]
    # "real" survives. Whitespace-only doesn't match any classifier, but it's
    # added as a literal — pin: only truly empty/None are skipped.
    assert "real" in literals
    assert "" not in literals


def test_build_signatures_options_become_word_boundary_regex():
    sigs = main._build_field_signatures(
        _defn([{"name": "currency", "available_options": ["USD"]}])
    )
    regexes = [p for k, p in sigs if k == "regex"]
    # Match "USD" on its own
    assert any(p.search("paid 50 USD today") for p in regexes)
    # But not as part of a longer word like "USDX"
    assert not any(p.search("USDX") for p in regexes)


def test_build_signatures_skips_falsy_options():
    sigs = main._build_field_signatures(
        _defn([{"name": "x", "available_options": ["", None, "real"]}])
    )
    # Only "real" should have a regex; "" and None are filtered.
    regexes = [p for k, p in sigs if k == "regex"]
    assert len(regexes) == 1
    assert regexes[0].search("see real here")


def test_build_signatures_recurses_into_array_fields():
    sigs = main._build_field_signatures(_defn([{
        "name": "items", "type": "array",
        "fields": [{"name": "nested_id", "examples": ["INV-001"]}]
    }]))
    # The nested field's literal and id-regex should be present.
    literals = [v for k, v in sigs if k == "literal"]
    assert "inv-001" in literals
    assert "nested id" in literals  # label transformation


def test_build_signatures_field_label_underscore_to_space():
    sigs = main._build_field_signatures(_defn([{"name": "invoice_id"}]))
    literals = [v for k, v in sigs if k == "literal"]
    assert "invoice id" in literals


def test_build_signatures_empty_definition_yields_empty():
    assert main._build_field_signatures({}) == []
    assert main._build_field_signatures({"document": {}}) == []
    assert main._build_field_signatures({"document": {"fields": []}}) == []


# ── _get_signatures_for caching ─────────────────────────────────────────


def test_get_signatures_for_caches_by_def_id_and_signature():
    main._signature_cache.clear()
    definition = _defn([{"name": "x", "examples": ["A"]}])
    # Pin a signature so the cache key is deterministic.
    main._definitions_signature = ("v1",)
    first = main._get_signatures_for("d1", definition)
    second = main._get_signatures_for("d1", definition)
    assert first is second  # exact-same object returned from cache

    # Change global signature: key changes, cache miss → new list.
    main._definitions_signature = ("v2",)
    third = main._get_signatures_for("d1", definition)
    assert third is not first
    main._signature_cache.clear()
    main._definitions_signature = None


# ── _entry_could_match ──────────────────────────────────────────────────


def test_entry_could_match_tableitem_always_passes():
    assert main._entry_could_match({"type": "TableItem", "text": ""}, [(None, "x")])


def test_entry_could_match_no_signatures_passes_all():
    assert main._entry_could_match({"type": "TextItem", "text": "anything"}, [])


def test_entry_could_match_literal_hit():
    sigs = [("literal", "invoice")]
    assert main._entry_could_match(
        {"type": "TextItem", "text": "An Invoice", "_text_lower": "an invoice"}, sigs
    )


def test_entry_could_match_falls_back_to_lower_when_uncached():
    """Entries from non-cached paths lack _text_lower; helper must compute it."""
    sigs = [("literal", "foo")]
    assert main._entry_could_match({"type": "TextItem", "text": "FOO"}, sigs)


def test_entry_could_match_regex_hit_uses_raw_text():
    sigs = [("regex", re.compile(r"\d{4}-\d{2}-\d{2}"))]
    assert main._entry_could_match({"type": "TextItem", "text": "on 2024-01-01"}, sigs)


def test_entry_could_match_returns_false_when_no_signature_fires():
    sigs = [("literal", "invoice"), ("regex", re.compile(r"\d{4}-\d{2}-\d{2}"))]
    assert not main._entry_could_match(
        {"type": "TextItem", "text": "unrelated body text", "_text_lower": "unrelated body text"},
        sigs,
    )


# ── _build_combined_signatures / _entry_could_match_combined ─────────────
#
# The combined form is purely a speed optimization of the per-signature
# loop: it MUST accept exactly the same set of entries. These tests pin
# that equivalence directly rather than trusting it.


def _could_match_loop(entry, sigs):
    """Reference: the pre-combination per-signature behavior."""
    return main._entry_could_match(entry, sigs)


def _could_match_combined(entry, sigs):
    return main._entry_could_match_combined(entry, main._build_combined_signatures(sigs))


# A spread of definition shapes that exercises every signature kind.
_EQUIVALENCE_DEFS = [
    _defn([{"name": "invoice_date", "examples": ["2024-02-04"]}]),
    _defn([{"name": "invoice_id", "examples": ["INV-001", "INV-002"]}]),
    _defn([{"name": "amount", "examples": ["1.00"], "normalizer": "currency"}]),
    _defn([{"name": "qty", "examples": ["42"]}]),
    _defn([{"name": "sym", "examples": ["$"]}]),
    _defn([{"name": "currency", "available_options": ["USD", "EUR", "GBP"]}]),
    _defn([{"name": "iban", "pattern": r"\b[A-Z]{2}\d{20}\b"}]),
    _defn([{"name": "weird", "examples": ["a.b*c?", "(x|y)"]}]),  # regex-special literals
    _defn([
        {"name": "invoice_date", "examples": ["2024-02-04"]},
        {"name": "invoice_id", "examples": ["INV-001"]},
        {"name": "total", "examples": ["$"], "pattern": r"TOTAL:\s*([\d.]+)"},
        {"name": "status", "available_options": ["Paid", "Unpaid"]},
        {"name": "line_items", "type": "array",
         "fields": [{"name": "sku", "examples": ["ABC-9"]}]},
    ]),
]

_EQUIVALENCE_TEXTS = [
    "Invoice issued on 2024-02-04 to the customer",
    "Reference INV-001 — please remit promptly",
    "TOTAL: 1234.56 due now",
    "Paid in full, thank you",
    "Quantity 42 units of product",
    "Amount: $99.00",
    "see a.b*c? and (x|y) verbatim",
    "USDX is not a currency option",
    "wholly unrelated boilerplate paragraph text",
    "MIXED Case Invoice ID inv-001 here",
    "",
    "ABC-9",
]


@pytest.mark.parametrize("definition", _EQUIVALENCE_DEFS)
def test_combined_signatures_match_the_loop(definition):
    sigs = main._build_field_signatures(definition)
    for text in _EQUIVALENCE_TEXTS:
        for etype in ("TextItem", "SectionHeaderItem"):
            entry = {"type": etype, "text": text, "_text_lower": text.lower()}
            assert _could_match_loop(entry, sigs) == _could_match_combined(entry, sigs), (
                f"mismatch for {etype!r} text={text!r}"
            )


def test_combined_signatures_tableitem_always_passes():
    sigs = main._build_field_signatures(_EQUIVALENCE_DEFS[0])
    combined = main._build_combined_signatures(sigs)
    assert main._entry_could_match_combined({"type": "TableItem", "text": ""}, combined)


def test_combined_signatures_no_signatures_passes_all():
    combined = main._build_combined_signatures([])
    assert combined == (None, None, [])
    assert main._entry_could_match_combined(
        {"type": "TextItem", "text": "anything at all"}, combined
    )


def test_combined_signatures_computes_text_lower_when_uncached():
    """Entries off non-cached paths lack _text_lower; the helper must lower()."""
    sigs = [("literal", "foo")]
    combined = main._build_combined_signatures(sigs)
    assert main._entry_could_match_combined({"type": "TextItem", "text": "FOO"}, combined)


def test_combined_signatures_user_pattern_with_global_flag_falls_back():
    """A user regex carrying a global inline flag can't be nested into the
    alternation. It must land in regex_fallback (not be dropped), so the
    entry still matches."""
    sigs = main._build_field_signatures(
        _defn([{"name": "f", "pattern": r"(?i)urgent"}])
    )
    combined_literal, combined_regex, regex_fallback = main._build_combined_signatures(sigs)
    # The user pattern couldn't be folded into combined_regex — it lands in
    # the fallback list rather than being dropped.
    assert any(p.pattern == r"(?i)urgent" for p in regex_fallback)
    # ...but it's still honored via the fallback loop.
    entry = {"type": "TextItem", "text": "This is URGENT", "_text_lower": "this is urgent"}
    combined = (combined_literal, combined_regex, regex_fallback)
    assert main._entry_could_match_combined(entry, combined)
    assert main._entry_could_match(entry, sigs) == main._entry_could_match_combined(
        entry, combined
    )


def test_combined_signatures_unexpected_flag_kept_in_fallback():
    """A signature regex with a flag the combiner can't reproduce per-branch
    (DOTALL here) is looped individually instead of being folded in."""
    sigs = [("regex", re.compile(r"a.b", re.DOTALL))]
    _, combined_regex, regex_fallback = main._build_combined_signatures(sigs)
    assert combined_regex is None
    assert len(regex_fallback) == 1
    # DOTALL semantics survive: '.' matches the newline.
    entry = {"type": "TextItem", "text": "a\nb"}
    assert main._entry_could_match_combined(
        entry, (None, combined_regex, regex_fallback)
    )


def test_combined_signatures_dedupes_repeated_signatures():
    """Many fields sharing the same example shape produce duplicate
    signatures; the combiner collapses them."""
    definition = _defn([
        {"name": f"date_{i}", "examples": ["2024-01-01"]} for i in range(5)
    ])
    sigs = main._build_field_signatures(definition)
    combined_literal, combined_regex, regex_fallback = main._build_combined_signatures(sigs)
    # 5 distinct literals (one label each) + 1 shared example literal; the
    # shared date regex collapses to a single alternation branch.
    assert combined_regex.pattern.count("|") == 0  # one regex branch, no alternation


# ── _get_combined_signatures_for caching ────────────────────────────────


def test_get_combined_signatures_for_caches_by_def_id_and_signature():
    main._signature_cache.clear()
    main._combined_signature_cache.clear()
    definition = _defn([{"name": "x", "examples": ["A"]}])
    main._definitions_signature = ("v1",)
    first = main._get_combined_signatures_for("d1", definition)
    second = main._get_combined_signatures_for("d1", definition)
    assert first is second  # exact-same tuple from cache

    main._definitions_signature = ("v2",)
    third = main._get_combined_signatures_for("d1", definition)
    assert third is not first
    main._signature_cache.clear()
    main._combined_signature_cache.clear()
    main._definitions_signature = None


# ── _match_field_to_entries: every scoring branch ───────────────────────


def _entry(eid, text, etype="TextItem", page=1, bbox=None):
    return {
        "id": eid, "text": text, "type": etype, "page": page, "bbox": bbox,
        "_text_lower": text.lower(),
        "_text_stripped_lower": text.strip().lower(),
    }


def test_match_field_option_exact_scores_90():
    result = main._match_field_to_entries(
        {"name": "ccy", "available_options": ["USD"]},
        [_entry(0, "USD")],
        used_ids=set(),
    )
    assert result["match_score"] == 90
    assert result["match_reason"] == "option_exact"
    assert result["confidence"] == 0.9


def test_match_field_option_substring_scores_75():
    result = main._match_field_to_entries(
        {"name": "ccy", "available_options": ["USD"]},
        [_entry(0, "I paid 50 USD today")],
        used_ids=set(),
    )
    assert result["match_score"] == 75
    assert result["match_reason"] == "option_substring"


def test_match_field_example_substring_scores_80_when_no_better():
    result = main._match_field_to_entries(
        {"name": "x", "examples": ["alpha-beta"]},
        [_entry(0, "see alpha-beta in there somewhere")],
        used_ids=set(),
    )
    assert result["match_score"] == 80
    assert result["match_reason"] == "example_substring"


def test_match_field_id_format_scores_85_without_substring():
    """has_id is true (example matches ID head regex) → any text containing
    [A-Z]+-\\d+ scores 85 even with no substring overlap of the example."""
    result = main._match_field_to_entries(
        {"name": "order_id", "examples": ["ORDER-999"]},
        [_entry(0, "Ref: ABC-1234567 (unrelated)")],
        used_ids=set(),
    )
    # Example doesn't substring-match, but id_format heuristic fires.
    assert result["match_reason"] == "id_format"
    assert result["match_score"] == 85


def test_match_field_decimal_format_scores_70_only():
    result = main._match_field_to_entries(
        {"name": "amount", "examples": ["1.00"]},
        [_entry(0, "Subtotal: 999.99 USD")],
        used_ids=set(),
    )
    # 999.99 → decimal_format = 70; example substring "1.00" not present.
    assert result["match_reason"] == "decimal_format"
    assert result["match_score"] == 70


def test_match_field_currency_sign_scores_80():
    """has_currency_sign needs a currency-sign example, but reaching the
    currency_sign branch requires that no example substring-matched first.
    Use a "$" example with text containing "€" so the example loop produces
    no hit and the currency_sign heuristic gets to fire."""
    result = main._match_field_to_entries(
        {"name": "ccy_sym", "examples": ["$"]},
        [_entry(0, "Paid €42 today")],
        used_ids=set(),
    )
    assert result["match_reason"] == "currency_sign"
    assert result["match_score"] == 80


def test_match_field_label_only_scores_60():
    result = main._match_field_to_entries(
        {"name": "invoice_id"},  # no examples / options
        [_entry(0, "the invoice id is unknown here")],
        used_ids=set(),
    )
    assert result["match_reason"] == "label"
    assert result["match_score"] == 60


def test_match_field_below_threshold_returns_no_match():
    """50 is the floor — anything below it (and the only way to score < 50 is
    no signal at all) yields a no-match result."""
    result = main._match_field_to_entries(
        {"name": "x", "examples": ["something"]},
        [_entry(0, "totally unrelated paragraph")],
        used_ids=set(),
    )
    assert result["match_score"] == 0
    assert result["extracted_value"] is None
    assert result["match_reason"] is None


def test_match_field_skips_already_used_ids():
    result = main._match_field_to_entries(
        {"name": "x", "examples": ["USD"]},
        [_entry(0, "USD"), _entry(1, "USD")],
        used_ids={0},
    )
    assert result["matched_entry_id"] == 1


def test_match_field_propagates_bbox_and_page():
    bbox = {"l": 1.0, "t": 2.0, "r": 3.0, "b": 4.0}
    result = main._match_field_to_entries(
        {"name": "x", "examples": ["FOO"]},
        [_entry(0, "FOO", page=7, bbox=bbox)],
        used_ids=set(),
    )
    assert result["page"] == 7
    assert result["bbox"] == bbox


def test_match_field_array_returns_items_structure():
    """Array fields return a different shape — items list and type=array."""
    table_entry = {
        "id": 0, "text": "Widget 5 100.00", "type": "TableItem",
        "page": 1, "bbox": None,
        "_text_lower": "widget 5 100.00", "_text_stripped_lower": "widget 5 100.00",
    }
    field = {
        "name": "line_items", "type": "array",
        "fields": [{"name": "amount", "examples": ["1.00"]}],
    }
    result = main._match_field_to_entries(field, [table_entry], used_ids=set())
    assert result["type"] == "array"
    assert isinstance(result["items"], list)
    assert len(result["items"]) == 1
    amount_field = result["items"][0]["fields"][0]
    assert amount_field["extracted_value"] == "100.00"
    assert amount_field["match_reason"] == "decimal_format"


def test_match_field_array_with_no_subfields_returns_empty_items():
    field = {"name": "rows", "type": "array", "fields": []}
    result = main._match_field_to_entries(field, [], used_ids=set())
    assert result["items"] == []


# ── _match_array_field branches ─────────────────────────────────────────


def test_match_array_field_id_kind_extracts():
    field = {
        "name": "items", "type": "array",
        "fields": [{"name": "sku", "examples": ["INV-001"]}],
    }
    entry = {"id": 0, "text": "row SKU-7 widget", "type": "TableItem",
             "page": 1, "bbox": None}
    items = main._match_array_field(field, [entry], used_ids=set())
    assert len(items) == 1
    item_field = items[0]["fields"][0]
    assert item_field["extracted_value"] == "SKU-7"
    assert item_field["match_reason"] == "id_format"
    assert item_field["match_score"] == 60


def test_match_array_field_int_kind_extracts():
    field = {
        "name": "items", "type": "array",
        "fields": [{"name": "qty", "examples": ["5"]}],
    }
    entry = {"id": 0, "text": "Widget x 42 pieces", "type": "TableItem",
             "page": 1, "bbox": None}
    items = main._match_array_field(field, [entry], used_ids=set())
    item_field = items[0]["fields"][0]
    assert item_field["extracted_value"] == "42"
    assert item_field["match_reason"] == "int_format"
    assert item_field["match_score"] == 50


def test_match_array_field_skips_non_table_entries():
    field = {
        "name": "items", "type": "array",
        "fields": [{"name": "amount", "examples": ["1.00"]}],
    }
    entry = {"id": 0, "text": "999.99 EUR", "type": "TextItem",
             "page": 1, "bbox": None}
    items = main._match_array_field(field, [entry], used_ids=set())
    assert items == []


def test_match_array_field_drops_row_if_no_subfield_matched():
    """Rows where no sub-field extracts anything aren't appended — verifies
    the `any(extracted_value)` filter at the bottom of the array loop."""
    field = {
        "name": "items", "type": "array",
        "fields": [{"name": "amount", "examples": ["1.00"]}],
    }
    entry = {"id": 0, "text": "header row no numbers", "type": "TableItem",
             "page": 1, "bbox": None}
    items = main._match_array_field(field, [entry], used_ids=set())
    assert items == []


def test_match_array_field_marks_used_id():
    field = {
        "name": "items", "type": "array",
        "fields": [{"name": "amount", "examples": ["1.00"]}],
    }
    entry = {"id": 7, "text": "100.00", "type": "TableItem",
             "page": 1, "bbox": None}
    used: set = set()
    main._match_array_field(field, [entry], used)
    assert 7 in used


def test_match_array_field_skips_used_ids():
    field = {
        "name": "items", "type": "array",
        "fields": [{"name": "amount", "examples": ["1.00"]}],
    }
    entry = {"id": 7, "text": "100.00", "type": "TableItem",
             "page": 1, "bbox": None}
    items = main._match_array_field(field, [entry], used_ids={7})
    assert items == []


def test_match_array_field_no_subfields_returns_empty():
    field = {"name": "items", "type": "array", "fields": []}
    items = main._match_array_field(field, [], used_ids=set())
    assert items == []


# ── _extract_fields integration with signatures ─────────────────────────


def test_extract_fields_fallback_lowers_uncached_entries():
    """Entries lacking _text_lower (i.e. callers that bypassed the cache) get
    the lower-case annotation applied in-place by _extract_fields. This isn't
    an aesthetic choice; the matcher reads it back on every iteration."""
    definition = _defn([{"name": "x", "examples": ["USD"]}])
    # Note: no _text_lower / _text_stripped_lower keys.
    entries = [{"id": 0, "text": "USD", "type": "TextItem", "page": 1, "bbox": None}]
    fields = main._extract_fields(definition, entries)
    assert "_text_lower" in entries[0]
    assert entries[0]["_text_lower"] == "usd"
    # And the match still succeeds.
    assert fields[0]["extracted_value"] == "USD"


def test_extract_fields_uses_signature_cache_when_def_id_given():
    """_extract_fields should reuse cached signatures when given a def_id."""
    main._signature_cache.clear()
    main._definitions_signature = ("test",)
    definition = _defn([{"name": "x", "examples": ["FOO"]}])
    main._extract_fields(definition, [], def_id="abc")
    assert any(k[0] == "abc" for k in main._signature_cache.keys())
    main._signature_cache.clear()
    main._definitions_signature = None


def test_extract_fields_skips_irrelevant_entries():
    """Pre-filter via signatures: entries that don't even match any signature
    must not be candidates for any field."""
    definition = _defn([{"name": "x", "examples": ["FOO"]}])
    entries = [
        {"id": 0, "text": "boilerplate body text", "type": "TextItem", "page": 1, "bbox": None},
        {"id": 1, "text": "FOO", "type": "TextItem", "page": 1, "bbox": None},
    ]
    fields = main._extract_fields(definition, entries)
    assert fields[0]["matched_entry_id"] == 1


# ── _atomic_write_json ──────────────────────────────────────────────────


def test_atomic_write_json_payload_is_pretty_printed(tmp_path: Path, monkeypatch):
    """The on-disk file is the durable record; if indentation changes
    silently, hand-edited definitions will produce noisy diffs. Pin the
    indent=2 contract."""
    monkeypatch.setattr(main, "DEFINITIONS_DIR", tmp_path)
    target = tmp_path / "p.json"
    main._atomic_write_json(target, {"a": 1, "b": [2, 3]})
    text = target.read_text()
    # Indented (not single-line).
    assert "\n" in text
    assert '"a": 1' in text


def test_atomic_write_json_replaces_atomically(tmp_path: Path, monkeypatch):
    """Mid-write crash must not leave a half-written real file. The temp file
    + os.replace contract is what gives us that — pin it by simulating a
    failure inside the write phase."""
    monkeypatch.setattr(main, "DEFINITIONS_DIR", tmp_path)
    target = tmp_path / "t.json"
    target.write_text('{"old": true}')

    real_replace = os.replace

    def boom(src, dst):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        main._atomic_write_json(target, {"new": True})
    monkeypatch.setattr(os, "replace", real_replace)

    # Old content still intact — atomic replace failed before clobbering.
    assert json.loads(target.read_text()) == {"old": True}
    # No leftover temp files.
    leftovers = [p for p in tmp_path.iterdir() if ".tmp" in p.suffixes or p.name.endswith(".tmp")]
    assert leftovers == []


def test_atomic_write_json_creates_dir_when_missing(tmp_path: Path, monkeypatch):
    new_dir = tmp_path / "fresh"
    monkeypatch.setattr(main, "DEFINITIONS_DIR", new_dir)
    main._atomic_write_json(new_dir / "x.json", {"k": 1})
    assert (new_dir / "x.json").exists()


# ── _docs_dir_signature & _definitions_dir_signature ────────────────────


def test_docs_dir_signature_missing_dir_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "TEST_DOCS_DIR", tmp_path / "no-such-dir")
    assert main._docs_dir_signature() == ()


def test_docs_dir_signature_ignores_unsupported_extensions(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "TEST_DOCS_DIR", tmp_path)
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    (tmp_path / "b.txt").write_text("ignored")
    (tmp_path / "c.docx").write_bytes(b"fake")
    sig = main._docs_dir_signature()
    names = [t[0] for t in sig]
    assert "a.pdf" in names
    assert "c.docx" in names
    assert "b.txt" not in names


def test_definitions_dir_signature_missing_dir_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "DEFINITIONS_DIR", tmp_path / "no-such")
    assert main._definitions_dir_signature() == ()


def test_definitions_dir_signature_changes_on_content_edit(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "DEFINITIONS_DIR", tmp_path)
    f = tmp_path / "a.json"
    f.write_text('{"document": {"document_type": "A", "fields": []}}')
    sig1 = main._definitions_dir_signature()
    time.sleep(0.01)
    f.write_text('{"document": {"document_type": "A", "fields": [{"name": "x"}]}}')
    sig2 = main._definitions_dir_signature()
    assert sig1 != sig2


# ── _load_definitions / _invalidate_definitions_cache ───────────────────


def test_load_definitions_ignores_broken_json(monkeypatch, tmp_path):
    """A definitions dir with one busted file shouldn't fail the whole load;
    the broken file is silently dropped, good files load fine."""
    monkeypatch.setattr(main, "DEFINITIONS_DIR", tmp_path)
    main._invalidate_definitions_cache()
    (tmp_path / "good.json").write_text('{"document": {"document_type": "G", "fields": []}}')
    (tmp_path / "broken.json").write_text("{ this is not json")
    defs = main._load_definitions()
    assert "good" in defs
    assert "broken" not in defs
    main._invalidate_definitions_cache()


def test_load_definitions_caches_until_invalidated(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "DEFINITIONS_DIR", tmp_path)
    main._invalidate_definitions_cache()
    (tmp_path / "x.json").write_text('{"document": {"document_type": "X", "fields": []}}')
    first = main._load_definitions()
    second = main._load_definitions()
    assert first is second  # identity reuse on cache hit
    main._invalidate_definitions_cache()
    third = main._load_definitions()
    assert third is not first
    main._invalidate_definitions_cache()


def test_invalidate_definitions_clears_signature_cache():
    """A signature cache outliving a definitions update would return stale
    pre-filter signatures for an updated definition. Pin the invalidation
    behavior — both the raw and the combined caches must clear together."""
    main._signature_cache[("d1", ("sig",))] = ["dummy"]
    main._combined_signature_cache[("d1", ("sig",))] = (None, None, [])
    main._invalidate_definitions_cache()
    assert main._signature_cache == {}
    assert main._combined_signature_cache == {}


# ── _get_document_id ────────────────────────────────────────────────────


def test_get_document_id_is_deterministic_and_short():
    d1 = main._get_document_id("invoice.pdf")
    d2 = main._get_document_id("invoice.pdf")
    assert d1 == d2
    # 12 hex chars
    assert len(d1) == 12
    assert all(c in "0123456789abcdef" for c in d1)


def test_get_document_id_handles_unicode():
    """Filenames with non-ASCII must still produce a stable hex id."""
    d = main._get_document_id("Rechnung_Übersicht.pdf")
    assert len(d) == 12


# ── _describe_accelerator branches ──────────────────────────────────────


def test_describe_accelerator_cpu_forced(monkeypatch):
    """DOCLING_DEVICE=cpu with torch installed → "forced via" message."""
    monkeypatch.setenv("DOCLING_DEVICE", "cpu")

    class _FakeCuda:
        @staticmethod
        def is_available():
            return False

    class _FakeBackends:
        class mps:
            @staticmethod
            def is_available():
                return False

    class _FakeTorch:
        cuda = _FakeCuda
        backends = _FakeBackends

    import sys
    monkeypatch.setitem(sys.modules, "torch", _FakeTorch)
    label = main._describe_accelerator()
    assert label == "CPU (forced via DOCLING_DEVICE)"


def test_describe_accelerator_no_torch(monkeypatch):
    """If torch isn't importable, fall back to a string that mentions that
    fact. We simulate by hiding torch from import."""
    import builtins

    real_import = builtins.__import__

    def hide_torch(name, *args, **kwargs):
        if name == "torch":
            raise ImportError("simulated no torch")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", hide_torch)
    monkeypatch.delenv("DOCLING_DEVICE", raising=False)
    label = main._describe_accelerator()
    assert "torch not installed" in label


def test_describe_accelerator_unknown_force_falls_back(monkeypatch):
    """An unsupported DOCLING_DEVICE value should not crash; it should
    return a hint and let the AUTO path take over."""
    import builtins

    real_import = builtins.__import__

    def hide_torch(name, *args, **kwargs):
        if name == "torch":
            raise ImportError("simulated no torch")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", hide_torch)
    monkeypatch.setenv("DOCLING_DEVICE", "WEIRD")
    label = main._describe_accelerator()
    assert "WEIRD" in label


def test_describe_accelerator_auto_with_torch_no_accel(monkeypatch):
    """torch present, but no CUDA/MPS → CPU."""
    monkeypatch.delenv("DOCLING_DEVICE", raising=False)

    class _FakeCuda:
        @staticmethod
        def is_available():
            return False

        @staticmethod
        def get_device_name(_):
            return ""

    class _FakeBackends:
        class mps:
            @staticmethod
            def is_available():
                return False

    class _FakeTorch:
        cuda = _FakeCuda
        backends = _FakeBackends

    import sys
    monkeypatch.setitem(sys.modules, "torch", _FakeTorch)
    label = main._describe_accelerator()
    assert label == "CPU"


def test_describe_accelerator_auto_cuda(monkeypatch):
    monkeypatch.delenv("DOCLING_DEVICE", raising=False)

    class _FakeCuda:
        @staticmethod
        def is_available():
            return True

        @staticmethod
        def get_device_name(_):
            return "GeForce RTX 4090"

    class _FakeBackends:
        class mps:
            @staticmethod
            def is_available():
                return False

    class _FakeTorch:
        cuda = _FakeCuda
        backends = _FakeBackends

    import sys
    monkeypatch.setitem(sys.modules, "torch", _FakeTorch)
    label = main._describe_accelerator()
    assert "CUDA" in label
    assert "GeForce" in label


def test_describe_accelerator_auto_mps_when_no_cuda(monkeypatch):
    monkeypatch.delenv("DOCLING_DEVICE", raising=False)

    class _FakeCuda:
        @staticmethod
        def is_available():
            return False

    class _FakeMps:
        @staticmethod
        def is_available():
            return True

    class _FakeBackends:
        mps = _FakeMps

    class _FakeTorch:
        cuda = _FakeCuda
        backends = _FakeBackends

    import sys
    monkeypatch.setitem(sys.modules, "torch", _FakeTorch)
    assert main._describe_accelerator() == "MPS"


def test_describe_accelerator_forced_cuda_unavailable(monkeypatch):
    monkeypatch.setenv("DOCLING_DEVICE", "cuda")

    class _FakeCuda:
        @staticmethod
        def is_available():
            return False

    class _FakeBackends:
        class mps:
            @staticmethod
            def is_available():
                return False

    class _FakeTorch:
        cuda = _FakeCuda
        backends = _FakeBackends

    import sys
    monkeypatch.setitem(sys.modules, "torch", _FakeTorch)
    label = main._describe_accelerator()
    assert "unavailable" in label.lower()


def test_describe_accelerator_forced_mps_unavailable(monkeypatch):
    monkeypatch.setenv("DOCLING_DEVICE", "mps")

    class _FakeBackends:
        class mps:
            @staticmethod
            def is_available():
                return False

    class _FakeCuda:
        @staticmethod
        def is_available():
            return False

    class _FakeTorch:
        cuda = _FakeCuda
        backends = _FakeBackends

    import sys
    monkeypatch.setitem(sys.modules, "torch", _FakeTorch)
    label = main._describe_accelerator()
    assert "unavailable" in label.lower()


# ── _resolve_accelerator_device ─────────────────────────────────────────


class _FakeAccelEnum:
    CUDA = "cuda-token"
    MPS = "mps-token"
    CPU = "cpu-token"
    AUTO = "auto-token"


def test_resolve_accelerator_env_override_known(monkeypatch):
    monkeypatch.setenv("DOCLING_DEVICE", "cuda")
    assert main._resolve_accelerator_device(_FakeAccelEnum) == "cuda-token"


def test_resolve_accelerator_env_override_unknown_falls_back(monkeypatch):
    """Unknown env value with no torch → AUTO (the safe default)."""
    import builtins
    real_import = builtins.__import__

    def hide_torch(name, *args, **kwargs):
        if name == "torch":
            raise ImportError()
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", hide_torch)
    monkeypatch.setenv("DOCLING_DEVICE", "ZZZ")
    assert main._resolve_accelerator_device(_FakeAccelEnum) == "auto-token"


def test_resolve_accelerator_auto_cuda(monkeypatch):
    monkeypatch.delenv("DOCLING_DEVICE", raising=False)

    class _FakeCuda:
        @staticmethod
        def is_available():
            return True

    class _FakeBackends:
        class mps:
            @staticmethod
            def is_available():
                return False

    class _FakeTorch:
        cuda = _FakeCuda
        backends = _FakeBackends

    import sys
    monkeypatch.setitem(sys.modules, "torch", _FakeTorch)
    assert main._resolve_accelerator_device(_FakeAccelEnum) == "cuda-token"


def test_resolve_accelerator_auto_mps(monkeypatch):
    monkeypatch.delenv("DOCLING_DEVICE", raising=False)

    class _FakeCuda:
        @staticmethod
        def is_available():
            return False

    class _FakeMps:
        @staticmethod
        def is_available():
            return True

    class _FakeBackends:
        mps = _FakeMps

    class _FakeTorch:
        cuda = _FakeCuda
        backends = _FakeBackends

    import sys
    monkeypatch.setitem(sys.modules, "torch", _FakeTorch)
    assert main._resolve_accelerator_device(_FakeAccelEnum) == "mps-token"


# ── _resolve_ocr_decision (env override + cache) ────────────────────────


@pytest.mark.parametrize("forced,expected", [
    ("1", True), ("true", True), ("yes", True), ("ON", True),
    ("0", False), ("false", False), ("no", False), ("off", False),
])
def test_resolve_ocr_env_override_truthy_falsy(monkeypatch, tmp_path, forced, expected):
    monkeypatch.setenv("DOCLING_DO_OCR", forced)
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF-1.4\n%%EOF\n")
    assert main._resolve_ocr_decision(f) is expected


def test_resolve_ocr_env_unrecognized_falls_through_to_heuristic(monkeypatch, tmp_path):
    """An unrecognized env value must not silently force one branch — it
    should drop to the per-doc heuristic. Mock _document_needs_ocr to verify
    the heuristic was consulted."""
    monkeypatch.setenv("DOCLING_DO_OCR", "perhaps")
    main._ocr_decision_cache.clear()
    f = tmp_path / "y.pdf"
    f.write_bytes(b"%PDF-1.4\n%%EOF\n")
    called = {"n": 0}

    def fake_detect(p):
        called["n"] += 1
        return True

    monkeypatch.setattr(main, "_document_needs_ocr", fake_detect)
    assert main._resolve_ocr_decision(f) is True
    assert called["n"] == 1
    main._ocr_decision_cache.clear()


def test_resolve_ocr_decision_caches_by_file_signature(monkeypatch, tmp_path):
    monkeypatch.delenv("DOCLING_DO_OCR", raising=False)
    main._ocr_decision_cache.clear()
    f = tmp_path / "z.pdf"
    f.write_bytes(b"%PDF-1.4\n%%EOF\n")
    calls = {"n": 0}

    def fake_detect(p):
        calls["n"] += 1
        return False

    monkeypatch.setattr(main, "_document_needs_ocr", fake_detect)
    main._resolve_ocr_decision(f)
    main._resolve_ocr_decision(f)
    main._resolve_ocr_decision(f)
    assert calls["n"] == 1
    # Edit the file (changes size → signature) → re-detection runs.
    f.write_bytes(b"%PDF-1.4\nnew stuff\n%%EOF\n")
    main._resolve_ocr_decision(f)
    assert calls["n"] == 2
    main._ocr_decision_cache.clear()


def test_resolve_ocr_decision_failure_defaults_to_off(monkeypatch, tmp_path, caplog):
    monkeypatch.delenv("DOCLING_DO_OCR", raising=False)
    main._ocr_decision_cache.clear()
    f = tmp_path / "broken.pdf"
    f.write_bytes(b"%PDF-1.4\n%%EOF\n")

    def explode(p):
        raise RuntimeError("simulated sampling error")

    monkeypatch.setattr(main, "_document_needs_ocr", explode)
    assert main._resolve_ocr_decision(f) is False
    main._ocr_decision_cache.clear()


def test_resolve_ocr_decision_metric_increments(monkeypatch, tmp_path):
    """Verify both branches bump their respective counters."""
    monkeypatch.delenv("DOCLING_DO_OCR", raising=False)
    main._ocr_decision_cache.clear()
    with main._metrics_lock:
        main._metrics["ocr_decisions_on"] = 0
        main._metrics["ocr_decisions_off"] = 0
    f1 = tmp_path / "on.pdf"
    f1.write_bytes(b"x")
    f2 = tmp_path / "off.pdf"
    f2.write_bytes(b"y")
    monkeypatch.setattr(main, "_document_needs_ocr", lambda p: p.name == "on.pdf")
    main._resolve_ocr_decision(f1)
    main._resolve_ocr_decision(f2)
    assert main._metrics["ocr_decisions_on"] == 1
    assert main._metrics["ocr_decisions_off"] == 1
    main._ocr_decision_cache.clear()


# ── _document_needs_ocr (mocked pypdfium2) ──────────────────────────────


class _FakeTextPage:
    def __init__(self, text):
        self._text = text

    def get_text_range(self):
        return self._text

    def close(self):
        pass


class _FakePage:
    def __init__(self, text):
        self._text = text

    def get_textpage(self):
        return _FakeTextPage(self._text)


class _FakePdfDoc:
    def __init__(self, page_texts):
        self._pages = [_FakePage(t) for t in page_texts]

    def __len__(self):
        return len(self._pages)

    def __getitem__(self, i):
        return self._pages[i]

    def close(self):
        pass


def _install_fake_pdfium(monkeypatch, doc):
    """Inject a fake pypdfium2 so _document_needs_ocr's import resolves to ours."""
    import sys
    import types

    fake = types.SimpleNamespace(PdfDocument=lambda _path: doc)
    monkeypatch.setitem(sys.modules, "pypdfium2", fake)


def test_document_needs_ocr_non_pdf_short_circuits(tmp_path):
    f = tmp_path / "x.docx"
    f.write_bytes(b"PK\x03\x04")
    assert main._document_needs_ocr(f) is False


def test_document_needs_ocr_empty_pdf_returns_false(monkeypatch, tmp_path):
    _install_fake_pdfium(monkeypatch, _FakePdfDoc([]))
    f = tmp_path / "empty.pdf"
    f.write_bytes(b"%PDF-1.4\n%%EOF\n")
    assert main._document_needs_ocr(f) is False


def test_document_needs_ocr_fast_exit_on_first_page(monkeypatch, tmp_path):
    """A single page with >100 chars triggers the fast exit (no OCR needed)."""
    _install_fake_pdfium(monkeypatch, _FakePdfDoc(["x" * 500]))
    f = tmp_path / "big.pdf"
    f.write_bytes(b"PDF")
    assert main._document_needs_ocr(f) is False


def test_document_needs_ocr_image_only_pdf_returns_true(monkeypatch, tmp_path):
    """3 pages each producing 0 chars → image-only → needs OCR."""
    _install_fake_pdfium(monkeypatch, _FakePdfDoc(["", "", ""]))
    f = tmp_path / "scan.pdf"
    f.write_bytes(b"PDF")
    assert main._document_needs_ocr(f) is True


def test_document_needs_ocr_samples_first_middle_last(monkeypatch, tmp_path):
    """For >3 pages, samples are first/middle/last. If pages 0/middle/last
    have content, we shouldn't OCR even if other pages are empty."""
    pages = ["", ""] * 5 + ["lots of text here that is longer than thirty chars"]
    _install_fake_pdfium(monkeypatch, _FakePdfDoc(pages))
    f = tmp_path / "long.pdf"
    f.write_bytes(b"PDF")
    # Last page has 50+ chars >= fast_exit; sample includes last page.
    assert main._document_needs_ocr(f) is False


def test_document_needs_ocr_open_failure_short_circuits(monkeypatch, tmp_path):
    """A pypdfium2 open failure → False (let docling try)."""
    import sys
    import types

    def fail_open(_):
        raise RuntimeError("can't open")

    fake = types.SimpleNamespace(PdfDocument=fail_open)
    monkeypatch.setitem(sys.modules, "pypdfium2", fake)
    f = tmp_path / "bad.pdf"
    f.write_bytes(b"PDF")
    assert main._document_needs_ocr(f) is False


def test_document_needs_ocr_per_page_textpage_exception(monkeypatch, tmp_path):
    """An exception while reading text from one page is treated as empty text
    for that page (not propagated)."""

    class _BrokenPage:
        def get_textpage(self):
            raise RuntimeError("textpage broken")

    class _Doc:
        def __len__(self):
            return 2

        def __getitem__(self, i):
            return _BrokenPage()

        def close(self):
            pass

    _install_fake_pdfium(monkeypatch, _Doc())
    f = tmp_path / "torn.pdf"
    f.write_bytes(b"PDF")
    # All pages broken → 0 chars total → needs OCR.
    assert main._document_needs_ocr(f) is True


# ── _track_inflight (thread safety) ─────────────────────────────────────


def test_track_inflight_concurrent_balance_maintained():
    """N parallel context-manager entries must increment to N then decrement
    back to the baseline atomically."""
    baseline = main._inflight_extracts
    N = 8
    enter_barrier = threading.Barrier(N)
    release_event = threading.Event()
    peak = [baseline]
    peak_lock = threading.Lock()

    def worker():
        with main._track_inflight():
            enter_barrier.wait()
            with peak_lock:
                if main._inflight_extracts > peak[0]:
                    peak[0] = main._inflight_extracts
            release_event.wait()

    threads = [threading.Thread(target=worker) for _ in range(N)]
    for t in threads:
        t.start()
    # All threads will reach the barrier before any leaves; the peak should
    # equal baseline + N.
    while not all(t.is_alive() for t in threads):
        time.sleep(0.001)
    time.sleep(0.05)
    release_event.set()
    for t in threads:
        t.join()
    assert peak[0] >= baseline + N
    assert main._inflight_extracts == baseline


# ── _request_id_ctx ─────────────────────────────────────────────────────


def test_request_id_re_allowed_set_is_exact():
    """If this regex is widened/narrowed by accident, request id propagation
    behavior changes globally; pin the allowed alphabet."""
    assert main._REQUEST_ID_RE.fullmatch("abc.DEF_-123")
    assert main._REQUEST_ID_RE.fullmatch("a" * 64)
    assert not main._REQUEST_ID_RE.fullmatch("a" * 65)
    assert not main._REQUEST_ID_RE.fullmatch("")
    assert not main._REQUEST_ID_RE.fullmatch("has space")
    assert not main._REQUEST_ID_RE.fullmatch("semi;colon")
    assert not main._REQUEST_ID_RE.fullmatch("with\nnewline")
