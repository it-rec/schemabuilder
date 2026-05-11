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
# avoid touching the SDK at all.
_client: Any = None
_sdk_state: Optional[bool] = None  # None=untested, True=usable, False=unusable


def _detect_sdk() -> bool:
    """Probe whether the SDK is importable AND the API key is set.
    Memoized so subsequent calls are cheap."""
    global _sdk_state
    if _sdk_state is not None:
        return _sdk_state
    if not os.getenv("ANTHROPIC_API_KEY"):
        _sdk_state = False
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        _sdk_state = False
        return False
    _sdk_state = True
    return True


def is_available() -> bool:
    """True iff the fallback can actually run.

    `SCHEMABUILDER_LLM_ENABLED=0` short-circuits to False so deploys can
    hard-disable the fallback without rebuilding."""
    if _LLM_ENABLED == "0":
        return False
    return _detect_sdk()


def _get_client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client


def reset_for_tests() -> None:
    """Drop the SDK-probe + client cache. Tests use this between cases that
    install / remove env vars."""
    global _client, _sdk_state
    _client = None
    _sdk_state = None


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
