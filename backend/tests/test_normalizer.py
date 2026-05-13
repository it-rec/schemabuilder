"""Unit + matcher-integration tests for the per-field normalizer slot."""
import pytest

import main
import normalizers


def _entry(eid, text, etype="TextItem"):
    return {
        "id": eid,
        "text": text,
        "type": etype,
        "page": 1,
        "bbox": None,
        "_text_lower": text.lower(),
        "_text_stripped_lower": text.strip().lower(),
    }


# ── normalizers module (unit) ────────────────────────────────────────────


@pytest.mark.parametrize(
    "spec,value,expected",
    [
        ("number", "12", 12),
        ("number", "12.5", 12.5),
        ("number", "1.234,56", 1234.56),
        ("number", "not a number", None),
        ("currency", "$1,234.56", 1234.56),
        ("currency", "1.234,56 EUR", 1234.56),
        ("percent", "5%", 0.05),
        ("percent", "0.05", 0.05),
        ("boolean", "yes", True),
        ("boolean", "Nein", False),
        ("boolean", "maybe", None),
        ("trim", "  spaced  ", "spaced"),
        ("lowercase", "ABC", "abc"),
        ("uppercase", "abc", "ABC"),
        ("date", "2024-02-04", "2024-02-04"),
        ("date", "04.02.2024", "2024-02-04"),
        ("date:DD/MM/YYYY", "04/02/2024", "2024-02-04"),
    ],
)
def test_normalize_keyword_specs(spec, value, expected):
    applied, out = normalizers.normalize(spec, value)
    assert applied is True
    assert out == expected


def test_normalize_returns_not_applied_when_unset():
    applied, out = normalizers.normalize(None, "x")
    assert applied is False
    assert out is None


def test_normalize_unknown_keyword_is_not_applied():
    applied, out = normalizers.normalize("frobnicate", "x")
    assert applied is False
    assert out is None


def test_normalize_dict_form():
    applied, out = normalizers.normalize(
        {"name": "date", "format": "DD/MM/YYYY"}, "04/02/2024"
    )
    assert applied is True
    assert out == "2024-02-04"


# ── matcher integration ────────────────────────────────────────────────


def test_matcher_attaches_normalized_value_for_currency():
    field = {
        "name": "total",
        "examples": ["1234.56"],
        "normalizer": "currency",
    }
    entries = [_entry(0, "Total due: 1.234,56 EUR")]
    result = main._match_field_to_entries(field, entries, used_ids=set())
    assert result["extracted_value"] is not None
    assert result["normalized_value"] == pytest.approx(1234.56)
    assert result["normalizer"] == "currency"


def test_matcher_leaves_normalized_value_null_when_unparseable():
    field = {"name": "total", "examples": ["1234.56"], "normalizer": "currency"}
    entries = [_entry(0, "1234.56")]
    result = main._match_field_to_entries(field, entries, used_ids=set())
    assert result["normalized_value"] == pytest.approx(1234.56)


def test_matcher_skips_normalizer_when_unset():
    field = {"name": "label", "examples": ["FOO"]}
    entries = [_entry(0, "FOO")]
    result = main._match_field_to_entries(field, entries, used_ids=set())
    assert result["normalized_value"] is None


# ── Pydantic validation rejects typos ──────────────────────────────────


def test_field_spec_rejects_unknown_normalizer():
    import pydantic

    from main import FieldSpec

    with pytest.raises(pydantic.ValidationError):
        FieldSpec(name="x", normalizer="curency")  # typo


def test_field_spec_accepts_dict_form():
    from main import FieldSpec

    spec = FieldSpec(name="x", normalizer={"name": "date", "format": "DD/MM/YYYY"})
    assert spec.normalizer == {"name": "date", "format": "DD/MM/YYYY"}
