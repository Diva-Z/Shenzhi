# encoding:utf-8
"""
泄漏独白剥离器。

mimo-v2.5 在 thinking 关闭时会间歇性把推理独白直接写进 content，
格式为「<第三人称分析独白><句末标点><1-2 个 ASCII 空格><真实回复>」。
一旦一条泄漏进对话历史，模型会模仿自己之前的回复格式，泄漏开始自我强化
（2026-06-11 在晨风实例观测到完整过程：首条泄漏后几乎每条都带独白）。

剥离采用双重门槛防误伤：
1. 结构特征：句末标点后紧跟 1-2 个 ASCII 空格再接正文。中文 IM 回复里
   几乎不会出现这种半角空格分隔，是推理段与回复段拼接的典型痕迹。
2. 内容特征：空格前的段落必须含元话语标记（"按照人设""简短回应"等
   分析措辞）或第三人称自指（人格名），单纯结构命中不剥。
"""

import os
import re
import threading

from common.log import logger

# 句末标点 + 1-2 个半角空格 + 非空白，即候选拼接点
_SEP_RE = re.compile(r'(?<=[。！？!?…”")])[ \t]{1,2}(?=\S)')

# 推理独白的元话语标记（对照 2026-06-11 泄漏样本整理，刻意保持窄口径）
_META_MARKERS = (
    "人设", "角色", "设定", "按照",
    "回应", "回复", "回答",
    "短句", "简短", "短回复",
    "的逻辑是", "明显是", "可能是在", "这是在", "是在试探",
    "不需要再", "用户",
)

# 从 AGENT.md 提取人格显示名的模式（如「# 角色设定：晨风」「你是晨风，」）
_NAME_PATTERNS = (
    re.compile(r'^#\s*角色设定[:：]\s*([^\s，。,]{1,12})', re.MULTILINE),
    re.compile(r'你是([^\s，。,、“”"]{1,8})[，,]'),
)

_name_cache_lock = threading.Lock()
_name_cache = {}  # persona_id -> display_name ("" 表示已解析但未取到)


def _persona_agent_md_path() -> str:
    from common.utils import expand_path
    from config import conf
    base = expand_path(conf().get("agent_workspace", "~/cow"))
    persona = (conf().get("active_persona") or "").strip()
    if persona:
        return os.path.join(base, "personas", persona, "AGENT.md")
    return os.path.join(base, "AGENT.md")


def get_persona_display_name() -> str:
    """从当前人格的 AGENT.md 提取显示名（如「晨风」），按人格缓存。"""
    try:
        from config import conf
        persona_id = (conf().get("active_persona") or "_root").strip() or "_root"
    except Exception:
        return ""
    with _name_cache_lock:
        if persona_id in _name_cache:
            return _name_cache[persona_id]
    name = ""
    try:
        path = _persona_agent_md_path()
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                head = f.read(2000)
            for pattern in _NAME_PATTERNS:
                m = pattern.search(head)
                if m:
                    name = m.group(1).strip()
                    break
    except Exception as e:
        logger.debug(f"[MonologueFilter] persona name resolve failed: {e}")
    with _name_cache_lock:
        _name_cache[persona_id] = name
    return name


def _prefix_is_monologue(prefix: str, persona_name: str) -> bool:
    if len(prefix) < 10 or len(prefix) > 500:
        return False
    if "\n" in prefix or "[MSG]" in prefix:
        return False
    if any(marker in prefix for marker in _META_MARKERS):
        return True
    # 第三人称自指：独白里人格以旁观视角谈论"自己会怎么回"
    if persona_name and persona_name in prefix:
        return True
    return False


def _strip_once(text: str, persona_name: str) -> str:
    for m in _SEP_RE.finditer(text):
        prefix = text[:m.start()]
        reply = text[m.end():]
        if not reply.strip():
            return text
        if _prefix_is_monologue(prefix, persona_name):
            return reply
    return text


def strip_leaked_monologue(text: str) -> str:
    """剥掉回复正文开头泄漏的推理独白；无命中时原样返回。"""
    if not text or " " not in text:
        return text
    persona_name = get_persona_display_name()
    result = text
    # 独白可能含内部拼接点被分次剥离，给个小上限防御
    for _ in range(3):
        stripped = _strip_once(result, persona_name)
        if stripped == result:
            break
        result = stripped
    if result is not text and result != text:
        logger.warning(
            "[MonologueFilter] stripped leaked reasoning prefix: "
            f"{text[:100]!r} -> {result[:60]!r}"
        )
    return result
