"""LLM fallback for extraction.

When the rule-based matcher can't find a value for a field, optionally
consult Claude. Opt-in per field via `use_llm_fallback: true` on the
FieldSpec so a single click doesn't fan out into surprise API spend.

Design choices documented inline:
- Lazy-imports the `anthropic` SDK so the test environment + production
  code path that doesn't use LLM fallback don't have to install it.
- Defaults to `claude-opus-4-7` to honor the SDK skill's "always Opus
  unless the user named a different model" rule. Users can downgrade
  via `SCHEMABUILDER_LLM_MODEL=claude-haiku-4-5` — extraction fallback
  is a textbook Haiku task.
- Pydantic-typed structured output via `messages.parse()` so the
  response is validated, not regex-scraped.
- Prompt caching on the (constant) system prompt. The breakpoint sits
  on the system block so every fallback call after the first hits the
  cache for ~90% of system-prompt tokens.
- effort=low + thinking disabled because a single-field extraction is a
  narrow task, not an agentic loop. Keeps latency tight.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("schemabuilder")

_LLM_MODEL = os.getenv("SCHEMABUILDER_LLM_MODEL") or "claude-opus-4-7"
# Soft on/off switch. "auto" (default) consults the SDK + env to decide;
# "0" force-disables; "1" forces enabled (still requires the SDK + key).
_LLM_ENABLED = (os.getenv("SCHEMABUILDER_LLM_ENABLED") or "auto").lower()
# Hard cap on the document text we send. Chosen well below Claude's
# context window to leave room for the system prompt + tool schema +
# generation budget. Trims from the end (early-document text is usually
# where headers / IDs / structured fields live).
_MAX_DOCUMENT_CHARS = max(
    1000, int(os.getenv("SCHEMABUILDER_LLM_MAX_CHARS") or 150_000)
)

_SYSTEM_PROMPT = """You are a precision extraction assistant.

Given (1) the name and description of a field, and (2) the full text of a
document, return the value of that field exactly as it appears in the
document, or null if it isn't there.

Rules:
- Return only the verbatim substring from the document. No paraphrasing,
  no normalization, no quoting.
- If the field plainly is not in the document, return value=null and
  confidence=0.
- Confidence is your own calibration: 1.0 means "the value is clearly
  this exact string"; 0.5 means "I think so but the document is ambiguous";
  <0.5 means "I'm guessing".
"""


class FieldExtraction(BaseModel):
    """Schema the LLM is forced to fill via structured outputs."""

    value: Optional[str] = Field(
        default=None,
        description="Verbatim substring from the document, or null.",
    )
    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Self-rated confidence, 0–1.",
    )


# Lazy-initialized SDK + client. Tests can monkeypatch _client directly to
# avoid touching the SDK at all. `_sdk_import_ok` memoizes only the import
# probe (immutable across a process); the API-key check is re-run on every
# is_available() call so a rotated / unset key takes effect immediately.
_client: Any = None
_client_key: Optional[str] = None  # the key _client was created with
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
    """True iff the fallback can actually run.

    `SCHEMABUILDER_LLM_ENABLED=0` short-circuits to False so deploys can
    hard-disable the fallback without rebuilding. The API-key check is
    NOT memoized — rotating or unsetting `ANTHROPIC_API_KEY` at runtime
    flips this on the next call rather than at process restart."""
    if _LLM_ENABLED == "0":
        return False
    if not os.getenv("ANTHROPIC_API_KEY"):
        return False
    return _import_ok()


def _get_client():
    """Return a cached Anthropic client; rebuild when the API key changes
    so a rotated key doesn't keep authenticating with the old credentials."""
    global _client, _client_key
    current_key = os.getenv("ANTHROPIC_API_KEY")
    if _client is None or _client_key != current_key:
        import anthropic
        _client = anthropic.Anthropic()
        _client_key = current_key
    return _client


def reset_for_tests() -> None:
    """Drop every memoized SDK-related piece. Tests use this between
    cases that install / remove env vars."""
    global _client, _client_key, _sdk_import_ok
    _client = None
    _client_key = None
    _sdk_import_ok = None


def extract_field(
    *,
    field_name: str,
    field_description: str,
    examples: Optional[list],
    document_text: str,
    min_confidence: float = 0.5,
) -> Optional[dict]:
    """Ask Claude to extract a single field from the document text.

    Returns `{"value", "confidence"}` when the LLM returns a value at or
    above `min_confidence`, otherwise None. Any exception (network,
    auth, parse error) is logged and swallowed — fallback must never
    make a successful matcher pass fail.
    """
    if not is_available():
        return None
    try:
        client = _get_client()
        # Compose the user-side context. Field metadata first (small,
        # rarely-changing slice), then the document body (the bulk). The
        # system prompt is identical across calls so it benefits from
        # prompt caching.
        user_lines = [f"Field name: {field_name}"]
        if field_description:
            user_lines.append(f"Field description: {field_description}")
        if examples:
            joined = ", ".join(str(e) for e in examples if e is not None)
            if joined:
                user_lines.append(f"Examples of this field's values: {joined}")
        user_lines.append("")
        user_lines.append("Document text:")
        # Clamp the body so a 500-page PDF can't blow Claude's context
        # window. Tail-truncate because heading / IDs / structured fields
        # usually live near the top of business documents.
        if len(document_text) > _MAX_DOCUMENT_CHARS:
            user_lines.append(document_text[:_MAX_DOCUMENT_CHARS])
            user_lines.append(f"[truncated to {_MAX_DOCUMENT_CHARS} chars]")
        else:
            user_lines.append(document_text)
        user_msg = "\n".join(user_lines)

        # messages.parse() validates the response against FieldExtraction
        # and exposes the typed instance via .parsed_output. The
        # cache_control on the system block caches the system prompt
        # (~150 tokens) plus the model's compiled JSON schema for
        # FieldExtraction — both stable across calls.
        response = client.messages.parse(
            model=_LLM_MODEL,
            max_tokens=512,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": user_msg}],
            output_format=FieldExtraction,
        )
        parsed = response.parsed_output
        if parsed is None or parsed.value is None:
            return None
        if parsed.confidence < min_confidence:
            return None
        return {
            "value": parsed.value,
            "confidence": float(parsed.confidence),
        }
    except Exception:
        logger.exception("LLM fallback failed for field %s", field_name)
        return None
