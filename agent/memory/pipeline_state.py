"""Durable state for the memory flush pipeline.

The flush pipeline used to keep its three dedup hashes purely in memory, so a
restart replayed already-flushed turns and re-dreamed identical dailies. This
module persists them in a small JSON file next to the daily memory files and
adds pending/committed semantics so a failed flush never poisons the dedup
state (previously a hash was marked "flushed" before the write succeeded).

Usage:
    tx = state.begin(new_trim_hashes={...}, new_content_hash="...")
    ... do the LLM call + file write ...
    state.commit(tx)      # on success — that tx's hashes become durable
    state.rollback(tx)    # on failure — that tx's pending hashes are discarded

Each ``begin()`` returns an opaque transaction id and stages its hashes in an
isolated slot, so concurrent flushes on the same ``memory_dir`` never clobber
each other's pending state. Instances are shared per ``memory_dir`` via
:meth:`PipelineState.get`, so sibling sessions of one persona funnel their
commits through a single in-memory state and a single serialized writer.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Dict, Optional, Set

from common.log import logger

STATE_FILE_NAME = ".memory_pipeline_state.json"

# Upper bound on remembered trim hashes. Each entry is a 32-char md5, so 2000
# entries stay well under 100 KB while covering far more turns than any
# realistic context window.
MAX_TRIM_HASHES = 2000


class PipelineState:
    """Transactional store for the flush pipeline's dedup hashes.

    Three pieces of state are tracked:
    - ``trim_flushed_hashes``: per-message content hashes already written
    - ``last_flushed_content_hash``: whole-batch hash for daily-summary dedup
    - ``last_dream_input_hash``: ``"{date}:{daily_hash}"`` for Deep Dream dedup
    """

    # Per-memory_dir registry: sibling MemoryFlushManager instances (one per
    # session of the same persona) share a single PipelineState so their
    # commits do not overwrite each other's on-disk state.
    _instances: Dict[Path, "PipelineState"] = {}
    _class_lock = threading.Lock()

    def __init__(self, memory_dir: Path):
        self.memory_dir = Path(memory_dir)
        self.state_file_path = self.memory_dir / STATE_FILE_NAME

        # committed: durable, mirrored to disk on every commit()
        self._trim_hashes: "OrderedDict[str, None]" = OrderedDict()
        self._content_hash: str = ""
        self._dream_hash: str = ""

        # pending: one isolated slot per in-flight transaction, keyed by the
        # tx id returned from begin(). commit()/rollback() only touch their own
        # slot, so an aborted flush never clears a concurrent flush's hashes.
        self._pending_txns: Dict[str, dict] = {}

        # Serializes begin/commit/rollback and guards _pending_txns + the disk
        # write, so concurrent flushes on the same state can't interleave.
        self._tx_lock = threading.Lock()

        self.load()

    @classmethod
    def get(cls, memory_dir: Path) -> "PipelineState":
        """Return the shared instance for *memory_dir* (creating it once).

        This is the primary API; constructing ``PipelineState`` directly still
        works (used by tests to simulate a restart with a fresh instance).
        """
        key = Path(memory_dir).resolve()
        with cls._class_lock:
            inst = cls._instances.get(key)
            if inst is None:
                inst = cls(memory_dir)
                cls._instances[key] = inst
            return inst

    @classmethod
    def reset_all(cls) -> None:
        """For testing: drop the per-memory_dir instance registry."""
        with cls._class_lock:
            cls._instances.clear()

    # ---- committed-state views -------------------------------------------

    @property
    def trim_flushed_hashes(self) -> Set[str]:
        """Committed per-message hashes (copy; mutating it has no effect)."""
        return set(self._trim_hashes.keys())

    @property
    def last_flushed_content_hash(self) -> str:
        return self._content_hash

    @property
    def last_dream_input_hash(self) -> str:
        return self._dream_hash

    @property
    def has_pending(self) -> bool:
        return bool(self._pending_txns)

    def is_trimmed(self, msg_hash: str) -> bool:
        """True when *msg_hash* was already flushed in a committed batch."""
        return msg_hash in self._trim_hashes

    def content_hash_matches(self, content_hash: str) -> bool:
        return bool(content_hash) and content_hash == self._content_hash

    def dream_hash_matches(self, dream_hash: str) -> bool:
        return bool(dream_hash) and dream_hash == self._dream_hash

    # ---- transaction ----------------------------------------------------

    def begin(
        self,
        new_trim_hashes: Optional[Set[str]] = None,
        new_content_hash: Optional[str] = None,
        new_dream_hash: Optional[str] = None,
    ) -> str:
        """Stage hashes for the flush that is about to run.

        Returns an opaque transaction id that must be passed to ``commit`` or
        ``rollback``. Staged values are invisible to ``is_trimmed`` /
        ``*_matches`` until the matching ``commit()``, so a crashed or failed
        flush leaves no trace. Each transaction owns an isolated slot, so a
        concurrent flush's ``rollback()`` can never clear these hashes.
        """
        tx_id = uuid.uuid4().hex
        with self._tx_lock:
            self._pending_txns[tx_id] = {
                "trim_hashes": set(new_trim_hashes) if new_trim_hashes else set(),
                "content_hash": new_content_hash,
                "dream_hash": new_dream_hash,
            }
        return tx_id

    def commit(self, tx_id: str) -> None:
        """Merge *tx_id*'s staged values into committed state and persist.

        A no-op for an unknown tx id (already committed/rolled back).
        """
        with self._tx_lock:
            pending = self._pending_txns.pop(tx_id, None)
            if pending is None:
                return

            for h in sorted(pending["trim_hashes"]):
                # Re-inserting must not refresh FIFO position of a known hash.
                if h not in self._trim_hashes:
                    self._trim_hashes[h] = None
            if pending["content_hash"] is not None:
                self._content_hash = pending["content_hash"]
            if pending["dream_hash"] is not None:
                self._dream_hash = pending["dream_hash"]

            self._evict_overflow()
            self.save()

    def rollback(self, tx_id: str) -> None:
        """Discard *tx_id*'s staged values; committed state and disk untouched.

        Only removes this transaction's slot, so other in-flight transactions
        keep their pending hashes.
        """
        with self._tx_lock:
            self._pending_txns.pop(tx_id, None)

    def _evict_overflow(self) -> None:
        """Drop oldest trim hashes once the cap is exceeded (FIFO)."""
        overflow = len(self._trim_hashes) - MAX_TRIM_HASHES
        for _ in range(max(0, overflow)):
            self._trim_hashes.popitem(last=False)

    # ---- persistence ----------------------------------------------------

    def load(self) -> None:
        """Read state from disk; a missing or corrupt file yields empty state."""
        try:
            raw = self.state_file_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except FileNotFoundError:
            return
        except Exception as e:
            logger.warning(
                f"[PipelineState] Unreadable state file {self.state_file_path}: {e}; "
                f"starting with empty state"
            )
            return

        if not isinstance(data, dict):
            logger.warning("[PipelineState] State file is not an object; ignoring")
            return

        hashes = data.get("trim_flushed_hashes") or []
        if isinstance(hashes, list):
            for h in hashes:
                if isinstance(h, str) and h and h not in self._trim_hashes:
                    self._trim_hashes[h] = None
        self._evict_overflow()

        content_hash = data.get("last_flushed_content_hash") or ""
        dream_hash = data.get("last_dream_input_hash") or ""
        self._content_hash = content_hash if isinstance(content_hash, str) else ""
        self._dream_hash = dream_hash if isinstance(dream_hash, str) else ""

    def save(self) -> None:
        """Atomically write committed state (tmp file + fsync + os.replace)."""
        payload = {
            "trim_flushed_hashes": sorted(self._trim_hashes.keys()),
            "last_flushed_content_hash": self._content_hash,
            "last_dream_input_hash": self._dream_hash,
        }
        tmp_path = self.state_file_path.with_suffix(self.state_file_path.suffix + ".tmp")
        try:
            self.memory_dir.mkdir(parents=True, exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
        except Exception as e:
            logger.warning(f"[PipelineState] Failed to write state file: {e}")
            return

        # os.replace can transiently fail on Windows when a scanner/indexer
        # holds the destination open; a short retry makes it reliable.
        for attempt in range(3):
            try:
                os.replace(tmp_path, self.state_file_path)
                return
            except PermissionError as e:
                if attempt == 2:
                    logger.warning(
                        f"[PipelineState] Failed to replace state file after retries: {e}"
                    )
                    return
                time.sleep(0.1)
            except Exception as e:
                logger.warning(f"[PipelineState] Failed to replace state file: {e}")
                return
