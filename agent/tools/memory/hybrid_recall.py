"""Hybrid recall tool — two-stage memory retrieval.

Stage 1: Semantic search over daily memory summaries to position relevant dates/topics.
Stage 2: SQLite precise raw text retrieval from the positioned dates.

Returns both the memory summary context AND exact conversation excerpts, so the
agent gets the "what happened" narrative and the verbatim wording in one call
instead of manually chaining memory_search -> conversation_search.
"""

from __future__ import annotations

import re
from datetime import date as Date
from typing import Any, Dict, List, Optional

from agent.tools.base_tool import BaseTool, ToolResult


# memory/2026-08-20.md, memory/users/alice/2026-08-20.md, memory/daily/... etc.
_DATE_IN_PATH = re.compile(r"(\d{4})-(\d{2})-(\d{2})\.md")

# Summary matches shown in stage-1 section, and per-message snippet budget.
_MAX_SUMMARY_LINES = 5
_SUMMARY_SNIPPET_CHARS = 200
_MESSAGE_SNIPPET_CHARS = 300


def _extract_date_from_path(path: str) -> Optional[Date]:
    """Extract the calendar date from a dated memory file path.

    Returns None for evergreen files (MEMORY.md, knowledge pages) and for
    paths whose date component is not a real calendar date.
    """
    if not path:
        return None
    match = _DATE_IN_PATH.search(path)
    if not match:
        return None
    try:
        return Date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _rank_dates(search_results: List[Any], max_dates: int) -> List[Date]:
    """Pick the top dates from stage-1 hits, ranked by each date's best score."""
    date_scores: Dict[Date, float] = {}
    for result in search_results:
        d = _extract_date_from_path(getattr(result, "path", "") or "")
        if d is None:
            continue
        score = float(getattr(result, "score", 0.0) or 0.0)
        if d not in date_scores or score > date_scores[d]:
            date_scores[d] = score
    ranked = sorted(date_scores.items(), key=lambda item: (-item[1], item[0]))
    return [d for d, _ in ranked[:max_dates]]


def _message_text(message: Dict[str, Any]) -> str:
    """Flatten a stored message's content into displayable text."""
    content = message.get("content", "")
    try:
        from agent.memory.conversation_store import _extract_display_text

        text = _extract_display_text(content)
    except Exception:
        text = content if isinstance(content, str) else str(content)
    return (text or "").strip()


def _format_hybrid_results(
    search_results: List[Any],
    dates: List[Date],
    raw_conversations: Dict[str, List[Dict[str, Any]]],
) -> str:
    """Format combined stage-1 + stage-2 results for agent consumption."""
    parts: List[str] = ["## Memory Summary Matches", ""]

    if search_results:
        for r in search_results[:_MAX_SUMMARY_LINES]:
            path = getattr(r, "path", "?")
            score = float(getattr(r, "score", 0.0) or 0.0)
            snippet = (getattr(r, "snippet", "") or "").replace("\n", " ").strip()
            parts.append(f"- [{path}] (score: {score:.2f}): {snippet[:_SUMMARY_SNIPPET_CHARS]}")
    else:
        parts.append("(no memory summary matched)")

    parts.append("")
    parts.append("## Raw Conversation Excerpts")

    has_excerpt = False
    for d in dates:
        date_str = str(d)
        messages = raw_conversations.get(date_str) or []
        if not messages:
            continue
        has_excerpt = True
        parts.append("")
        parts.append(f"### {date_str} ({len(messages)} messages)")
        parts.append("")
        for msg in messages:
            role = msg.get("role", "?")
            text = _message_text(msg).replace("\n", " ")
            if not text:
                continue
            parts.append(f"**{role}**: {text[:_MESSAGE_SNIPPET_CHARS]}")

    if not has_excerpt:
        parts.append("")
        parts.append("(no dated memory matched, so no raw conversation was retrieved)")

    return "\n".join(parts)


class HybridRecallTool(BaseTool):
    """Two-stage recall: semantic date positioning, then raw text retrieval."""

    name: str = "hybrid_recall"
    description: str = (
        "Two-stage memory recall: first searches memory summaries semantically, "
        "then retrieves exact raw conversation from matched dates. "
        "Use when you need both the summary context and the precise original words."
    )
    params: dict = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query (natural language question or keywords)",
            },
            "max_dates": {
                "type": "integer",
                "description": "How many matched dates to pull raw conversation for (default: 3)",
                "default": 3,
            },
            "max_messages_per_date": {
                "type": "integer",
                "description": "Maximum raw messages to return per date (default: 20)",
                "default": 20,
            },
        },
        "required": ["query"],
    }

    def __init__(self, memory_manager, user_id: Optional[str] = None):
        """
        Args:
            memory_manager: MemoryManager instance used for stage-1 search
            user_id: Optional user ID for scoped search
        """
        super().__init__()
        self.memory_manager = memory_manager
        self.user_id = user_id

    def execute(self, args: dict) -> ToolResult:
        import asyncio

        query = (args.get("query") or "").strip()
        if not query:
            return ToolResult.fail("Error: query parameter is required")

        max_dates = self._positive_int(args.get("max_dates"), self._conf_int("hybrid_recall_max_dates", 3), 10)
        max_messages = self._positive_int(
            args.get("max_messages_per_date"), self._conf_int("hybrid_recall_max_messages", 20), 100
        )

        # --- Stage 1: semantic + keyword search over memory summaries ---
        try:
            search_results = asyncio.run(
                self.memory_manager.search(
                    query=query,
                    user_id=self.user_id,
                    max_results=10,
                    include_shared=True,
                )
            ) or []
        except Exception as e:
            return ToolResult.fail(f"Error searching memory: {e}")

        dates = _rank_dates(search_results, max_dates)

        # --- Stage 2: precise raw conversation from the positioned dates ---
        raw_conversations: Dict[str, List[Dict[str, Any]]] = {}
        if dates:
            try:
                from agent.memory import get_conversation_store

                store = get_conversation_store()
                for d in dates:
                    messages = store.load_messages_by_date(d) or []
                    raw_conversations[str(d)] = messages[:max_messages]
            except Exception as e:
                from common.log import logger

                logger.warning(f"[HybridRecall] Raw conversation retrieval failed: {e}")

        if not search_results and not raw_conversations:
            return ToolResult.success(
                f"No memories found for '{query}'. "
                f"This is normal if no memories have been stored yet."
            )

        return ToolResult.success(
            _format_hybrid_results(search_results, dates, raw_conversations)
        )

    @staticmethod
    def _positive_int(value, default: int, cap: int) -> int:
        """Coerce a tool argument into a sane positive int within [1, cap]."""
        try:
            if value is None:
                value = default
            return max(1, min(int(value), cap))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _conf_int(key: str, fallback: int) -> int:
        try:
            from config import conf

            return int(conf().get(key, fallback))
        except Exception:
            return fallback
