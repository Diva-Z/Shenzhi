"""Cross-channel identity management.

Links multiple channel-specific sessions to a single canonical identity,
enabling unified conversation history access.

Design notes
------------
This module shares the ConversationStore's SQLite database. It does NOT hold
a long-lived connection; instead it borrows the store's per-call connection
helper (``_connect``) and its re-entrant lock (``_lock``) so it obeys the same
thread-safety and WAL settings as the rest of the store.
"""

from __future__ import annotations

import time
import uuid
from typing import List, Optional

from common.log import logger


class IdentityManager:
    """Manages cross-channel identity bindings within ConversationStore's DB.

    An *identity* represents one real person. Each identity can be bound to
    many channel accounts (Telegram, WeChat, Web, ...). Once bound, all the
    sessions belonging to those accounts can be searched together.
    """

    def __init__(self, store):
        """Takes a ConversationStore instance to share its DB connection."""
        self._store = store
        self._init_tables()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_tables(self) -> None:
        """Create identity tables if absent and migrate the sessions table."""
        with self._store._lock:
            conn = self._store._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS identities (
                        identity_id   TEXT PRIMARY KEY,
                        display_name  TEXT NOT NULL DEFAULT '',
                        created_at    INTEGER NOT NULL,
                        updated_at    INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS identity_bindings (
                        id            INTEGER PRIMARY KEY AUTOINCREMENT,
                        identity_id   TEXT NOT NULL,
                        channel_type  TEXT NOT NULL,
                        external_id   TEXT NOT NULL,
                        session_id    TEXT NOT NULL DEFAULT '',
                        created_at    INTEGER NOT NULL,
                        UNIQUE(channel_type, external_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_bindings_identity
                        ON identity_bindings(identity_id);
                    CREATE INDEX IF NOT EXISTS idx_bindings_channel
                        ON identity_bindings(channel_type, external_id);
                    """
                )
                conn.commit()
                # Migrate sessions table: add identity_id column if missing.
                cols = {
                    row[1]
                    for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
                }
                if "identity_id" not in cols:
                    try:
                        conn.execute(
                            "ALTER TABLE sessions ADD COLUMN identity_id TEXT DEFAULT NULL"
                        )
                        conn.commit()
                        logger.info(
                            "[IdentityManager] Migrated: added sessions.identity_id column"
                        )
                    except Exception as e:  # pragma: no cover - defensive
                        logger.warning(f"[IdentityManager] identity_id migration failed: {e}")
                try:
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_sessions_identity "
                        "ON sessions(identity_id)"
                    )
                    conn.commit()
                except Exception as e:  # pragma: no cover - defensive
                    logger.warning(f"[IdentityManager] identity index failed: {e}")
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_identity(self, display_name: str = "") -> str:
        """Create a new identity, returns its identity_id."""
        identity_id = str(uuid.uuid4())[:8]
        now = int(time.time())
        with self._store._lock:
            conn = self._store._connect()
            try:
                conn.execute(
                    "INSERT INTO identities (identity_id, display_name, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (identity_id, display_name, now, now),
                )
                conn.commit()
            finally:
                conn.close()
        return identity_id

    def bind_channel(
        self,
        identity_id: str,
        channel_type: str,
        external_id: str,
        session_id: str = "",
    ) -> bool:
        """Bind a channel account to an identity.

        Returns True on success (or when already bound to the same identity),
        False when the account is already bound to a *different* identity.
        """
        with self._store._lock:
            conn = self._store._connect()
            try:
                existing = conn.execute(
                    "SELECT identity_id FROM identity_bindings "
                    "WHERE channel_type=? AND external_id=?",
                    (channel_type, external_id),
                ).fetchone()
                if existing:
                    if existing[0] == identity_id:
                        # Idempotent: refresh the cached session tag if provided.
                        if session_id:
                            conn.execute(
                                "UPDATE sessions SET identity_id=? WHERE session_id=?",
                                (identity_id, session_id),
                            )
                            conn.commit()
                        return True
                    return False  # Bound to a different identity

                now = int(time.time())
                conn.execute(
                    "INSERT INTO identity_bindings "
                    "(identity_id, channel_type, external_id, session_id, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (identity_id, channel_type, external_id, session_id, now),
                )
                # Also tag the session so resolution is a single-row lookup later.
                if session_id:
                    conn.execute(
                        "UPDATE sessions SET identity_id=? WHERE session_id=?",
                        (identity_id, session_id),
                    )
                conn.commit()
                return True
            finally:
                conn.close()

    def resolve_identity(
        self, session_id: str, channel_type: str = ""
    ) -> Optional[str]:
        """Given a session_id, resolve to an identity_id, or None if unlinked."""
        with self._store._lock:
            conn = self._store._connect()
            try:
                # Fast path: session already tagged.
                row = conn.execute(
                    "SELECT identity_id FROM sessions WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                if row and row[0]:
                    return row[0]

                # Slow path: derive the external id from the session_id pattern
                # and look up an existing binding.
                external_id = self._extract_external_id(session_id, channel_type)
                if external_id and channel_type:
                    row = conn.execute(
                        "SELECT identity_id FROM identity_bindings "
                        "WHERE channel_type=? AND external_id=?",
                        (channel_type, external_id),
                    ).fetchone()
                    if row:
                        # Cache the result in the sessions table for next time.
                        conn.execute(
                            "UPDATE sessions SET identity_id=? WHERE session_id=?",
                            (row[0], session_id),
                        )
                        conn.commit()
                        return row[0]
                return None
            finally:
                conn.close()

    def get_linked_sessions(self, identity_id: str) -> List[str]:
        """Return all session_ids linked to an identity."""
        with self._store._lock:
            conn = self._store._connect()
            try:
                rows = conn.execute(
                    "SELECT session_id FROM sessions WHERE identity_id=?",
                    (identity_id,),
                ).fetchall()
                session_ids = [r[0] for r in rows]
                # Include bindings whose session may not be tagged yet.
                binding_rows = conn.execute(
                    "SELECT session_id FROM identity_bindings "
                    "WHERE identity_id=? AND session_id != ''",
                    (identity_id,),
                ).fetchall()
                for r in binding_rows:
                    if r[0] and r[0] not in session_ids:
                        session_ids.append(r[0])
                return session_ids
            finally:
                conn.close()

    def search_across_identity(
        self, identity_id: str, query: str, limit: int = 50
    ) -> dict:
        """Search messages across all sessions linked to this identity.

        Returns the same dict shape as ``ConversationStore.search_messages``
        ({"query", "matches", "total_candidates"}). When the identity has no
        linked sessions, returns an empty result set.
        """
        session_ids = self.get_linked_sessions(identity_id)
        if not session_ids:
            return {"query": query, "matches": [], "total_candidates": 0}
        return self._store.search_messages(
            query=query, session_ids=session_ids, limit=limit
        )

    def get_identity_info(self, identity_id: str) -> Optional[dict]:
        """Return identity metadata and all its bindings, or None if absent."""
        with self._store._lock:
            conn = self._store._connect()
            try:
                row = conn.execute(
                    "SELECT identity_id, display_name, created_at, updated_at "
                    "FROM identities WHERE identity_id=?",
                    (identity_id,),
                ).fetchone()
                if not row:
                    return None
                bindings = conn.execute(
                    "SELECT channel_type, external_id, session_id, created_at "
                    "FROM identity_bindings WHERE identity_id=?",
                    (identity_id,),
                ).fetchall()
            finally:
                conn.close()
        return {
            "identity_id": row[0],
            "display_name": row[1],
            "created_at": row[2],
            "updated_at": row[3],
            "bindings": [
                {
                    "channel_type": b[0],
                    "external_id": b[1],
                    "session_id": b[2],
                    "created_at": b[3],
                }
                for b in bindings
            ],
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_external_id(session_id: str, channel_type: str) -> Optional[str]:
        """Extract the external user id from a session_id by known patterns."""
        if not session_id:
            return None
        prefixes = {
            "telegram": "tg_user_",
            "discord": "discord_user_",
            "slack": "slack_user_",
        }
        prefix = prefixes.get(channel_type, "")
        if prefix and session_id.startswith(prefix):
            return session_id[len(prefix):]
        # WeChat / Feishu: the session_id IS the external id. For the Feishu
        # group format "from:other", take the first part (the sender).
        if channel_type in ("weixin", "feishu"):
            return session_id.split(":")[0] if ":" in session_id else session_id
        return None
