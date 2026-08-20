"""
Memory module for AgentMesh

Provides both long-term memory (vector/keyword search) and short-term
conversation history persistence (SQLite).
"""

from agent.memory.manager import MemoryManager
from agent.memory.config import MemoryConfig, get_default_memory_config, set_global_memory_config
from agent.memory.embedding import create_embedding_provider
from agent.memory.conversation_store import ConversationStore, get_conversation_store
from agent.memory.identity import IdentityManager
from agent.memory.summarizer import ensure_daily_memory_file

__all__ = [
    'MemoryManager',
    'MemoryConfig',
    'get_default_memory_config',
    'set_global_memory_config',
    'create_embedding_provider',
    'ConversationStore',
    'get_conversation_store',
    'IdentityManager',
    'get_identity_manager',
    'ensure_daily_memory_file',
]


# ---------------------------------------------------------------------------
# Cross-channel identity singleton
# ---------------------------------------------------------------------------

_identity_manager = None


def get_identity_manager() -> IdentityManager:
    """Return the process-wide IdentityManager singleton.

    Shares the ConversationStore singleton's database so identity tables live
    alongside the sessions / messages tables in a single SQLite file.
    """
    global _identity_manager
    if _identity_manager is None:
        _identity_manager = IdentityManager(get_conversation_store())
    return _identity_manager
