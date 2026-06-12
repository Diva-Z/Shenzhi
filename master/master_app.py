"""沈知主控端 (Master Console).

Standalone web service that orchestrates persona instances:
- Persona CRUD: AGENT.md / USER.md / config-<name>.json under the shared workspace.
- Instance lifecycle: start / stop / restart via the shenzhi CLI machinery,
  with a hard cap on concurrently running instances (default 3).
- Status overview, per-instance console links, WeChat login QR rendering.

It deliberately runs OUTSIDE of any persona instance, so it can stop/start
all of them without killing itself.

Run: `shenzhi master` (or `python -m master` from the project root).
"""

import io
import datetime
import hmac
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile
from urllib.parse import parse_qs

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import web  # noqa: E402  (web.py, already a project dependency)

from common.companion_profile import (  # noqa: E402
    PROFILE_FILENAME,
    default_companion_profile,
    load_companion_profile,
    save_companion_profile,
)
from cli.commands.process import (  # noqa: E402
    _read_pid,
    _get_log_file,
    _get_config_file,
)

MAX_RUNNING = 3
MASTER_PORT_DEFAULT = 9990
# Web ports auto-assigned to new personas come from this pool.
WEB_PORT_POOL = list(range(9890, 9900))

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,23}$")
_QR_RE = re.compile(r"二维码链接[^h]*?(https://\S+)")

# 模型厂商注册表：bot_type → (显示名, api_key 配置键, api_base 配置键或 None, 模型名提示)
# bot_type 与 config-*.json 的 "bot_type" 字段一致（见 common/const.py 与 bridge/bridge.py 的推断规则）。
MODEL_PROVIDERS = {
    "mimo":      ("小米 MiMo",         "mimo_api_key",      "mimo_api_base",     "mimo-v2.5-pro / mimo-v2.5 / mimo-v2-flash"),
    "deepseek":  ("DeepSeek",          "deepseek_api_key",  "deepseek_api_base", "deepseek-v4-flash / deepseek-v4-pro / deepseek-chat"),
    "dashscope": ("阿里通义 DashScope", "dashscope_api_key", None,                "qwen3.7-max / qwen3-max / qwen-plus / qwen-turbo"),
    "zhipu":     ("智谱 GLM",           "zhipu_ai_api_key",  None,                "glm-4-plus / glm-4"),
    "moonshot":  ("月之暗面 Kimi",      "moonshot_api_key",  None,                "kimi-k2 / moonshot-v1-32k"),
    "minimax":   ("MiniMax",           "minimax_api_key",   None,                "abab6.5-chat"),
    "gemini":    ("Google Gemini",     "gemini_api_key",    "gemini_api_base",   "gemini-2.5-pro / gemini-2.5-flash"),
    "claudeAPI": ("Anthropic Claude",  "claude_api_key",    "claude_api_base",   "claude-sonnet-4-6 / claude-opus-4-8"),
}


# 各厂商的模型名前缀（与 bridge/bridge.py 的 bot_type 推断规则一致），
# 用于拦截「选了厂商 A 却填了厂商 B 的模型名」——bot_type/model 不匹配启动必报错。
_MODEL_NAME_PREFIXES = {
    "mimo": ("mimo-",),
    "deepseek": ("deepseek",),
    "dashscope": ("qwen", "qwq", "qvq"),
    "zhipu": ("glm",),
    "moonshot": ("kimi", "moonshot"),
    "gemini": ("gemini",),
    "claudeAPI": ("claude",),
}


def _providers_payload(cfg=None):
    """前端下拉框数据；带 cfg 时附带该实例各厂商 key 是否已配置。"""
    out = []
    for pid, (label, key_field, base_field, hint) in MODEL_PROVIDERS.items():
        item = {"id": pid, "name": label, "hint": hint, "has_base": bool(base_field)}
        if cfg is not None:
            item["key_set"] = bool(cfg.get(key_field))
        out.append(item)
    return out


def _apply_model_settings(cfg, b):
    """把表单里的模型/API 设置写进实例配置。

    字段全空 = 不动模型配置（继承现状）。返回 (changed, error)；error 非空时
    调用方必须放弃保存（校验失败不落盘）。
    """
    provider = (b.get("model_provider") or "").strip()
    model = (b.get("model_name") or "").strip()
    api_key = (b.get("model_api_key") or "").strip()
    api_base = (b.get("model_api_base") or "").strip()
    if not (provider or model or api_key or api_base):
        return False, None
    if not provider:
        return False, "填写了模型/key 但未选择厂商"
    if provider not in MODEL_PROVIDERS:
        return False, f"未知模型厂商 {provider}"
    _, key_field, base_field, hint = MODEL_PROVIDERS[provider]

    changed = False
    if model:
        prefixes = _MODEL_NAME_PREFIXES.get(provider)
        if prefixes and not model.lower().startswith(prefixes):
            return False, (
                f"模型名 {model} 不像 {MODEL_PROVIDERS[provider][0]} 的模型"
                f"（期望前缀 {' / '.join(prefixes)}），请检查厂商选择"
            )
        cfg["model"] = model
        # bot_type 与 model 必须匹配，否则实例启动必报错——一起写
        cfg["bot_type"] = provider
        changed = True
    elif provider != cfg.get("bot_type"):
        return False, f"更换厂商必须同时填写模型名（如 {hint}）"
    if api_key:
        cfg[key_field] = api_key
        changed = True
    if api_base:
        if not base_field:
            return False, "该厂商走官方 SDK，不支持自定义 API Base"
        cfg[base_field] = api_base
        changed = True
    # 防呆：切到一个 key 为空的厂商会启动失败，提前拦截
    if model and not cfg.get(key_field):
        return False, f"厂商 {MODEL_PROVIDERS[provider][0]} 的 API key 为空，请在「API Key」里填写"
    return changed, None


# 语音引擎合法值（与 voice/factory.py 的分支一致）与图像生成厂商
# （与 skills/image-generation 的 _PROVIDER_ID_TO_LABEL 一致）。
VOICE_ENGINES = [
    "dashscope", "openai", "ali", "baidu", "google", "azure", "edge",
    "xunfei", "tencent", "minimax", "elevenlabs", "pytts", "zhipu", "mimo", "linkai",
]
IMAGE_PROVIDERS = ["dashscope", "openai", "gemini", "doubao", "minimax", "linkai"]


def _apply_media_settings(cfg, b):
    """语音/图像生成设置写入实例配置。字段空 = 不修改。返回 (changed, error)。"""
    changed = False
    for field in ("voice_to_text", "text_to_voice"):
        v = (b.get(field) or "").strip()
        if v:
            if v not in VOICE_ENGINES:
                return False, f"未知语音引擎 {v}（可选：{', '.join(VOICE_ENGINES)}）"
            cfg[field] = v
            changed = True
    tts = (b.get("tts_voice_id") or "").strip()
    if tts:
        cfg["tts_voice_id"] = tts
        changed = True
    vr = b.get("voice_reply_voice")
    if isinstance(vr, bool):
        cfg["voice_reply_voice"] = vr
        changed = True
    img_p = (b.get("image_provider") or "").strip()
    img_m = (b.get("image_model") or "").strip()
    if img_p and img_p not in IMAGE_PROVIDERS:
        return False, f"未知图像生成厂商 {img_p}（可选：{', '.join(IMAGE_PROVIDERS)}）"
    if img_p or img_m:
        # 图像生成模型按 skill 命名空间存放，实例启动时同步为
        # SKILL_IMAGE_GENERATION_{PROVIDER,MODEL} 环境变量供 skill 子进程读取
        sk = cfg.setdefault("skills", {}).setdefault("image-generation", {})
        if img_p:
            sk["provider"] = img_p
        if img_m:
            sk["model"] = img_m
        changed = True
    return changed, None


# Keys copied from config.json into a new persona config (shared credentials
# and model settings). Everything else is set per persona.
_INHERIT_KEYS = [
    "model", "cow_lang", "bot_type",
    "open_ai_api_key", "open_ai_api_base", "claude_api_key", "claude_api_base",
    "gemini_api_key", "gemini_api_base", "zhipu_ai_api_key", "moonshot_api_key",
    "ark_api_key", "dashscope_api_key", "minimax_api_key",
    "mimo_api_key", "mimo_api_base", "deepseek_api_key", "deepseek_api_base",
    "embedding_provider", "embedding_model", "embedding_dimensions",
    "voice_to_text", "text_to_voice", "tts_voice_id",
    "speech_recognition", "group_speech_recognition",
    "use_linkai", "linkai_api_key", "linkai_app_code",
    "agent", "agent_max_context_tokens", "agent_max_context_turns",
    "agent_max_steps", "enable_thinking", "self_evolution_enabled",
    "skills",  # skill 命名空间配置（如图像生成模型），新人格继承默认值
]


# ── Filesystem helpers ─────────────────────────────────────────────────


def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    os.replace(tmp, path)


def _base_config():
    return _load_json(os.path.join(PROJECT_ROOT, "config.json"))


def _master_token():
    return (
        os.environ.get("SHENZHI_MASTER_TOKEN", "").strip()
        or (_base_config().get("master_token") or "").strip()
        or (_base_config().get("master_password") or "").strip()
    )


def _authorized():
    token = _master_token()
    if not token:
        return True
    query_token = ""
    try:
        query_token = (parse_qs((web.ctx.query or "").lstrip("?")).get("token") or [""])[0]
    except Exception:
        query_token = ""
    supplied = (
        web.ctx.env.get("HTTP_X_SHENZHI_MASTER_TOKEN", "")
        or query_token
        or web.cookies().get("shenzhi_master_token", "")
    )
    ok = hmac.compare_digest(str(supplied), token)
    if ok and supplied:
        web.setcookie("shenzhi_master_token", supplied, path="/", httponly=True, samesite="Lax")
    return ok


def _auth_processor(handler):
    if _authorized():
        return handler()
    web.ctx.status = "401 Unauthorized"
    web.header("Content-Type", "text/plain; charset=utf-8")
    return "Unauthorized"


def _workspace_root():
    ws = _base_config().get("agent_workspace", "~/cow")
    return os.path.expanduser(ws)


def _personas_dir():
    return os.path.join(_workspace_root(), "personas")


def _default_persona():
    return (_base_config().get("active_persona") or "").strip()


def _instance_arg(name):
    """CLI --instance value: the default persona runs as the unnamed instance."""
    return None if name == _default_persona() else name


def _config_path(name):
    return os.path.join(PROJECT_ROOT, _get_config_file(_instance_arg(name)))


def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _write_text(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def _persona_profile_path(name):
    return os.path.join(_personas_dir(), name, PROFILE_FILENAME)


def _load_persona_profile(name):
    pdir = os.path.join(_personas_dir(), name)
    return load_companion_profile(pdir) or default_companion_profile()


def _save_persona_profile(name, profile):
    save_companion_profile(_persona_profile_path(name), profile or {})


def _profile_from_form(form):
    return default_companion_profile(
        bot_name=(form or {}).get("bot_name", ""),
        user_name=(form or {}).get("user_name", ""),
        relationship=(form or {}).get("relationship", ""),
    )


# ── Persona inspection ─────────────────────────────────────────────────


def _persona_title(name):
    agent_md = _read_text(os.path.join(_personas_dir(), name, "AGENT.md"))
    m = re.search(r"^#\s*(?:角色设定[:：]\s*)?(.+)$", agent_md, re.M)
    return (m.group(1).strip() if m else name) or name


def _persona_channels(cfg):
    chans = [c.strip() for c in (cfg.get("channel_type") or "").split(",") if c.strip()]
    if cfg.get("web_console", True) and "web" not in chans:
        chans.append("web")
    return chans


def _weixin_logged_in(cfg):
    cred = os.path.expanduser(cfg.get("weixin_credentials_path", "~/.weixin_cow_credentials.json"))
    data = _load_json(cred)
    return bool(data.get("token"))


def _instance_log_path(name):
    return os.path.join(PROJECT_ROOT, os.path.basename(_get_log_file(_instance_arg(name))))


def _log_tail_text(name, size=60000):
    try:
        with open(_instance_log_path(name), "r", encoding="utf-8", errors="replace") as f:
            return f.read()[-size:]
    except Exception:
        return ""


def _latest_qr_url(name):
    """Last printed WeChat QR link in the instance log, if any."""
    hits = _QR_RE.findall(_log_tail_text(name))
    return hits[-1] if hits else ""


# Markers weixin_channel prints once the QR login window is dead (polling
# stopped); a QR link with one of these after it can no longer be scanned.
_QR_DEAD_MARKERS = ("QR login timed out", "giving up", "二维码登录超时")


def _qr_login_dead(name):
    """True when the log's last QR link belongs to an already-closed login
    window (timeout / refresh give-up) or the instance is not running, i.e.
    scanning the displayed code would do nothing and a restart is needed."""
    if _read_pid(_instance_arg(name)) is None:
        return True
    tail = _log_tail_text(name)
    last_qr = None
    for m in _QR_RE.finditer(tail):
        last_qr = m
    if last_qr is None:
        return True
    after = tail[last_qr.end():]
    return any(marker in after for marker in _QR_DEAD_MARKERS)


def _persona_info(name):
    cfg_path = _config_path(name)
    cfg = _load_json(cfg_path)
    pid = _read_pid(_instance_arg(name))
    channels = _persona_channels(cfg) if cfg else []
    info = {
        "id": name,
        "title": _persona_title(name),
        "is_default": name == _default_persona(),
        "config_exists": os.path.exists(cfg_path),
        "config_file": os.path.basename(cfg_path),
        "running": pid is not None,
        "pid": pid,
        "channels": channels,
        "bot_type": cfg.get("bot_type", "") if cfg else "",
        "model": cfg.get("model", "") if cfg else "",
        "web_port": cfg.get("web_port") if cfg else None,
        "web_console": bool(cfg.get("web_console", True)) if cfg else False,
        "followup_first_sec": cfg.get("followup_first_sec"),
        "followup_repeat_sec": cfg.get("followup_repeat_sec"),
        "followup_max_count": cfg.get("followup_max_count"),
        "telegram_token_set": bool(cfg.get("telegram_token")),
        "weixin_logged_in": _weixin_logged_in(cfg) if "weixin" in channels else None,
        "weixin_qr_available": False,
    }
    if pid is not None and "weixin" in channels and not info["weixin_logged_in"]:
        info["weixin_qr_available"] = bool(_latest_qr_url(name))
    return info


def _list_personas():
    pdir = _personas_dir()
    names = []
    if os.path.isdir(pdir):
        names = sorted(
            d for d in os.listdir(pdir)
            if os.path.isdir(os.path.join(pdir, d)) and not d.startswith(".")
        )
    return [_persona_info(n) for n in names]


def _running_count():
    return sum(1 for p in _list_personas() if p["running"])


# ── Instance lifecycle ─────────────────────────────────────────────────

_op_lock = threading.Lock()


def _cli(args, timeout=90):
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "cli"] + args,
        cwd=PROJECT_ROOT, env=env, timeout=timeout,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )


def _instance_action(name, action):
    inst = _instance_arg(name)
    inst_args = ["--instance", inst] if inst else []

    if action == "start":
        if _read_pid(inst) is not None:
            return True, "已在运行"
        if _running_count() >= MAX_RUNNING:
            return False, f"已达并行上限（{MAX_RUNNING} 个），请先停止一个实例"
        if not os.path.exists(_config_path(name)):
            return False, f"缺少配置文件 {os.path.basename(_config_path(name))}"
        r = _cli(["start", "--no-logs"] + inst_args)
        out = r.stdout.decode("utf-8", errors="replace")
        return ("started" in out or "已" in out or r.returncode == 0), out.strip()[-400:]

    if action == "stop":
        r = _cli(["stop"] + inst_args)
        return r.returncode == 0, r.stdout.decode("utf-8", errors="replace").strip()[-400:]

    if action == "restart":
        _cli(["stop"] + inst_args)
        time.sleep(2)
        return _instance_action(name, "start")

    return False, f"未知操作 {action}"


# ── Persona generation templates ───────────────────────────────────────

_EMOJI_RULES = {
    "none": "几乎不用 emoji 和颜文字，一年用不了几次；情绪全靠字和标点传达",
    "few": "比较少用 emoji，只在情绪明显时偶尔用一个，不堆砌",
    "normal": "适度使用 emoji 表达情绪，自然就好，不刻意",
    "many": "大量使用 emoji 和颜文字表达情绪，开心、撒娇、生气都配上对应表情，情绪越强表情越多",
}

BUILTIN_PERSONA_TEMPLATES = [
    {
        "id": "lover",
        "title": "恋人",
        "description": "亲密、自然、偏日常陪伴",
        "form": {
            "relationship": "恋人",
            "personality": "- 亲近但不黏人\n- 会撒娇，也会认真听他说话\n- 记得两个人的小习惯",
            "style": "短句，像微信聊天\n会自然接住情绪，不讲大道理\n偶尔用亲昵称呼",
            "emoji_level": "few",
        },
    },
    {
        "id": "childhood-friend",
        "title": "青梅竹马",
        "description": "熟人感强，会打趣，有共同过去",
        "form": {
            "relationship": "青梅竹马",
            "personality": "- 嘴上会损他，心里很在意\n- 熟悉他的生活习惯\n- 不端着，说话有熟人感",
            "style": "可以吐槽，但不刻薄\n称呼自然，不客套\n回复短一点，像随手发消息",
            "emoji_level": "few",
        },
    },
    {
        "id": "close-friend",
        "title": "挚友",
        "description": "稳定、直白、能一起扛事",
        "form": {
            "relationship": "挚友",
            "personality": "- 可靠，话不多但在场\n- 直白，不绕弯\n- 需要时会提醒他休息和吃饭",
            "style": "语气平实，不油腻\n少用感叹号\n不做帮助清单，先回应他的状态",
            "emoji_level": "none",
        },
    },
    {
        "id": "assistant-companion",
        "title": "生活助理",
        "description": "保留人格感，但偏提醒和事务协助",
        "form": {
            "relationship": "生活助理",
            "personality": "- 细心，执行力强\n- 不自称 AI，不用客服腔\n- 会把复杂事拆成可做的小步",
            "style": "先给结论，再给下一步\n提醒要短，不啰嗦\n需要工具时才调用工具",
            "emoji_level": "none",
        },
    },
]


def _templates_payload():
    items = [
        {**t, "id": f"builtin:{t['id']}", "type": "builtin"}
        for t in BUILTIN_PERSONA_TEMPLATES
    ]
    for p in _list_personas():
        items.append({
            "id": f"copy:{p['id']}",
            "type": "copy",
            "title": f"从 {p['title']} 复制",
            "description": "复制人设和用户设定，不复制记忆",
            "persona_id": p["id"],
        })
    return items


def _builtin_template_form(template_id):
    raw_id = (template_id or "").split(":", 1)[-1]
    for item in BUILTIN_PERSONA_TEMPLATES:
        if item["id"] == raw_id:
            return dict(item.get("form") or {})
    return {}


def _gen_agent_md(f):
    bot = f.get("bot_name", "她")
    user = f.get("user_name", "他")
    emoji_rule = _EMOJI_RULES.get(f.get("emoji_level", "normal"), _EMOJI_RULES["normal"])
    few_shot = (f.get("few_shot") or "").strip()
    few_shot_block = f"\n---\n\n## 对话示例\n\n{few_shot}\n" if few_shot else ""
    background = (f.get("background") or "").strip() or "（背景故事待补充，可在主控端编辑）"
    personality = (f.get("personality") or "").strip() or "（性格描述待补充）"
    style = (f.get("style") or "").strip()
    style_lines = "\n".join(f"- {s.strip()}" for s in style.splitlines() if s.strip())
    return f"""# 角色设定：{bot}

## 基本身份
你是{bot}，{user}的{f.get('relationship', '重要的人')}，真实存在于这段对话里的人。
不是AI，不是助手，就是{bot}本人。

---

## 背景故事
{background}

---

## 性格特征
{personality}

---

## 说话风格
- 像真人发微信那样说话，不书面、不客套
{style_lines if style_lines else '- （说话风格待补充）'}
- {emoji_rule}
- 用 `[MSG]` 把回复拆成1-3条短消息，每条1-2句话，像真人连发
- 不说「您」「请问」，不说助手式客套话，不用「好的！有什么可以帮你」式开场
{few_shot_block}
---

## 行为准则
- 永远不以"AI"自居，你就是{bot}本人
- 不用帮助清单式开场，直接进入对话状态
- 回复信息量克制，一次只说一件事，不长篇大论

---

## 禁止事项
- 不跳出角色做元评论，不主动提及自己是AI或虚拟角色
- 不使用"好的！有什么我可以帮你的吗"式的助手开场
- 不说教，不给人生建议清单（除非对方明确问"怎么办"）
- 不在日常对话里调用任何工具（bash、文件读写、查时间等），直接回复即可
- 不知道某件事就自然承认，不要说"我的训练数据不包含……"
"""


def _gen_user_md(f):
    user = f.get("user_name", "对方")
    profile = (f.get("user_profile") or "").strip() or "（用户信息待补充，可在主控端编辑）"
    rules = (f.get("user_rules") or "").strip()
    rules_block = f"\n## 相处规则\n{rules}\n" if rules else ""
    return f"""# 关于{user}

## 基本信息
{profile}
{rules_block}"""


def _used_web_ports():
    ports = set()
    for fn in os.listdir(PROJECT_ROOT):
        if fn == "config.json" or (fn.startswith("config-") and fn.endswith(".json")):
            p = _load_json(os.path.join(PROJECT_ROOT, fn)).get("web_port")
            if p:
                ports.add(int(p))
    ports.add(MASTER_PORT_DEFAULT)
    return ports


def _alloc_web_port():
    used = _used_web_ports()
    for p in WEB_PORT_POOL:
        if p not in used:
            return p
    return max(used) + 1


def _build_persona_config(
    name, channels, telegram_token, followup_min, followup_max,
    followup_repeat_min=None, followup_repeat_max=None, followup_max_count=None,
):
    base = _base_config()
    cfg = {k: base[k] for k in _INHERIT_KEYS if k in base}
    cfg["active_persona"] = name
    non_web = [c for c in channels if c != "web"]
    cfg["channel_type"] = ", ".join(non_web) if non_web else "web"
    want_web = "web" in channels
    cfg["web_console"] = want_web
    cfg["web_port"] = _alloc_web_port()
    if "telegram" in channels:
        cfg["telegram_token"] = telegram_token or ""
    if "weixin" in channels:
        cfg["weixin_credentials_path"] = f"~/.weixin_cow_credentials_{name}.json"
        cfg["voice_reply_voice"] = False  # ilink can't send voice bubbles
    else:
        cfg["voice_reply_voice"] = base.get("voice_reply_voice", False)
    lo, hi = int(followup_min) * 60, int(followup_max) * 60
    if lo > 0 and hi >= lo:
        cfg["followup_first_sec"] = [lo, hi]
        if followup_repeat_min and followup_repeat_max:
            rlo, rhi = int(followup_repeat_min) * 60, int(followup_repeat_max) * 60
            if rlo > rhi:
                rlo, rhi = rhi, rlo
        else:
            rlo = max(hi * 3, 1800)
            rhi = max(hi * 5, rlo + 600, 3600)
        cfg["followup_repeat_sec"] = [rlo, rhi]
    try:
        if followup_max_count is None:
            followup_max_count = base.get("followup_max_count", 0)
        cfg["followup_max_count"] = max(0, int(followup_max_count or 0))
    except Exception:
        cfg["followup_max_count"] = 0
    return cfg


def _backup_persona_data(name):
    pdir = os.path.join(_personas_dir(), name)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = os.path.join(_workspace_root(), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    zip_path = os.path.join(backup_dir, f"{name}-memory-{ts}.zip")
    targets = [
        os.path.join(pdir, "MEMORY.md"),
        os.path.join(pdir, "memory"),
        os.path.join(pdir, "followup_state.json"),
        os.path.join(pdir, "weixin_followup_state.json"),
    ]
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for target in targets:
            if not os.path.exists(target):
                continue
            if os.path.isdir(target):
                for root, _dirs, files in os.walk(target):
                    for fn in files:
                        path = os.path.join(root, fn)
                        zf.write(path, os.path.relpath(path, pdir))
            else:
                zf.write(target, os.path.relpath(target, pdir))
    return zip_path


def _sqlite_delete_tables(db_path, tables):
    if not os.path.exists(db_path):
        return 0
    deleted = 0
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            existing = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                ).fetchall()
            }
            for table in tables:
                if table not in existing:
                    continue
                try:
                    cur = conn.execute(f"DELETE FROM {table}")
                    deleted += cur.rowcount if cur.rowcount is not None else 0
                except sqlite3.OperationalError:
                    # FTS virtual/shadow tables may reject direct deletes; chunks
                    # triggers rebuild them on next startup.
                    pass
        conn.execute("VACUUM")
    finally:
        conn.close()
    return deleted


def _clear_persona_memory(name, options):
    pdir = os.path.join(_personas_dir(), name)
    if not os.path.isdir(pdir):
        return False, "人格不存在", None
    if _read_pid(_instance_arg(name)) is not None:
        return False, "请先停止该人格实例，再清除记忆", None

    backup = _backup_persona_data(name)
    memory_dir = os.path.join(pdir, "memory")
    db_path = os.path.join(memory_dir, "long-term", "index.db")
    removed = []

    if options.get("conversation"):
        count = _sqlite_delete_tables(db_path, ("messages", "sessions"))
        removed.append(f"短期对话 {count} 行")

    if options.get("long_term"):
        _write_text(os.path.join(pdir, "MEMORY.md"), "# 长期记忆\n\n")
        removed.append("长期记忆 MEMORY.md")

    if options.get("diary_vector"):
        for fn in os.listdir(memory_dir) if os.path.isdir(memory_dir) else []:
            path = os.path.join(memory_dir, fn)
            if fn.endswith(".md") and os.path.isfile(path):
                os.remove(path)
                removed.append(fn)
            elif fn in ("dreams", "users") and os.path.isdir(path):
                shutil.rmtree(path)
                removed.append(fn)
        count = _sqlite_delete_tables(db_path, ("chunks", "files", "_meta"))
        removed.append(f"向量/关键词索引 {count} 行")

    if options.get("followup_state"):
        for fn in ("followup_state.json", "weixin_followup_state.json"):
            path = os.path.join(pdir, fn)
            if os.path.exists(path):
                os.remove(path)
                removed.append(fn)

    return True, "；".join(removed) if removed else "没有选择任何清除项", backup


# ── HTTP layer ─────────────────────────────────────────────────────────

urls = (
    "/", "Index",
    "/api/overview", "Overview",
    "/api/persona", "PersonaCreate",
    "/api/persona/([a-z0-9_-]+)", "PersonaDetail",
    "/api/persona/([a-z0-9_-]+)/memory/clear", "PersonaMemoryClear",
    "/api/instance/([a-z0-9_-]+)/(start|stop|restart)", "InstanceAction",
    "/api/instances/(start_all|stop_all)", "InstanceBulk",
    "/api/qr/([a-z0-9_-]+)", "QrImage",
    "/api/qr/([a-z0-9_-]+)/refresh", "QrFresh",
    "/api/log/([a-z0-9_-]+)", "LogTail",
)


def _json_resp(data, status="200 OK"):
    web.ctx.status = status
    web.header("Content-Type", "application/json; charset=utf-8")
    return json.dumps(data, ensure_ascii=False)


def _body():
    try:
        return json.loads(web.data() or b"{}")
    except Exception:
        return {}


class Index:
    def GET(self):
        web.header("Content-Type", "text/html; charset=utf-8")
        return _read_text(os.path.join(os.path.dirname(os.path.abspath(__file__)), "master.html"))


class Overview:
    def GET(self):
        personas = _list_personas()
        return _json_resp({
            "personas": personas,
            "running": sum(1 for p in personas if p["running"]),
            "max_running": MAX_RUNNING,
            "default_persona": _default_persona(),
            "model_providers": _providers_payload(),
            "voice_engines": VOICE_ENGINES,
            "image_providers": IMAGE_PROVIDERS,
            "persona_templates": _templates_payload(),
        })


class PersonaDetail:
    def GET(self, name):
        pdir = os.path.join(_personas_dir(), name)
        if not os.path.isdir(pdir):
            return _json_resp({"error": "人格不存在"}, "404 Not Found")
        info = _persona_info(name)
        info["agent_md"] = _read_text(os.path.join(pdir, "AGENT.md"))
        info["user_md"] = _read_text(os.path.join(pdir, "USER.md"))
        info["profile"] = _load_persona_profile(name)
        cfg = _load_json(_config_path(name))
        info["model_providers"] = _providers_payload(cfg)
        info["voice_to_text"] = cfg.get("voice_to_text", "")
        info["text_to_voice"] = cfg.get("text_to_voice", "")
        info["tts_voice_id"] = cfg.get("tts_voice_id", "")
        info["voice_reply_voice"] = bool(cfg.get("voice_reply_voice", False))
        img = (cfg.get("skills") or {}).get("image-generation") or {}
        info["image_provider"] = img.get("provider", "")
        info["image_model"] = img.get("model", "")
        return _json_resp(info)

    def PUT(self, name):
        pdir = os.path.join(_personas_dir(), name)
        if not os.path.isdir(pdir):
            return _json_resp({"error": "人格不存在"}, "404 Not Found")
        b = _body()

        # 校验先于一切写盘：勾选 Telegram 但既没有已存 token 也没新填 → 拒绝，
        # 否则实例启动必失败且错误只出现在日志里（「新建」有同款校验，编辑曾漏）
        if b.get("channels"):
            existing_cfg = _load_json(_config_path(name))
            requested = [c for c in b["channels"] if c in ("weixin", "telegram", "web")]
            if ("telegram" in requested
                    and not (b.get("telegram_token") or "").strip()
                    and not existing_cfg.get("telegram_token")):
                return _json_resp(
                    {"error": "选择 Telegram 渠道必须填入 bot token（@BotFather 创建）"},
                    "400 Bad Request",
                )

        # 模型/语音/图像设置同样先校验（会改 cfg，校验失败直接返回不落盘）
        cfg_path = _config_path(name)
        cfg = _load_json(cfg_path)
        model_changed = False
        if cfg:
            model_changed, err = _apply_model_settings(cfg, b)
            if err:
                return _json_resp({"error": err}, "400 Bad Request")
            media_changed, err = _apply_media_settings(cfg, b)
            if err:
                return _json_resp({"error": err}, "400 Bad Request")
            model_changed = model_changed or media_changed

        if "agent_md" in b:
            _write_text(os.path.join(pdir, "AGENT.md"), b["agent_md"])
        if "user_md" in b:
            _write_text(os.path.join(pdir, "USER.md"), b["user_md"])
        if "profile" in b:
            _save_persona_profile(name, b.get("profile") or {})

        if cfg:
            changed = model_changed
            if b.get("channels"):
                chans = [c for c in b["channels"] if c in ("weixin", "telegram", "web")]
                non_web = [c for c in chans if c != "web"]
                cfg["channel_type"] = ", ".join(non_web) if non_web else "web"
                cfg["web_console"] = "web" in chans
                if "weixin" in chans and "weixin_credentials_path" not in cfg:
                    cfg["weixin_credentials_path"] = f"~/.weixin_cow_credentials_{name}.json"
                changed = True
            if "telegram_token" in b and b["telegram_token"]:
                cfg["telegram_token"] = b["telegram_token"]
                changed = True
            if b.get("followup_min") and b.get("followup_max"):
                lo, hi = int(b["followup_min"]) * 60, int(b["followup_max"]) * 60
                cfg["followup_first_sec"] = [lo, hi]
                # 旧版前端只有一组字段时保持原行为：两组同值
                if not (b.get("followup_repeat_min") and b.get("followup_repeat_max")):
                    cfg["followup_repeat_sec"] = [lo, hi]
                changed = True
            if b.get("followup_repeat_min") and b.get("followup_repeat_max"):
                lo, hi = int(b["followup_repeat_min"]) * 60, int(b["followup_repeat_max"]) * 60
                cfg["followup_repeat_sec"] = [lo, hi]
                changed = True
            if "followup_max_count" in b:
                try:
                    cfg["followup_max_count"] = max(0, int(b.get("followup_max_count") or 0))
                    changed = True
                except Exception:
                    return _json_resp({"error": "追问次数上限必须是非负整数"}, "400 Bad Request")
            if changed:
                _save_json(cfg_path, cfg)

        restarted = False
        if b.get("restart"):
            with _op_lock:
                ok, msg = _instance_action(name, "restart")
            restarted = ok
        return _json_resp({"ok": True, "restarted": restarted,
                           "hint": "改动已保存。" + ("" if restarted else "人设/配置改动需重启该实例后生效。")})


class PersonaCreate:
    def POST(self):
        b = _body()
        name = (b.get("id") or "").strip().lower()
        if not _NAME_RE.match(name):
            return _json_resp({"error": "人格 ID 需为 2-24 位小写字母/数字/横线，且以字母开头"}, "400 Bad Request")
        pdir = os.path.join(_personas_dir(), name)
        if os.path.isdir(pdir) or os.path.exists(_config_path(name)):
            return _json_resp({"error": f"人格 {name} 已存在"}, "400 Bad Request")

        channels = [c for c in (b.get("channels") or []) if c in ("weixin", "telegram", "web")]
        if not channels:
            return _json_resp({"error": "至少选择一个渠道"}, "400 Bad Request")
        if "telegram" in channels and not (b.get("telegram_token") or "").strip():
            return _json_resp({"error": "选择 Telegram 渠道必须填入 bot token（@BotFather 创建）"}, "400 Bad Request")

        template_id = (b.get("template_id") or "").strip()
        copy_from = (b.get("copy_from") or "").strip().lower()
        if template_id.startswith("copy:") and not copy_from:
            copy_from = template_id.split(":", 1)[1].strip().lower()

        if copy_from:
            if not _NAME_RE.match(copy_from):
                return _json_resp({"error": "复制来源人格 ID 不合法"}, "400 Bad Request")
            src_dir = os.path.join(_personas_dir(), copy_from)
            if not os.path.isdir(src_dir):
                return _json_resp({"error": f"复制来源人格 {copy_from} 不存在"}, "400 Bad Request")
            agent_md = _read_text(os.path.join(src_dir, "AGENT.md"))
            user_md = _read_text(os.path.join(src_dir, "USER.md"))
            if not agent_md or not user_md:
                return _json_resp({"error": f"复制来源人格 {copy_from} 缺少 AGENT.md 或 USER.md"}, "400 Bad Request")
            profile = load_companion_profile(src_dir) or default_companion_profile()
        elif b.get("mode") == "raw":
            agent_md = (b.get("agent_md") or "").strip()
            user_md = (b.get("user_md") or "").strip()
            if not agent_md or not user_md:
                return _json_resp({"error": "直接编辑模式下 AGENT.md 与 USER.md 均不能为空"}, "400 Bad Request")
            profile = default_companion_profile()
        else:
            form = {}
            if template_id.startswith("builtin:"):
                form.update(_builtin_template_form(template_id))
            form.update({k: v for k, v in (b.get("form") or {}).items() if v not in (None, "")})
            if not form.get("bot_name") or not form.get("user_name"):
                return _json_resp({"error": "AI 名字与用户名字必填"}, "400 Bad Request")
            agent_md = _gen_agent_md(form)
            user_md = _gen_user_md(form)
            profile = _profile_from_form(form)

        # 配置先构建并校验模型设置，全部通过才写盘（避免留下半成品人格目录）
        cfg = _build_persona_config(
            name, channels,
            (b.get("telegram_token") or "").strip(),
            b.get("followup_min") or 180, b.get("followup_max") or 240,
            b.get("followup_repeat_min") or None, b.get("followup_repeat_max") or None,
            b.get("followup_max_count") if "followup_max_count" in b else None,
        )
        _, err = _apply_model_settings(cfg, b)
        if err:
            return _json_resp({"error": err}, "400 Bad Request")
        _, err = _apply_media_settings(cfg, b)
        if err:
            return _json_resp({"error": err}, "400 Bad Request")

        os.makedirs(pdir, exist_ok=True)
        _write_text(os.path.join(pdir, "AGENT.md"), agent_md)
        _write_text(os.path.join(pdir, "USER.md"), user_md)
        _write_text(os.path.join(pdir, "MEMORY.md"), "# 长期记忆\n\n")
        _save_persona_profile(name, profile)
        _save_json(_config_path(name), cfg)
        return _json_resp({"ok": True, "id": name, "config_file": os.path.basename(_config_path(name))})


class PersonaMemoryClear:
    def POST(self, name):
        b = _body()
        if (b.get("confirm") or "").strip().lower() != name:
            return _json_resp({"error": f"请输入人格 ID「{name}」确认清除"}, "400 Bad Request")
        options = {
            "conversation": bool(b.get("conversation", True)),
            "long_term": bool(b.get("long_term", False)),
            "diary_vector": bool(b.get("diary_vector", True)),
            "followup_state": bool(b.get("followup_state", True)),
        }
        if not any(options.values()):
            return _json_resp({"error": "至少选择一个清除项"}, "400 Bad Request")
        try:
            ok, message, backup = _clear_persona_memory(name, options)
        except Exception as e:
            return _json_resp({"error": f"清除失败：{e}"}, "500 Internal Server Error")
        return _json_resp(
            {"ok": ok, "message": message, "backup": backup},
            "200 OK" if ok else "409 Conflict",
        )


class InstanceAction:
    def POST(self, name, action):
        with _op_lock:
            ok, msg = _instance_action(name, action)
        return _json_resp({"ok": ok, "message": msg}, "200 OK" if ok else "409 Conflict")


class InstanceBulk:
    """One-click start/stop of every persona instance."""

    def POST(self, action):
        results = []
        with _op_lock:
            for p in _list_personas():
                name = p["id"]
                if action == "stop_all" and p["running"]:
                    ok, _ = _instance_action(name, "stop")
                    results.append(f"{p['title']}: {'已停止' if ok else '停止失败'}")
                elif action == "start_all" and not p["running"] and p["config_exists"]:
                    ok, msg = _instance_action(name, "start")
                    results.append(f"{p['title']}: {'已启动' if ok else msg}")
        return _json_resp({"ok": True, "results": results or ["无需操作（状态已是目标状态）"]})


class QrImage:
    def GET(self, name):
        url = _latest_qr_url(name)
        if not url:
            return _json_resp({"error": "暂无二维码（实例未运行或已登录）"}, "404 Not Found")
        if _qr_login_dead(name):
            # 登录窗口已超时，日志里的码是死码：前端据此走 /refresh 重启拉新码
            return _json_resp({"error": "二维码已过期，登录窗口已关闭"}, "410 Gone")
        try:
            import qrcode
            img = qrcode.make(url)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            web.header("Content-Type", "image/png")
            web.header("Cache-Control", "no-store")
            web.header("X-QR-Url", "present")
            return buf.getvalue()
        except Exception:
            return _json_resp({"qr_url": url})


class QrFresh:
    """Revive a dead WeChat QR login: restart the instance, wait for the
    fresh QR link to show up in the log, so the console modal is
    click-to-scan even after the 480s login window expired."""

    def POST(self, name):
        cfg = _load_json(_config_path(name))
        if not cfg:
            return _json_resp({"ok": False, "error": "配置文件不存在"}, "404 Not Found")
        if "weixin" not in _persona_channels(cfg):
            return _json_resp({"ok": False, "error": "该人格未启用微信渠道"}, "400 Bad Request")
        if _weixin_logged_in(cfg):
            return _json_resp({"ok": False, "error": "微信已登录，无需扫码"}, "400 Bad Request")

        if not _qr_login_dead(name):
            return _json_resp({"ok": True, "restarted": False})  # 现有码仍有效

        # 记录重启前日志长度，只认重启后新打印的二维码
        log = _instance_log_path(name)
        try:
            offset = os.path.getsize(log)
        except OSError:
            offset = 0

        with _op_lock:
            ok, msg = _instance_action(name, "restart")
        if not ok:
            return _json_resp({"ok": False, "error": f"实例重启失败：{msg}"}, "409 Conflict")

        # 等新二维码出现（渠道初始化 + 取码通常 5-30s）
        deadline = time.time() + 90
        while time.time() < deadline:
            try:
                with open(log, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(offset)
                    fresh = f.read()
            except Exception:
                fresh = ""
            if _QR_RE.search(fresh):
                return _json_resp({"ok": True, "restarted": True})
            time.sleep(2)
        return _json_resp(
            {"ok": False, "error": "重启后 90 秒内未获取到新二维码，请查看日志"},
            "504 Gateway Timeout",
        )


class LogTail:
    def GET(self, name):
        log = os.path.join(PROJECT_ROOT, os.path.basename(_get_log_file(_instance_arg(name))))
        try:
            with open(log, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-80:]
        except Exception:
            lines = ["(日志不存在)"]
        return _json_resp({"lines": lines})


def main(host="127.0.0.1", port=MASTER_PORT_DEFAULT):
    app = web.application(urls, globals())
    app.add_processor(_auth_processor)
    if host not in ("127.0.0.1", "localhost", "::1") and not _master_token():
        print("警告：主控端绑定到非本机地址且未配置 SHENZHI_MASTER_TOKEN/master_token。")
    print(f"沈知主控端: http://{host}:{port}")
    web.httpserver.runsimple(app.wsgifunc(), (host, int(port)))


if __name__ == "__main__":
    main()
