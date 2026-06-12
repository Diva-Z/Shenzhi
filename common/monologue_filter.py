# encoding:utf-8
"""
泄漏独白剥离器。

mimo-v2.5 在 thinking 关闭时会间歇性把推理独白直接写进 content，
格式最早表现为「<第三人称分析独白><句末标点><1-2 个 ASCII 空格><真实回复>」。
后续又观察到「分析\n\n正文」、英文 reasoning 整段写入 content、追问
prompt 引用污染等变体。
一旦一条泄漏进对话历史，模型会模仿自己之前的回复格式，泄漏开始自我强化
（2026-06-11 在晨风实例观测到完整过程：首条泄漏后几乎每条都带独白）。

剥离采用多重门槛防误伤：
1. 结构特征：句末标点后紧跟 1-2 个 ASCII 空格再接正文。中文 IM 回复里
   几乎不会出现这种半角空格分隔，是推理段与回复段拼接的典型痕迹。
2. 内容特征：空格前的段落必须含元话语标记（"按照人设""简短回应"等
   分析措辞）或第三人称自指（人格名），单纯结构命中不剥。
3. 段落特征：首段/前几段是分析、末段是干净回复时，只保留末段。
4. 英文 reasoning 特征：中文人设下以 "The user..." 等元分析开头时，
   若末尾带中文回复则提取中文尾段，否则整条丢弃。
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
    "不需要再", "用户", "对方", "语气", "口吻", "内心", "权衡",
    "不该", "该不该", "要不要", "不发", "发消息",
)

_CJK_RE = re.compile(r'[\u3400-\u9fff]')
_PARAGRAPH_RE = re.compile(r'\n\s*\n+')
_REASONING_HEADING_RE = re.compile(
    r'^\s*(?:分析|思考|推理|判断|内心|草稿|回复思路|思路|reasoning|analysis)\s*[:：]?',
    re.IGNORECASE,
)
_EN_REASONING_START_RE = re.compile(
    r'^\s*(?:the\s+user|user|he|she|they)\s+'
    r'(?:sent|said|wrote|asked|is|seems|appears|probably|likely|just|wants|needs|has)\b',
    re.IGNORECASE,
)
_EN_META_RE = re.compile(
    r'\b(?:user|message|reply|respond|response|persona|character|tone|'
    r'conversation|follow[- ]?up|should|need to|needs to|likely|probably|'
    r'intent|intention|context)\b',
    re.IGNORECASE,
)
_REPLY_LABEL_RE = re.compile(
    r'(?:回复|回应|正文|最终回复|实际回复|final\s+reply|reply|response)\s*[:：]\s*',
    re.IGNORECASE,
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
    if len(prefix) < 6 or len(prefix) > 1200:
        return False
    if "[MSG]" in prefix.upper():
        return False
    if any(marker in prefix for marker in _META_MARKERS):
        return True
    # 第三人称自指：独白里人格以旁观视角谈论"自己会怎么回"
    if persona_name and persona_name in prefix:
        return True
    return False


def _has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def _looks_like_reasoning(text: str, persona_name: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if "[MSG]" in t.upper():
        return False
    if _prefix_is_monologue(t, persona_name):
        return True
    first_line = t.splitlines()[0].strip()
    if _REASONING_HEADING_RE.match(first_line):
        return True
    lower = t.lower()
    if _EN_REASONING_START_RE.match(lower) and _EN_META_RE.search(lower):
        return True
    if not _has_cjk(t) and len(t) >= 40 and _EN_META_RE.search(lower):
        if "user" in lower or "reply" in lower or "respond" in lower:
            return True
    return False


def _extract_cjk_tail(text: str, persona_name: str) -> str:
    """Return the last clean Chinese-looking reply segment embedded in text."""
    if not _has_cjk(text):
        return ""
    pieces = []
    for para in _PARAGRAPH_RE.split(text.replace("\r\n", "\n").replace("\r", "\n")):
        pieces.extend(para.splitlines())
    for piece in reversed(pieces):
        p = piece.strip()
        if not p or not _has_cjk(p):
            continue
        if _looks_like_reasoning(p, persona_name):
            continue
        # Drop an English label before the Chinese reply: "Reply: 知道了"
        m = _CJK_RE.search(p)
        return p[m.start():].strip() if m else p
    return ""


def _strip_once(text: str, persona_name: str) -> str:
    for m in _SEP_RE.finditer(text):
        prefix = text[:m.start()]
        reply = text[m.end():]
        if not reply.strip():
            return text
        if _prefix_is_monologue(prefix, persona_name):
            return reply
    return text


def _strip_labeled_reply(text: str, persona_name: str) -> str:
    for m in _REPLY_LABEL_RE.finditer(text):
        prefix = text[:m.start()]
        reply = text[m.end():].strip()
        if not reply:
            continue
        if _looks_like_reasoning(prefix, persona_name):
            if _looks_like_reasoning(reply, persona_name):
                return _extract_cjk_tail(reply, persona_name) or reply
            return reply
    return text


def _strip_paragraph_reasoning(text: str, persona_name: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    parts = [p.strip() for p in _PARAGRAPH_RE.split(normalized) if p.strip()]
    if len(parts) < 2:
        return text

    leading = "\n\n".join(parts[:-1])
    tail = parts[-1]
    if _looks_like_reasoning(leading, persona_name) or _looks_like_reasoning(parts[0], persona_name):
        if _looks_like_reasoning(tail, persona_name):
            return _extract_cjk_tail(tail, persona_name) or ""
        return tail
    return text


def _strip_full_english_reasoning(text: str, persona_name: str) -> str:
    t = (text or "").strip()
    if not t:
        return text
    if not _looks_like_reasoning(t, persona_name):
        return text
    tail = _extract_cjk_tail(t, persona_name)
    if tail:
        return tail
    if not _has_cjk(t) and (_EN_REASONING_START_RE.match(t) or _EN_META_RE.search(t)):
        return ""
    return text


def _sanitize_text(text: str, persona_name: str) -> str:
    if not text:
        return text
    result = text
    # 独白可能含内部拼接点被分次剥离，给个小上限防御。
    for _ in range(4):
        stripped = _strip_once(result, persona_name)
        stripped = _strip_labeled_reply(stripped, persona_name)
        stripped = _strip_paragraph_reasoning(stripped, persona_name)
        stripped = _strip_full_english_reasoning(stripped, persona_name)
        if stripped == result:
            break
        result = stripped
    return result


def is_probable_full_monologue(text: str) -> bool:
    """True when the text is reasoning/meta output with no user-facing reply."""
    if not text:
        return False
    persona_name = get_persona_display_name()
    cleaned = _sanitize_text(text, persona_name)
    if not cleaned.strip() and text.strip():
        return True
    if cleaned != text:
        return False
    return _looks_like_reasoning(text, persona_name) and not _extract_cjk_tail(text, persona_name)


def sanitize_streaming_assistant_text(text: str) -> str:
    """Return the safe prefix to stream to UI, holding suspicious early text.

    The first tokens are delayed until either the reply looks safe or a clean
    tail can be extracted. This prevents SSE clients from seeing raw reasoning
    that will later be removed at message_end.
    """
    if not text:
        return text
    if control_marker_drop_reason(text):
        return ""
    persona_name = get_persona_display_name()
    cleaned = _sanitize_text(text, persona_name)
    if cleaned != text:
        return cleaned

    stripped = text.lstrip()
    if len(stripped) < 80:
        return ""
    head = stripped[:220]
    if _looks_like_reasoning(head, persona_name):
        return ""
    return text


# 追问回复里模型主动放弃的合法出口标记（prompt 里约定）
_SKIP_RE = re.compile(r'\[\s*SKIP\s*\]', re.IGNORECASE)
_MSG_MARKER_RE = re.compile(r'\[\s*MSG\s*\]', re.IGNORECASE)
_CONTROL_PADDING_CHARS = ' \t\r\n"\'`“”‘’（）()【】{}.,，。;；:：!！?？…-—_*/\\|'


def control_marker_drop_reason(text: str) -> str:
    """Return "skip" when a reply is only an internal control marker.

    `[SKIP]` is a private sentinel used by follow-up prompts. In ordinary chat
    it is never useful user-facing content, but we keep the matcher narrow so a
    normal explanation that mentions `[SKIP]` is not dropped.
    """
    if not text:
        return ""
    candidate = strip_leaked_monologue(str(text)).strip()
    if not candidate:
        return ""
    candidate = _MSG_MARKER_RE.sub("", candidate).strip(_CONTROL_PADDING_CHARS)
    if not candidate:
        return ""
    remainder = _SKIP_RE.sub("", candidate).strip(_CONTROL_PADDING_CHARS)
    if not remainder:
        return "skip"
    return ""

# 追问场景元话题标记：整条回复都在权衡"该不该追问/发消息"本身。
# 刻意只收录决策措辞，不收"不回消息"这类正常追问也可能出现的词。
_FOLLOWUP_META_MARKERS = (
    "追问", "追着问", "不发了", "不发这条", "要不要发", "该不该发",
    "指令", "人设", "口吻", "内心", "权衡",
)


def followup_drop_reason(text: str) -> str:
    """追问回复是否该整条丢弃。返回 "skip" / "monologue" / ""（不丢）。

    skip：模型按 prompt 约定输出 [SKIP]，主动放弃本次追问。
    monologue：整条都是泄漏的内心独白（没有正文可剥，strip_leaked_monologue
    无能为力的形态，2026-06-11 #11）。三重门槛全中才丢：
      1. 无 [MSG]（多气泡回复不会是纯独白）；
      2. 人格名出现在回复里（第三人称自指）；
      3. 含"追问/不发了/指令"等元话题决策措辞。
    仅追问路径调用，正常聊天回复不经过此判定。
    """
    if not text:
        return ""
    if control_marker_drop_reason(text) or _SKIP_RE.search(text):
        return "skip"
    cleaned = strip_leaked_monologue(text)
    if cleaned.strip() and cleaned != text:
        return ""
    if is_probable_full_monologue(text):
        return "monologue"
    if any(marker in text for marker in _FOLLOWUP_META_MARKERS):
        persona_name = get_persona_display_name()
        if persona_name and persona_name in text:
            return "monologue"
    return ""


def strip_leaked_monologue(text: str) -> str:
    """剥掉回复正文开头泄漏的推理独白；无命中时原样返回。"""
    if not text:
        return text
    persona_name = get_persona_display_name()
    result = _sanitize_text(text, persona_name)
    if result is not text and result != text:
        logger.warning(
            "[MonologueFilter] stripped leaked reasoning prefix: "
            f"{text[:100]!r} -> {result[:60]!r}"
        )
    return result
