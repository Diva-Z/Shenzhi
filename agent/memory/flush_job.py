"""Flush job state machine for memory pipeline reliability."""
import enum
import threading
import time
from typing import Optional, Set


class FlushStatus(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    SKIPPED_NO_CONTENT = "skipped_no_content"
    FAILED = "failed"
    TIMEOUT = "timeout"


class FlushJob:
    """Tracks the lifecycle of a single flush operation.

    Callers can wait() for completion (blocking) or poll status.
    __bool__ returns True unless FAILED/TIMEOUT, preserving existing
    `if result:` call-site semantics.
    """

    def __init__(self, message_hashes: Optional[Set[str]] = None):
        self.status = FlushStatus.PENDING
        self.error: Optional[str] = None
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.message_hashes: Set[str] = message_hashes or set()
        self._event = threading.Event()

    def __bool__(self):
        return self.status not in (FlushStatus.FAILED, FlushStatus.TIMEOUT)

    @property
    def is_done(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: Optional[float] = None) -> "FlushStatus":
        """Block until job reaches a terminal state or timeout expires.

        On timeout this returns ``FlushStatus.TIMEOUT`` to the caller WITHOUT
        mutating ``self.status``: the worker thread may still be running and
        will set the real terminal state (SUCCESS/SKIPPED/FAILED) later.
        """
        completed = self._event.wait(timeout=timeout)
        if not completed:
            return FlushStatus.TIMEOUT
        return self.status

    def mark_running(self):
        self.status = FlushStatus.RUNNING
        self.started_at = time.time()

    def mark_success(self):
        self.status = FlushStatus.SUCCESS
        self.finished_at = time.time()
        self._event.set()

    def mark_skipped(self):
        self.status = FlushStatus.SKIPPED_NO_CONTENT
        self.finished_at = time.time()
        self._event.set()

    def mark_failed(self, error: str = ""):
        self.status = FlushStatus.FAILED
        self.error = error
        self.finished_at = time.time()
        self._event.set()
