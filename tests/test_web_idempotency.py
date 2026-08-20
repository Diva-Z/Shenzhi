"""Tests for Web channel inbound idempotency (client_msg_id dedup).

A frontend retry whose previous HTTP response was lost must NOT trigger a
second agent run; the backend has to return the original request_id. The
same client_msg_id reused for a *different* payload is a conflict, not a
retry, and must be rejected with HTTP 409.
"""

import importlib
import json
import sys

import pytest


def _ensure_real_web_module():
    """Guarantee ``channel.web.web_channel.web`` points at the real web.py.

    Sibling test modules (e.g. tests/test_models_handler.py) install a bare
    ``types.ModuleType`` stub into ``sys.modules['web']`` for their own
    isolation. When those tests run first (alphabetical order puts
    test_models_handler before test_web_idempotency), the stub lacks ``ctx``
    and ``HTTPError`` behaves like a plain ``Exception`` without ``.data``.
    Both are required by this suite: the fixture seeds ``web.ctx.status``,
    and the 409-conflict tests read ``err.data`` off a raised
    ``web.HTTPError``. Detect the stub and swap the real module back in.
    """
    from channel.web import web_channel as wc

    if not hasattr(wc.web, "ctx"):
        # Drop the stub so importlib re-loads the real package from disk.
        sys.modules.pop("web", None)
        real_web = importlib.import_module("web")
        wc.web = real_web
    return wc


@pytest.fixture()
def channel():
    wc = _ensure_real_web_module()

    ch = wc.WebChannel()
    # Clean idempotency state between tests (WebChannel is a singleton).
    ch.client_msg_cache.clear()
    # web.py stores response status/headers on a thread-local request context
    # that only exists inside a live request. Seed a minimal one so raising
    # web.HTTPError (used for the 409 conflict) works under the test harness.
    wc.web.ctx.status = "200 OK"
    wc.web.ctx.headers = []
    return ch


def _post(
    channel,
    monkeypatch,
    client_msg_id,
    message="在干嘛",
    session_id="session_test",
    attachments=None,
    is_voice=False,
    stream=True,
):
    """Run post_message with a fake request body; count produce() calls."""
    from channel.web import web_channel as wc

    payload = {
        "session_id": session_id,
        "message": message,
        "stream": stream,
        "client_msg_id": client_msg_id,
        "is_voice": is_voice,
    }
    if attachments is not None:
        payload["attachments"] = attachments
    body = json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(wc.web, "data", lambda: body)

    produced = []

    class _FakeThread:
        def __init__(self, target=None, args=(), **kwargs):
            self._target = target
            self._args = args

        def start(self):
            produced.append(self._args)

    monkeypatch.setattr(wc.threading, "Thread", _FakeThread)

    # HTTPError is raised for deliberate non-200 responses (e.g. 409 conflict);
    # surface it to the caller so tests can assert on the status code.
    raw = channel.post_message()
    resp = json.loads(raw)
    return resp, produced


# ---------------------------------------------------------------------------
# core dedup behaviour
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# low-level cache primitives (composite key + dict entry)
# ---------------------------------------------------------------------------


def test_client_msg_id_expires_after_ttl(channel, monkeypatch):
    from channel.web import web_channel as wc

    key = wc._client_msg_key("session_test", "cmsg-ttl")
    channel._remember_client_msg(key, "req-1", True, "hash-1")
    entry = channel._lookup_client_msg(key)
    assert entry is not None
    assert entry["request_id"] == "req-1"
    assert entry["use_sse"] is True
    assert entry["payload_hash"] == "hash-1"

    now = wc.time.time()
    monkeypatch.setattr(wc.time, "time", lambda: now + wc._CLIENT_MSG_TTL_SECONDS + 1)
    assert channel._lookup_client_msg(key) is None, "entry must expire after TTL"


def test_cache_pruning_drops_expired_entries(channel, monkeypatch):
    from channel.web import web_channel as wc

    base = wc.time.time()
    old_key = wc._client_msg_key("session_test", "cmsg-old")
    new_key = wc._client_msg_key("session_test", "cmsg-new")
    channel._remember_client_msg(old_key, "req-old", True, "h-old")

    # Advance past TTL and add a new entry: the old one must be pruned.
    monkeypatch.setattr(wc.time, "time", lambda: base + wc._CLIENT_MSG_TTL_SECONDS + 5)
    channel._remember_client_msg(new_key, "req-new", True, "h-new")
    assert old_key not in channel.client_msg_cache
    assert new_key in channel.client_msg_cache


# ---------------------------------------------------------------------------
# payload-fingerprint idempotency (change 1)
# ---------------------------------------------------------------------------


def test_same_id_same_payload_returns_cached_request_id(channel, monkeypatch):
    resp1, produced1 = _post(channel, monkeypatch, "cmsg-dup", message="你在忙吗")
    resp2, produced2 = _post(channel, monkeypatch, "cmsg-dup", message="你在忙吗")

    assert len(produced1) == 1 and produced2 == []
    assert resp2.get("duplicate") is True
    assert resp2["request_id"] == resp1["request_id"]


def test_same_id_different_payload_returns_409(channel, monkeypatch):
    from channel.web import web_channel as wc

    resp1, produced1 = _post(channel, monkeypatch, "cmsg-conflict", message="第一条消息")
    assert len(produced1) == 1
    assert resp1["status"] == "success"

    # Same id, different message -> not a legitimate retry.
    with pytest.raises(wc.web.HTTPError) as exc_info:
        _post(channel, monkeypatch, "cmsg-conflict", message="换成了另一句话")

    err = exc_info.value
    assert "409" in str(err), f"expected 409 status, got {err!s}"
    body = json.loads(err.data)
    assert body["error"] == "IDEMPOTENCY_CONFLICT"
    assert "different payload" in body["message"]


def test_different_session_same_client_msg_id_isolated(channel, monkeypatch):
    # Two browser tabs happen to hand out the same client_msg_id: dedup must
    # scope per session_id so tab B doesn't get tab A's request_id back.
    resp_a, produced_a = _post(
        channel, monkeypatch, "cmsg-shared", session_id="session-A"
    )
    resp_b, produced_b = _post(
        channel, monkeypatch, "cmsg-shared", session_id="session-B"
    )

    assert len(produced_a) == 1 and len(produced_b) == 1
    assert resp_b.get("duplicate") is not True
    assert resp_a["request_id"] != resp_b["request_id"]


def test_payload_hash_includes_voice_and_attachments(channel, monkeypatch):
    from channel.web import web_channel as wc

    _post(channel, monkeypatch, "cmsg-voice", message="同一段话", is_voice=False)

    # Only is_voice flips -> different fingerprint -> 409.
    with pytest.raises(wc.web.HTTPError) as voice_exc:
        _post(channel, monkeypatch, "cmsg-voice", message="同一段话", is_voice=True)
    assert "409" in str(voice_exc.value)

    # Attachments change only -> different fingerprint -> 409.
    _post(channel, monkeypatch, "cmsg-atts", message="同一段话", attachments=[])
    with pytest.raises(wc.web.HTTPError) as att_exc:
        _post(
            channel,
            monkeypatch,
            "cmsg-atts",
            message="同一段话",
            attachments=[{"file_type": "file", "file_path": "/tmp/a.txt"}],
        )
    assert "409" in str(att_exc.value)
