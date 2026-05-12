"""Tests for visible_if / required_if field dependencies."""
import pytest

import dependencies
import main


def _entry(eid, text, etype="TextItem", page=1):
    return {
        "id": eid,
        "text": text,
        "type": etype,
        "page": page,
        "bbox": None,
        "_text_lower": text.lower(),
        "_text_stripped_lower": text.strip().lower(),
    }


# ── unit ────────────────────────────────────────────────────────────────


def test_evaluate_equals_uses_normalized_value():
    scope = {
        "method": {
            "name": "method",
            "extracted_value": "CARD",
            "normalized_value": "card",
        }
    }
    assert dependencies.evaluate({"field": "method", "equals": "card"}, scope) is True


def test_evaluate_equals_is_case_insensitive():
    scope = {"m": {"name": "m", "extracted_value": "Card", "normalized_value": None}}
    assert dependencies.evaluate({"field": "m", "equals": "card"}, scope) is True


def test_evaluate_in_set():
    scope = {"c": {"name": "c", "extracted_value": "DE", "normalized_value": None}}
    assert dependencies.evaluate({"field": "c", "in": ["AT", "DE", "CH"]}, scope) is True
    assert dependencies.evaluate({"field": "c", "in": ["FR"]}, scope) is False


def test_evaluate_present():
    scope = {
        "a": {"name": "a", "extracted_value": "yep", "normalized_value": None},
        "b": {"name": "b", "extracted_value": None, "normalized_value": None},
    }
    assert dependencies.evaluate({"field": "a", "present": True}, scope) is True
    assert dependencies.evaluate({"field": "b", "present": True}, scope) is False
    assert dependencies.evaluate({"field": "b", "absent": True}, scope) is True


def test_evaluate_all_and_any():
    scope = {
        "a": {"name": "a", "extracted_value": "x", "normalized_value": None},
        "b": {"name": "b", "extracted_value": "y", "normalized_value": None},
    }
    cond_all = {
        "all": [
            {"field": "a", "equals": "x"},
            {"field": "b", "equals": "y"},
        ]
    }
    cond_any = {
        "any": [
            {"field": "a", "equals": "wrong"},
            {"field": "b", "equals": "y"},
        ]
    }
    assert dependencies.evaluate(cond_all, scope) is True
    assert dependencies.evaluate(cond_any, scope) is True


def test_validate_condition_rejects_missing_operator():
    with pytest.raises(ValueError):
        dependencies.validate_condition({"field": "x"})


def test_validate_condition_rejects_missing_field():
    with pytest.raises(ValueError):
        dependencies.validate_condition({"equals": "x"})


# ── integration via _extract_fields ────────────────────────────────────


def test_visible_if_suppresses_field_when_false():
    definition = {
        "document": {
            "document_type": "X",
            "fields": [
                {"name": "method", "examples": ["cash"]},
                {
                    "name": "iban",
                    "examples": ["DE89"],
                    "visible_if": {"field": "method", "equals": "card"},
                },
            ],
        }
    }
    entries = [_entry(0, "cash"), _entry(1, "DE89")]
    results = main._extract_fields(definition, entries)
    iban = next(f for f in results if f["name"] == "iban")
    assert iban["is_visible"] is False
    assert iban["extracted_value"] is None
    assert iban["match_reason"] == "hidden_by_dependency"


def test_visible_if_keeps_field_when_true():
    definition = {
        "document": {
            "document_type": "X",
            "fields": [
                {"name": "method", "examples": ["card"]},
                {
                    "name": "iban",
                    "examples": ["DE89"],
                    "visible_if": {"field": "method", "equals": "card"},
                },
            ],
        }
    }
    entries = [_entry(0, "card"), _entry(1, "DE89")]
    results = main._extract_fields(definition, entries)
    iban = next(f for f in results if f["name"] == "iban")
    assert iban["is_visible"] is True
    assert iban["extracted_value"] == "DE89"


def test_required_if_flags_missing_value():
    definition = {
        "document": {
            "document_type": "X",
            "fields": [
                {"name": "method", "examples": ["card"]},
                {
                    "name": "card_number",
                    "examples": ["4111-1111-1111-1111"],
                    "required_if": {"field": "method", "equals": "card"},
                },
            ],
        }
    }
    # No card number in entries → required should be flagged unsatisfied.
    entries = [_entry(0, "card"), _entry(1, "totally unrelated")]
    results = main._extract_fields(definition, entries)
    card = next(f for f in results if f["name"] == "card_number")
    assert card["required"] is True
    assert card["required_satisfied"] is False


def test_required_if_bool_true_always_required():
    definition = {
        "document": {
            "document_type": "X",
            "fields": [
                {"name": "id", "examples": ["A-1"], "required_if": True},
            ],
        }
    }
    entries = [_entry(0, "lorem ipsum")]
    results = main._extract_fields(definition, entries)
    assert results[0]["required"] is True
    assert results[0]["required_satisfied"] is False
