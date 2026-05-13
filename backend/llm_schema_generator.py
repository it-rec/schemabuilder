"""LLM-based schema generation.

Given the text of a document the user uploaded but has no matching
definition for, ask Claude to propose a `DocumentSpec`-shaped JSON the
user can hand-edit and save as a new definition. The endpoint is
explicitly user-triggered (one click, one paid call), never automatic.

Mirrors the layout of `llm_fallback` so both LLM-backed features share
their env vars (ANTHROPIC_API_KEY, SCHEMABUILDER_LLM_MODEL,
SCHEMABUILDER_LLM_ENABLED, SCHEMABUILDER_LLM_MAX_CHARS) and the same
"silently no-op when not configured" gating contract.
"""
from __future__ import annotations

import logging
import os
from typing import Any, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("schemabuilder")

_LLM_MODEL = os.getenv("SCHEMABUILDER_LLM_MODEL") or "claude-opus-4-7"
_LLM_ENABLED = (os.getenv("SCHEMABUILDER_LLM_ENABLED") or "auto").lower()
# Hard cap on the document text we send. Schema suggestion is more
# expensive than field extraction (much bigger output), so leave even
# more headroom for the response than the fallback does — but reuse the
# same env var so both features can be tuned together.
_MAX_DOCUMENT_CHARS = max(
    1000, int(os.getenv("SCHEMABUILDER_LLM_MAX_CHARS") or 150_000)
)
# Upper bound on fields a single suggestion may return. The LLM tends to
# over-elaborate on long documents ("page_number", "footer_text", ...).
# A user can always Add more fields in the editor; trimming up-front
# keeps the suggestion focused on the obviously-useful ones.
_MAX_SUGGESTED_FIELDS = max(
    1, int(os.getenv("SCHEMABUILDER_SCHEMA_MAX_FIELDS") or 25)
)

_SYSTEM_PROMPT = """You are a precision schema designer for a document
extraction tool.

Given the full text of a document, propose a JSON schema that captures
the structured fields a user would plausibly want to extract from
documents of this kind.

Output rules:
- document_type: a short human-readable name in Title Case
  (e.g. "Purchase Order", "Lab Report"). 1-4 words. No quotes.
- document_description: one sentence in plain English explaining what
  this kind of document is. Omit if uncertain.
- fields: 3-15 of the most useful fields. Prefer fields that recur
  across documents of this class (ids, dates, parties, totals,
  line-item tables) over one-off labels. Do NOT include page numbers,
  footers, or boilerplate.
- For each field:
  - name: snake_case identifier (lowercase, underscores). Must be unique
    within the schema.
  - type: "scalar" for single values, "array" for repeating rows
    (line items, transactions). Default to "scalar" if unsure.
  - description: one short clause explaining the field.
  - examples: 1-3 verbatim values copied from the document if you can
    find them, otherwise omit. Plain strings only.

Do not invent fields the document plainly does not contain. If the
document is unreadable or empty, return document_type="Unknown" and
fields=[].
"""


class SuggestedField(BaseModel):
    """Schema the LLM is forced to fill per field via structured outputs."""

    name: str = Field(min_length=1)
    type: str = Field(default="scalar")
    description: Optional[str] = None
    examples: Optional[List[str]] = None


class SchemaSuggestion(BaseModel):
    """Top-level shape the LLM returns. Mirrors `DocumentSpec` so the
    frontend can drop it straight into the definition editor."""

    document_type: str = Field(min_length=1)
    document_description: Optional[str] = None
    fields: List[SuggestedField] = Field(default_factory=list)


_client: Any = None
_client_key: Optional[str] = None
_sdk_import_ok: Optional[bool] = None


def _import_ok() -> bool:
    global _sdk_import_ok
    if _sdk_import_ok is not None:
        return _sdk_import_ok
    try:
        import anthropic  # noqa: F401
    except ImportError:
        _sdk_import_ok = False
        return False
    _sdk_import_ok = True
    return True


def is_available() -> bool:
    """True iff the schema generator can actually run.

    Same gating contract as `llm_fallback.is_available()` so a deploy
    that turns off the LLM features turns off both at once."""
    if _LLM_ENABLED == "0":
        return False
    if not os.getenv("ANTHROPIC_API_KEY"):
        return False
    return _import_ok()


def _get_client():
    """Return a cached Anthropic client; rebuild when the API key changes."""
    global _client, _client_key
    current_key = os.getenv("ANTHROPIC_API_KEY")
    if _client is None or _client_key != current_key:
        import anthropic
        _client = anthropic.Anthropic()
        _client_key = current_key
    return _client


def reset_for_tests() -> None:
    """Drop every memoized SDK-related piece. Mirrors `llm_fallback`."""
    global _client, _client_key, _sdk_import_ok
    _client = None
    _client_key = None
    _sdk_import_ok = None


def _sanitize_field_name(name: str) -> str:
    """Coerce an LLM-supplied field name into the snake_case shape the
    rest of the app assumes. Drops anything non-alphanumeric/underscore
    and lowercases. Empty results are rejected by the caller."""
    cleaned = "".join(
        ch if (ch.isalnum() or ch == "_") else "_" for ch in name.lower()
    )
    # Collapse repeated underscores and strip leading/trailing ones so
    # "Invoice # / Date" doesn't end up as "invoice_____date".
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip("_")


def _normalize_suggestion(parsed: SchemaSuggestion) -> dict:
    """Convert the validated LLM output into the JSON shape the
    /api/definitions endpoint accepts. Sanitizes field names, dedupes
    on collision, and caps the field count."""
    seen: set[str] = set()
    fields: list[dict] = []
    for raw in parsed.fields:
        name = _sanitize_field_name(raw.name)
        if not name or name in seen:
            continue
        seen.add(name)
        field: dict = {"name": name}
        ftype = (raw.type or "scalar").lower()
        if ftype == "array":
            field["type"] = "array"
        if raw.description and raw.description.strip():
            field["description"] = raw.description.strip()
        if raw.examples:
            # Plain strings only; the matcher coerces non-strings but
            # the rest of the editor assumes List[str].
            examples = [
                str(e).strip() for e in raw.examples if e is not None and str(e).strip()
            ]
            if examples:
                field["examples"] = examples[:3]
        fields.append(field)
        if len(fields) >= _MAX_SUGGESTED_FIELDS:
            break
    out: dict = {
        "document_type": parsed.document_type.strip(),
        "fields": fields,
    }
    if parsed.document_description and parsed.document_description.strip():
        out["document_description"] = parsed.document_description.strip()
    return out


def generate_schema(
    *,
    document_text: str,
    filename_hint: Optional[str] = None,
) -> Optional[dict]:
    """Ask Claude to propose a definition body for the given document.

    Returns a JSON-ready dict shaped like `DocumentSpec`
    (``{document_type, document_description?, fields: [...]}``) on
    success, or None when the LLM is unavailable, returns nothing
    useful, or raises. Exceptions are logged and swallowed — schema
    suggestion is best-effort and the user can always fall back to
    typing the definition by hand.
    """
    if not is_available():
        return None
    if not document_text or not document_text.strip():
        return None
    try:
        client = _get_client()
        user_lines: list[str] = []
        if filename_hint:
            user_lines.append(f"Document filename: {filename_hint}")
            user_lines.append("")
        user_lines.append("Document text:")
        if len(document_text) > _MAX_DOCUMENT_CHARS:
            user_lines.append(document_text[:_MAX_DOCUMENT_CHARS])
            user_lines.append(f"[truncated to {_MAX_DOCUMENT_CHARS} chars]")
        else:
            user_lines.append(document_text)
        user_msg = "\n".join(user_lines)

        response = client.messages.parse(
            model=_LLM_MODEL,
            max_tokens=4096,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_msg}],
            output_format=SchemaSuggestion,
        )
        parsed = response.parsed_output
        if parsed is None:
            return None
        normalized = _normalize_suggestion(parsed)
        if not normalized.get("document_type") or not normalized.get("fields"):
            # An empty / placeholder suggestion is worse than no
            # suggestion — surface it as a None so the API layer can
            # return a clean "couldn't generate" status.
            return None
        return normalized
    except Exception:
        logger.exception("LLM schema generation failed")
        return None
