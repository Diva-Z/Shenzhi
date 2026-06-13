"""
Weixin channel implementation.

Uses HTTP long-poll (getUpdates) to receive messages and sendMessage to reply.
Login via QR code scan through the ilink bot API.
"""

import json
import os
import random
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta

import requests

from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from channel.chat_channel import ChatChannel, check_prefix
from channel.weixin.weixin_api import (
    WeixinApi, upload_media_to_cdn,
    DEFAULT_BASE_URL, CDN_BASE_URL,
)
from channel.weixin.weixin_message import WeixinMessage
from common.expired_dict import ExpiredDict
from common.log import logger
from common.message_splitter import split_text_bubbles
from common.singleton import singleton
from config import conf

MAX_CONSECUTIVE_FAILURES = 3
BACKOFF_DELAY = 30
RETRY_DELAY = 2
SESSION_EXPIRED_ERRCODE = -14
TEXT_CHUNK_LIMIT = 4000
QR_LOGIN_TIMEOUT_S = 480
QR_MAX_REFRESHES = 10


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
    config keys: followup_first_sec / followup_repeat_sec ([min, max] seconds)."""
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
    return random.randint(lo, hi)


def _followup_max_count() -> int:
    """Maximum consecutive follow-up messages before stopping; 0 means unlimited."""
    try:
        return max(0, int(conf().get("followup_max_count", 0) or 0))
    except Exception:
        return 0


def _load_credentials(cred_path: str) -> dict:
    """Load saved credentials from JSON file."""
    try:
        if os.path.exists(cred_path):
            with open(cred_path, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"[Weixin] Failed to load credentials: {e}")
    return {}


def _save_credentials(cred_path: str, data: dict):
    """Atomically save credentials to JSON file (tmp + rename)."""
    os.makedirs(os.path.dirname(cred_path), exist_ok=True)
    tmp_path = f"{cred_path}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    try:
        os.chmod(tmp_path, 0o600)
    except Exception:
        pass
    os.replace(tmp_path, cred_path)


@singleton
class WeixinChannel(ChatChannel):

    # ilink bot protocol has no outbound voice item; deliver TTS as a file.
    NOT_SUPPORT_REPLYTYPE = []

    LOGIN_STATUS_IDLE = "idle"
    LOGIN_STATUS_WAITING = "waiting_scan"
    LOGIN_STATUS_SCANNED = "scanned"
    LOGIN_STATUS_OK = "logged_in"

    def __init__(self):
        super().__init__()
        self.api = None
        self._stop_event = threading.Event()
        self._poll_thread = None
        # user_id -> context_token. Guarded by _context_tokens_lock for any
        # mutation that races with disk persistence.
        self._context_tokens = {}
        self._context_tokens_lock = threading.Lock()
        self._received_msgs = ExpiredDict(60 * 60 * 7.1)
        self._get_updates_buf = ""
        self._credentials_path = ""
        self.login_status = self.LOGIN_STATUS_IDLE
        self._current_qr_url = ""

        # Follow-up tracking: if user goes quiet for hours, bot sends one low-key nudge
        self._last_bot_msg_time = {}   # str(user_id) -> datetime
        self._last_bot_msg_text = {}   # str(user_id) -> str
        self._last_user_msg_time = {}  # str(user_id) -> datetime
        self._followup_fired = {}      # str(user_id) -> bool
        self._followup_delay = {}      # str(user_id) -> int (seconds until next nudge)
        self._followup_stopped = {}    # str(user_id) -> bool (user ended the chat -> no nudge)
        self._followup_count = {}      # str(user_id) -> consecutive follow-ups sent
        # Guards all follow-up state dicts: read/written from both the cow consume
        # thread (send) and the followup background thread.
        self._followup_lock = threading.Lock()
        self._last_state_save = None   # throttle disk writes
        # Persona-scoped state file (shadows the class default) so a persona switch
        # never inherits another persona's pending follow-up.
        self._FOLLOWUP_STATE_FILE = _persona_state_file("weixin_followup_state.json")
        self._load_followup_state()

        conf()["single_chat_prefix"] = [""]

    # ── Lifecycle ──────────────────────────────────────────────────────

    def startup(self):
        self._stop_event.clear()

        base_url = conf().get("weixin_base_url", DEFAULT_BASE_URL)
        cdn_base_url = conf().get("weixin_cdn_base_url", CDN_BASE_URL)
        token = conf().get("weixin_token", "")

        self._credentials_path = os.path.expanduser(
            conf().get("weixin_credentials_path", "~/.weixin_cow_credentials.json")
        )

        # Always load credentials so we can restore context_tokens even when
        # the bot token itself comes from config.
        creds = _load_credentials(self._credentials_path)
        if not token:
            token = creds.get("token", "")
            if creds.get("base_url"):
                base_url = creds["base_url"]

        # Restore persisted context_tokens so scheduler can deliver pushes
        # immediately after restart, without waiting for the user to ping
        # the bot first.
        self._restore_context_tokens_from_creds(creds)

        if not token:
            token, base_url = self._login_with_retry(base_url)
            if not token:
                return

        self.api = WeixinApi(base_url=base_url, token=token, cdn_base_url=cdn_base_url)
        self.login_status = self.LOGIN_STATUS_OK

        logger.info(f"[Weixin] 微信通道已启动，凭证保存在 {self._credentials_path}，"
                     f"如需重新扫码登录请删除该文件后重启")
        self.report_startup_success()

        # Start follow-up nudge loop in background thread
        if conf().get("weixin_followup_enabled", True):
            t = threading.Thread(target=self._followup_check_loop, daemon=True, name="weixin-followup")
            t.start()
            first = conf().get("followup_first_sec", [10800, 14400])
            repeat = conf().get("followup_repeat_sec", [21600, 28800])
            logger.info(f"[Weixin] Follow-up nudge enabled (first {first}s, repeat {repeat}s)")

        self._poll_loop()

    def _login_with_retry(self, base_url: str) -> tuple:
        """Attempt QR login, then wait for stop if failed.
        Returns (token, base_url) on success, or ("", "") if stopped."""
        logger.info("[Weixin] No token found, starting QR login...")
        self.login_status = self.LOGIN_STATUS_WAITING
        login_result = self._qr_login(base_url)
        if login_result:
            return login_result["token"], login_result.get("base_url", base_url)

        self.login_status = self.LOGIN_STATUS_IDLE
        if not self._stop_event.is_set():
            logger.info("[Weixin] QR login timed out, waiting for stop or reconnect...")
            print("  二维码登录超时，请通过控制台重新接入\n")
            self._stop_event.wait()

        logger.info("[Weixin] Login cancelled by stop event")
        return "", ""

    def stop(self):
        logger.info("[Weixin] stop() called")
        self._stop_event.set()

    def _relogin(self) -> bool:
        """Re-login after session expiry. Returns True on success."""
        base_url = self.api.base_url if self.api else DEFAULT_BASE_URL
        # Clearing the whole credentials file is intentional: the new login
        # will issue a fresh `token` and persisted context_tokens belong to
        # the previous bot identity, so they must not survive.
        with self._context_tokens_lock:
            self._context_tokens.clear()
            if os.path.exists(self._credentials_path):
                try:
                    os.remove(self._credentials_path)
                except Exception:
                    pass
        self.login_status = self.LOGIN_STATUS_WAITING
        result = self._qr_login(base_url)
        if not result:
            self.login_status = self.LOGIN_STATUS_IDLE
            return False
        self.api = WeixinApi(
            base_url=result.get("base_url", base_url),
            token=result["token"],
            cdn_base_url=self.api.cdn_base_url if self.api else CDN_BASE_URL,
        )
        self.login_status = self.LOGIN_STATUS_OK
        return True

    # ── Context token persistence ──────────────────────────────────────
    # ilink requires every outbound send to echo the context_token from the
    # user's latest inbound message. We mirror the in-memory map into the
    # credentials JSON so scheduled pushes survive process restarts.
    # All mutation + disk IO is serialized via _context_tokens_lock so that
    # concurrent updates can never lose each other's writes.

    def _restore_context_tokens_from_creds(self, creds: dict) -> None:
        if not isinstance(creds, dict):
            return
        tokens = creds.get("context_tokens")
        if not isinstance(tokens, dict):
            return
        restored = 0
        with self._context_tokens_lock:
            for user_id, token in tokens.items():
                if isinstance(user_id, str) and isinstance(token, str) and token:
                    self._context_tokens[user_id] = token
                    restored += 1
        if restored:
            logger.info(f"[Weixin] Restored {restored} context_tokens from credentials")

    def _persist_context_tokens_locked(self) -> None:
        """Flush the token map to disk. Caller must hold _context_tokens_lock."""
        if not self._credentials_path:
            return
        try:
            creds = _load_credentials(self._credentials_path) or {}
            creds["context_tokens"] = dict(self._context_tokens)
            _save_credentials(self._credentials_path, creds)
        except Exception as e:
            logger.warning(f"[Weixin] Failed to persist context_tokens: {e}")

    def _update_context_token(self, user_id: str, token: str) -> None:
        """Update the in-memory token for a user; flush to disk only on change."""
        if not user_id or not token:
            return
        with self._context_tokens_lock:
            if self._context_tokens.get(user_id) == token:
                return
            self._context_tokens[user_id] = token
            self._persist_context_tokens_locked()

    def _invalidate_context_token(self, user_id: str) -> None:
        """Drop the cached token for a user (used after -14 / send rejection)."""
        if not user_id:
            return
        with self._context_tokens_lock:
            if user_id not in self._context_tokens:
                return
            del self._context_tokens[user_id]
            logger.info(f"[Weixin] Invalidated stale context_token for {user_id}")
            self._persist_context_tokens_locked()

    # ── QR Login ───────────────────────────────────────────────────────

    @staticmethod
    def _print_qr(qrcode_url: str):
        """Print QR code to terminal for scanning."""
        print("\n" + "=" * 60)
        print("  请使用微信扫描二维码登录 (二维码约2分钟后过期)")
        print("=" * 60)
        try:
            import qrcode as qr_lib
            import io
            qr = qr_lib.QRCode(error_correction=qr_lib.constants.ERROR_CORRECT_L, box_size=1, border=1)
            qr.add_data(qrcode_url)
            qr.make(fit=True)
            buf = io.StringIO()
            qr.print_ascii(out=buf, invert=True)
            try:
                print(buf.getvalue())
            except UnicodeEncodeError:
                # Windows GBK terminals cannot render Unicode block characters
                print(f"\n  (终端不支持显示二维码，请使用链接扫码)")
                print(f"  二维码链接: {qrcode_url}\n")
        except ImportError:
            print(f"\n  二维码链接: {qrcode_url}")
            print("  (安装 'qrcode' 包可在终端显示二维码)\n")

    def _notify_cloud_qrcode(self, qrcode_url: str):
        """Send QR code URL to cloud console when running in cloud mode."""
        if not self.cloud_mode:
            return
        try:
            from common import cloud_client
            client = getattr(cloud_client, "chat_client", None)
            if client and getattr(client, "client_id", None):
                client.send_channel_qrcode("weixin", qrcode_url)
        except Exception as e:
            logger.warning(f"[Weixin] Failed to notify cloud QR code: {e}")

    def _notify_cloud_connected(self):
        """Send connected status to cloud console when login succeeds."""
        if not self.cloud_mode:
            return
        try:
            from common import cloud_client
            client = getattr(cloud_client, "chat_client", None)
            if client and getattr(client, "client_id", None):
                client.send_channel_status("weixin", "connected")
        except Exception as e:
            logger.warning(f"[Weixin] Failed to notify cloud connected: {e}")

    def _qr_login(self, base_url: str) -> dict:
        """Perform interactive QR code login. Returns dict with token/base_url or empty dict."""
        api = WeixinApi(base_url=base_url)
        try:
            qr_resp = api.fetch_qr_code()
        except Exception as e:
            logger.error(f"[Weixin] Failed to fetch QR code: {e}")
            return {}

        qrcode = qr_resp.get("qrcode", "")
        qrcode_url = qr_resp.get("qrcode_img_content", "")

        if not qrcode:
            logger.error("[Weixin] No QR code returned from server")
            return {}

        self._current_qr_url = qrcode_url
        logger.info(f"[Weixin] 微信二维码链接: {qrcode_url}")
        self._print_qr(qrcode_url)
        self._notify_cloud_qrcode(qrcode_url)
        print("  等待扫码...\n")

        scanned_printed = False
        refresh_count = 0
        poll_errors = 0
        deadline = time.time() + QR_LOGIN_TIMEOUT_S

        while not self._stop_event.is_set():
            if time.time() >= deadline:
                logger.warning(f"[Weixin] QR login timed out after {QR_LOGIN_TIMEOUT_S}s")
                print(f"\n  二维码登录超时（{QR_LOGIN_TIMEOUT_S}s），请重启后重试")
                break

            try:
                status_resp = api.poll_qr_status(qrcode)
                poll_errors = 0
            except Exception as e:
                # Transient network/proxy hiccups must not kill the whole
                # login window; only give up after several failures in a row.
                poll_errors += 1
                logger.error(f"[Weixin] QR status poll error ({poll_errors}/5): {e}")
                if poll_errors >= 5:
                    return {}
                self._stop_event.wait(5)
                continue

            status = status_resp.get("status", "wait")

            if status == "wait":
                pass
            elif status == "scaned":
                self.login_status = self.LOGIN_STATUS_SCANNED
                if not scanned_printed:
                    print("  已扫码，请在手机上确认...")
                    scanned_printed = True
            elif status == "expired":
                refresh_count += 1
                if refresh_count >= QR_MAX_REFRESHES:
                    logger.warning(f"[Weixin] QR code refreshed {QR_MAX_REFRESHES} times, giving up")
                    print(f"\n  二维码已刷新 {QR_MAX_REFRESHES} 次仍未扫码，请重启后重试")
                    break
                print(f"  二维码已过期，正在刷新（{refresh_count}/{QR_MAX_REFRESHES}）...")
                try:
                    qr_resp = api.fetch_qr_code()
                    qrcode = qr_resp.get("qrcode", "")
                    qrcode_url = qr_resp.get("qrcode_img_content", "")
                    scanned_printed = False
                    self._current_qr_url = qrcode_url
                    logger.info(f"[Weixin] 微信二维码链接 ({refresh_count}/{QR_MAX_REFRESHES}): {qrcode_url}")
                    self._print_qr(qrcode_url)
                    self._notify_cloud_qrcode(qrcode_url)
                except Exception as e:
                    # Same as poll errors: retry on the next tick instead of
                    # aborting (the expired status will trigger refresh again).
                    logger.error(f"[Weixin] QR refresh failed, will retry: {e}")
                    self._stop_event.wait(5)
                    continue
            elif status == "confirmed":
                bot_token = status_resp.get("bot_token", "")
                bot_id = status_resp.get("ilink_bot_id", "")
                result_base_url = status_resp.get("baseurl", base_url)
                user_id = status_resp.get("ilink_user_id", "")

                if not bot_token or not bot_id:
                    logger.error("[Weixin] Login confirmed but missing token/bot_id")
                    return {}

                self._current_qr_url = ""
                print(f"\n  ✅ 微信登录成功！bot_id={bot_id}")
                logger.info(f"[Weixin] Login confirmed: bot_id={bot_id}")
                self._notify_cloud_connected()

                creds = {
                    "token": bot_token,
                    "base_url": result_base_url,
                    "bot_id": bot_id,
                    "user_id": user_id,
                }
                _save_credentials(self._credentials_path, creds)
                logger.info(f"[Weixin] Credentials saved to {self._credentials_path}")

                return {"token": bot_token, "base_url": result_base_url}

            self._stop_event.wait(1)

        self._current_qr_url = ""
        if self._stop_event.is_set():
            logger.info("[Weixin] QR login cancelled by stop event")
        return {}

    # ── Long-poll loop ─────────────────────────────────────────────────

    def _poll_loop(self):
        """Main long-poll loop: getUpdates -> parse -> produce."""
        logger.info("[Weixin] Starting long-poll loop")
        consecutive_failures = 0

        while not self._stop_event.is_set():
            try:
                resp = self.api.get_updates(self._get_updates_buf)

                ret = resp.get("ret", 0)
                errcode = resp.get("errcode", 0)

                is_error = (ret != 0) or (errcode != 0)
                if is_error:
                    if errcode == SESSION_EXPIRED_ERRCODE or ret == SESSION_EXPIRED_ERRCODE:
                        logger.error("[Weixin] Session expired (errcode -14), starting re-login...")
                        if self._relogin():
                            logger.info("[Weixin] Re-login successful, resuming long-poll")
                            self._get_updates_buf = ""
                            consecutive_failures = 0
                            continue
                        else:
                            logger.error("[Weixin] Re-login failed, will retry in 5 minutes")
                            self._stop_event.wait(300)
                            continue

                    consecutive_failures += 1
                    errmsg = resp.get("errmsg", "")
                    logger.error(f"[Weixin] getUpdates error: ret={ret} errcode={errcode} "
                                 f"errmsg={errmsg} ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES})")
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        consecutive_failures = 0
                        self._stop_event.wait(BACKOFF_DELAY)
                    else:
                        self._stop_event.wait(RETRY_DELAY)
                    continue

                consecutive_failures = 0

                # Update sync cursor
                new_buf = resp.get("get_updates_buf", "")
                if new_buf:
                    self._get_updates_buf = new_buf

                # Process messages
                msgs = resp.get("msgs", [])
                for raw_msg in msgs:
                    try:
                        self._process_message(raw_msg)
                    except Exception as e:
                        logger.error(f"[Weixin] Failed to process message: {e}", exc_info=True)

            except Exception as e:
                if self._stop_event.is_set():
                    break
                consecutive_failures += 1
                logger.error(f"[Weixin] getUpdates exception: {e} "
                             f"({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES})")
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    consecutive_failures = 0
                    self._stop_event.wait(BACKOFF_DELAY)
                else:
                    self._stop_event.wait(RETRY_DELAY)

        logger.info("[Weixin] Long-poll loop ended")

    def _process_message(self, raw_msg: dict):
        """Parse a single inbound message and produce to the handling queue."""
        msg_type = raw_msg.get("message_type", 0)
        if msg_type != 1:  # Only process USER messages (type=1)
            return

        msg_id = str(raw_msg.get("message_id", raw_msg.get("seq", "")))
        if self._received_msgs.get(msg_id):
            return
        self._received_msgs[msg_id] = True

        from_user = raw_msg.get("from_user_id", "")
        context_token = raw_msg.get("context_token", "")

        if context_token and from_user:
            self._update_context_token(from_user, context_token)

        cdn_base_url = self.api.cdn_base_url if self.api else CDN_BASE_URL
        try:
            wx_msg = WeixinMessage(raw_msg, cdn_base_url=cdn_base_url)
        except Exception as e:
            logger.error(f"[Weixin] Failed to parse WeixinMessage: {e}", exc_info=True)
            return

        logger.info(f"[Weixin] Received: from={from_user} ctype={wx_msg.ctype} "
                     f"content={str(wx_msg.content)[:50]}")

        # File cache logic
        from channel.file_cache import get_file_cache
        file_cache = get_file_cache()
        session_id = from_user

        if wx_msg.ctype == ContextType.IMAGE:
            if hasattr(wx_msg, "image_path") and wx_msg.image_path:
                file_cache.add(session_id, wx_msg.image_path, file_type="image")
                logger.info(f"[Weixin] Image cached for session {session_id}")
            return

        if wx_msg.ctype == ContextType.FILE:
            wx_msg.prepare()
            file_cache.add(session_id, wx_msg.content, file_type="file")
            logger.info(f"[Weixin] File cached for session {session_id}: {wx_msg.content}")
            return

        if wx_msg.ctype == ContextType.TEXT:
            cached_files = file_cache.get(session_id)
            if cached_files:
                refs = []
                for fi in cached_files:
                    ftype, fpath = fi["type"], fi["path"]
                    if ftype == "image":
                        refs.append(f"[图片: {fpath}]")
                    elif ftype == "video":
                        refs.append(f"[视频: {fpath}]")
                    else:
                        refs.append(f"[文件: {fpath}]")
                wx_msg.content = wx_msg.content + "\n" + "\n".join(refs)
                file_cache.clear(session_id)

        context = self._compose_context(
            wx_msg.ctype,
            wx_msg.content,
            isgroup=False,
            msg=wx_msg,
            no_need_at=True,
        )
        if context:
            self.produce(context)
            # Follow-up tracking: user replied, reset pending state
            str_uid = str(from_user)
            terminating = _is_terminating_message(str(getattr(wx_msg, "content", "") or ""))
            with self._followup_lock:
                self._last_user_msg_time[str_uid] = datetime.now()
                self._followup_fired[str_uid] = False
                self._followup_stopped[str_uid] = terminating
                self._followup_count[str_uid] = 0

    # ── _compose_context ───────────────────────────────────────────────

    def _compose_context(self, ctype: ContextType, content, **kwargs):
        context = Context(ctype, content)
        context.kwargs = kwargs
        if "channel_type" not in context:
            context["channel_type"] = self.channel_type
        if "origin_ctype" not in context:
            context["origin_ctype"] = ctype

        cmsg = context["msg"]
        context["session_id"] = cmsg.from_user_id
        context["receiver"] = cmsg.other_user_id

        if ctype == ContextType.TEXT:
            img_match_prefix = check_prefix(content, conf().get("image_create_prefix"))
            if img_match_prefix:
                content = content.replace(img_match_prefix, "", 1)
                context.type = ContextType.IMAGE_CREATE
            else:
                context.type = ContextType.TEXT
            context.content = content.strip()
            if "desire_rtype" not in context and conf().get("always_reply_voice"):
                context["desire_rtype"] = ReplyType.VOICE

        elif ctype == ContextType.VOICE:
            if "desire_rtype" not in context and (
                conf().get("voice_reply_voice") or conf().get("always_reply_voice")
            ):
                context["desire_rtype"] = ReplyType.VOICE

        return context

    # ── Send reply ─────────────────────────────────────────────────────

    def send(self, reply: Reply, context: Context):
        receiver = context.get("receiver", "")
        msg = context.get("msg")
        context_token = self._get_context_token(receiver, msg)

        if not context_token:
            logger.error(f"[Weixin] No context_token for receiver={receiver}, cannot send")
            return

        if reply.type == ReplyType.TEXT:
            text = str(reply.content) if reply.content is not None else ""
            msg_parts = split_text_bubbles(text)
            if not msg_parts:
                msg_parts = [text]
            for i, part in enumerate(msg_parts):
                if i > 0:
                    time.sleep(0.8)
                self._send_text(part, receiver, context_token)
        elif reply.type in (ReplyType.IMAGE_URL, ReplyType.IMAGE):
            self._send_image(reply.content, receiver, context_token)
        elif reply.type == ReplyType.FILE:
            self._send_file(reply.content, receiver, context_token)
        elif reply.type in (ReplyType.VIDEO, ReplyType.VIDEO_URL):
            self._send_video(reply.content, receiver, context_token)
        elif reply.type == ReplyType.VOICE:
            # ilink has no outbound voice item; deliver TTS as a file attachment.
            self._send_file(reply.content, receiver, context_token)
        else:
            logger.warning(f"[Weixin] Unsupported reply type: {reply.type}, fallback to text")
            self._send_text(str(reply.content), receiver, context_token)

        # Track last bot message for follow-up nudge (text only)
        if reply.type == ReplyType.TEXT:
            track_text = reply.content or ""
            if track_text:
                is_followup = context.get("is_followup", False)
                str_uid = str(receiver)
                with self._followup_lock:
                    self._last_bot_msg_time[str_uid] = datetime.now()
                    self._last_bot_msg_text[str_uid] = track_text
                    if is_followup:
                        self._followup_count[str_uid] = int(self._followup_count.get(str_uid, 0) or 0) + 1
                    else:
                        self._followup_count[str_uid] = 0
                    sent_count = self._followup_count[str_uid]
                    max_count = _followup_max_count()
                    if self._followup_stopped.get(str_uid, False):
                        # User ended the conversation — suppress nudging until they
                        # message again (which clears the flag).
                        self._followup_fired[str_uid] = True
                    elif max_count > 0 and sent_count >= max_count:
                        self._followup_fired[str_uid] = True
                        self._followup_stopped[str_uid] = True
                        logger.info(
                            f"[Weixin] followup limit reached for {str_uid} "
                            f"({sent_count}/{max_count}), stopping nudges"
                        )
                    else:
                        self._followup_fired[str_uid] = False
                        self._followup_delay[str_uid] = _followup_delay_for(is_followup)
                self._save_followup_state()

    # ── Follow-up nudge ────────────────────────────────────────────────

    _FOLLOWUP_STATE_FILE = os.path.expanduser("~/cow/weixin_followup_state.json")
    _STATE_MAX_AGE = 7200  # don't restore entries older than 2h

    def _save_followup_state(self, force=False):
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
            os.makedirs(os.path.dirname(self._FOLLOWUP_STATE_FILE), exist_ok=True)
            tmp_path = self._FOLLOWUP_STATE_FILE + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
            os.replace(tmp_path, self._FOLLOWUP_STATE_FILE)
        except Exception as e:
            logger.warning(f"[Weixin] Failed to save followup state: {e}")

    def _load_followup_state(self):
        try:
            if not os.path.exists(self._FOLLOWUP_STATE_FILE):
                return
            with open(self._FOLLOWUP_STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            cutoff = datetime.now() - timedelta(seconds=self._STATE_MAX_AGE)
            restored = 0
            for k, v in state.get("last_bot_msg_time", {}).items():
                ts = datetime.fromisoformat(v)
                if ts >= cutoff:
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
                if k in state.get("last_user_msg_time", {}):
                    try:
                        self._last_user_msg_time[k] = datetime.fromisoformat(state["last_user_msg_time"][k])
                    except Exception:
                        pass
            if restored:
                logger.info(f"[Weixin] Restored followup state for {restored} user(s)")
        except Exception as e:
            logger.warning(f"[Weixin] Failed to load followup state: {e}")

    def _followup_check_loop(self):
        """Background thread: every 30s check if any user needs a nudge."""
        while not self._stop_event.is_set():
            self._stop_event.wait(30)
            if self._stop_event.is_set():
                break
            try:
                self._maybe_send_followups()
            except Exception as e:
                logger.warning(f"[Weixin] followup check error: {e}")

    def _maybe_send_followups(self):
        now = datetime.now()
        max_count = _followup_max_count()
        to_nudge = []
        state_changed = False
        with self._followup_lock:
            for user_id, sent_at in list(self._last_bot_msg_time.items()):
                if self._followup_stopped.get(user_id, False):
                    self._followup_fired[user_id] = True
                    continue
                if self._followup_fired.get(user_id, False):
                    continue
                sent_count = int(self._followup_count.get(user_id, 0) or 0)
                if max_count > 0 and sent_count >= max_count:
                    self._followup_fired[user_id] = True
                    self._followup_stopped[user_id] = True
                    state_changed = True
                    logger.info(
                        f"[Weixin] followup limit reached for {user_id} "
                        f"({sent_count}/{max_count}), stopping nudges"
                    )
                    continue
                delay_sec = self._followup_delay.get(user_id) or _followup_delay_for(False)
                elapsed = (now - sent_at).total_seconds()
                if elapsed < delay_sec:
                    continue
                self._followup_fired[user_id] = True
                state_changed = True
                last_user = self._last_user_msg_time.get(user_id)
                if last_user and last_user >= sent_at:
                    continue
                to_nudge.append((user_id, elapsed))
        for user_id, elapsed in to_nudge:
            logger.info(f"[Weixin] No reply in {int(elapsed)}s, sending followup to {user_id}")
            self._trigger_followup(user_id, elapsed)
        if to_nudge or state_changed:
            self._save_followup_state(force=True)

    def _trigger_followup(self, user_id: str, elapsed: float = 0):
        """Generate and queue an agent follow-up for the given user."""
        try:
            last_msg = self._last_bot_msg_text.get(user_id, "")
            last_msg_clean = re.sub(r'\[MSG\]', ' ', last_msg, flags=re.IGNORECASE).strip()
            from common.monologue_filter import is_probable_full_monologue, strip_leaked_monologue
            last_msg_clean = strip_leaked_monologue(last_msg_clean).strip()
            if is_probable_full_monologue(last_msg_clean):
                logger.warning("[Weixin] followup seed dropped because it still looks like monologue")
                last_msg_clean = ""
            task = _build_followup_task(last_msg_clean, elapsed)
            context = Context(ContextType.TEXT, task)
            context["receiver"] = user_id
            context["session_id"] = user_id
            context["channel_type"] = self.channel_type
            context["isgroup"] = False
            context["is_followup"] = True
            self.produce(context)
        except Exception as e:
            logger.error(f"[Weixin] Failed to trigger followup for {user_id}: {e}")

    def _get_context_token(self, receiver: str, msg=None) -> str:
        """Get the context_token for a receiver, required for all sends."""
        if msg and hasattr(msg, "context_token") and msg.context_token:
            return msg.context_token
        return self._context_tokens.get(receiver, "")

    def _check_send_response(self, resp, receiver: str) -> None:
        """Inspect a send-API response; drop stale context_token on -14.

        ilink uses ret/errcode = -14 to signal that the session (and any
        cached context_token) is no longer valid. The plugin keeps running
        because the bot itself can re-login; we just need to forget the
        per-user token so the next push won't retry forever.
        """
        if not isinstance(resp, dict):
            return
        ret = resp.get("ret")
        errcode = resp.get("errcode")
        if ret == -14 or errcode == -14:
            logger.warning(
                f"[Weixin] Send returned -14 (session expired) for "
                f"receiver={receiver}; dropping cached context_token"
            )
            self._invalidate_context_token(receiver)

    def _send_text(self, text: str, receiver: str, context_token: str):
        if len(text) <= TEXT_CHUNK_LIMIT:
            try:
                resp = self.api.send_text(receiver, text, context_token)
                self._check_send_response(resp, receiver)
                logger.debug(f"[Weixin] Text sent to {receiver}, len={len(text)}")
            except Exception as e:
                logger.error(f"[Weixin] Failed to send text: {e}")
            return

        chunks = self._split_text(text, TEXT_CHUNK_LIMIT)
        for i, chunk in enumerate(chunks):
            try:
                resp = self.api.send_text(receiver, chunk, context_token)
                self._check_send_response(resp, receiver)
                logger.debug(f"[Weixin] Text chunk {i+1}/{len(chunks)} sent to {receiver}, len={len(chunk)}")
            except Exception as e:
                logger.error(f"[Weixin] Failed to send text chunk {i+1}/{len(chunks)}: {e}")
                break
            if i < len(chunks) - 1:
                time.sleep(0.5)

    @staticmethod
    def _split_text(text: str, limit: int) -> list:
        """Split text into chunks, preferring to break at paragraph or line boundaries."""
        if len(text) <= limit:
            return [text]
        chunks = []
        while text:
            if len(text) <= limit:
                chunks.append(text)
                break
            cut = text.rfind("\n\n", 0, limit)
            if cut <= 0:
                cut = text.rfind("\n", 0, limit)
            if cut <= 0:
                cut = limit
            chunks.append(text[:cut])
            text = text[cut:].lstrip("\n")
        return chunks

    def _send_image(self, img_path_or_url: str, receiver: str, context_token: str):
        local_path = self._resolve_media_path(img_path_or_url)
        if not local_path:
            self._send_text("[Image send failed: file not found]", receiver, context_token)
            return
        try:
            result = upload_media_to_cdn(self.api, local_path, receiver, media_type=1)
            resp = self.api.send_image_item(
                to=receiver,
                context_token=context_token,
                encrypt_query_param=result["encrypt_query_param"],
                aes_key_b64=result["aes_key_b64"],
                ciphertext_size=result["ciphertext_size"],
            )
            self._check_send_response(resp, receiver)
            logger.info(f"[Weixin] Image sent to {receiver}")
        except Exception as e:
            logger.error(f"[Weixin] Image send failed: {e}")
            self._send_text("[Image send failed]", receiver, context_token)

    def _send_file(self, file_path_or_url: str, receiver: str, context_token: str):
        local_path = self._resolve_media_path(file_path_or_url)
        if not local_path:
            self._send_text("[File send failed: file not found]", receiver, context_token)
            return
        try:
            result = upload_media_to_cdn(self.api, local_path, receiver, media_type=3)
            resp = self.api.send_file_item(
                to=receiver,
                context_token=context_token,
                encrypt_query_param=result["encrypt_query_param"],
                aes_key_b64=result["aes_key_b64"],
                file_name=os.path.basename(local_path),
                file_size=result["raw_size"],
            )
            self._check_send_response(resp, receiver)
            logger.info(f"[Weixin] File sent to {receiver}")
        except Exception as e:
            logger.error(f"[Weixin] File send failed: {e}")
            self._send_text("[File send failed]", receiver, context_token)

    def _send_video(self, video_path_or_url: str, receiver: str, context_token: str):
        local_path = self._resolve_media_path(video_path_or_url)
        if not local_path:
            self._send_text("[Video send failed: file not found]", receiver, context_token)
            return
        try:
            result = upload_media_to_cdn(self.api, local_path, receiver, media_type=2)
            resp = self.api.send_video_item(
                to=receiver,
                context_token=context_token,
                encrypt_query_param=result["encrypt_query_param"],
                aes_key_b64=result["aes_key_b64"],
                ciphertext_size=result["ciphertext_size"],
            )
            self._check_send_response(resp, receiver)
            logger.info(f"[Weixin] Video sent to {receiver}")
        except Exception as e:
            logger.error(f"[Weixin] Video send failed: {e}")
            self._send_text("[Video send failed]", receiver, context_token)

    @staticmethod
    def _resolve_media_path(path_or_url: str) -> str:
        """Resolve a file path or URL to a local file path. Downloads if needed."""
        if not path_or_url:
            return ""

        local_path = path_or_url
        if local_path.startswith("file://"):
            local_path = local_path[7:]

        if local_path.startswith(("http://", "https://")):
            try:
                resp = requests.get(local_path, timeout=60)
                resp.raise_for_status()
                ct = resp.headers.get("Content-Type", "")
                ext = ".bin"
                if "jpeg" in ct or "jpg" in ct:
                    ext = ".jpg"
                elif "png" in ct:
                    ext = ".png"
                elif "gif" in ct:
                    ext = ".gif"
                elif "webp" in ct:
                    ext = ".webp"
                elif "mp4" in ct:
                    ext = ".mp4"
                elif "pdf" in ct:
                    ext = ".pdf"

                tmp_path = os.path.join(tempfile.gettempdir(), f"wx_media_{uuid.uuid4().hex[:8]}{ext}")
                with open(tmp_path, "wb") as f:
                    f.write(resp.content)
                return tmp_path
            except Exception as e:
                logger.error(f"[Weixin] Failed to download media: {e}")
                return ""

        if os.path.exists(local_path):
            return local_path

        logger.warning(f"[Weixin] Media file not found: {local_path}")
        return ""
