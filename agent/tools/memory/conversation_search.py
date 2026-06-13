"""
Raw conversation search tool.

Searches the persisted SQLite conversation log, not summarized memory files.
Use this when the user asks for exact previous wording or whether something
was said before.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

from agent.tools.base_tool import BaseTool, ToolResult


class ConversationSearchTool(BaseTool):
    """Search original persisted chat messages."""

    name: str = "conversation_search"
    description: str = (
        "Search the original persisted conversation log for exact previous "
        "messages. Use this for questions about what the user or assistant "
        "said before, exact wording, or whether a topic was mentioned."
    )
    params: dict = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Keywords or phrase to search in original chat records",
            },
            "session_id": {
                "type": "string",
                "description": "Optional session_id. Defaults to the current session.",
            },
            "search_all_sessions": {
                "type": "boolean",
                "description": "Set true only when the user asks to search beyond the current chat/session.",
                "default": False,
            },
            "role": {
                "type": "string",
                "description": "Optional role filter: user, assistant, or all",
                "default": "all",
            },
            "days": {
                "type": "integer",
                "description": "Optional lookback window in days",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum matches to return (default 8, max 30)",
                "default": 8,
            },
        },
        "required": ["query"],
    }

    def execute(self, args: dict) -> ToolResult:
        query = (args.get("query") or "").strip()
        if not query:
            return ToolResult.fail("Error: query parameter is required")

        session_id = (args.get("session_id") or "").strip()
        if not session_id:
            session_id = self._current_session_id()

        search_all = bool(args.get("search_all_sessions", False))
        if not session_id and not search_all:
            return ToolResult.fail(
                "Error: no current session_id is available. Pass session_id or set search_all_sessions=true."
            )
        if search_all:
            session_id = None

        role = (args.get("role") or "all").strip().lower()
        if role not in ("user", "assistant"):
            role = None

        created_after = None
        days = args.get("days")
        if days:
            try:
                days_int = max(1, min(int(days), 3650))
                created_after = int(time.time()) - days_int * 86400
            except (TypeError, ValueError):
                created_after = None

        try:
            limit = max(1, min(int(args.get("limit") or 8), 30))
        except (TypeError, ValueError):
            limit = 8

        try:
            from agent.memory import get_conversation_store

            result = get_conversation_store().search_messages(
                query=query,
                session_id=session_id,
                role=role,
                created_after=created_after,
                limit=limit,
            )
        except Exception as e:
            return ToolResult.fail(f"Error searching original conversations: {e}")

        matches = result.get("matches") or []
        if not matches:
            scope = "all sessions" if search_all else f"session {session_id}"
            return ToolResult.success(f"No original conversation matches for '{query}' in {scope}.")

        lines = [
            f"Found {len(matches)} original conversation match(es) for '{query}'.",
            "Use conversation_get with session_id and seq to inspect surrounding context.",
            "",
        ]
        for i, item in enumerate(matches, 1):
            ts = self._format_ts(item.get("created_at"))
            title = item.get("title") or "(untitled)"
            role_name = item.get("role", "")
            snippet = item.get("snippet", "")
            lines.append(
                f"{i}. {ts} | {item.get('session_id')} seq={item.get('seq')} "
                f"| {role_name} | {title}"
            )
            lines.append(f"   {snippet}")

        return ToolResult.success("\n".join(lines))

    def _current_session_id(self) -> str:
        ctx = getattr(self, "context", None)
        return getattr(ctx, "_current_session_id", "") or ""

    @staticmethod
    def _format_ts(ts: Optional[int]) -> str:
        if not ts:
            return "unknown-time"
        try:
            return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return str(ts)
