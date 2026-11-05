"""A typed answer to a clarify question with choices: "2", "1,3" or a label becomes the choice(s) it names.

Hermes does this for text typed into a chat (tools/clarify_gateway.py `_coerce_text_response_detailed`), but
has no public function for an adapter answering through `resolve_gateway_clarify`, so these are the same rules,
adapted from Hermes Agent (MIT licence): a number picks that choice, a label matches case-insensitively and
ignores the "(Recommended)" suffix, a multi-select answer ("1,3", "1 3", "staging, prod") becomes a JSON array
of labels, and one bad token rejects the whole answer. "skip" skips.
"""

from __future__ import annotations

import json
from typing import Any, Optional

RECOMMENDED_LABEL = "(Recommended)"
SKIP_WORD = "skip"


def strip_recommended(text: str) -> str:
    stripped = str(text).strip()
    if stripped.casefold().endswith(RECOMMENDED_LABEL.casefold()):
        return stripped[: -len(RECOMMENDED_LABEL)].strip()
    return stripped


def _match_label(text: str, choices: list) -> Optional[str]:
    wanted = strip_recommended(text).casefold()
    for choice in choices:
        if strip_recommended(str(choice)).casefold() == wanted:
            return str(choice).strip()
    return None


def _split_tokens(text: str) -> Optional[list[str]]:
    """Comma-separated tokens, or space-separated all-numeric tokens ("1 3"); else None."""
    if "," in text:
        return [t.strip() for t in text.split(",") if t.strip()]
    parts = text.split()
    return parts if len(parts) > 1 and all(p.isdigit() for p in parts) else None


def _is_int(text: str) -> bool:
    try:
        int(text)
        return True
    except ValueError:
        return False


def _multi(choices: list, text: str) -> Optional[str]:
    if not text:
        return None
    selected: list[str] = []
    tokens = _split_tokens(text)
    for token in [text] if tokens is None else tokens:
        if token.isdigit():
            idx = int(token) - 1
            label = str(choices[idx]).strip() if 0 <= idx < len(choices) else None
        else:
            label = _match_label(token, choices)
        if label is None:
            return None
        if label not in selected:
            selected.append(label)
    return json.dumps(selected, ensure_ascii=False) if selected else None


def coerce(entry: Any, response: str, skipped: Any) -> Optional[str]:
    """The answer Hermes' clarify tool expects for `response` typed against `entry` (Hermes' pending clarify:
    `choices`, `multi_select`), `skipped` for "skip", or None when it names no choice (the caller then sends
    the text as it is)."""
    text = str(response).strip()
    choices = list(getattr(entry, "choices", None) or [])
    if not choices:
        return None
    if getattr(entry, "multi_select", False):
        coerced = _multi(choices, text)
    else:
        idx = int(text) - 1 if _is_int(text) else -1
        coerced = choices[idx] if 0 <= idx < len(choices) else _match_label(text, choices)
    if coerced is not None:
        return coerced
    return skipped if text.casefold() == SKIP_WORD else None
