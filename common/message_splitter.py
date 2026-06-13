"""Utilities for turning one assistant text into chat bubbles."""

from __future__ import annotations

import re
from typing import List


_MSG_MARKER_RE = re.compile(r"\[MSG\]", re.IGNORECASE)
_BLANK_LINE_RE = re.compile(r"\n\s*\n+")
_URL_RE = re.compile(r"https?://", re.IGNORECASE)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_MARKDOWN_LINE_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+|#{1,6}\s+|>|```|\|)")
_TERMINAL_PUNCT = "。！？!?"
_BOUNDARY_TRAIL = _TERMINAL_PUNCT + "」』”’）】》〉)]"


def _skip_implicit_split(text: str) -> bool:
    if len(text) > 220:
        return True
    if _URL_RE.search(text) or "![" in text or "[图片" in text or "[视频" in text:
        return True
    return any(_MARKDOWN_LINE_RE.match(line) for line in text.splitlines())


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
    if _skip_implicit_split(part):
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


def split_text_bubbles(text: str, max_parts: int = 4) -> List[str]:
    """Split assistant text for multi-bubble chat delivery.

    Explicit ``[MSG]`` markers are treated as hard boundaries. Inside each
    marker-delimited part, short plain Chinese text may be split on paragraph
    breaks and terminal sentence punctuation as a conservative fallback.
    """
    raw = str(text or "")
    explicit = [p.strip() for p in _MSG_MARKER_RE.split(raw) if p.strip()]
    if not explicit and raw.strip():
        explicit = [raw.strip()]

    limit = max(max_parts, len(explicit), 1)
    bubbles: List[str] = []
    for part in explicit:
        for sub in _split_implicit_part(part):
            if not sub:
                continue
            if len(bubbles) < limit:
                bubbles.append(sub)
            else:
                bubbles[-1] = (bubbles[-1].rstrip() + "\n\n" + sub.strip()).strip()
    return bubbles
