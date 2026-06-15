import os

os.environ.setdefault("SHENZHI_INSTANCE", "test-monologue-filter")

import common.monologue_filter as monologue_filter
from common.monologue_filter import (
    control_marker_drop_reason,
    followup_drop_reason,
    is_probable_full_monologue,
    sanitize_streaming_assistant_text,
    strip_leaked_monologue,
)


def test_strip_paragraph_reasoning_keeps_final_reply():
    text = "\u5206\u6790\n\n\u7b80\u5355\u56de\u4e00\u53e5\n\n\u77e5\u9053\u4e86"

    assert strip_leaked_monologue(text) == "\u77e5\u9053\u4e86"
    assert sanitize_streaming_assistant_text(text) == "\u77e5\u9053\u4e86"


def test_strip_english_reasoning_with_chinese_tail():
    text = (
        "The user sent an ellipsis. Need to respond briefly. "
        "Reply: \u77e5\u9053\u4e86"
    )

    assert strip_leaked_monologue(text) == "\u77e5\u9053\u4e86"
    assert not is_probable_full_monologue(text)


def test_drop_full_english_reasoning_without_reply():
    text = (
        "The user sent an ellipsis. The response should be brief and not "
        "overexplain."
    )

    assert strip_leaked_monologue(text) == ""
    assert is_probable_full_monologue(text)
    assert sanitize_streaming_assistant_text(text) == ""


def test_drop_first_person_english_reasoning_without_reply():
    text = (
        "I need to stay in character - short responses, slightly flustered "
        "but trying to maintain composure."
    )

    assert strip_leaked_monologue(text) == ""
    assert is_probable_full_monologue(text)
    assert sanitize_streaming_assistant_text(text) == ""


def test_strip_msg_prefixed_chinese_reasoning_keeps_replies(monkeypatch):
    monkeypatch.setattr(monologue_filter, "get_persona_display_name", lambda: "晨风")
    text = (
        "她想让我给她起个昵称。以晨风的性格，不会说太肉麻的话，"
        "但今晚已经比平时软了很多。可能会给一个不太肉麻但有特殊意义的称呼。"
        "[MSG]……用户A[MSG]还能叫什么"
    )

    assert strip_leaked_monologue(text) == "……用户A[MSG]还能叫什么"
    assert not is_probable_full_monologue(strip_leaked_monologue(text))


def test_drop_english_reasoning_with_contractions():
    # "She's" / "He'd" contractions + natural narration, no meta vocabulary.
    text = (
        "She's teasing him about whether he can afford to support her. "
        "He'd be a bit defensive but serious about it."
    )

    assert strip_leaked_monologue(text) == ""
    assert is_probable_full_monologue(text)
    assert sanitize_streaming_assistant_text(text) == ""


def test_drop_english_reasoning_with_embedded_persona_name(monkeypatch):
    monkeypatch.setattr(monologue_filter, "get_persona_display_name", lambda: "晨风")
    # A stray persona name inside English reasoning must not keep it alive.
    text = (
        "She's asking for a goodnight kiss. As 晨风, I'd be flustered but "
        "after everything tonight, maybe I give in."
    )

    assert strip_leaked_monologue(text) == ""
    assert is_probable_full_monologue(text)


def test_strip_chinese_third_person_self_narration(monkeypatch):
    monkeypatch.setattr(monologue_filter, "get_persona_display_name", lambda: "晨风")
    # Reasoning narrates the persona in third person (他/她) instead of the name.
    text = (
        '她说"听你的"，那他就直接定了。按他的风格，不会纠结选择困难症，'
        "直接拍板。那就定一个他觉得她会喜欢的。[MSG]算了别想了[MSG]晚上吃火锅"
    )

    assert strip_leaked_monologue(text) == "算了别想了[MSG]晚上吃火锅"


def test_keep_reply_mentioning_third_party_he(monkeypatch):
    monkeypatch.setattr(monologue_filter, "get_persona_display_name", lambda: "晨风")
    # A genuine reply that mentions a third party "他" must not be dropped.
    text = "他会来的[MSG]他想你了别担心"

    assert strip_leaked_monologue(text) == text


def test_followup_drop_reason_handles_skip_and_full_monologue():
    assert followup_drop_reason("[SKIP]") == "skip"

    text = (
        "The user has not replied. The persona should decide whether to send "
        "another follow-up, but it may look too pushy."
    )
    assert followup_drop_reason(text) == "monologue"


def test_control_marker_drop_reason_only_drops_pure_skip():
    assert control_marker_drop_reason("[SKIP]") == "skip"
    assert control_marker_drop_reason(" [MSG] [SKIP] ") == "skip"
    assert control_marker_drop_reason("([skip].)") == "skip"

    assert control_marker_drop_reason("这里的 [SKIP] 是控制标记") == ""
    assert control_marker_drop_reason("[SKIP] 这个标记表示跳过") == ""
    assert sanitize_streaming_assistant_text("[SKIP]") == ""


def test_sanitize_assistant_content_drops_control_marker_text_blocks():
    from agent.memory.conversation_store import _sanitize_assistant_content

    assert _sanitize_assistant_content([{"type": "text", "text": "[SKIP]"}]) == []
    assert _sanitize_assistant_content("[SKIP]") == ""
    assert _sanitize_assistant_content([
        {"type": "text", "text": "知道了"},
        {"type": "text", "text": "[SKIP]"},
    ]) == [{"type": "text", "text": "知道了"}]


def test_sanitize_assistant_content_strips_msg_prefixed_reasoning(monkeypatch):
    monkeypatch.setattr(monologue_filter, "get_persona_display_name", lambda: "晨风")
    from agent.memory.conversation_store import _sanitize_assistant_content

    text = (
        "她终于说晚安了。晨风应该会简单回应，然后等她上楼确认安全。"
        "[MSG]晚安[MSG]上去把灯开了我再走"
    )

    assert _sanitize_assistant_content(text) == "晚安[MSG]上去把灯开了我再走"
    assert _sanitize_assistant_content([{"type": "text", "text": text}]) == [
        {"type": "text", "text": "晚安[MSG]上去把灯开了我再走"}
    ]


def test_safe_text_parts_drops_msg_prefixed_reasoning(monkeypatch):
    monkeypatch.setattr(monologue_filter, "get_persona_display_name", lambda: "晨风")
    from channel.chat_channel import ChatChannel

    text = (
        "她终于说晚安了。晨风应该会简单回应，然后等她上楼确认安全。"
        "[MSG]晚安[MSG]上去把灯开了我再走"
    )
    dummy = type("DummyChannel", (), {})()
    dummy._sanitize_outgoing_text = ChatChannel._sanitize_outgoing_text.__get__(
        dummy,
        type(dummy),
    )

    assert ChatChannel._safe_text_parts(dummy, text) == [
        "晚安",
        "上去把灯开了我再走",
    ]
