import json
from common.companion_profile import (
    PROFILE_FILENAME,
    companion_profile_prompt_lines,
    default_companion_profile,
    load_companion_profile,
    normalize_companion_profile,
    profile_path,
    render_companion_profile_prompt,
    save_companion_profile,
)


def test_absent_profile_does_not_inject_structured_prompt(tmp_path):
    assert companion_profile_prompt_lines(str(tmp_path), "zh") == []


def test_default_profile_uses_names_from_persona_form():
    profile = default_companion_profile("沈知", "用户A", "稳定陪伴")

    assert profile["relationship"]["stage"] == "稳定陪伴"
    assert profile["relationship"]["user_address"] == "用户A"
    assert profile["relationship"]["bot_address"] == "沈知"
    assert profile["style"]["warmth"] == 0.7


def test_normalize_profile_clamps_values_and_cleans_lists():
    profile = normalize_companion_profile({
        "enabled": "false",
        "relationship": {
            "boundaries": ["深夜少追问", "深夜少追问", ""],
            "taboos": "不要讲大道理\n不要反复问为什么不回复",
        },
        "style": {
            "warmth": 2,
            "humor": -1,
            "reply_length": "unknown",
            "emoji_density": "many",
        },
        "proactive": {
            "max_daily": 99,
        },
    })

    assert profile["enabled"] is False
    assert profile["relationship"]["boundaries"] == ["深夜少追问"]
    assert profile["relationship"]["taboos"] == ["不要讲大道理", "不要反复问为什么不回复"]
    assert profile["style"]["warmth"] == 1.0
    assert profile["style"]["humor"] == 0.0
    assert profile["style"]["reply_length"] == "short"
    assert profile["style"]["emoji_density"] == "many"
    assert profile["proactive"]["max_daily"] == 12


def test_load_and_save_profile_round_trip(tmp_path):
    raw = default_companion_profile("晨风", "用户A", "青梅竹马")
    raw["relationship"]["boundaries"] = ["工作时少打扰"]
    save_companion_profile(str(tmp_path / PROFILE_FILENAME), raw)

    loaded = load_companion_profile(str(tmp_path))

    assert profile_path(str(tmp_path)).endswith(PROFILE_FILENAME)
    assert loaded["relationship"]["stage"] == "青梅竹马"
    assert loaded["relationship"]["boundaries"] == ["工作时少打扰"]
    with open(tmp_path / PROFILE_FILENAME, "r", encoding="utf-8") as f:
        assert json.load(f)["version"] == 1


def test_render_profile_prompt_contains_personalization_rules():
    profile = default_companion_profile("沈知", "用户A", "稳定陪伴")
    profile["relationship"]["boundaries"] = ["深夜少追问"]
    profile["relationship"]["taboos"] = ["不讲大道理"]
    profile["proactive"]["scenarios"] = ["早安问候", "GitHub PR 提醒"]
    profile["channels"]["weixin"]["notes"] = "更像熟人聊天，少解释"
    profile["notes"] = ["用户忙时先给一句话版本"]

    text = "\n".join(render_companion_profile_prompt(profile, "zh"))

    assert "个性化伴侣档案" in text
    assert "关系阶段：稳定陪伴" in text
    assert "称呼用户：用户A" in text
    assert "相处边界：深夜少追问" in text
    assert "禁忌/避免：不讲大道理" in text
    assert "GitHub PR 提醒" in text
    assert "微信: 更像熟人聊天，少解释" in text
    assert "不要主动提到 PROFILE.json" in text
