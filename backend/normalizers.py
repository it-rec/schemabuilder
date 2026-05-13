"""Per-field value normalization.

After the matcher finds an `extracted_value`, an optional `normalizer` slot
on the field definition is applied to produce a parsed `normalized_value` —
"1.234,56 €" -> 1234.56 for currency, "12/03/2024" -> "2024-03-12" for date,
"5%" -> 0.05 for percent, etc. Failed parses surface as
`normalized_value: None` with the raw match still on `extracted_value` so
the UI can show both.

Keeping this module independent of FastAPI / Docling so it can be unit-
tested in isolation and reused from any future export path.
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

# Normalizer ids accepted on a field's `normalizer` slot. The matcher /
# Pydantic validator both consult this set to fail-fast on a typo so a
# misconfigured definition raises a 422 at upload time rather than silently
# producing `normalized_value: None` at extract time.
SUPPORTED_NORMALIZERS = frozenset(
    {
        "number",
        "currency",
        "date",
        "percent",
        "boolean",
        "trim",
        "lowercase",
        "uppercase",
    }
)


def _split(spec: str) -> tuple[str, Optional[str]]:
    """Split a ``name[:arg]`` spec into its parts.

    The ``arg`` form is currently only meaningful for ``date:<format>`` but
    leaving the split generic keeps the door open for future parametric
    normalizers without breaking the wire format.
    """
    if ":" in spec:
        name, arg = spec.split(":", 1)
        return name.strip().lower(), arg
    return spec.strip().lower(), None


def parse_spec(value: Any) -> Optional[tuple[str, Optional[str]]]:
    """Decode whatever is sitting on a field's ``normalizer`` slot.

    Accepts either a bare string (``"currency"``, ``"date:DD/MM/YYYY"``) or
    a dict (``{"name": "date", "format": "DD/MM/YYYY"}``). Returns
    ``(name, arg)`` for any recognized normalizer, ``None`` otherwise so
    the caller can no-op cleanly when the slot is absent.
    """
    if value is None or value == "":
        return None
    if isinstance(value, str):
        name, arg = _split(value)
    elif isinstance(value, dict):
        name = str(value.get("name") or "").strip().lower()
        arg = value.get("format") or value.get("arg")
        if arg is not None:
            arg = str(arg)
    else:
        return None
    if name not in SUPPORTED_NORMALIZERS:
        return None
    return name, arg


_NUMBER_CLEAN_RE = re.compile(r"[^\d.,\-]")


def _to_decimal(raw: str) -> Optional[Decimal]:
    """Heuristic: figure out which separator is the decimal one.

    Same logic as the currency transform — kept inline so this module
    stays self-contained.
    """
    cleaned = _NUMBER_CLEAN_RE.sub("", raw)
    if not cleaned or cleaned in {"-", ".", ","}:
        return None
    last_dot = cleaned.rfind(".")
    last_comma = cleaned.rfind(",")
    if last_dot >= 0 and last_comma >= 0:
        if last_comma > last_dot:
            normalized = cleaned.replace(".", "").replace(",", ".")
        else:
            normalized = cleaned.replace(",", "")
    elif last_comma >= 0:
        if re.search(r",\d{1,2}$", cleaned):
            normalized = cleaned.replace(".", "").replace(",", ".")
        else:
            normalized = cleaned.replace(",", "")
    else:
        normalized = cleaned
    try:
        return Decimal(normalized)
    except (InvalidOperation, ValueError):
        return None


# Date input formats we'll try in order before giving up. Includes common
# European day-first patterns; the user can override with `date:<FORMAT>`
# for anything we don't cover.
_DEFAULT_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d.%m.%Y",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%m/%d/%Y",
    "%Y/%m/%d",
    "%d.%m.%y",
    "%Y%m%d",
)

_DATE_TOKEN_MAP = [
    ("YYYY", "%Y"),
    ("YY", "%y"),
    ("MM", "%m"),
    ("DD", "%d"),
    ("HH", "%H"),
    ("mm", "%M"),
    ("ss", "%S"),
]


def _cldr_to_strftime(fmt: str) -> str:
    out = fmt
    for token, repl in _DATE_TOKEN_MAP:
        out = out.replace(token, repl)
    return out


def _normalize_date(raw: str, arg: Optional[str]) -> Optional[str]:
    s = raw.strip()
    if not s:
        return None
    formats: tuple[str, ...]
    if arg:
        formats = (_cldr_to_strftime(arg),) + _DEFAULT_DATE_FORMATS
    else:
        formats = _DEFAULT_DATE_FORMATS
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


_BOOL_TRUE = frozenset({"true", "yes", "y", "1", "ja", "oui", "si", "x", "✓"})
_BOOL_FALSE = frozenset({"false", "no", "n", "0", "nein", "non", "-", "✗"})


def normalize(spec: Any, value: Any) -> tuple[bool, Any]:
    """Apply ``spec`` to ``value`` and return ``(applied, normalized)``.

    ``applied`` is ``True`` only when a normalizer was actually invoked —
    i.e. the slot was non-empty AND its name was recognized. The caller
    uses that flag to decide whether to attach ``normalized_value`` to the
    extracted field at all (an absent slot leaves the output untouched).
    On parse failure, ``applied`` is still True but ``normalized`` is None
    so the UI can render "couldn't normalize" instead of silently dropping
    the raw value.
    """
    parsed = parse_spec(spec)
    if parsed is None or value is None:
        return False, None
    name, arg = parsed
    s = value if isinstance(value, str) else str(value)
    if name == "trim":
        return True, s.strip()
    if name == "lowercase":
        return True, s.lower()
    if name == "uppercase":
        return True, s.upper()
    if name == "boolean":
        key = s.strip().lower()
        if key in _BOOL_TRUE:
            return True, True
        if key in _BOOL_FALSE:
            return True, False
        return True, None
    if name == "number":
        d = _to_decimal(s)
        if d is None:
            return True, None
        # Preserve integers as ints, decimals as floats. JSON handles both
        # natively and the FE can pick its own formatting.
        if d == d.to_integral_value():
            return True, int(d)
        return True, float(d)
    if name == "currency":
        d = _to_decimal(s)
        if d is None:
            return True, None
        return True, float(d)
    if name == "percent":
        d = _to_decimal(s.replace("%", ""))
        if d is None:
            return True, None
        # "5%" -> 0.05; the trailing % is the signal that the value is a
        # percentage rather than a raw fraction. If no % was present we
        # assume the user already wrote a fraction.
        if "%" in s:
            return True, float(d) / 100.0
        return True, float(d)
    if name == "date":
        return True, _normalize_date(s, arg)
    return False, None
