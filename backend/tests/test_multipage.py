"""Tests for multi-page array tables (multi_page + header_pattern)."""
import main


def _table_item(eid, text, page):
    return {
        "id": eid,
        "text": text,
        "type": "TableItem",
        "page": page,
        "bbox": None,
        "_text_lower": text.lower(),
        "_text_stripped_lower": text.strip().lower(),
    }


def _array_field(extra=None):
    f = {
        "name": "line_items",
        "type": "array",
        "fields": [
            {"name": "amount", "examples": ["100.00"]},
        ],
    }
    if extra:
        f.update(extra)
    return f


def test_array_emits_pages_spanned_summary():
    field = _array_field()
    entries = [
        _table_item(0, "Coffee 12.50", 1),
        _table_item(1, "Tea 8.00", 1),
        _table_item(2, "Cake 5.00", 2),
    ]
    result = main._match_field_to_entries(field, entries, used_ids=set())
    assert result["pages_spanned"] == [1, 2]
    assert result["is_multi_page"] is True
    assert len(result["items"]) == 3


def test_header_pattern_skips_repeating_header_rows():
    field = _array_field({"header_pattern": r"^(Item|Description|Amount)"})
    entries = [
        _table_item(0, "Item Amount", 1),       # header row, should be skipped
        _table_item(1, "Coffee 12.50", 1),
        _table_item(2, "Item Amount", 2),        # header row repeat on p.2
        _table_item(3, "Cake 5.00", 2),
    ]
    result = main._match_field_to_entries(field, entries, used_ids=set())
    # 2 items (the two real rows), headers filtered out.
    assert len(result["items"]) == 2


def test_multi_page_auto_detects_repeated_label_header():
    # With multi_page=True the matcher should detect that a TableItem whose
    # text is purely sub-field labels is a header repeat and skip it.
    field = _array_field({"multi_page": True})
    entries = [
        _table_item(0, "amount sku quantity", 1),  # header-like
        _table_item(1, "100.00", 1),
        _table_item(2, "amount sku quantity", 2),  # header repeat
        _table_item(3, "50.00", 2),
    ]
    field = {
        "name": "line_items",
        "type": "array",
        "multi_page": True,
        "fields": [
            {"name": "amount", "examples": ["100.00"]},
            {"name": "sku"},
            {"name": "quantity"},
        ],
    }
    result = main._match_field_to_entries(field, entries, used_ids=set())
    assert len(result["items"]) == 2
    assert result["is_multi_page"] is True


def test_invalid_header_pattern_rejected_by_field_spec():
    import pydantic

    from main import FieldSpec

    try:
        FieldSpec(name="x", type="array", header_pattern="(", fields=[])
    except pydantic.ValidationError:
        return
    raise AssertionError("Expected ValidationError for bad header_pattern")


def test_single_page_array_reports_is_multi_page_false():
    field = _array_field()
    entries = [
        _table_item(0, "Coffee 12.50", 1),
        _table_item(1, "Tea 8.00", 1),
    ]
    result = main._match_field_to_entries(field, entries, used_ids=set())
    assert result["pages_spanned"] == [1]
    assert result["is_multi_page"] is False
