"""Per-field dependencies (visible_if / required_if).

Field definitions can carry conditional rules referring to another field's
value, e.g. ``"required_if": {"field": "payment_method", "equals": "card"}``.
After extraction, the conditions are evaluated against the other fields'
matched values so the frontend can:

  * hide / dim a field that the document semantically doesn't apply to
    (``visible_if`` evaluated False),
  * mark a missing-but-required field as a validation error
    (``required_if`` evaluated True AND ``extracted_value`` is None).

Kept independent of FastAPI / Docling so it's trivially unit-testable.
"""
from __future__ import annotations

from typing import Any, Optional


def _normalize_condition(cond: Any) -> Optional[dict]:
    """Return a sanity-checked condition dict or None.

    Accepts a single dict (one condition) or a list of dicts (treated as
    AND — every condition must hold). Returns None if the shape is bogus
    so the matcher can no-op safely; ``validate_condition`` on the
    Pydantic side surfaces the same shape as a 422 at upload time.
    """
    if cond is None:
        return None
    if isinstance(cond, list):
        if not cond:
            return None
        children = [_normalize_condition(c) for c in cond]
        if any(c is None for c in children):
            return None
        return {"all": children}
    if not isinstance(cond, dict):
        return None
    if "all" in cond and isinstance(cond["all"], list):
        children = [_normalize_condition(c) for c in cond["all"]]
        if any(c is None for c in children):
            return None
        return {"all": children}
    if "any" in cond and isinstance(cond["any"], list):
        children = [_normalize_condition(c) for c in cond["any"]]
        if any(c is None for c in children):
            return None
        return {"any": children}
    if not cond.get("field") or not isinstance(cond["field"], str):
        return None
    # At least one operator must be present, else the condition is
    # ambiguous — treat as malformed rather than silently truthy.
    if not any(k in cond for k in ("equals", "in", "present", "absent")):
        return None
    return cond


def validate_condition(cond: Any) -> None:
    """Raise ValueError on a malformed condition. Hook for Pydantic."""
    if cond is None:
        return
    if _normalize_condition(cond) is None:
        raise ValueError(
            "Condition must be {field, equals|in|present|absent} "
            "(or {all|any: [...]}) — got " + repr(cond)
        )


def _coerce(value: Any) -> Any:
    """Lowercase-strip strings for comparison; pass other types through.

    The matcher's `extracted_value` is always a string (the raw text);
    the `normalized_value` may be a bool/number/None. Comparisons against
    `equals` use whichever is more specific:
        - bool / number: strict equality on the typed value
        - string: case-insensitive whitespace-trimmed equality
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip().lower()
    return value


def _effective_value(field_result: Optional[dict]) -> Any:
    """Pick the value to compare against — prefer normalized when present."""
    if not isinstance(field_result, dict):
        return None
    if field_result.get("normalized_value") is not None:
        return field_result["normalized_value"]
    return field_result.get("extracted_value")


def _eval_leaf(cond: dict, scope: dict) -> bool:
    target = scope.get(cond["field"])
    value = _effective_value(target)
    if "present" in cond:
        present = value is not None and value != ""
        return bool(cond["present"]) == present
    if "absent" in cond:
        absent = value is None or value == ""
        return bool(cond["absent"]) == absent
    if "equals" in cond:
        return _coerce(value) == _coerce(cond["equals"])
    if "in" in cond and isinstance(cond["in"], list):
        return _coerce(value) in {_coerce(v) for v in cond["in"]}
    return False


def evaluate(cond: Any, scope: dict) -> bool:
    """Evaluate a normalized condition against a name -> field_result map.

    Returns True when the condition holds. Unknown / missing target fields
    are treated as "value is None" — i.e. ``present: true`` against a
    missing field is False, ``absent: true`` is True. A malformed
    condition (rejected by ``_normalize_condition``) returns False so it
    can't accidentally suppress a field.
    """
    norm = _normalize_condition(cond)
    if norm is None:
        return False
    if "all" in norm:
        return all(evaluate(c, scope) for c in norm["all"])
    if "any" in norm:
        return any(evaluate(c, scope) for c in norm["any"])
    return _eval_leaf(norm, scope)


def apply(field_results: list[dict]) -> None:
    """Decorate `field_results` in place with dependency metadata.

    For each field result, sets:

      * ``is_visible`` — False when ``visible_if`` evaluates to False; in
        that case ``extracted_value`` / ``normalized_value`` are also
        wiped so the FE doesn't accidentally render stale data for a
        suppressed field.
      * ``required`` — True when ``required_if`` evaluates to True (or
        is a literal True flag on the field).
      * ``required_satisfied`` — False when required and there's no
        ``extracted_value``.

    Conditions reference fields by name within the same scope. Only the
    top level of the document is considered here; sub-fields of an array
    are handled separately by `apply_items` per-item.
    """
    scope = {fr.get("name"): fr for fr in field_results if isinstance(fr, dict)}
    for fr in field_results:
        _apply_one(fr, scope)
        if fr.get("type") == "array" and isinstance(fr.get("items"), list):
            for item in fr["items"]:
                if isinstance(item, dict) and isinstance(item.get("fields"), list):
                    item_scope = {
                        sf.get("name"): sf
                        for sf in item["fields"]
                        if isinstance(sf, dict)
                    }
                    for sf in item["fields"]:
                        _apply_one(sf, item_scope)


def _apply_one(fr: dict, scope: dict) -> None:
    visible_cond = fr.get("visible_if") if isinstance(fr, dict) else None
    if visible_cond is not None:
        visible = evaluate(visible_cond, scope)
        fr["is_visible"] = visible
        if not visible:
            fr["extracted_value"] = None
            fr["normalized_value"] = None
            fr["confidence"] = 0
            fr["match_reason"] = "hidden_by_dependency"
            fr["match_score"] = 0
            fr["rejected_candidate"] = None
    else:
        fr["is_visible"] = True

    required_raw = fr.get("required_if") if isinstance(fr, dict) else None
    required = False
    if isinstance(required_raw, bool):
        required = required_raw
    elif required_raw is not None:
        required = evaluate(required_raw, scope)
    fr["required"] = required
    fr["required_satisfied"] = not (
        required and fr.get("is_visible", True) and fr.get("extracted_value") is None
    )
