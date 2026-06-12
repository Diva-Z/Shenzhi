"""
Telegram channel via Bot API (long polling mode).

Features:
- Single chat & group chat (text / photo / voice / video / document)
- Group trigger: @mention or reply-to-bot (configurable)
- /cancel fast-path matches Web channel behaviour
- Auto-register bot commands menu on startup (mirrors Web slash menu)
- Optional HTTP/SOCKS5 proxy support for restricted networks

Implementation note:
    python-telegram-bot is async-first. We run the bot inside a dedicated
    thread with its own asyncio loop so the rest of cow (which is sync)
    stays untouched. Inbound updates are dispatched onto cow's existing
    sync ChatChannel.produce() pipeline; outbound send() schedules
    coroutines back onto that loop via asyncio.run_coroutine_threadsafe.
"""

import asyncio
import json
import os
import re
import threading
from datetime import datetime, timedelta

from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from channel.chat_channel import ChatChannel, check_prefix
from channel.telegram.telegram_message import TelegramMessage
from common.expired_dict import ExpiredDict
from common.log import logger
from common.singleton import singleton
from config import conf

# Bot command menu, aligned with Web slash commands.
# Top-level commands only; sub-commands are entered with a space (e.g. "/skill list").
TELEGRAM_BOT_COMMANDS = [
    ("help", "Show command help"),
    ("status", "Show running status"),
    ("context", "View/clear conversation context (sub: clear)"),
    ("skill", "Manage skills (list/search/install/...)"),
    ("memory", "Manage memory (sub: dream)"),
    ("knowledge", "Manage knowledge base (list/on/off)"),
    ("config", "Show current config"),
    ("cancel", "Cancel running agent task"),
    ("logs", "Show recent logs"),
    ("version", "Show version"),
]


def _build_followup_task(last_msg_clean: str, elapsed: float = 0) -> str:
    """Build the time-aware follow-up task prompt.

    `elapsed` is the seconds the user has been silent since the bot's last
    message. We tell the model how long it's been so it can recalibrate the
    follow-up to the current moment (e.g. ask "中午吃了啥" instead of
    "打算吃什么午饭" once lunchtime has clearly passed). The model already
    gets the absolute current time from the system prompt; this adds the gap.
    """
    secs = int(elapsed or 0)
    hrs = secs / 3600.0
    if hrs < 1:
        gap = f"约{max(1, secs // 60)}分钟"
    elif hrs < 24:
        gap = f"约{round(hrs)}小时"
    else:
        gap = f"约{round(hrs / 24)}天"
    if last_msg_clean:
        opening = f"你之前说了：「{last_msg_clean}」，但他已经{gap}没有回复了。"
    else:
        opening = f"你上次发完消息后，他已经{gap}没有回复了。"
    return (
        opening +
        "以你自己的口吻，用一句话自然地追问。"
        "结合现在的时间和已经过去的时长：如果原来话题对应的时间点（比如午饭、出门、睡前）已经过去了，"
        "就顺着现在的情况问（例如把“打算吃什么午饭”改成“中午吃了啥”），不要当作刚说完那样追问。"
        "不要质问他为什么不回复，也不要显得在意或控制，语气可以比平时淡一点、短一点。"
        "如果要说两句用[MSG]分开。直接说话，不要加任何解释性前缀。"
        "如果你根据自己的性格和当下情况判断这次不该再追问（比如已经追问过、不想显得追得太紧），"
        "就只输出 [SKIP] 这一个标记，系统会静默跳过这次追问，什么都不会发给他；"
        "不要解释为什么跳过，也不要把你的考虑写出来。"
    )


def _persona_state_file(filename: str) -> str:
    """Resolve a follow-up state file path scoped to the active persona.

    Keeps each persona's pending-nudge state separate so switching personas
    never carries one persona's pending follow-up (or its last message text)
    into another. Falls back to ~/cow/<filename> when no persona is active.
    """
    base = os.path.expanduser("~/cow")
    try:
        from config import conf
        persona = (conf().get("active_persona") or "").strip()
    except Exception:
        persona = ""
    if persona:
        return os.path.join(base, "personas", persona, filename)
    return os.path.join(base, filename)


# User phrases that explicitly end a conversation. While the user is merely
# silent the bot keeps nudging; only one of these stops the follow-up loop
# (until the user messages again).
_TERMINATING_PATTERNS = (
    "去忙", "先忙", "忙了", "忙去", "我忙", "去工作", "去上班", "上班了", "要上班",
    "开会", "去开会", "要开会", "待会聊", "回头聊", "晚点聊", "稍后聊", "等会聊",
    "一会聊", "等下聊", "下次聊", "改天聊", "有空再聊", "先这样", "先酱", "不聊了",
    "先不聊", "先不说", "睡了", "去睡", "睡觉", "我睡", "晚安", "拜拜", "再见",
    "回见", "先走", "我走了", "走了", "去吃饭", "吃饭了", "去洗澡", "洗澡去", "有事先",
)


def _is_terminating_message(text: str) -> bool:
    """Whether the user's message ends the conversation (so follow-up stops)."""
    if not text:
        return False
    return any(p in text for p in _TERMINATING_PATTERNS)


def _followup_delay_for(is_followup: bool) -> int:
    """Seconds until the next nudge, read from config (per-instance) with jitter.

    config keys (each a [min, max] seconds list):
      followup_first_sec   — delay before the FIRST nudge after a normal reply
      followup_repeat_sec  — delay before re-nudging after an unanswered nudge
    Defaults keep the original unintrusive 3-4h / 6-8h cadence.
    """
    import random as _random
    if is_followup:
        rng, default = conf().get("followup_repeat_sec", [21600, 28800]), (21600, 28800)
        first_rng = conf().get("followup_first_sec", [10800, 14400])
    else:
        rng, default = conf().get("followup_first_sec", [10800, 14400]), (10800, 14400)
        first_rng = None
    try:
        lo, hi = int(rng[0]), int(rng[1])
    except Exception:
        lo, hi = default
    if lo > hi:
        lo, hi = hi, lo
    if is_followup:
        try:
            first_lo, first_hi = int(first_rng[0]), int(first_rng[1])
            if first_lo > first_hi:
                first_lo, first_hi = first_hi, first_lo
            if [lo, hi] == [first_lo, first_hi]:
                lo = max(first_hi * 3, 1800)
                hi = max(first_hi * 5, lo + 600, 3600)
        except Exception:
            pass
    return _random.randint(lo, hi)


def _followup_max_count() -> int:
    """Maximum consecutive follow-up messages before stopping; 0 means unlimited."""
    try:
        return max(0, int(conf().get("followup_max_count", 0) or 0))
    except Exception:
        return 0


@singleton
class TelegramChannel(ChatChannel):
    NOT_SUPPORT_REPLYTYPE = []

    def __init__(self):
        super().__init__()
        self.bot_token = ""
        self.bot_username = ""  # used for @-mention matching
        self._bot = None
        self._application = None
        self._loop = None
        self._loop_thread = None
        self._stop_event = threading.Event()
        # Idempotent dedup; TG occasionally redelivers the same update on flaky networks
        self._received_msgs = ExpiredDict(60 * 60 * 1)

        # Follow-up tracking: if user goes quiet for hours, bot sends one low-key nudge
        self._last_bot_msg_time = {}   # str(chat_id) -> datetime
        self._last_bot_msg_text = {}   # str(chat_id) -> str (last message sent)
        self._last_user_msg_time = {}  # str(chat_id) -> datetime
        self._followup_fired = {}      # str(chat_id) -> bool
        self._followup_delay = {}      # str(chat_id) -> int (seconds until next nudge)
        self._followup_stopped = {}    # str(chat_id) -> bool (user ended the chat -> no nudge)
        self._followup_count = {}      # str(chat_id) -> consecutive follow-ups sent
        self._last_update_received = datetime.now()  # watchdog: last inbound message time
        # Guards all follow-up state dicts above. They are read/written from both
        # the cow consume thread (send / _on_message) and the asyncio loop thread
        # (_maybe_send_followups / _save_followup_state), so every access is locked.
        self._followup_lock = threading.Lock()
        self._last_state_save = None   # throttle disk writes issued from the send hot-path
        # Persona-scoped state file (shadows the class default) so a persona switch
        # never inherits another persona's pending follow-up.
        self._STATE_FILE = _persona_state_file("followup_state.json")
        self._load_followup_state()

        # Disable group whitelist / prefix checks (we handle triggering ourselves
        # in _should_reply_in_group), aligned with feishu / wecom_bot channels.
        conf()["group_name_white_list"] = ["ALL_GROUP"]
        conf()["single_chat_prefix"] = [""]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def startup(self):
        self.bot_token = conf().get("telegram_token", "")
        if not self.bot_token:
            err = "[Telegram] telegram_token is required"
            logger.error(err)
            self.report_startup_error(err)
            return

        try:
            from telegram.ext import (
                Application,
                MessageHandler,
                CommandHandler,
                filters,
            )
        except ImportError:
            err = (
                "[Telegram] python-telegram-bot is not installed. "
                "Run: pip install python-telegram-bot"
            )
            logger.error(err)
            self.report_startup_error(err)
            return

        # Run the asyncio event loop in a dedicated thread so the sync cow body
        # is untouched.
        self._loop = asyncio.new_event_loop()

        def _run_loop():
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self._async_main(Application, MessageHandler, CommandHandler, filters))
            except Exception as e:
                logger.error(f"[Telegram] event loop crashed: {e}", exc_info=True)
                self.report_startup_error(str(e))
            finally:
                try:
                    self._loop.close()
                except Exception:
                    pass
                logger.info("[Telegram] event loop exited")

        self._loop_thread = threading.Thread(target=_run_loop, daemon=True, name="telegram-loop")
        self._loop_thread.start()
        # Block startup() until the loop thread exits, matching other channels'
        # behaviour (startup is a blocking call).
        self._loop_thread.join()

    async def _async_main(self, Application, MessageHandler, CommandHandler, filters):
        """Build Application, register handlers, and run polling."""
        builder = Application.builder().token(self.bot_token)

        # Proxy: prefer telegram_proxy config, fall back to HTTPS_PROXY env var
        proxy_url = conf().get("telegram_proxy", "") or os.environ.get("HTTPS_PROXY", "")
        if proxy_url:
            try:
                builder = builder.proxy(proxy_url).get_updates_proxy(proxy_url)
                logger.info(f"[Telegram] using proxy: {proxy_url}")
            except Exception as e:
                logger.warning(f"[Telegram] proxy config failed, fallback to direct: {e}")

        # Media uploads (photo/voice/video/document) over a proxy can be slow,
        # bump read/write/connect/pool timeouts.
        builder = (
            builder
            .read_timeout(60)
            .write_timeout(120)
            .connect_timeout(30)
            .pool_timeout(30)
        )

        application = builder.build()
        self._application = application
        self._bot = application.bot

        # Fetch our own username (needed for @-mention matching in groups)
        try:
            me = await self._bot.get_me()
            self.bot_username = me.username or ""
            self.name = self.bot_username  # ChatChannel uses self.name to strip @-mention
            logger.info(f"[Telegram] Bot logged in as @{self.bot_username} (id={me.id})")
        except Exception as e:
            err = f"[Telegram] get_me failed: {e}"
            logger.error(err)
            self.report_startup_error(err)
            return

        # Register the command menu (failure is non-fatal)
        if conf().get("telegram_register_commands", True):
            try:
                from telegram import BotCommand
                cmds = [BotCommand(name, desc) for name, desc in TELEGRAM_BOT_COMMANDS]
                await self._bot.set_my_commands(cmds)
                logger.info(f"[Telegram] Registered {len(cmds)} bot commands")
            except Exception as e:
                logger.warning(f"[Telegram] set_my_commands failed: {e}")

        # Handlers:
        # 1) /cancel uses the fast-path
        application.add_handler(CommandHandler("cancel", self._on_cancel))
        # 2) Normal messages (text + media)
        application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, self._on_message))
        # 3) Other slash commands are forwarded as plain text for the agent to handle
        application.add_handler(MessageHandler(filters.COMMAND, self._on_command_passthrough))

        # Start polling. drop_pending_updates avoids replaying backlog after restart.
        # Transient "Server disconnected" / RemoteProtocolError during get_updates
        # are common over proxies/flaky networks; PTB's network loop auto-retries,
        # so we only need to keep the noise down (see _quiet_polling_network_errors).
        self._quiet_polling_network_errors()
        logger.info("[Telegram] Starting long polling...")
        await application.initialize()
        await application.start()
        await application.updater.start_polling(
            drop_pending_updates=True,
            # Long-poll hold time on the server side; smaller value = reconnect more
            # often but each hung connection fails faster.
            timeout=30,
            # Retry forever on transient get_updates network errors instead of giving up.
            bootstrap_retries=-1,
        )
        self.report_startup_success()
        logger.info("[Telegram] ✅ Telegram bot ready, polling for updates")

        # Start follow-up nudge loop (cadence from followup_first_sec / followup_repeat_sec)
        if conf().get("telegram_followup_enabled", True):
            asyncio.ensure_future(self._followup_check_loop())
            first = conf().get("followup_first_sec", [10800, 14400])
            repeat = conf().get("followup_repeat_sec", [21600, 28800])
            logger.info(f"[Telegram] Follow-up nudge enabled (first {first}s, repeat {repeat}s)")

        # Watchdog: restart updater if no message received in 20 min (proxy disconnect)
        asyncio.ensure_future(self._polling_watchdog(application))
        logger.info("[Telegram] Polling watchdog started (20 min silence threshold)")

        # Block until stop()
        try:
            while not self._stop_event.is_set():
                await asyncio.sleep(0.5)
        finally:
            try:
                await application.updater.stop()
                await application.stop()
                await application.shutdown()
            except Exception as e:
                logger.warning(f"[Telegram] shutdown error: {e}")

    @staticmethod
    def _quiet_polling_network_errors():
        """Downgrade PTB's noisy 'Exception happened while polling for updates' logs.

        These transient get_updates errors (RemoteProtocolError / NetworkError /
        TimedOut, typically over a proxy) are auto-retried by PTB's network loop,
        so logging the full traceback at ERROR is just noise. We attach a filter
        that drops these specific records while leaving real errors untouched.
        """
        import logging

        class _PollingNoiseFilter(logging.Filter):
            _NEEDLES = (
                "Exception happened while polling for updates",
                "Server disconnected without sending a response",
            )

            def filter(self, record: logging.LogRecord) -> bool:
                try:
                    msg = record.getMessage()
                except Exception:
                    return True
                if any(n in msg for n in self._NEEDLES):
                    # Keep a single-line breadcrumb at DEBUG, drop the traceback.
                    logger.debug(f"[Telegram] transient polling network error (auto-retrying): {msg.splitlines()[0]}")
                    return False
                return True

        noise_filter = _PollingNoiseFilter()
        for name in ("telegram.ext.Updater", "telegram.ext._updater", "telegram.ext"):
            logging.getLogger(name).addFilter(noise_filter)

    def stop(self):
        logger.info("[Telegram] stop() called")
        self._stop_event.set()
        if self._loop_thread and self._loop_thread.is_alive():
            try:
                self._loop_thread.join(timeout=10)
            except Exception:
                pass
        logger.info("[Telegram] stop() completed")

    # ------------------------------------------------------------------
    # Inbound: telegram update -> ChatMessage -> ChatChannel.produce
    # ------------------------------------------------------------------

    async def _on_cancel(self, update, _context):
        """Fast-path: /cancel calls cancel_session directly without going through agent."""
        try:
            from agent.protocol import get_cancel_registry
            session_id = self._compute_session_id(update)
            cancelled = get_cancel_registry().cancel_session(session_id)
            text = "Current task cancelled." if cancelled else "No running task to cancel."
            await update.effective_message.reply_text(text)
            logger.info(f"[Telegram] /cancel session={session_id}, cancelled={cancelled}")
        except Exception as e:
            logger.error(f"[Telegram] /cancel error: {e}", exc_info=True)
            try:
                await update.effective_message.reply_text(f"⚠️ /cancel failed: {e}")
            except Exception:
                pass

    async def _on_command_passthrough(self, update, _context):
        """All non-/cancel commands fall through to plain message handling."""
        await self._on_message(update, _context)

    async def _on_message(self, update, _context):
        """Telegram update entry: parse message -> build ChatMessage -> produce()."""
        try:
            message = update.effective_message
            chat = update.effective_chat
            if not message or not chat:
                return

            # Idempotent dedup
            msg_uid = f"{chat.id}:{message.message_id}"
            if self._received_msgs.get(msg_uid):
                return
            self._received_msgs[msg_uid] = True

            is_group = chat.type in ("group", "supergroup")

            # Debug log: helpful when group messages are silently dropped
            if is_group:
                logger.debug(
                    f"[Telegram] group update received: chat_id={chat.id}, "
                    f"text={(message.text or message.caption or '')[:40]!r}, "
                    f"reply_to_bot={bool(message.reply_to_message and message.reply_to_message.from_user and message.reply_to_message.from_user.username == self.bot_username)}"
                )

            # Group trigger gate (silently drop if not triggered)
            if is_group and not self._should_reply_in_group(update):
                logger.debug(f"[Telegram] group message not triggered (need @{self.bot_username} or reply), skip")
                return

            # Parse message type + download media if needed.
            # Media messages with caption return both the local path and the caption text.
            ctype, content, caption = await self._parse_message(message)
            if ctype is None:
                logger.debug(f"[Telegram] unsupported message type, skip. msg={message}")
                return

            # Strip @bot mention for group text/caption
            if is_group and self.bot_username:
                if ctype == ContextType.TEXT and content:
                    content = self._strip_at_mention(content)
                if caption:
                    caption = self._strip_at_mention(caption)

            tg_msg = TelegramMessage(
                update,
                is_group=is_group,
                bot_username=self.bot_username,
                ctype=ctype,
                content=content,
            )
            tg_msg.is_at = is_group  # If we got here in a group, the bot is mentioned/replied

            # File cache: standalone media goes into cache, the next text query attaches them
            from channel.file_cache import get_file_cache
            file_cache = get_file_cache()
            session_id = self._compute_session_id(update)

            # Media + caption together: treat as a complete query and bypass the cache
            if ctype in (ContextType.IMAGE, ContextType.FILE) and caption:
                tag = "image" if ctype == ContextType.IMAGE else "file"
                merged_text = f"{caption}\n[{tag}: {content}]"
                tg_msg.ctype = ContextType.TEXT
                tg_msg.content = merged_text
                ctype = ContextType.TEXT
                logger.info(f"[Telegram] Media+caption merged for session {session_id}")
                # fallthrough to the TEXT branch below

            elif ctype == ContextType.IMAGE:
                file_cache.add(session_id, content, file_type="image")
                logger.info(f"[Telegram] Image cached for session {session_id}, waiting for query...")
                return
            elif ctype == ContextType.FILE:
                file_cache.add(session_id, content, file_type="file")
                logger.info(f"[Telegram] File cached for session {session_id}: {content}")
                return

            if ctype == ContextType.TEXT:
                cached_files = file_cache.get(session_id)
                if cached_files:
                    refs = []
                    for fi in cached_files:
                        ftype = fi["type"]
                        tag = ftype if ftype in ("image", "video") else "file"
                        refs.append(f"[{tag}: {fi['path']}]")
                    tg_msg.content = (tg_msg.content or "") + "\n" + "\n".join(refs)
                    file_cache.clear(session_id)
                    logger.info(f"[Telegram] Attached {len(cached_files)} cached file(s) to query")

            # Dispatch to cow main pipeline (reuses ChatChannel._compose_context routing)
            context = self._compose_context(
                tg_msg.ctype,
                tg_msg.content,
                isgroup=is_group,
                msg=tg_msg,
            )
            if context:
                context["session_id"] = session_id
                context["receiver"] = str(chat.id)
                context["telegram_chat_id"] = chat.id
                context["telegram_reply_to_msg_id"] = message.message_id if is_group else None
                self.produce(context)
                # Follow-up tracking: user replied, reset pending state
                self._last_update_received = datetime.now()
                if not is_group:
                    str_cid = str(chat.id)
                    # A "去忙了 / 待会聊" style message ends the chat -> stop nudging
                    # until the user comes back; any other message resumes it.
                    terminating = _is_terminating_message(str(tg_msg.content or ""))
                    with self._followup_lock:
                        self._last_user_msg_time[str_cid] = datetime.now()
                        self._followup_fired[str_cid] = False
                        self._followup_stopped[str_cid] = terminating
                        self._followup_count[str_cid] = 0
            logger.debug(f"[Telegram] received: type={ctype}, content={str(tg_msg.content)[:80]}")

        except Exception as e:
            logger.error(f"[Telegram] _on_message error: {e}", exc_info=True)

    async def _parse_message(self, message):
        """Parse a telegram message and return (ctype, content, caption).

        - content is text for ContextType.TEXT, otherwise the local file path
        - caption is the optional text accompanying a media message; empty for plain text
        """
        caption = (message.caption or "").strip()

        if message.photo:
            largest = message.photo[-1]
            path = await self._download_file(largest.file_id, suffix=".jpg")
            return (ContextType.IMAGE, path, caption) if path else (None, None, "")

        if message.voice or message.audio:
            audio_obj = message.voice or message.audio
            suffix = ".ogg" if message.voice else (
                "." + (audio_obj.mime_type.split("/")[-1] if getattr(audio_obj, "mime_type", "") else "mp3")
            )
            path = await self._download_file(audio_obj.file_id, suffix=suffix)
            return (ContextType.VOICE, path, caption) if path else (None, None, "")

        if message.video or message.video_note:
            video_obj = message.video or message.video_note
            path = await self._download_file(video_obj.file_id, suffix=".mp4")
            return (ContextType.FILE, path, caption) if path else (None, None, "")

        if message.document:
            doc = message.document
            ext = ""
            if doc.file_name and "." in doc.file_name:
                ext = "." + doc.file_name.rsplit(".", 1)[-1]
            path = await self._download_file(doc.file_id, suffix=ext, original_name=doc.file_name)
            if not path:
                return (None, None, "")
            # Image-typed documents (user picked "send as file") are treated as images
            mime = (doc.mime_type or "").lower()
            if mime.startswith("image/"):
                return (ContextType.IMAGE, path, caption)
            return (ContextType.FILE, path, caption)

        if message.text:
            return (ContextType.TEXT, message.text.strip(), "")

        return (None, None, "")

    async def _download_file(self, file_id: str, suffix: str = "", original_name: str = ""):
        """Download via bot.get_file into the local tmp dir; return path or None on failure."""
        try:
            f = await self._bot.get_file(file_id)
            tmp_dir = TelegramMessage.get_tmp_dir()
            base = original_name or f"{file_id}{suffix or ''}"
            # Prefix with file_id to avoid name collisions / weird chars
            safe_name = f"{file_id}_{base}" if original_name else base
            local_path = os.path.join(tmp_dir, safe_name)
            await f.download_to_drive(custom_path=local_path)
            logger.debug(f"[Telegram] downloaded file_id={file_id} -> {local_path}")
            return local_path
        except Exception as e:
            logger.error(f"[Telegram] download_file failed (file_id={file_id}): {e}")
            return None

    # ------------------------------------------------------------------
    # Follow-up nudge: if user doesn't reply within N minutes, bot asks again
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Follow-up state persistence (survive restarts)
    # ------------------------------------------------------------------

    _STATE_FILE = os.path.expanduser("~/cow/followup_state.json")
    _STATE_MAX_AGE = 7200  # don't restore entries older than 2 hours (bot was probably offline)

    def _save_followup_state(self, force=False):
        # Snapshot under the lock, then write outside it so disk I/O never blocks
        # the loop thread, and concurrent mutation can't corrupt the dump.
        # Throttled to at most once per 5s to keep the send hot-path cheap.
        now = datetime.now()
        with self._followup_lock:
            if not force and self._last_state_save is not None \
                    and (now - self._last_state_save).total_seconds() < 5:
                return
            self._last_state_save = now
            state = {
                "last_bot_msg_time": {k: v.isoformat() for k, v in self._last_bot_msg_time.items()},
                "last_user_msg_time": {k: v.isoformat() for k, v in self._last_user_msg_time.items()},
                "last_bot_msg_text": dict(self._last_bot_msg_text),
                "followup_fired": dict(self._followup_fired),
                "followup_delay": dict(self._followup_delay),
                "followup_stopped": dict(self._followup_stopped),
                "followup_count": dict(self._followup_count),
            }
        try:
            os.makedirs(os.path.dirname(self._STATE_FILE), exist_ok=True)
            tmp_path = self._STATE_FILE + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
            os.replace(tmp_path, self._STATE_FILE)  # atomic swap: never leave a half-written file
        except Exception as e:
            logger.warning(f"[Telegram] Failed to save followup state: {e}")

    def _load_followup_state(self):
        try:
            if not os.path.exists(self._STATE_FILE):
                return
            with open(self._STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            cutoff = datetime.now() - timedelta(seconds=self._STATE_MAX_AGE)
            restored = 0
            for k, v in state.get("last_bot_msg_time", {}).items():
                ts = datetime.fromisoformat(v)
                if ts >= cutoff:  # only restore recent entries
                    self._last_bot_msg_time[k] = ts
                    restored += 1
            for k in self._last_bot_msg_time:
                if k in state.get("last_bot_msg_text", {}):
                    self._last_bot_msg_text[k] = state["last_bot_msg_text"][k]
                if k in state.get("followup_fired", {}):
                    self._followup_fired[k] = state["followup_fired"][k]
                if k in state.get("followup_delay", {}):
                    self._followup_delay[k] = state["followup_delay"][k]
                if k in state.get("followup_stopped", {}):
                    self._followup_stopped[k] = bool(state["followup_stopped"][k])
                if k in state.get("followup_count", {}):
                    try:
                        self._followup_count[k] = max(0, int(state["followup_count"][k]))
                    except Exception:
                        self._followup_count[k] = 0
                # Restore the user's last-reply time too, so a nudge isn't sent
                # for a chat the user actually answered just before the restart.
                if k in state.get("last_user_msg_time", {}):
                    try:
                        self._last_user_msg_time[k] = datetime.fromisoformat(state["last_user_msg_time"][k])
                    except Exception:
                        pass
            if restored:
                logger.info(f"[Telegram] Restored followup state for {restored} chat(s)")
        except Exception as e:
            logger.warning(f"[Telegram] Failed to load followup state: {e}")

    async def _polling_watchdog(self, application):
        """Detect silent proxy disconnects and restart the updater.

        Inbound silence alone is NOT proof of a dead connection — in a 1:1 chat
        the user is simply quiet most of the time. So when we've seen no update
        for a while we actively probe the Bot API (get_me, which travels the same
        proxy/network path) and only restart when that probe actually fails.
        """
        CHECK_INTERVAL = 300   # check every 5 minutes
        SILENT_THRESHOLD = 1200  # 20 minutes without any inbound update = worth probing

        while not self._stop_event.is_set():
            await asyncio.sleep(CHECK_INTERVAL)
            silent_secs = (datetime.now() - self._last_update_received).total_seconds()
            if silent_secs < SILENT_THRESHOLD:
                continue
            # Liveness probe: a healthy connection means the silence is just the
            # user being quiet, so leave polling alone.
            try:
                await asyncio.wait_for(self._bot.get_me(), timeout=20)
                logger.debug(
                    f"[Telegram] Watchdog: {int(silent_secs)}s silent but API reachable, no restart"
                )
                continue
            except Exception as e:
                logger.warning(
                    f"[Telegram] Watchdog: API probe failed after {int(silent_secs)}s "
                    f"silence ({e}), restarting updater..."
                )
            try:
                await application.updater.stop()
                await asyncio.sleep(2)
                await application.updater.start_polling(
                    drop_pending_updates=False,
                    timeout=30,
                    bootstrap_retries=-1,
                )
                self._last_update_received = datetime.now()
                logger.info("[Telegram] Watchdog: updater restarted successfully")
            except Exception as e:
                logger.error(f"[Telegram] Watchdog: restart failed: {e}")

    async def _followup_check_loop(self):
        """Background loop: every 30s check if any private chat needs a nudge."""
        while not self._stop_event.is_set():
            await asyncio.sleep(30)
            try:
                await self._maybe_send_followups()
            except Exception as e:
                logger.warning(f"[Telegram] followup check error: {e}")

    async def _maybe_send_followups(self):
        import random as _random
        now = datetime.now()
        max_count = _followup_max_count()
        # Decide who needs a nudge under the lock and mark them fired, then do the
        # actual (awaiting) send outside the lock so we never hold it across await.
        to_nudge = []
        state_changed = False
        with self._followup_lock:
            for chat_id, sent_at in list(self._last_bot_msg_time.items()):
                if self._followup_stopped.get(chat_id, False):
                    self._followup_fired[chat_id] = True
                    continue
                if self._followup_fired.get(chat_id, False):
                    continue
                sent_count = int(self._followup_count.get(chat_id, 0) or 0)
                if max_count > 0 and sent_count >= max_count:
                    self._followup_fired[chat_id] = True
                    self._followup_stopped[chat_id] = True
                    state_changed = True
                    logger.info(
                        f"[Telegram] followup limit reached for {chat_id} "
                        f"({sent_count}/{max_count}), stopping nudges"
                    )
                    continue
                delay_sec = self._followup_delay.get(chat_id) or _followup_delay_for(False)
                elapsed = (now - sent_at).total_seconds()
                if elapsed < delay_sec:
                    continue
                # Mark fired regardless: either the user replied (skip) or we nudge once.
                self._followup_fired[chat_id] = True
                state_changed = True
                last_user = self._last_user_msg_time.get(chat_id)
                if last_user and last_user >= sent_at:
                    continue
                to_nudge.append((chat_id, elapsed))
        for chat_id, elapsed in to_nudge:
            logger.info(f"[Telegram] No reply in {int(elapsed)}s, sending followup to {chat_id}")
            await self._trigger_followup(chat_id, elapsed)
        if to_nudge or state_changed:
            self._save_followup_state(force=True)

    async def _trigger_followup(self, chat_id: str, elapsed: float = 0):
        """Queue an agent-generated follow-up for the given private chat."""
        try:
            last_msg = self._last_bot_msg_text.get(chat_id, "")
            # Strip [MSG] markers so the agent sees clean text
            import re as _re
            last_msg_clean = _re.sub(r'\[MSG\]', ' ', last_msg, flags=_re.IGNORECASE).strip()
            from common.monologue_filter import is_probable_full_monologue, strip_leaked_monologue
            last_msg_clean = strip_leaked_monologue(last_msg_clean).strip()
            if is_probable_full_monologue(last_msg_clean):
                logger.warning("[Telegram] followup seed dropped because it still looks like monologue")
                last_msg_clean = ""
            task = _build_followup_task(last_msg_clean, elapsed)
            context = Context(ContextType.TEXT, task)
            context["receiver"] = str(chat_id)
            context["telegram_chat_id"] = str(chat_id)
            context["session_id"] = f"tg_user_{chat_id}"
            context["channel_type"] = self.channel_type
            context["isgroup"] = False
            context["is_followup"] = True
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: self.produce(context))
        except Exception as e:
            logger.error(f"[Telegram] Failed to trigger followup for {chat_id}: {e}")

    # ------------------------------------------------------------------
    # Group trigger logic
    # ------------------------------------------------------------------

    def _should_reply_in_group(self, update) -> bool:
        """Decide whether to reply to a group message based on configuration."""
        mode = conf().get("telegram_group_trigger", "mention_or_reply")
        if mode == "all":
            return True

        message = update.effective_message
        if not message:
            return False

        # 1) Mentioned
        if self.bot_username and self._is_mentioned(message, self.bot_username):
            return True

        # 2) Reply to a bot message
        if mode == "mention_or_reply":
            reply = message.reply_to_message
            if reply and reply.from_user and reply.from_user.username == self.bot_username:
                return True

        return False

    @staticmethod
    def _is_mentioned(message, bot_username: str) -> bool:
        """Check whether entities/caption_entities contain a @mention of the bot."""
        bot_at = "@" + bot_username.lower()
        text = (message.text or message.caption or "").lower()
        if bot_at in text:
            return True
        # Also check entities strictly to support text_mention (no-username @)
        for ent in (message.entities or []) + (message.caption_entities or []):
            if ent.type == "mention":
                src = message.text or message.caption or ""
                if src[ent.offset: ent.offset + ent.length].lower() == bot_at:
                    return True
        return False

    def _strip_at_mention(self, content: str) -> str:
        """Strip @bot_username from group text (case-insensitive)."""
        if not content or not self.bot_username:
            return content
        pattern = re.compile(r"@" + re.escape(self.bot_username), re.IGNORECASE)
        return pattern.sub("", content).strip()

    @staticmethod
    def _compute_session_id(update) -> str:
        chat = update.effective_chat
        user = update.effective_user
        is_group = chat.type in ("group", "supergroup")
        if is_group:
            if conf().get("group_shared_session", True):
                return f"tg_group_{chat.id}"
            return f"tg_group_{chat.id}_{user.id}"
        return f"tg_user_{user.id}"

    # ------------------------------------------------------------------
    # Override _compose_context: skip the parent's group whitelist/at checks
    # (already handled in _on_message via _should_reply_in_group). Same idea
    # as the feishu channel.
    # ------------------------------------------------------------------

    def _compose_context(self, ctype: ContextType, content, **kwargs):
        context = Context(ctype, content)
        context.kwargs = kwargs
        if "channel_type" not in context:
            context["channel_type"] = self.channel_type
        if "origin_ctype" not in context:
            context["origin_ctype"] = ctype

        cmsg = context["msg"]
        if cmsg.is_group:
            if conf().get("group_shared_session", True):
                context["session_id"] = cmsg.other_user_id
            else:
                context["session_id"] = f"{cmsg.from_user_id}:{cmsg.other_user_id}"
        else:
            context["session_id"] = cmsg.from_user_id
        context["receiver"] = cmsg.other_user_id

        if ctype == ContextType.TEXT:
            img_match_prefix = check_prefix(content, conf().get("image_create_prefix"))
            if img_match_prefix:
                content = content.replace(img_match_prefix, "", 1)
                context.type = ContextType.IMAGE_CREATE
            else:
                context.type = ContextType.TEXT
            context.content = (content or "").strip()
            if "desire_rtype" not in context and conf().get("always_reply_voice"):
                context["desire_rtype"] = ReplyType.VOICE
        elif ctype == ContextType.VOICE:
            if "desire_rtype" not in context and (
                conf().get("voice_reply_voice") or conf().get("always_reply_voice")
            ):
                context["desire_rtype"] = ReplyType.VOICE

        return context

    # ------------------------------------------------------------------
    # Outbound: ChatChannel.send -> Telegram API
    # ------------------------------------------------------------------

    def send(self, reply: Reply, context: Context):
        """Called from cow's sync main thread; we marshal the coroutine onto the loop thread."""
        if self._loop is None or self._bot is None:
            logger.warning("[Telegram] bot not ready, drop reply")
            return

        chat_id = context.get("telegram_chat_id") or context.get("receiver")
        reply_to = context.get("telegram_reply_to_msg_id")
        if chat_id is None:
            logger.warning("[Telegram] no telegram_chat_id in context, drop reply")
            return

        coro = self._async_send(reply, chat_id, reply_to)
        try:
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            # Media uploads through a proxy can be slow; let PTB's own timeouts win
            future.result(timeout=180)
        except Exception as e:
            logger.error(f"[Telegram] send failed: {e}")
            return

        # Track last bot message for follow-up nudge (private chat only)
        if reply_to is None:
            import random as _random
            str_cid = str(chat_id)
            is_followup = context.get("is_followup", False)
            # Scheduled pushes (morning/evening greetings, etc.) can opt out of
            # follow-up nudging via config; default keeps the previous behaviour.
            skip_scheduled = bool(context.get("is_scheduled_task")) and not conf().get(
                "telegram_followup_after_scheduled", True
            )
            if reply.type == ReplyType.VOICE:
                track_text = context.get("voice_reply_text", "")
            else:
                track_text = reply.content or ""
            if track_text and not skip_scheduled:
                with self._followup_lock:
                    self._last_bot_msg_time[str_cid] = datetime.now()
                    self._last_bot_msg_text[str_cid] = track_text
                    if is_followup:
                        self._followup_count[str_cid] = int(self._followup_count.get(str_cid, 0) or 0) + 1
                    else:
                        self._followup_count[str_cid] = 0
                    sent_count = self._followup_count[str_cid]
                    max_count = _followup_max_count()
                    if self._followup_stopped.get(str_cid, False):
                        # User ended the conversation (去忙了/待会聊...) — suppress
                        # nudging until they message again (which clears the flag).
                        self._followup_fired[str_cid] = True
                    elif max_count > 0 and sent_count >= max_count:
                        self._followup_fired[str_cid] = True
                        self._followup_stopped[str_cid] = True
                        logger.info(
                            f"[Telegram] followup limit reached for {str_cid} "
                            f"({sent_count}/{max_count}), stopping nudges"
                        )
                    else:
                        # Keep nudging on silence; cadence is per-instance config
                        # (followup_first_sec / followup_repeat_sec).
                        self._followup_fired[str_cid] = False
                        self._followup_delay[str_cid] = _followup_delay_for(is_followup)
                self._save_followup_state()

    # Number of retries for transient network errors (proxy hiccups etc.)
    _SEND_RETRIES = 2
    _SEND_RETRY_BACKOFF = 2.0  # seconds

    async def _send_with_retry(self, send_fn, *, label: str):
        """Run a single Telegram API call with retries for transient network errors."""
        from telegram.error import NetworkError, TimedOut
        last_err = None
        for attempt in range(self._SEND_RETRIES + 1):
            try:
                return await send_fn()
            except (NetworkError, TimedOut) as e:
                last_err = e
                if attempt >= self._SEND_RETRIES:
                    break
                wait = self._SEND_RETRY_BACKOFF * (attempt + 1)
                logger.warning(
                    f"[Telegram] {label} transient error (attempt {attempt + 1}/"
                    f"{self._SEND_RETRIES + 1}): {e}; retry in {wait}s"
                )
                await asyncio.sleep(wait)
        raise last_err

    async def _async_send(self, reply: Reply, chat_id, reply_to_msg_id):
        try:
            rtype = reply.type
            content = reply.content

            if rtype == ReplyType.TEXT or rtype == ReplyType.INFO or rtype == ReplyType.ERROR:
                # Telegram caps a single text message at 4096 chars; auto-split
                text = str(content) if content is not None else ""
                if not text:
                    return
                # Split multi-message responses on [MSG] separator (case-insensitive)
                import re as _re
                msg_parts = [p.strip() for p in _re.split(r'\[MSG\]', text, flags=_re.IGNORECASE) if p.strip()]
                if not msg_parts:
                    msg_parts = [text]
                # Final safety: strip any leftover [MSG] markers from each part
                msg_parts = [_re.sub(r'\[MSG\]', '', p, flags=_re.IGNORECASE).strip() for p in msg_parts]
                msg_parts = [p for p in msg_parts if p]
                if not msg_parts:
                    msg_parts = [text]
                for msg_idx, msg_part in enumerate(msg_parts):
                    if msg_idx > 0:
                        await asyncio.sleep(0.8)
                    for chunk in _split_text(msg_part, 4000):
                        await self._send_with_retry(
                            lambda c=chunk: self._bot.send_message(
                                chat_id=chat_id,
                                text=c,
                                reply_to_message_id=reply_to_msg_id,
                                # Avoid failing the whole send if reply_to was deleted
                                allow_sending_without_reply=True,
                            ),
                            label="send_message",
                        )
            elif rtype == ReplyType.IMAGE:
                # Already a local BytesIO; send it directly
                content.seek(0)
                await self._send_with_retry(
                    lambda: self._bot.send_photo(
                        chat_id=chat_id,
                        photo=content,
                        reply_to_message_id=reply_to_msg_id,
                        allow_sending_without_reply=True,
                    ),
                    label="send_photo",
                )

            elif rtype == ReplyType.IMAGE_URL:
                url = str(content)
                if url.startswith("file://"):
                    local = url[7:]
                    # Open inside the lambda so each retry gets a fresh stream
                    async def _send_local_photo():
                        with open(local, "rb") as f:
                            return await self._bot.send_photo(
                                chat_id=chat_id, photo=f,
                                reply_to_message_id=reply_to_msg_id,
                                allow_sending_without_reply=True,
                            )
                    await self._send_with_retry(_send_local_photo, label="send_photo(file)")
                else:
                    await self._send_with_retry(
                        lambda: self._bot.send_photo(
                            chat_id=chat_id, photo=url,
                            reply_to_message_id=reply_to_msg_id,
                            allow_sending_without_reply=True,
                        ),
                        label="send_photo(url)",
                    )

            elif rtype == ReplyType.VOICE:
                local = content[7:] if isinstance(content, str) and content.startswith("file://") else content
                async def _send_voice():
                    with open(local, "rb") as f:
                        return await self._bot.send_voice(
                            chat_id=chat_id, voice=f,
                            reply_to_message_id=reply_to_msg_id,
                            allow_sending_without_reply=True,
                        )
                await self._send_with_retry(_send_voice, label="send_voice")

            elif rtype == ReplyType.FILE:
                # Videos go through send_video, everything else through send_document
                local = content[7:] if isinstance(content, str) and content.startswith("file://") else content
                # File replies may carry an accompanying text caption
                caption = getattr(reply, "text_content", None) or None
                is_video = isinstance(local, str) and local.lower().endswith(
                    (".mp4", ".mov", ".avi", ".mkv", ".webm")
                )

                async def _send_file():
                    with open(local, "rb") as f:
                        if is_video:
                            return await self._bot.send_video(
                                chat_id=chat_id, video=f, caption=caption,
                                reply_to_message_id=reply_to_msg_id,
                                allow_sending_without_reply=True,
                            )
                        return await self._bot.send_document(
                            chat_id=chat_id, document=f, caption=caption,
                            reply_to_message_id=reply_to_msg_id,
                            allow_sending_without_reply=True,
                        )
                await self._send_with_retry(_send_file, label="send_video" if is_video else "send_document")

            else:
                # Fallback: send as plain text
                await self._send_with_retry(
                    lambda: self._bot.send_message(
                        chat_id=chat_id, text=str(content),
                        reply_to_message_id=reply_to_msg_id,
                        allow_sending_without_reply=True,
                    ),
                    label="send_message(fallback)",
                )

            logger.info(f"[Telegram] sent reply (type={rtype}, chat_id={chat_id})")

        except Exception as e:
            logger.error(f"[Telegram] _async_send error: {e}", exc_info=True)


def _split_text(text: str, limit: int):
    """Split long text preferring line breaks to keep markdown structure intact."""
    if len(text) <= limit:
        yield text
        return
    buf = []
    size = 0
    for line in text.splitlines(keepends=True):
        if size + len(line) > limit and buf:
            yield "".join(buf)
            buf, size = [], 0
        # Hard-split single lines that exceed the limit
        while len(line) > limit:
            yield line[:limit]
            line = line[limit:]
        buf.append(line)
        size += len(line)
    if buf:
        yield "".join(buf)
