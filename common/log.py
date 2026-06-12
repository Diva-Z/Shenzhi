import logging
import sys
import io
import os
import re


def _log_file_name():
    instance = (os.environ.get("SHENZHI_INSTANCE") or "").strip()
    if not instance:
        config_path = os.environ.get("SHENZHI_CONFIG") or os.environ.get("COW_CONFIG") or ""
        base = os.path.basename(config_path)
        if base.startswith("config-") and base.endswith(".json"):
            instance = base[len("config-"):-len(".json")]
    if instance:
        safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", instance).strip("-")
        if safe and safe != "default":
            return f"run-{safe}.log"
    return "run.log"


def _reset_logger(log):
    for handler in log.handlers:
        handler.close()
        log.removeHandler(handler)
        del handler
    log.handlers.clear()
    log.propagate = False
    stdout = sys.stdout
    if hasattr(stdout, "buffer"):
        stdout = io.TextIOWrapper(stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    console_handle = logging.StreamHandler(stdout)
    console_handle.setFormatter(
        logging.Formatter(
            "[%(levelname)s][%(asctime)s][%(filename)s:%(lineno)d] - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    file_handle = logging.FileHandler(_log_file_name(), encoding="utf-8")
    file_handle.setFormatter(
        logging.Formatter(
            "[%(levelname)s][%(asctime)s][%(filename)s:%(lineno)d] - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    log.addHandler(file_handle)
    log.addHandler(console_handle)


def _get_logger():
    log = logging.getLogger("log")
    _reset_logger(log)
    log.setLevel(logging.INFO)
    return log


# 日志句柄
logger = _get_logger()
