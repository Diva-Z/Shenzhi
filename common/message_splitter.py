"""Utilities for turning one assistant text into chat bubbles."""

from __future__ import annotations

import re
from typing import List


_MSG_MARKER_RE = re.compile(r"\[MSG\]", re.IGNORECASE)
_BLANK_LINE_RE = re.compile(r"\n\s*\n+")
_URL_RE = re.compile(r"https?://", re.IGNORECASE)
_CJK_RE = re.compile(r"[一-鿿]")
_FENCE_RE = re.compile(r"^\s*```")
_MARKDOWN_LINE_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+|#{1,6}\s+|>|```|\|)")
_TERMINAL_PUNCT = "。！？!?"
_BOUNDARY_TRAIL = _TERMINAL_PUNCT + "」』”’）】》〉)]"

# Defaults; can be overridden via config.json:
#   message_bubble_max_parts / message_bubble_soft_chars / message_bubble_hard_chars
DEFAULT_MAX_PARTS = 5
DEFAULT_SOFT_CHARS = 80
DEFAULT_HARD_CHARS = 160
# Content blocks longer than this are never sentence-split (URLs inside would
# risk being cut). Short mentions still skip splitting to stay conservative.
_PROTECTED_MAX_CHARS = 400


def _bubble_conf():
    try:
        from config import conf

        c = conf()
    except Exception:
        return DEFAULT_MAX_PARTS, DEFAULT_SOFT_CHARS, DEFAULT_HARD_CHARS, True

    def _int(key, default):
        try:
            value = int(c.get(key, default))
        except Exception:
            return default
        return value if value > 0 else default

    max_parts = _int("message_bubble_max_parts", DEFAULT_MAX_PARTS)
    soft_chars = _int("message_bubble_soft_chars", DEFAULT_SOFT_CHARS)
    hard_chars = _int("message_bubble_hard_chars", DEFAULT_HARD_CHARS)
    enabled = bool(c.get("message_bubble_enabled", True))
    if soft_chars > hard_chars:
        soft_chars = hard_chars
    return max_parts, soft_chars, hard_chars, enabled


def _is_protected(text: str) -> bool:
    """Structured content that must never be sentence-split."""
    if _URL_RE.search(text) or "![" in text or "[图片" in text or "[视频" in text:
        return True
    lines = text.splitlines()
    if any(_FENCE_RE.match(line) for line in lines):
        return True
    return any(_MARKDOWN_LINE_RE.match(line) for line in lines)


def _split_sentences(text: str) -> List[str]:
    if not _CJK_RE.search(text):
        return [text.strip()] if text.strip() else []

    parts: List[str] = []
    start = 0
    i = 0
    while i < len(text):
        if text[i] not in _TERMINAL_PUNCT:
            i += 1
            continue
        j = i + 1
        while j < len(text) and text[j] in _BOUNDARY_TRAIL:
            j += 1
        rest = text[j:].lstrip()
        current = text[start:j].strip()
        if rest and current:
            parts.append(current)
            start = j
        i = j

    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts or ([text.strip()] if text.strip() else [])


def _split_implicit_part(text: str) -> List[str]:
    part = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not part:
        return []
    if _is_protected(part):
        return [part]

    if "\n\n" in part:
        base = [p.strip() for p in _BLANK_LINE_RE.split(part) if p.strip()]
    elif "\n" in part:
        base = [p.strip() for p in part.splitlines() if p.strip()]
    else:
        base = [part]

    out: List[str] = []
    for item in base:
        out.extend(_split_sentences(item))
    return out or [part]


def _merge_into_bubbles(subs: List[str], max_parts: int, soft_chars: int, hard_chars: int) -> List[str]:
    """One sentence per bubble first, then fold down to ``max_parts``.

    Sentence granularity is preserved whenever possible (each sentence gets
    its own bubble). Only when there are more fragments than the cap do we
    merge adjacent bubbles, always picking the smallest adjacent pair first
    and preferring merges that stay within ``soft_chars``. Fragments larger
    than ``hard_chars`` are hard-wrapped so no bubble is unbounded. Content
    is never dropped."""
    bubbles: List[str] = []
    for sub in subs:
        while len(sub) > hard_chars:
            bubbles.append(sub[:hard_chars])
            sub = sub[hard_chars:].lstrip()
        if sub:
            bubbles.append(sub)

    while len(bubbles) > max_parts:
        best_idx = 0
        best_score = None
        for i in range(len(bubbles) - 1):
            combined = len(bubbles[i]) + 2 + len(bubbles[i + 1])
            # Prefer pairs that fit the soft budget; only break it when no
            # within-budget merge exists.
            score = combined if combined <= soft_chars else combined + 100000
            if best_score is None or score < best_score:
                best_score = score
                best_idx = i
        merged = bubbles[best_idx].rstrip() + "\n\n" + bubbles[best_idx + 1].strip()
        bubbles[best_idx:best_idx + 2] = [merged]
    return bubbles


def split_text_bubbles(text: str, max_parts: int = 0) -> List[str]:
    """Split assistant text for multi-bubble chat delivery.

    Explicit ``[MSG]`` markers are treated as hard boundaries and always
    respected. Inside each marker-delimited part, plain chat text is split on
    paragraph breaks and terminal sentence punctuation, then merged back into
    bubbles under a soft/hard character budget. URLs, Markdown, images and
    code blocks are kept intact.

    ``max_parts=0`` means "use the configured default"
    (``message_bubble_max_parts``, default 5).
    """
    conf_max, soft_chars, hard_chars, enabled = _bubble_conf()
    limit = max_parts if max_parts > 0 else conf_max

    raw = str(text or "")
    explicit = [p.strip() for p in _MSG_MARKER_RE.split(raw) if p.strip()]
    if not explicit and raw.strip():
        explicit = [raw.strip()]
    elif len(explicit) > 1:
        # Explicit [MSG] markers are the model's hard split decision: never
        # fold them together, even when they outnumber the configured cap.
        limit = max(limit, len(explicit))

    if not enabled:
        return explicit

    subs: List[str] = []
    for part in explicit:
        if _is_protected(part) and len(part) > _PROTECTED_MAX_CHARS:
            # Long structured block: keep intact instead of risking a cut.
            subs.append(part)
            continue
        subs.extend(_split_implicit_part(part))

    return _merge_into_bubbles(subs, limit, soft_chars, hard_chars)
