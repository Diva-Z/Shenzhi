"""Tests for cross-channel identity management (PR4).

Exercises IdentityManager over a real (temp-file) ConversationStore, covering
the full lifecycle: create -> bind -> resolve -> linked sessions -> search.
"""

import pytest

from agent.memory.conversation_store import ConversationStore
from agent.memory.identity import IdentityManager


@pytest.fixture()
def store(tmp_path):
    return ConversationStore(tmp_path / "conversations.db")


@pytest.fixture()
def mgr(store):
    return IdentityManager(store)


# ---------------------------------------------------------------------------
# create / bind
# ---------------------------------------------------------------------------

def test_create_identity(mgr):
    identity_id = mgr.create_identity("Hai Yang")
    assert isinstance(identity_id, str)
    assert len(identity_id) == 8

    info = mgr.get_identity_info(identity_id)
    assert info is not None
    assert info["display_name"] == "Hai Yang"


def test_bind_channel(mgr):
    identity_id = mgr.create_identity("Multi")
    assert mgr.bind_channel(identity_id, "telegram", "123456", "tg_user_123456") is True
    assert mgr.bind_channel(identity_id, "weixin", "wxid_abc", "wxid_abc") is True

    info = mgr.get_identity_info(identity_id)
    channels = {b["channel_type"] for b in info["bindings"]}
    assert channels == {"telegram", "weixin"}


def test_bind_duplicate_returns_true(mgr):
    identity_id = mgr.create_identity()
    assert mgr.bind_channel(identity_id, "telegram", "999", "tg_user_999") is True
    # Re-binding the same account to the same identity is idempotent.
    assert mgr.bind_channel(identity_id, "telegram", "999", "tg_user_999") is True

    info = mgr.get_identity_info(identity_id)
    assert len(info["bindings"]) == 1


def test_bind_conflict_returns_false(mgr):
    first = mgr.create_identity("First")
    second = mgr.create_identity("Second")
    assert mgr.bind_channel(first, "telegram", "555", "tg_user_555") is True
    # The same channel account cannot be bound to a different identity.
    assert mgr.bind_channel(second, "telegram", "555", "tg_user_555") is False


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------

def test_resolve_identity_from_binding(store, mgr):
    identity_id = mgr.create_identity()
    # Session exists but is not tagged; binding provides the external id.
    store.append_messages("tg_user_777", [{"role": "user", "content": "hi"}], channel_type="telegram")
    mgr.bind_channel(identity_id, "telegram", "777")  # no session_id passed

    resolved = mgr.resolve_identity("tg_user_777", "telegram")
    assert resolved == identity_id


def test_resolve_identity_caches_in_session(store, mgr):
    identity_id = mgr.create_identity()
    store.append_messages("tg_user_888", [{"role": "user", "content": "hi"}], channel_type="telegram")
    mgr.bind_channel(identity_id, "telegram", "888")

    # First resolve caches identity_id onto the sessions row.
    assert mgr.resolve_identity("tg_user_888", "telegram") == identity_id

    conn = store._connect()
    try:
        row = conn.execute(
            "SELECT identity_id FROM sessions WHERE session_id=?", ("tg_user_888",)
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == identity_id

    # Second resolve hits the sessions table directly (no channel_type needed).
    assert mgr.resolve_identity("tg_user_888") == identity_id


def test_resolve_unlinked_returns_none(store, mgr):
    store.append_messages("tg_user_000", [{"role": "user", "content": "hi"}], channel_type="telegram")
    assert mgr.resolve_identity("tg_user_000", "telegram") is None


# ---------------------------------------------------------------------------
# linked sessions / search
# ---------------------------------------------------------------------------

def test_get_linked_sessions(store, mgr):
    identity_id = mgr.create_identity()
    store.append_messages("tg_user_111", [{"role": "user", "content": "hi tg"}], channel_type="telegram")
    store.append_messages("wxid_222", [{"role": "user", "content": "hi wx"}], channel_type="weixin")
    mgr.bind_channel(identity_id, "telegram", "111", "tg_user_111")
    mgr.bind_channel(identity_id, "weixin", "wxid_222", "wxid_222")

    linked = set(mgr.get_linked_sessions(identity_id))
    assert linked == {"tg_user_111", "wxid_222"}


def test_search_across_identity(store, mgr):
    identity_id = mgr.create_identity()
    store.append_messages(
        "tg_user_1",
        [{"role": "user", "content": "I love hiking in the mountains"}],
        channel_type="telegram",
    )
    store.append_messages(
        "wxid_2",
        [{"role": "user", "content": "hiking gear recommendations please"}],
        channel_type="weixin",
    )
    # An unrelated session that should NOT show up.
    store.append_messages(
        "tg_user_9",
        [{"role": "user", "content": "hiking somewhere else"}],
        channel_type="telegram",
    )
    mgr.bind_channel(identity_id, "telegram", "1", "tg_user_1")
    mgr.bind_channel(identity_id, "weixin", "wxid_2", "wxid_2")

    result = mgr.search_across_identity(identity_id, "hiking")
    found_sessions = {m["session_id"] for m in result["matches"]}
    assert found_sessions == {"tg_user_1", "wxid_2"}


def test_search_across_identity_no_sessions(mgr):
    identity_id = mgr.create_identity()
    result = mgr.search_across_identity(identity_id, "anything")
    assert result["matches"] == []


# ---------------------------------------------------------------------------
# helpers / info
# ---------------------------------------------------------------------------

def test_extract_external_id_patterns():
    f = IdentityManager._extract_external_id
    assert f("tg_user_123456", "telegram") == "123456"
    assert f("discord_user_abc", "discord") == "abc"
    assert f("slack_user_xyz", "slack") == "xyz"
    # WeChat: session_id IS the external id.
    assert f("wxid_hello", "weixin") == "wxid_hello"
    # Feishu group format "from:other" -> take the sender.
    assert f("uu111:oc222", "feishu") == "uu111"
    assert f("uu111", "feishu") == "uu111"
    # Group / unknown patterns yield None.
    assert f("tg_group_555", "telegram") is None
    assert f("session_1700000000", "web") is None
    assert f("", "telegram") is None


def test_identity_info(mgr):
    identity_id = mgr.create_identity("Full Info")
    mgr.bind_channel(identity_id, "telegram", "42", "tg_user_42")

    info = mgr.get_identity_info(identity_id)
    assert info["identity_id"] == identity_id
    assert info["display_name"] == "Full Info"
    assert isinstance(info["created_at"], int)
    assert isinstance(info["updated_at"], int)
    assert len(info["bindings"]) == 1
    b = info["bindings"][0]
    assert b["channel_type"] == "telegram"
    assert b["external_id"] == "42"
    assert b["session_id"] == "tg_user_42"

    # Unknown identity returns None.
    assert mgr.get_identity_info("deadbeef") is None
