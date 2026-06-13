"""
Raw conversation context reader.

Loads neighbouring original messages around a ``session_id`` + ``seq`` anchor
returned by conversation_search.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from agent.tools.base_tool import BaseTool, ToolResult


class ConversationGetTool(BaseTool):
    """Read original chat context around a persisted message."""

    name: str = "conversation_get"
    description: str = (
        "Read original conversation context around a message returned by "
        "conversation_search. Use this to verify exact wording before answering."
    )
    params: dict = {
        "type": "object",
        "properties": {
            "seq": {
                "type": "integer",
                "description": "Message seq returned by conversation_search",
            },
            "session_id": {
                "type": "string",
                "description": "Optional session_id. Defaults to the current session.",
            },
            "before": {
                "type": "integer",
                "description": "How many previous raw rows to include (default 3, max 20)",
                "default": 3,
            },
            "after": {
                "type": "integer",
                "description": "How many following raw rows to include (default 3, max 20)",
                "default": 3,
            },
            "include_internal": {
                "type": "boolean",
                "description": "Include internal tool_result or marker rows",
                "default": False,
            },
        },
        "required": ["seq"],
    }

    def execute(self, args: dict) -> ToolResult:
        if "seq" not in args:
            return ToolResult.fail("Error: seq parameter is required")
        try:
            seq = int(args.get("seq"))
        except (TypeError, ValueError):
            return ToolResult.fail("Error: seq must be an integer")

        session_id = (args.get("session_id") or "").strip()
        if not session_id:
            session_id = self._current_session_id()
        if not session_id:
            return ToolResult.fail("Error: no current session_id is available. Pass session_id.")

        before = self._bounded_int(args.get("before"), default=3)
        after = self._bounded_int(args.get("after"), default=3)
        include_internal = bool(args.get("include_internal", False))

        try:
            from agent.memory import get_conversation_store

            result = get_conversation_store().load_message_context(
                session_id=session_id,
                seq=seq,
                before=before,
                after=after,
                include_internal=include_internal,
            )
        except Exception as e:
            return ToolResult.fail(f"Error reading original conversation context: {e}")

        messages = result.get("messages") or []
        if not messages:
            return ToolResult.success(f"No original conversation context found for {session_id} seq={seq}.")

        session = result.get("session") or {}
        title = session.get("title") or "(untitled)"
        lines = [
            f"Original conversation context: {session_id} | {title} | anchor seq={seq}",
            "",
        ]
        for msg in messages:
            marker = ">>" if msg.get("is_anchor") else "  "
            ts = self._format_ts(msg.get("created_at"))
            role = msg.get("role", "")
            content = (msg.get("content") or "").strip()
            lines.append(f"{marker} [{msg.get('seq')}] {ts} {role}: {content}")

        return ToolResult.success("\n".join(lines))

    def _current_session_id(self) -> str:
        ctx = getattr(self, "context", None)
        return getattr(ctx, "_current_session_id", "") or ""

    @staticmethod
    def _bounded_int(value, default: int) -> int:
        try:
            return max(0, min(int(value), 20))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _format_ts(ts: Optional[int]) -> str:
        if not ts:
            return "unknown-time"
        try:
            return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return str(ts)
