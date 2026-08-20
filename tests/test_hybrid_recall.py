"""Tests for the hybrid two-stage recall tool.

Stage 1 (semantic memory search) is mocked at the MemoryManager level and
stage 2 (raw conversation retrieval) at the ConversationStore level, so these
tests exercise the chaining, date ranking and output contract without touching
an embedding provider or a real SQLite index.
"""

import datetime
import pathlib

import pytest

from agent.memory.storage import SearchResult
from agent.tools.memory.hybrid_recall import (
    HybridRecallTool,
    _extract_date_from_path,
    _format_hybrid_results,
    _rank_dates,
)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------

class FakeMemoryManager:
    """MemoryManager stand-in exposing only the async search() contract."""

    def __init__(self, results):
        self.results = results
        self.calls = []

    async def search(self, query, user_id=None, max_results=None, include_shared=True):
        self.calls.append({
            "query": query,
            "user_id": user_id,
            "max_results": max_results,
            "include_shared": include_shared,
        })
        return list(self.results)


class FakeConversationStore:
    """ConversationStore stand-in for load_messages_by_date()."""

    def __init__(self, by_date):
        self.by_date = by_date
        self.requested_dates = []

    def load_messages_by_date(self, target_date, session_id=None, include_internal=False):
        self.requested_dates.append(target_date)
        return list(self.by_date.get(str(target_date), []))


def _result(path, score, snippet="summary text"):
    return SearchResult(
        path=path,
        start_line=1,
        end_line=5,
        score=score,
        snippet=snippet,
        source="memory",
    )


@pytest.fixture
def patched_store(monkeypatch):
    """Install a FakeConversationStore behind agent.memory.get_conversation_store."""
    holder = {}

    def install(by_date):
        store = FakeConversationStore(by_date)
        holder["store"] = store
        import agent.memory as memory_pkg

        monkeypatch.setattr(memory_pkg, "get_conversation_store", lambda: store)
        return store

    return install


# ---------------------------------------------------------------------------
# _extract_date_from_path
# ---------------------------------------------------------------------------

def test_extract_date_from_path():
    assert _extract_date_from_path("memory/2026-08-20.md") == datetime.date(2026, 8, 20)
    assert _extract_date_from_path("memory/users/alice/2026-01-02.md") == datetime.date(2026, 1, 2)
    assert _extract_date_from_path("memory/daily/bob/2025-12-31.md") == datetime.date(2025, 12, 31)
    # Windows-style separators still resolve.
    assert _extract_date_from_path("memory\\2026-03-04.md") == datetime.date(2026, 3, 4)

    # Evergreen and non-dated files have no date.
    assert _extract_date_from_path("MEMORY.md") is None
    assert _extract_date_from_path("knowledge/python.md") is None
    assert _extract_date_from_path("") is None
    assert _extract_date_from_path("memory/2026-08-20.txt") is None
    # Well-formed but impossible calendar date must not raise.
    assert _extract_date_from_path("memory/2026-13-45.md") is None


# ---------------------------------------------------------------------------
# Stage chaining
# ---------------------------------------------------------------------------

def test_hybrid_recall_chains_stages(patched_store):
    manager = FakeMemoryManager([
        _result("memory/2026-08-20.md", 0.9, "discussed the deploy plan"),
        _result("MEMORY.md", 0.8, "long-term index"),
    ])
    store = patched_store({
        "2026-08-20": [
            {"role": "user", "content": "when do we deploy?"},
            {"role": "assistant", "content": "friday after the review"},
        ]
    })

    result = HybridRecallTool(manager).execute({"query": "deploy plan"})

    assert result.status == "success"
    # Stage 1 ran with the query.
    assert manager.calls and manager.calls[0]["query"] == "deploy plan"
    # Stage 2 ran only for the dated hit; MEMORY.md contributes no date.
    assert store.requested_dates == [datetime.date(2026, 8, 20)]

    out = result.result
    assert "## Memory Summary Matches" in out
    assert "## Raw Conversation Excerpts" in out
    assert "discussed the deploy plan" in out
    assert "### 2026-08-20 (2 messages)" in out
    assert "**user**: when do we deploy?" in out
    assert "**assistant**: friday after the review" in out


def test_hybrid_recall_no_dates_found(patched_store):
    manager = FakeMemoryManager([
        _result("MEMORY.md", 0.7, "user prefers concise replies"),
        _result("knowledge/python.md", 0.5, "asyncio notes"),
    ])
    store = patched_store({"2026-08-20": [{"role": "user", "content": "unreachable"}]})

    result = HybridRecallTool(manager).execute({"query": "preferences"})

    assert result.status == "success"
    # No dated path → stage 2 must not be queried at all.
    assert store.requested_dates == []

    out = result.result
    assert "user prefers concise replies" in out
    assert "no dated memory matched" in out
    assert "unreachable" not in out


def test_hybrid_recall_respects_max_dates(patched_store):
    manager = FakeMemoryManager([
        _result("memory/2026-08-18.md", 0.4),
        _result("memory/2026-08-19.md", 0.9),
        _result("memory/2026-08-20.md", 0.6),
    ])
    store = patched_store({
        "2026-08-18": [{"role": "user", "content": "oldest"}],
        "2026-08-19": [{"role": "user", "content": "best match"}],
        "2026-08-20": [{"role": "user", "content": "runner up"}],
    })

    result = HybridRecallTool(manager).execute({"query": "anything", "max_dates": 2})

    # Only the two highest-scoring dates are fetched, best first.
    assert store.requested_dates == [
        datetime.date(2026, 8, 19),
        datetime.date(2026, 8, 20),
    ]
    out = result.result
    assert "best match" in out
    assert "runner up" in out
    assert "oldest" not in out


def test_hybrid_recall_respects_max_messages_per_date(patched_store):
    manager = FakeMemoryManager([_result("memory/2026-08-20.md", 0.9)])
    patched_store({
        "2026-08-20": [
            {"role": "user", "content": f"msg-{i}"} for i in range(10)
        ]
    })

    result = HybridRecallTool(manager).execute(
        {"query": "anything", "max_messages_per_date": 3}
    )

    out = result.result
    assert "### 2026-08-20 (3 messages)" in out
    assert "msg-0" in out and "msg-2" in out
    assert "msg-3" not in out


def test_hybrid_recall_requires_query():
    result = HybridRecallTool(FakeMemoryManager([])).execute({"query": "  "})
    assert result.status == "error"


def test_hybrid_recall_empty_search_reports_no_memories(patched_store):
    patched_store({})
    result = HybridRecallTool(FakeMemoryManager([])).execute({"query": "ghosts"})

    assert result.status == "success"
    assert "No memories found for 'ghosts'" in result.result


def test_hybrid_recall_survives_store_failure(monkeypatch):
    """A stage-2 failure must still return the stage-1 summaries."""
    manager = FakeMemoryManager([_result("memory/2026-08-20.md", 0.9, "the deploy plan")])

    class BrokenStore:
        def load_messages_by_date(self, *a, **kw):
            raise RuntimeError("db locked")

    import agent.memory as memory_pkg
    monkeypatch.setattr(memory_pkg, "get_conversation_store", lambda: BrokenStore())

    result = HybridRecallTool(manager).execute({"query": "deploy"})

    assert result.status == "success"
    assert "the deploy plan" in result.result


def test_hybrid_recall_reports_search_failure():
    class BrokenManager:
        async def search(self, **kwargs):
            raise RuntimeError("embedding down")

    result = HybridRecallTool(BrokenManager()).execute({"query": "deploy"})

    assert result.status == "error"
    assert "embedding down" in result.result


# ---------------------------------------------------------------------------
# Ranking and formatting
# ---------------------------------------------------------------------------

def test_rank_dates_keeps_best_score_per_date():
    results = [
        _result("memory/2026-08-20.md", 0.2),
        _result("memory/2026-08-20.md", 0.8),
        _result("memory/2026-08-19.md", 0.5),
        _result("MEMORY.md", 0.99),
    ]
    assert _rank_dates(results, 5) == [
        datetime.date(2026, 8, 20),
        datetime.date(2026, 8, 19),
    ]


def test_format_output_structure():
    results = [_result("memory/2026-08-20.md", 0.87, "line one\nline two")]
    dates = [datetime.date(2026, 8, 20)]
    raw = {"2026-08-20": [{"role": "user", "content": "hello there"}]}

    out = _format_hybrid_results(results, dates, raw)
    lines = out.splitlines()

    assert lines[0] == "## Memory Summary Matches"
    assert "## Raw Conversation Excerpts" in lines
    # Summary section precedes the raw section.
    assert lines.index("## Memory Summary Matches") < lines.index("## Raw Conversation Excerpts")
    # Score is rendered with two decimals and newlines are flattened.
    assert "- [memory/2026-08-20.md] (score: 0.87): line one line two" in out
    assert "### 2026-08-20 (1 messages)" in out
    assert "**user**: hello there" in out


def test_format_output_handles_no_matches():
    out = _format_hybrid_results([], [], {})
    assert "## Memory Summary Matches" in out
    assert "(no memory summary matched)" in out
    assert "## Raw Conversation Excerpts" in out


# ---------------------------------------------------------------------------
# Incremental indexing (MemoryManager.index_file)
# ---------------------------------------------------------------------------

def _memory_manager(tmp_path):
    from agent.memory.config import MemoryConfig
    from agent.memory.manager import MemoryManager

    # No embedding provider: keyword-only, so the test needs no network.
    return MemoryManager(MemoryConfig(workspace_root=str(tmp_path)))


def test_index_file_makes_daily_content_searchable(tmp_path):
    manager = _memory_manager(tmp_path)
    try:
        daily = tmp_path / "memory" / "2026-08-20.md"
        daily.write_text("# Daily Memory: 2026-08-20\n\n- shipped the hybrid recall tool\n", encoding="utf-8")

        assert manager.index_file(daily) is True
        # Stored under the workspace-relative path, so memory_get can resolve it.
        rel = str(pathlib.Path("memory") / "2026-08-20.md")
        assert manager.storage.get_file_hash(rel)

        # Unchanged file is a no-op on the second call.
        assert manager.index_file(daily) is False

        # An append changes the hash, so it gets re-indexed.
        with open(daily, "a", encoding="utf-8") as f:
            f.write("- and wrote its tests\n")
        assert manager.index_file(daily) is True
    finally:
        manager.close()


def test_index_file_skips_missing_and_outside_files(tmp_path):
    manager = _memory_manager(tmp_path)
    try:
        assert manager.index_file(tmp_path / "memory" / "nope.md") is False

        outside = tmp_path.parent / "outside.md"
        outside.write_text("not in the workspace\n", encoding="utf-8")
        assert manager.index_file(outside) is False
    finally:
        manager.close()


def test_index_file_resolves_user_scope(tmp_path):
    from agent.memory.manager import MemoryManager

    workspace = tmp_path
    user_file = workspace / "memory" / "users" / "alice" / "2026-08-20.md"
    shared_file = workspace / "memory" / "2026-08-20.md"

    assert MemoryManager._resolve_scope(user_file, workspace) == ("user", "alice")
    assert MemoryManager._resolve_scope(shared_file, workspace) == ("shared", None)

