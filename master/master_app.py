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
import json
import os
import re
import subprocess
import sys
import threading
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import web  # noqa: E402  (web.py, already a project dependency)

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


def _latest_qr_url(name):
    """Last printed WeChat QR link in the instance log, if any."""
    log = os.path.join(PROJECT_ROOT, os.path.basename(_get_log_file(_instance_arg(name))))
    try:
        with open(log, "r", encoding="utf-8", errors="replace") as f:
            tail = f.read()[-60000:]
    except Exception:
        return ""
    hits = _QR_RE.findall(tail)
    return hits[-1] if hits else ""


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
        "web_port": cfg.get("web_port") if cfg else None,
        "web_console": bool(cfg.get("web_console", True)) if cfg else False,
        "followup_first_sec": cfg.get("followup_first_sec"),
        "followup_repeat_sec": cfg.get("followup_repeat_sec"),
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


def _build_persona_config(name, channels, telegram_token, followup_min, followup_max):
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
        cfg["followup_repeat_sec"] = [lo, hi]
    return cfg


# ── HTTP layer ─────────────────────────────────────────────────────────

urls = (
    "/", "Index",
    "/api/overview", "Overview",
    "/api/persona", "PersonaCreate",
    "/api/persona/([a-z0-9_-]+)", "PersonaDetail",
    "/api/instance/([a-z0-9_-]+)/(start|stop|restart)", "InstanceAction",
    "/api/qr/([a-z0-9_-]+)", "QrImage",
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
        })


class PersonaDetail:
    def GET(self, name):
        pdir = os.path.join(_personas_dir(), name)
        if not os.path.isdir(pdir):
            return _json_resp({"error": "人格不存在"}, "404 Not Found")
        info = _persona_info(name)
        info["agent_md"] = _read_text(os.path.join(pdir, "AGENT.md"))
        info["user_md"] = _read_text(os.path.join(pdir, "USER.md"))
        return _json_resp(info)

    def PUT(self, name):
        pdir = os.path.join(_personas_dir(), name)
        if not os.path.isdir(pdir):
            return _json_resp({"error": "人格不存在"}, "404 Not Found")
        b = _body()
        if "agent_md" in b:
            _write_text(os.path.join(pdir, "AGENT.md"), b["agent_md"])
        if "user_md" in b:
            _write_text(os.path.join(pdir, "USER.md"), b["user_md"])

        cfg_path = _config_path(name)
        cfg = _load_json(cfg_path)
        if cfg:
            changed = False
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
                cfg["followup_repeat_sec"] = [lo, hi]
                changed = True
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

        if b.get("mode") == "raw":
            agent_md = (b.get("agent_md") or "").strip()
            user_md = (b.get("user_md") or "").strip()
            if not agent_md or not user_md:
                return _json_resp({"error": "直接编辑模式下 AGENT.md 与 USER.md 均不能为空"}, "400 Bad Request")
        else:
            form = b.get("form") or {}
            if not form.get("bot_name") or not form.get("user_name"):
                return _json_resp({"error": "AI 名字与用户名字必填"}, "400 Bad Request")
            agent_md = _gen_agent_md(form)
            user_md = _gen_user_md(form)

        os.makedirs(pdir, exist_ok=True)
        _write_text(os.path.join(pdir, "AGENT.md"), agent_md)
        _write_text(os.path.join(pdir, "USER.md"), user_md)
        _write_text(os.path.join(pdir, "MEMORY.md"), "# 长期记忆\n\n")

        cfg = _build_persona_config(
            name, channels,
            (b.get("telegram_token") or "").strip(),
            b.get("followup_min") or 180, b.get("followup_max") or 240,
        )
        _save_json(_config_path(name), cfg)
        return _json_resp({"ok": True, "id": name, "config_file": os.path.basename(_config_path(name))})


class InstanceAction:
    def POST(self, name, action):
        with _op_lock:
            ok, msg = _instance_action(name, action)
        return _json_resp({"ok": ok, "message": msg}, "200 OK" if ok else "409 Conflict")


class QrImage:
    def GET(self, name):
        url = _latest_qr_url(name)
        if not url:
            return _json_resp({"error": "暂无二维码（实例未运行或已登录）"}, "404 Not Found")
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
    print(f"沈知主控端: http://{host}:{port}")
    web.httpserver.runsimple(app.wsgifunc(), (host, int(port)))


if __name__ == "__main__":
    main()
