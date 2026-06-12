"""Structured companion personalization profile.

The profile is intentionally lightweight and optional. It lives next to a
persona's AGENT.md as PROFILE.json and is rendered into a short prompt section
only when that file exists.
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict, List, Optional


PROFILE_FILENAME = "PROFILE.json"


DEFAULT_COMPANION_PROFILE: Dict[str, Any] = {
    "version": 1,
    "enabled": True,
    "relationship": {
        "stage": "",
        "user_address": "",
        "bot_address": "",
        "boundaries": [],
        "taboos": [],
    },
    "style": {
        "warmth": 0.7,
        "humor": 0.3,
        "teasing": 0.1,
        "logic": 0.6,
        "initiative": 0.5,
        "reply_length": "short",
        "emoji_density": "few",
    },
    "mood": {
        "current": "stable",
        "expression": "",
    },
    "proactive": {
        "enabled": True,
        "quiet_hours": "23:30-08:30",
        "max_daily": 3,
        "scenarios": [],
    },
    "channels": {
        "telegram": {"notes": ""},
        "weixin": {"notes": ""},
        "web": {"notes": ""},
    },
    "notes": [],
}


_REPLY_LENGTH_LABEL_ZH = {
    "short": "短句，默认 1-3 条短消息",
    "normal": "中等长度，必要时解释清楚",
    "detailed": "可以较详细，但仍避免长篇说教",
}

_REPLY_LENGTH_LABEL_EN = {
    "short": "short by default, usually 1-3 brief message bubbles",
    "normal": "medium length, explain clearly when needed",
    "detailed": "can be more detailed, but avoid lectures",
}

_EMOJI_LABEL_ZH = {
    "none": "几乎不用",
    "few": "少量使用",
    "normal": "适度使用",
    "many": "较多使用",
}

_EMOJI_LABEL_EN = {
    "none": "almost never",
    "few": "sparingly",
    "normal": "moderately",
    "many": "often",
}


def default_companion_profile(
    bot_name: str = "",
    user_name: str = "",
    relationship: str = "",
) -> Dict[str, Any]:
    profile = copy.deepcopy(DEFAULT_COMPANION_PROFILE)
    rel = profile["relationship"]
    rel["stage"] = _clean_str(relationship, 80)
    rel["user_address"] = _clean_str(user_name, 40)
    rel["bot_address"] = _clean_str(bot_name, 40)
    return profile


def profile_path(workspace_dir: str) -> str:
    return os.path.join(workspace_dir, PROFILE_FILENAME)


def load_companion_profile(workspace_dir: str) -> Optional[Dict[str, Any]]:
    """Load and normalize a profile, returning None when it is absent/invalid."""
    path = profile_path(workspace_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    return normalize_companion_profile(raw)


def save_companion_profile(path: str, profile: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(normalize_companion_profile(profile), f, ensure_ascii=False, indent=4)
        f.write("\n")
    os.replace(tmp, path)


def normalize_companion_profile(raw: Dict[str, Any]) -> Dict[str, Any]:
    base = copy.deepcopy(DEFAULT_COMPANION_PROFILE)
    if not isinstance(raw, dict):
        return base

    out = copy.deepcopy(base)
    out["version"] = 1
    out["enabled"] = _as_bool(raw.get("enabled"), base["enabled"])

    rel_raw = raw.get("relationship") if isinstance(raw.get("relationship"), dict) else {}
    out["relationship"] = {
        "stage": _clean_str(rel_raw.get("stage"), 80),
        "user_address": _clean_str(rel_raw.get("user_address"), 40),
        "bot_address": _clean_str(rel_raw.get("bot_address"), 40),
        "boundaries": _clean_list(rel_raw.get("boundaries"), limit=8, item_limit=80),
        "taboos": _clean_list(rel_raw.get("taboos"), limit=8, item_limit=80),
    }

    style_raw = raw.get("style") if isinstance(raw.get("style"), dict) else {}
    out["style"] = {
        "warmth": _clamp_float(style_raw.get("warmth"), base["style"]["warmth"]),
        "humor": _clamp_float(style_raw.get("humor"), base["style"]["humor"]),
        "teasing": _clamp_float(style_raw.get("teasing"), base["style"]["teasing"]),
        "logic": _clamp_float(style_raw.get("logic"), base["style"]["logic"]),
        "initiative": _clamp_float(style_raw.get("initiative"), base["style"]["initiative"]),
        "reply_length": _choice(
            style_raw.get("reply_length"),
            ("short", "normal", "detailed"),
            base["style"]["reply_length"],
        ),
        "emoji_density": _choice(
            style_raw.get("emoji_density"),
            ("none", "few", "normal", "many"),
            base["style"]["emoji_density"],
        ),
    }

    mood_raw = raw.get("mood") if isinstance(raw.get("mood"), dict) else {}
    out["mood"] = {
        "current": _clean_str(mood_raw.get("current"), 40) or base["mood"]["current"],
        "expression": _clean_str(mood_raw.get("expression"), 120),
    }

    proactive_raw = raw.get("proactive") if isinstance(raw.get("proactive"), dict) else {}
    out["proactive"] = {
        "enabled": _as_bool(proactive_raw.get("enabled"), base["proactive"]["enabled"]),
        "quiet_hours": _clean_str(proactive_raw.get("quiet_hours"), 40)
        or base["proactive"]["quiet_hours"],
        "max_daily": _clamp_int(proactive_raw.get("max_daily"), base["proactive"]["max_daily"], 0, 12),
        "scenarios": _clean_list(proactive_raw.get("scenarios"), limit=12, item_limit=80),
    }

    channels_raw = raw.get("channels") if isinstance(raw.get("channels"), dict) else {}
    out["channels"] = {}
    for channel in ("telegram", "weixin", "web"):
        value = channels_raw.get(channel)
        notes = value.get("notes") if isinstance(value, dict) else value
        out["channels"][channel] = {"notes": _clean_str(notes, 120)}

    out["notes"] = _clean_list(raw.get("notes"), limit=10, item_limit=100)
    return out


def companion_profile_prompt_lines(workspace_dir: str, language: str = "zh") -> List[str]:
    profile = load_companion_profile(workspace_dir)
    if not profile or not profile.get("enabled", True):
        return []
    return render_companion_profile_prompt(profile, language)


def render_companion_profile_prompt(profile: Dict[str, Any], language: str = "zh") -> List[str]:
    p = normalize_companion_profile(profile)
    if not p.get("enabled", True):
        return []

    is_en = language == "en"
    lines = [
        "## Companion personalization profile" if is_en else "## 个性化伴侣档案",
        "",
        (
            "The following structured preferences come from PROFILE.json. They supplement AGENT.md; "
            "if they conflict, follow AGENT.md for identity and use this profile for adjustable interaction style."
            if is_en else
            "以下结构化偏好来自 PROFILE.json，用来补充 AGENT.md。身份设定以 AGENT.md 为准；互动温度、称呼、边界和主动程度以本档案为可调偏好。"
        ),
        "",
    ]

    rel = p["relationship"]
    rel_parts = []
    if rel["stage"]:
        rel_parts.append(("relationship stage: " if is_en else "关系阶段：") + rel["stage"])
    if rel["user_address"]:
        rel_parts.append(("address the user as: " if is_en else "称呼用户：") + rel["user_address"])
    if rel["bot_address"]:
        rel_parts.append(("the user may address you as: " if is_en else "用户称呼你：") + rel["bot_address"])
    if rel_parts:
        lines.append("- " + ("; ".join(rel_parts)))

    style = p["style"]
    reply_labels = _REPLY_LENGTH_LABEL_EN if is_en else _REPLY_LENGTH_LABEL_ZH
    emoji_labels = _EMOJI_LABEL_EN if is_en else _EMOJI_LABEL_ZH
    if is_en:
        lines.append(
            "- Style dials: "
            f"warmth={style['warmth']:.2f}, humor={style['humor']:.2f}, "
            f"teasing={style['teasing']:.2f}, logic={style['logic']:.2f}, "
            f"initiative={style['initiative']:.2f}."
        )
        lines.append(
            f"- Reply length: {reply_labels[style['reply_length']]}; "
            f"emoji use: {emoji_labels[style['emoji_density']]}."
        )
    else:
        lines.append(
            "- 互动参数："
            f"温暖度={style['warmth']:.2f}，幽默={style['humor']:.2f}，"
            f"调侃={style['teasing']:.2f}，逻辑感={style['logic']:.2f}，"
            f"主动性={style['initiative']:.2f}。"
        )
        lines.append(
            f"- 回复长度：{reply_labels[style['reply_length']]}；"
            f"表情使用：{emoji_labels[style['emoji_density']]}。"
        )

    mood = p["mood"]
    mood_line = (f"current mood={mood['current']}" if is_en else f"当前 mood：{mood['current']}")
    if mood["expression"]:
        mood_line += ("; expression: " if is_en else "；表达方式：") + mood["expression"]
    lines.append("- " + mood_line)

    proactive = p["proactive"]
    if proactive["enabled"]:
        pro_line = (
            f"Proactive care is allowed, quiet hours {proactive['quiet_hours']}, "
            f"at most {proactive['max_daily']} proactive touches per day."
            if is_en else
            f"允许主动关怀；免打扰时间 {proactive['quiet_hours']}；每天最多主动触达 {proactive['max_daily']} 次。"
        )
    else:
        pro_line = "Do not proactively start new topics unless the user asks." if is_en else "不要主动开启新话题，除非用户明确要求。"
    lines.append("- " + pro_line)

    if proactive["scenarios"]:
        prefix = "Preferred proactive scenarios: " if is_en else "可主动服务场景："
        lines.append("- " + prefix + "；".join(proactive["scenarios"]))

    if rel["boundaries"]:
        lines.append("- " + ("Boundaries: " if is_en else "相处边界：") + "；".join(rel["boundaries"]))
    if rel["taboos"]:
        lines.append("- " + ("Avoid topics/actions: " if is_en else "禁忌/避免：") + "；".join(rel["taboos"]))

    channel_lines = []
    channel_labels = {"telegram": "Telegram", "weixin": "WeChat" if is_en else "微信", "web": "Web"}
    for channel, value in p["channels"].items():
        notes = (value or {}).get("notes", "")
        if notes:
            channel_lines.append(f"{channel_labels.get(channel, channel)}: {notes}")
    if channel_lines:
        lines.append("- " + ("Channel-specific style: " if is_en else "渠道差异：") + "；".join(channel_lines))

    if p["notes"]:
        lines.append("- " + ("Other personalization notes: " if is_en else "其他个性化偏好：") + "；".join(p["notes"]))

    lines.extend([
        "",
        (
            "Use these preferences naturally. Do not mention PROFILE.json or numeric dials to the user unless they ask to edit personalization."
            if is_en else
            "自然使用这些偏好；除非用户要求调整个性化，否则不要主动提到 PROFILE.json 或这些数值。"
        ),
        "",
    ])
    return lines


def _clean_str(value: Any, limit: int) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    text = " ".join(text.split())
    if len(text) > limit:
        return text[:limit].rstrip()
    return text


def _clean_list(value: Any, limit: int, item_limit: int) -> List[str]:
    if isinstance(value, str):
        raw_items = value.replace("\r", "\n").split("\n")
    elif isinstance(value, (list, tuple)):
        raw_items = value
    else:
        raw_items = []
    items = []
    for item in raw_items:
        cleaned = _clean_str(item, item_limit)
        if cleaned and cleaned not in items:
            items.append(cleaned)
        if len(items) >= limit:
            break
    return items


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on", "y"):
            return True
        if lowered in ("0", "false", "no", "off", "n"):
            return False
    if value is None:
        return default
    return bool(value)


def _clamp_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(0.0, min(1.0, number))


def _clamp_int(value: Any, default: int, lower: int, upper: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(lower, min(upper, number))


def _choice(value: Any, allowed: tuple[str, ...], default: str) -> str:
    text = str(value).strip().lower() if value is not None else ""
    return text if text in allowed else default
