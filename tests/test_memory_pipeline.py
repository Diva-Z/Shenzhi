"""Tests for the transactional memory flush pipeline.

Covers the FlushJob state machine, PipelineState persistence/transactions,
the per-persona write coordinator, an end-to-end flush with a mocked LLM, and
date-ranged loading from the conversation store.
"""

import datetime
import json
import sqlite3
import time

import pytest

from agent.memory.coordinator import PersonaMemoryCoordinator
from agent.memory.flush_job import FlushJob, FlushStatus
from agent.memory.pipeline_state import MAX_TRIM_HASHES, PipelineState
from agent.memory.summarizer import MemoryFlushManager


# ---------------------------------------------------------------------------
# FlushJob
# ---------------------------------------------------------------------------

def test_flush_job_success_transition():
    job = FlushJob()
    assert job.status is FlushStatus.PENDING
    assert not job.is_done

    job.mark_running()
    assert job.status is FlushStatus.RUNNING
    assert job.started_at is not None
    assert not job.is_done

    job.mark_success()
    assert job.status is FlushStatus.SUCCESS
    assert job.is_done
    assert job.finished_at is not None
    assert bool(job) is True
    assert job.wait(timeout=0.01) is FlushStatus.SUCCESS


def test_flush_job_failed_transition():
    job = FlushJob()
    job.mark_running()
    job.mark_failed("boom")

    assert job.status is FlushStatus.FAILED
    assert job.error == "boom"
    assert job.is_done
    # Call sites written as `if result:` must treat a failure as falsy.
    assert bool(job) is False


def test_flush_job_skipped_transition():
    job = FlushJob()
    job.mark_running()
    job.mark_skipped()

    assert job.status is FlushStatus.SKIPPED_NO_CONTENT
    assert job.is_done
    # Nothing to write is not an error.
    assert bool(job) is True


def test_flush_job_wait_times_out():
    job = FlushJob()
    job.mark_running()

    status = job.wait(timeout=0.05)

    # wait() reports the timeout to the caller but must NOT mutate the job:
    # the worker may still be running and will set the real terminal state.
    assert status is FlushStatus.TIMEOUT
    assert job.status is FlushStatus.RUNNING
    assert not job.is_done
    assert job.finished_at is None


def test_flush_job_wait_returns_when_worker_finishes():
    import threading

    job = FlushJob(message_hashes={"a"})
    job.mark_running()
    threading.Timer(0.05, job.mark_success).start()

    assert job.wait(timeout=5) is FlushStatus.SUCCESS
    assert job.message_hashes == {"a"}


# ---------------------------------------------------------------------------
# PipelineState
# ---------------------------------------------------------------------------

def test_pipeline_state_survives_restart(tmp_path):
    state = PipelineState(tmp_path)
    tx = state.begin(
        new_trim_hashes={"h1", "h2"},
        new_content_hash="content-1",
        new_dream_hash="2026-08-20:daily-1",
    )
    state.commit(tx)

    # A new instance stands in for a process restart.
    reloaded = PipelineState(tmp_path)
    assert reloaded.is_trimmed("h1")
    assert reloaded.is_trimmed("h2")
    assert reloaded.content_hash_matches("content-1")
    assert reloaded.dream_hash_matches("2026-08-20:daily-1")
    assert not reloaded.is_trimmed("h3")

    # State lives next to the daily memory files, serialized deterministically.
    data = json.loads(state.state_file_path.read_text(encoding="utf-8"))
    assert data["trim_flushed_hashes"] == ["h1", "h2"]
    assert data["last_flushed_content_hash"] == "content-1"


def test_pipeline_state_missing_file_is_empty(tmp_path):
    state = PipelineState(tmp_path)
    assert state.trim_flushed_hashes == set()
    assert state.last_flushed_content_hash == ""
    assert state.last_dream_input_hash == ""


def test_pipeline_state_corrupt_file_is_empty(tmp_path):
    (tmp_path / ".memory_pipeline_state.json").write_text("{not json", encoding="utf-8")

    state = PipelineState(tmp_path)

    assert state.trim_flushed_hashes == set()
    assert not state.is_trimmed("h1")


def test_pipeline_state_commit_moves_pending(tmp_path):
    state = PipelineState(tmp_path)
    tx = state.begin(new_trim_hashes={"h1"})

    # Pending hashes stay invisible until committed.
    assert not state.is_trimmed("h1")
    assert state.has_pending

    state.commit(tx)

    assert state.is_trimmed("h1")
    assert not state.has_pending
    assert state.state_file_path.exists()


def test_pipeline_state_rollback_discards_pending(tmp_path):
    state = PipelineState(tmp_path)
    tx = state.begin(new_trim_hashes={"h1"}, new_content_hash="c1", new_dream_hash="d1")

    state.rollback(tx)

    assert not state.is_trimmed("h1")
    assert not state.content_hash_matches("c1")
    assert not state.dream_hash_matches("d1")
    assert not state.has_pending
    # A rolled-back transaction never touches the disk.
    assert not state.state_file_path.exists()


def test_pipeline_state_rollback_keeps_earlier_commits(tmp_path):
    state = PipelineState(tmp_path)
    tx1 = state.begin(new_trim_hashes={"kept"})
    state.commit(tx1)

    tx2 = state.begin(new_trim_hashes={"dropped"})
    state.rollback(tx2)

    assert state.is_trimmed("kept")
    assert not state.is_trimmed("dropped")
    assert PipelineState(tmp_path).trim_flushed_hashes == {"kept"}


def test_pipeline_state_rollback_isolates_concurrent_transactions(tmp_path):
    """One transaction's rollback must not clear a sibling's pending hashes."""
    state = PipelineState(tmp_path)

    tx_a = state.begin(new_trim_hashes={"a"})
    tx_b = state.begin(new_trim_hashes={"b"})

    # Aborting A leaves B's slot untouched, so committing B still persists "b".
    state.rollback(tx_a)
    state.commit(tx_b)

    assert not state.is_trimmed("a")
    assert state.is_trimmed("b")


def test_pipeline_state_evicts_oldest_over_capacity(tmp_path):
    state = PipelineState(tmp_path)

    # Commit in two batches so insertion order (and thus eviction) is defined.
    tx1 = state.begin(new_trim_hashes={f"old-{i:05d}" for i in range(MAX_TRIM_HASHES)})
    state.commit(tx1)
    assert len(state.trim_flushed_hashes) == MAX_TRIM_HASHES

    tx2 = state.begin(new_trim_hashes={f"new-{i:05d}" for i in range(50)})
    state.commit(tx2)

    hashes = state.trim_flushed_hashes
    assert len(hashes) == MAX_TRIM_HASHES
    # The 50 newest survive; the 50 oldest were evicted FIFO.
    assert "new-00049" in hashes
    assert "old-00000" not in hashes
    assert "old-00049" not in hashes
    assert "old-00050" in hashes
    assert len(PipelineState(tmp_path).trim_flushed_hashes) == MAX_TRIM_HASHES


# ---------------------------------------------------------------------------
# PersonaMemoryCoordinator
# ---------------------------------------------------------------------------

def test_coordinator_is_per_persona():
    PersonaMemoryCoordinator.reset_all()
    try:
        haiyang = PersonaMemoryCoordinator.get("haiyang")
        assert PersonaMemoryCoordinator.get("haiyang") is haiyang
        assert PersonaMemoryCoordinator.get("chenfeng") is not haiyang
    finally:
        PersonaMemoryCoordinator.reset_all()


def test_coordinator_serializes_concurrent_appends(tmp_path):
    import threading

    PersonaMemoryCoordinator.reset_all()
    try:
        daily = tmp_path / "2026-08-20.md"
        daily.write_text("", encoding="utf-8")
        coordinator = PersonaMemoryCoordinator.get("haiyang")

        def _writer(tag):
            for _ in range(20):
                coordinator.append_daily(daily, f"[{tag}]\n")

        threads = [threading.Thread(target=_writer, args=(t,)) for t in "AB"]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        lines = [ln for ln in daily.read_text(encoding="utf-8").splitlines() if ln]
        assert len(lines) == 40
        # No interleaved half-writes.
        assert set(lines) == {"[A]", "[B]"}
    finally:
        PersonaMemoryCoordinator.reset_all()


# ---------------------------------------------------------------------------
# flush_from_messages integration (mocked LLM)
# ---------------------------------------------------------------------------

class _FakeLLM:
    """Minimal llm_model stand-in returning an OpenAI-shaped response."""

    def __init__(self, text="- 用户讨论了记忆刷写流程", error=None):
        self.text = text
        self.error = error
        self.calls = 0

    def call(self, request):
        self.calls += 1
        if self.error:
            raise self.error
        return {"choices": [{"message": {"content": self.text}}]}


MESSAGES = [
    {"role": "user", "content": "帮我确认一下记忆刷写有没有落盘"},
    {"role": "assistant", "content": "已经确认，写入成功"},
]


def _manager(tmp_path, llm):
    return MemoryFlushManager(workspace_dir=tmp_path, llm_model=llm, persona_name="test-persona")


def test_flush_commits_state_and_writes_file(tmp_path):
    manager = _manager(tmp_path, _FakeLLM())

    job = manager.flush_from_messages(MESSAGES, reason="trim", wait=True)

    assert job.status is FlushStatus.SUCCESS
    today = datetime.date.today().isoformat()
    daily = tmp_path / "memory" / f"{today}.md"
    assert "记忆刷写流程" in daily.read_text(encoding="utf-8")

    # Hashes are committed, so a replay of the same turns is a no-op...
    assert all(manager._state.is_trimmed(h) for h in job.message_hashes)
    replay = manager.flush_from_messages(MESSAGES, reason="trim", wait=True)
    assert replay.status is FlushStatus.SKIPPED_NO_CONTENT
    # ...and it survives a restart.
    assert PipelineState(tmp_path / "memory").is_trimmed(next(iter(job.message_hashes)))


def test_flush_writes_to_target_date_file(tmp_path):
    manager = _manager(tmp_path, _FakeLLM())

    job = manager.flush_from_messages(
        MESSAGES, reason="daily_summary", wait=True, target_date="2026-08-17"
    )

    assert job.status is FlushStatus.SUCCESS
    assert (tmp_path / "memory" / "2026-08-17.md").exists()
    assert not (tmp_path / "memory" / f"{datetime.date.today().isoformat()}.md").exists()


def test_flush_rolls_back_state_on_write_failure(tmp_path, monkeypatch):
    manager = _manager(tmp_path, _FakeLLM())

    def _boom(*args, **kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr(
        "agent.memory.summarizer.ensure_daily_memory_file", _boom
    )

    job = manager.flush_from_messages(MESSAGES, reason="trim", wait=True)

    assert job.status is FlushStatus.FAILED
    assert "disk on fire" in (job.error or "")
    assert bool(job) is False
    # Nothing was written, so the messages must remain flushable.
    assert not any(manager._state.is_trimmed(h) for h in job.message_hashes)
    assert not manager._state.has_pending

    monkeypatch.undo()
    retry = manager.flush_from_messages(MESSAGES, reason="trim", wait=True)
    assert retry.status is FlushStatus.SUCCESS


def test_flush_skips_when_llm_finds_nothing(tmp_path):
    manager = _manager(tmp_path, _FakeLLM(text="无"))

    job = manager.flush_from_messages(MESSAGES, reason="trim", wait=True)

    assert job.status is FlushStatus.SKIPPED_NO_CONTENT
    assert not manager._state.has_pending
    # A skip must not consume the hashes: real content may follow later.
    assert not any(manager._state.is_trimmed(h) for h in job.message_hashes)


def test_flush_without_wait_does_not_block(tmp_path):
    class _SlowLLM(_FakeLLM):
        def call(self, request):
            time.sleep(0.5)
            return super().call(request)

    manager = _manager(tmp_path, _SlowLLM())

    t0 = time.monotonic()
    job = manager.flush_from_messages(MESSAGES, reason="trim")
    dispatch_time = time.monotonic() - t0

    assert dispatch_time < 0.3
    assert job.wait(timeout=10) is FlushStatus.SUCCESS


def test_create_daily_summary_dedups_on_content_hash(tmp_path):
    llm = _FakeLLM()
    manager = _manager(tmp_path, llm)

    first = manager.create_daily_summary(MESSAGES, wait=True)
    second = manager.create_daily_summary(MESSAGES, wait=True)

    assert first.status is FlushStatus.SUCCESS
    assert second.status is FlushStatus.SKIPPED_NO_CONTENT
    assert llm.calls == 1
    # The batch hash is durable across restarts too.
    assert PipelineState(tmp_path / "memory").last_flushed_content_hash


def test_create_daily_summary_retries_after_failure(tmp_path, monkeypatch):
    manager = _manager(tmp_path, _FakeLLM())
    monkeypatch.setattr(
        "agent.memory.summarizer.ensure_daily_memory_file",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("nope")),
    )

    failed = manager.create_daily_summary(MESSAGES, wait=True)
    assert failed.status is FlushStatus.FAILED
    # The content hash must NOT be recorded, otherwise the day is lost forever.
    assert not manager._state.last_flushed_content_hash

    monkeypatch.undo()
    assert manager.create_daily_summary(MESSAGES, wait=True).status is FlushStatus.SUCCESS


def test_flush_manager_infers_persona_from_workspace(tmp_path):
    persona_ws = tmp_path / "personas" / "haiyang"
    persona_ws.mkdir(parents=True)

    manager = MemoryFlushManager(workspace_dir=persona_ws)

    assert manager._persona_name == "haiyang"


# ---------------------------------------------------------------------------
# ConversationStore.load_messages_by_date
# ---------------------------------------------------------------------------

@pytest.fixture()
def store(tmp_path):
    from agent.memory.conversation_store import ConversationStore

    return ConversationStore(tmp_path / "conversations.db")


def _ts(date, hour=12, minute=0):
    return int(datetime.datetime.combine(
        date, datetime.time(hour=hour, minute=minute)
    ).timestamp())


def _insert(store, session_id, seq, role, content, created_at):
    conn = sqlite3.connect(str(store._db_path))
    try:
        conn.execute(
            "INSERT OR IGNORE INTO sessions (session_id, created_at, last_active) VALUES (?, ?, ?)",
            (session_id, created_at, created_at),
        )
        conn.execute(
            "INSERT INTO messages (session_id, seq, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, seq, role, json.dumps(content, ensure_ascii=False), created_at),
        )
        conn.commit()
    finally:
        conn.close()


def test_load_messages_by_date_filters_to_the_day(store):
    target = datetime.date(2026, 8, 17)
    _insert(store, "s1", 1, "user", "前一天的话", _ts(target - datetime.timedelta(days=1), 23, 59))
    _insert(store, "s1", 2, "user", "当天上午", _ts(target, 0, 0))
    _insert(store, "s1", 3, "assistant", "当天回复", _ts(target, 9, 30))
    _insert(store, "s1", 4, "user", "当天深夜", _ts(target, 23, 59))
    _insert(store, "s1", 5, "user", "第二天的话", _ts(target + datetime.timedelta(days=1), 0, 0))

    msgs = store.load_messages_by_date(target)

    assert [m["content"] for m in msgs] == ["当天上午", "当天回复", "当天深夜"]


def test_load_messages_by_date_can_filter_by_session(store):
    target = datetime.date(2026, 8, 17)
    _insert(store, "s1", 1, "user", "会话一", _ts(target, 10))
    _insert(store, "s2", 1, "user", "会话二", _ts(target, 11))

    assert [m["content"] for m in store.load_messages_by_date(target, session_id="s2")] == ["会话二"]
    assert len(store.load_messages_by_date(target)) == 2


def test_load_messages_by_date_drops_internal_messages(store):
    target = datetime.date(2026, 8, 17)
    _insert(store, "s1", 1, "user", "[SCHEDULED] 定时任务注入", _ts(target, 8))
    _insert(store, "s1", 2, "user", [{"type": "tool_result", "content": "工具输出"}], _ts(target, 9))
    _insert(store, "s1", 3, "user", "真实提问", _ts(target, 10))

    assert [m["content"] for m in store.load_messages_by_date(target)] == ["真实提问"]
    assert len(store.load_messages_by_date(target, include_internal=True)) == 3


def test_load_messages_by_date_caps_and_warns(store, caplog):
    from agent.memory.conversation_store import MAX_DATE_MESSAGES

    target = datetime.date(2026, 8, 17)
    total = MAX_DATE_MESSAGES + 10
    conn = sqlite3.connect(str(store._db_path))
    try:
        base = _ts(target, 0, 0)
        conn.execute(
            "INSERT OR IGNORE INTO sessions (session_id, created_at, last_active) VALUES (?, ?, ?)",
            ("s1", base, base),
        )
        conn.executemany(
            "INSERT INTO messages (session_id, seq, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
            [
                ("s1", i, "user", json.dumps(f"消息{i}", ensure_ascii=False), base + i)
                for i in range(total)
            ],
        )
        conn.commit()
    finally:
        conn.close()

    with caplog.at_level("WARNING"):
        msgs = store.load_messages_by_date(target)

    assert len(msgs) == MAX_DATE_MESSAGES
    # The cap keeps the most recent messages.
    assert msgs[-1]["content"] == f"消息{total - 1}"
    assert any("exceed" in rec.getMessage() for rec in caplog.records)


def test_created_at_index_exists(store):
    conn = sqlite3.connect(str(store._db_path))
    try:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()}
    finally:
        conn.close()

    assert "idx_messages_created_at" in names
