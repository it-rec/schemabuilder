"""Target-table evaluation for document class definitions.

Definitions optionally carry a `target_tables` block that says "given the
fields extracted from a document, here is how to map them into one or more
flat result tables". Each column has a `source` that points at a literal,
a variable (e.g. `document_id`), a field on the extracted document, or a
nested transform expression. This module evaluates those expressions and
returns one row list per table — the runnable side of what the definitions
already model on paper.

Why a separate module: `main.py` is already ~2000 lines, and the transform
engine has no dependencies on FastAPI / Docling / caches. Keeping it
isolated makes it trivially unit-testable and reusable from any future
export path (batch jobs, CLI, etc.).
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

# Map of CLDR-ish date tokens that show up in real definitions to the
# strftime tokens Python understands. Longest tokens first so the regex
# substitution doesn't eat "YY" out of "YYYY". This intentionally stays a
# tiny subset — extend when a real definition needs more.
_DATE_TOKENS = [
    ("YYYY", "%Y"),
    ("YY", "%y"),
    ("MM", "%m"),
    ("DD", "%d"),
    ("HH", "%H"),
    ("mm", "%M"),
    ("ss", "%S"),
]


def _cldr_to_strftime(fmt: str) -> str:
    """Translate a CLDR-style date pattern to a strftime format string."""
    out = fmt
    for token, repl in _DATE_TOKENS:
        out = out.replace(token, repl)
    return out


def _t_identity(input: Any) -> Any:
    return input


def _t_string_to_date(input: Any, format: str) -> str | None:
    """Parse a date string per the supplied CLDR-ish format, return ISO date.

    Returns None on missing input or unparseable strings rather than raising
    — a single bad row shouldn't fail the entire export. The caller can use
    `default_value` on the column to substitute a sentinel if desired.
    """
    if input is None:
        return None
    s = str(input).strip()
    if not s:
        return None
    try:
        parsed = datetime.strptime(s, _cldr_to_strftime(format))
    except ValueError:
        return None
    return parsed.date().isoformat()


_CURRENCY_CLEAN_RE = re.compile(r"[^\d.,-]")


def _t_string_to_currency(input: Any) -> str | None:
    """Parse a currency-ish string ("$1,234.56", "1.234,56 EUR") to a decimal
    string.

    Heuristic: strip everything that isn't a digit / separator / sign, then
    decide whether comma or period is the decimal separator by looking at the
    rightmost occurrence. Returns a canonical "1234.56" string so downstream
    consumers (CSV, JSON) don't have to guess locale.
    """
    if input is None:
        return None
    raw = _CURRENCY_CLEAN_RE.sub("", str(input))
    if not raw:
        return None

    # Decide which character is the decimal separator: whichever appears last
    # is the one closest to the cents — "1.234,56" → comma decimal,
    # "1,234.56" → period decimal. If only one kind appears, treat it as the
    # decimal separator only when followed by exactly two digits, else as a
    # thousands separator.
    last_dot = raw.rfind(".")
    last_comma = raw.rfind(",")
    if last_dot >= 0 and last_comma >= 0:
        if last_comma > last_dot:
            normalized = raw.replace(".", "").replace(",", ".")
        else:
            normalized = raw.replace(",", "")
    elif last_comma >= 0:
        # Comma only: decimal if exactly two trailing digits, else thousands.
        if re.search(r",\d{2}$", raw):
            normalized = raw.replace(".", "").replace(",", ".")
        else:
            normalized = raw.replace(",", "")
    else:
        normalized = raw

    try:
        return format(Decimal(normalized), "f")
    except (InvalidOperation, ValueError):
        return None


# Registry of built-in transforms keyed by `transform_name`. Extending the
# engine is a one-line registration here plus the function definition above
# — no need to touch the evaluator.
TRANSFORMS: dict[str, Callable[..., Any]] = {
    "identity": _t_identity,
    "string_to_date": _t_string_to_date,
    "string_to_currency": _t_string_to_currency,
}


class TransformError(ValueError):
    """Raised when a target_tables block can't be evaluated.

    Distinct from ValueError so callers (the FastAPI endpoint) can map it
    cleanly to a 400 / 422 without catching the base class and accidentally
    swallowing unrelated bugs.
    """


def _eval_source(source: Any, scope: dict, context: dict) -> Any:
    """Recursively evaluate a `source` expression.

    `scope` is the field map for the current row (document-level for scalar
    tables, item-level for array tables). `context` carries cross-cutting
    values like `document_id` that don't live on a specific field.
    """
    if not isinstance(source, dict):
        # A bare value is treated as a literal so trivially-typed columns
        # ("source": "doc_id") don't require ceremony. Real definitions use
        # the dict form, but the test suite uses both.
        return source

    if "literal" in source:
        return source["literal"]
    if "variable" in source:
        return context.get(source["variable"])
    if "field" in source:
        return scope.get(source["field"])
    if "transform" in source:
        spec = source["transform"]
        name = spec.get("transform_name")
        if name not in TRANSFORMS:
            raise TransformError(f"Unknown transform: {name!r}")
        kwargs = {}
        for arg in spec.get("arguments", []) or []:
            if "name" not in arg:
                raise TransformError(
                    "Transform argument missing 'name' key: " + repr(arg)
                )
            kwargs[arg["name"]] = _eval_source(arg.get("value"), scope, context)
        try:
            return TRANSFORMS[name](**kwargs)
        except TypeError as e:
            # Mismatched argument signature — clearer error than the bare
            # TypeError from the Python call.
            raise TransformError(
                f"Transform {name!r} called with wrong arguments: {e}"
            ) from e

    # Empty / unrecognized source: surface as None rather than guessing, so
    # the column can fall back to its `default_value`.
    return None


def _scope_for_fields(fields: list[dict]) -> dict:
    """Flat name → extracted_value map from a list of extracted fields.

    Skips array fields (they have no scalar `extracted_value`); their items
    are addressed separately by the table-name → array-field convention.
    """
    out: dict[str, Any] = {}
    for f in fields:
        if f.get("type") == "array":
            continue
        out[f["name"]] = f.get("extracted_value")
    return out


def _array_field_items(fields: list[dict], array_name: str) -> list[dict]:
    """Items for an array field, each item collapsed to a name → value scope."""
    for f in fields:
        if f.get("name") == array_name and f.get("type") == "array":
            items = []
            for item in f.get("items", []) or []:
                scope = {
                    sf["name"]: sf.get("extracted_value")
                    for sf in item.get("fields", []) or []
                }
                items.append(scope)
            return items
    return []


def _emit_row(columns: list[dict], scope: dict, context: dict) -> dict:
    row: dict[str, Any] = {}
    for col in columns:
        name = col.get("name")
        if not name:
            # A column without a name is malformed — fail loudly rather than
            # silently dropping data into an empty key.
            raise TransformError("Column is missing a 'name' field.")
        value = _eval_source(col.get("source"), scope, context)
        if value is None and "default_value" in col:
            value = col["default_value"]
        row[name] = value
    return row


def build_export(
    definition: dict,
    doc_id: str,
    fields: list[dict],
) -> dict[str, list[dict]]:
    """Run every target table in `definition` and return rows per table.

    Convention: if a table's `name` matches an array field in the document
    spec, the table is treated as repeating — one row per item. Otherwise it
    emits exactly one row at document scope. This mirrors the implicit
    contract in invoice.json (the `line_items` table maps to the
    `line_items` array field).
    """
    tables = definition.get("target_tables") or []
    if not isinstance(tables, list):
        raise TransformError("target_tables must be a list")

    array_names = {
        f["name"]
        for f in definition.get("document", {}).get("fields", []) or []
        if f.get("type") == "array"
    }
    scalar_scope = _scope_for_fields(fields)
    context = {"document_id": doc_id}

    result: dict[str, list[dict]] = {}
    for table in tables:
        name = table.get("name")
        if not name:
            raise TransformError("target_tables entry is missing 'name'")
        columns = table.get("columns") or []
        if name in array_names:
            rows = [
                _emit_row(columns, item_scope, context)
                for item_scope in _array_field_items(fields, name)
            ]
        else:
            rows = [_emit_row(columns, scalar_scope, context)]
        result[name] = rows
    return result
