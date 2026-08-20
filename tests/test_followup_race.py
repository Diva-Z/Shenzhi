"""Tests for the companion follow-up race condition and restart restore window.

Two fixes are covered here (PR3 fix/companion-continuity):

A. Follow-up race: an inbound user message must register its
   ``_last_user_msg_time`` BEFORE ``produce()`` runs, and ``_trigger_followup``
   must re-check right before executing so a nudge is cancelled if the user
   already spoke. Otherwise the companion can fire a spurious follow-up against
   a message the user has already superseded.

B. Restart restore window: regular chat sessions must restore
   ``conversation_restore_turns`` (default 10) recent turns instead of the old
   ``max(3, max_turns // 6)`` heuristic that only gave ~3 turns.
"""

import asyncio
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from bridge.context import Context, ContextType


def _underlying_class(decorated):
    """Recover the real class behind the ``@singleton`` decorator.

    ``common.singleton.singleton`` replaces the class with a ``get_instance``
    closure, so ``TelegramChannel`` as imported is a function. We reach into
    its closure to grab the wrapped class and instantiate it with ``__new__``,
    bypassing the heavy ``__init__`` (threads, network, config).
    """
    if isinstance(decorated, type):
        return decorated
    for cell in (decorated.__closure__ or []):
        value = cell.cell_contents
        if isinstance(value, type):
            return value
    raise RuntimeError("could not locate underlying class for singleton")


def _make_telegram_channel():
    from channel.telegram import telegram_channel as tc

    cls = _underlying_class(tc.TelegramChannel)
    ch = cls.__new__(cls)
    # Minimal state needed by _on_message / _trigger_followup.
    ch._received_msgs = {}
    ch._followup_lock = threading.Lock()
    ch._last_user_msg_time = {}
    ch._last_bot_msg_time = {}
    ch._last_bot_msg_text = {}
    ch._followup_fired = {}
    ch._followup_stopped = {}
    ch._followup_count = {}
    ch.bot_username = "testbot"
    ch.channel_type = "telegram"
    return ch


# ---------------------------------------------------------------------------
# Fix A, part 1: user msg time recorded BEFORE produce()
# ---------------------------------------------------------------------------

def test_user_msg_time_updated_before_produce(monkeypatch):
    """_on_message must set _last_user_msg_time before invoking produce()."""
    from channel.telegram import telegram_channel as tc

    ch = _make_telegram_channel()

    chat_id = 12345
    str_cid = str(chat_id)

    # Stub out the parsing/composing steps so we exercise only the ordering.
    async def fake_parse(_message):
        return (ContextType.TEXT, "在干嘛", "")

    ch._parse_message = fake_parse
    ch._compute_session_id = lambda _update: "sess1"
    ch._compose_context = lambda *a, **k: Context(ContextType.TEXT, "在干嘛", kwargs={})

    # File cache must report "nothing pending" so we hit the produce path.
    fake_cache = MagicMock()
    fake_cache.get.return_value = None
    monkeypatch.setattr(
        "channel.file_cache.get_file_cache", lambda: fake_cache
    )

    # produce() records whether the user-time was already populated at call time.
    observed = {}

    def produce_side_effect(_ctx):
        observed["user_time_present_at_produce"] = str_cid in ch._last_user_msg_time

    ch.produce = MagicMock(side_effect=produce_side_effect)

    update = SimpleNamespace(
        effective_message=SimpleNamespace(
            message_id=1, date=datetime.now(), text="在干嘛", caption=None
        ),
        effective_chat=SimpleNamespace(id=chat_id, type="private", title=None),
        effective_user=SimpleNamespace(id=999, full_name="Tester", username="tester"),
    )

    asyncio.run(ch._on_message(update, None))

    assert ch.produce.call_count == 1
    assert observed.get("user_time_present_at_produce") is True, (
        "_last_user_msg_time must be set BEFORE produce() to close the race window"
    )
    assert str_cid in ch._last_user_msg_time
    assert ch._followup_fired[str_cid] is False


# ---------------------------------------------------------------------------
# Fix A, part 3: _trigger_followup cancels when the user already spoke
# ---------------------------------------------------------------------------

def test_trigger_followup_cancelled_when_user_spoke():
    """If the user spoke after the bot's last msg, the followup must be dropped."""
    ch = _make_telegram_channel()

    chat_id = "12345"
    now = datetime.now()
    # Bot sent 30s ago, user replied just now -> last_user >= sent_at.
    ch._last_bot_msg_time[chat_id] = now - timedelta(seconds=30)
    ch._last_user_msg_time[chat_id] = now
    ch._last_bot_msg_text[chat_id] = "上次的回复"

    ch.produce = MagicMock()

    asyncio.run(ch._trigger_followup(chat_id, elapsed=120))

    ch.produce.assert_not_called()


def test_trigger_followup_proceeds_when_user_silent():
    """Positive control: with no newer user msg, the followup fires produce()."""
    ch = _make_telegram_channel()

    chat_id = "67890"
    now = datetime.now()
    # Bot sent just now, user last spoke well before -> not cancelled.
    ch._last_bot_msg_time[chat_id] = now
    ch._last_user_msg_time[chat_id] = now - timedelta(seconds=300)
    ch._last_bot_msg_text[chat_id] = "上次的回复"

    ch.produce = MagicMock()

    asyncio.run(ch._trigger_followup(chat_id, elapsed=120))

    ch.produce.assert_called_once()


# ---------------------------------------------------------------------------
# Fix B: restart restore window honours conversation_restore_turns
# ---------------------------------------------------------------------------

def test_restore_turns_respects_config(monkeypatch):
    """Regular chat sessions restore conversation_restore_turns (default 10)."""
    from bridge import agent_initializer as ai

    conf_values = {
        "conversation_persistence": True,
        "agent_max_context_turns": 20,
        "conversation_restore_turns": 10,
    }
    monkeypatch.setattr("config.conf", lambda: conf_values)

    store = MagicMock()
    store.load_messages.return_value = []  # skip the injection branch
    import agent.memory as agent_memory
    monkeypatch.setattr(agent_memory, "get_conversation_store", lambda: store)

    init = ai.AgentInitializer.__new__(ai.AgentInitializer)
    fake_agent = MagicMock()

    init._restore_conversation_history(fake_agent, "user_123")

    store.load_messages.assert_called_once_with("user_123", max_turns=10)


def test_scheduler_session_still_uses_heuristic(monkeypatch):
    """Scheduler sessions keep the bounded max(1, max_turns // 5) window."""
    from bridge import agent_initializer as ai

    conf_values = {
        "conversation_persistence": True,
        "agent_max_context_turns": 20,
        "conversation_restore_turns": 10,
    }
    monkeypatch.setattr("config.conf", lambda: conf_values)

    store = MagicMock()
    store.load_messages.return_value = []
    import agent.memory as agent_memory
    monkeypatch.setattr(agent_memory, "get_conversation_store", lambda: store)

    init = ai.AgentInitializer.__new__(ai.AgentInitializer)
    init._restore_conversation_history(MagicMock(), "scheduler_task_1")

    # 20 // 5 == 4, unaffected by conversation_restore_turns.
    store.load_messages.assert_called_once_with("scheduler_task_1", max_turns=4)
