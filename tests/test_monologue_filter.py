import os

os.environ.setdefault("SHENZHI_INSTANCE", "test-monologue-filter")

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
