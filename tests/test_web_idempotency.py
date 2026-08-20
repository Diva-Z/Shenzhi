"""Tests for Web channel inbound idempotency (client_msg_id dedup).

A frontend retry whose previous HTTP response was lost must NOT trigger a
second agent run; the backend has to return the original request_id.
"""

import json

import pytest


@pytest.fixture()
def channel():
    from channel.web import web_channel as wc

    ch = wc.WebChannel()
    # Clean idempotency state between tests (WebChannel is a singleton).
    ch.client_msg_cache.clear()
    return ch


def _post(channel, monkeypatch, client_msg_id, message="在干嘛", session_id="session_test"):
    """Run post_message with a fake request body; count produce() calls."""
    from channel.web import web_channel as wc

    body = json.dumps({
        "session_id": session_id,
        "message": message,
        "stream": True,
        "client_msg_id": client_msg_id,
    }).encode("utf-8")

    monkeypatch.setattr(wc.web, "data", lambda: body)

    produced = []

    class _FakeThread:
        def __init__(self, target=None, args=(), **kwargs):
            self._target = target
            self._args = args

        def start(self):
            produced.append(self._args)

    monkeypatch.setattr(wc.threading, "Thread", _FakeThread)

    resp = json.loads(channel.post_message())
    return resp, produced


def test_duplicate_client_msg_id_returns_same_request_id(channel, monkeypatch):
    resp1, produced1 = _post(channel, monkeypatch, "cmsg-aaa")
    assert resp1["status"] == "success"
    assert resp1["request_id"]
    assert len(produced1) == 1, "first POST must produce exactly once"

    resp2, produced2 = _post(channel, monkeypatch, "cmsg-aaa")
    assert resp2["status"] == "success"
    assert resp2.get("duplicate") is True
    assert resp2["request_id"] == resp1["request_id"], "retry must reuse request_id"
    assert produced2 == [], "retry must NOT produce a second agent run"


def test_distinct_client_msg_ids_both_produce(channel, monkeypatch):
    resp1, produced1 = _post(channel, monkeypatch, "cmsg-one")
    resp2, produced2 = _post(channel, monkeypatch, "cmsg-two")
    assert len(produced1) == 1 and len(produced2) == 1
    assert resp1["request_id"] != resp2["request_id"]


def test_missing_client_msg_id_never_dedupes(channel, monkeypatch):
    # Legacy clients without the field keep working; no dedup is possible.
    resp1, produced1 = _post(channel, monkeypatch, "")
    resp2, produced2 = _post(channel, monkeypatch, "")
    assert len(produced1) == 1 and len(produced2) == 1
    assert resp1["request_id"] != resp2["request_id"]


def test_client_msg_id_expires_after_ttl(channel, monkeypatch):
    from channel.web import web_channel as wc

    channel._remember_client_msg("cmsg-ttl", "req-1", True)
    assert channel._lookup_client_msg("cmsg-ttl") == ("req-1", True)

    now = wc.time.time()
    monkeypatch.setattr(wc.time, "time", lambda: now + wc._CLIENT_MSG_TTL_SECONDS + 1)
    assert channel._lookup_client_msg("cmsg-ttl") is None, "entry must expire after TTL"


def test_cache_pruning_drops_expired_entries(channel, monkeypatch):
    from channel.web import web_channel as wc

    base = wc.time.time()
    channel._remember_client_msg("cmsg-old", "req-old", True)

    # Advance past TTL and add a new entry: the old one must be pruned.
    monkeypatch.setattr(wc.time, "time", lambda: base + wc._CLIENT_MSG_TTL_SECONDS + 5)
    channel._remember_client_msg("cmsg-new", "req-new", True)
    assert "cmsg-old" not in channel.client_msg_cache
    assert "cmsg-new" in channel.client_msg_cache
