"""
Agent Initializer - Handles agent initialization logic
"""

import os
import asyncio
import datetime
import threading
import time
from typing import Optional, List

from agent.protocol import Agent
from agent.tools import ToolManager
from common.log import logger
from common.utils import expand_path

# Module-level lock to serialize scheduler init across concurrent sessions
_scheduler_init_lock = threading.Lock()

# Track whether the embedding model log has been printed in this process,
# so we avoid spamming it once per session.
_embedding_logged: bool = False


class AgentInitializer:
    """
    Handles agent initialization including:
    - Workspace setup
    - Memory system initialization  
    - Tool loading
    - System prompt building
    """
    
    def __init__(self, bridge, agent_bridge):
        """
        Initialize agent initializer
        
        Args:
            bridge: COW bridge instance
            agent_bridge: AgentBridge instance (for create_agent method)
        """
        self.bridge = bridge
        self.agent_bridge = agent_bridge

    # ------------------------------------------------------------------
    # Channel memory isolation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _channel_workspace(workspace_root: str, channel_id: Optional[str]) -> str:
        """Return the per-channel memory workspace path.

        When channel_id is provided the memory sub-workspace lives at:
            {workspace_root}/channels/{channel_id}/
        Character background files (AGENT.md / USER.md / RULE.md) stay in
        workspace_root and are never moved into a channel sub-directory.
        """
        if not channel_id:
            return workspace_root
        return os.path.join(workspace_root, "channels", channel_id)

    @staticmethod
    def _override_channel_memory(context_files: list, memory_workspace: str) -> list:
        """Replace the shared MEMORY.md entry with the channel-specific one.

        Keeps AGENT.md / USER.md / RULE.md from the shared workspace untouched.
        """
        from agent.prompt.workspace import _truncate_memory_content, _is_template_placeholder
        from agent.prompt.builder import ContextFile

        channel_memory_path = os.path.join(memory_workspace, "MEMORY.md")
        # Drop the shared MEMORY.md (may or may not exist in context_files)
        filtered = [f for f in context_files if f.path != "MEMORY.md"]

        if os.path.exists(channel_memory_path):
            try:
                with open(channel_memory_path, "r", encoding="utf-8") as fh:
                    content = fh.read().strip()
                if content and not _is_template_placeholder(content):
                    filtered.append(ContextFile(
                        path="MEMORY.md",
                        content=_truncate_memory_content(content),
                    ))
            except Exception as e:
                logger.warning(f"[AgentInitializer] Failed to load channel MEMORY.md: {e}")

        return filtered

    @staticmethod
    def _active_persona() -> str:
        """Read the active persona name from config (empty = no persona)."""
        try:
            from config import conf
            return (conf().get("active_persona") or "").strip()
        except Exception:
            return ""

    def _warn_missing_persona(self, persona: str, persona_root: str) -> None:
        """Warn once per persona when active_persona points at a dir without
        AGENT.md — usually a typo in config.json. The agent will fall back to the
        shared root AGENT.md with a blank persona memory, which is easy to miss."""
        warned = getattr(self, "_persona_warned", None)
        if warned is None:
            warned = set()
            self._persona_warned = warned
        if persona in warned:
            return
        warned.add(persona)
        logger.warning(
            f"[AgentInitializer] active_persona='{persona}' but {persona_root}\\AGENT.md "
            f"not found. Falling back to the shared root AGENT.md with blank persona memory. "
            f"Check the active_persona spelling in config.json, or create "
            f"personas/{persona}/AGENT.md."
        )

    @staticmethod
    def _persona_root(workspace_root: str, persona: str) -> str:
        """Return the persona profile directory.

        A persona bundles its own character files (AGENT.md / USER.md) plus its
        own memory (MEMORY.md, daily diaries, vector index, conversation history)
        under {workspace_root}/personas/{persona}/. RULE.md and other shared
        workspace files stay in workspace_root.
        """
        if not persona:
            return workspace_root
        return os.path.join(workspace_root, "personas", persona)

    @staticmethod
    def _override_persona_files(context_files: list, persona_root: str) -> list:
        """Swap character + memory files for the active persona's versions.

        - AGENT.md / USER.md: prefer the persona's copy; fall back to the shared
          workspace version only as a safety net (a well-formed persona always
          provides both).
        - MEMORY.md: persona-only. The shared root MEMORY.md is NEVER inherited,
          so a brand-new persona starts with a blank long-term memory instead of
          leaking another persona's memory.

        Order is preserved. RULE.md / BOOTSTRAP.md are persona-agnostic and kept
        from the shared workspace untouched.
        """
        from agent.prompt.workspace import _truncate_memory_content, _is_template_placeholder
        from agent.prompt.builder import ContextFile

        char_files = ("AGENT.md", "USER.md")

        def load_persona(fname):
            p = os.path.join(persona_root, fname)
            if not os.path.exists(p):
                return None
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    content = fh.read().strip()
            except Exception as e:
                logger.warning(f"[AgentInitializer] Failed to load persona {fname}: {e}")
                return None
            if not content or _is_template_placeholder(content):
                return None
            if fname == "MEMORY.md":
                content = _truncate_memory_content(content)
            return ContextFile(path=fname, content=content)

        result = []
        seen = set()
        for cf in context_files:
            if cf.path in char_files:
                pv = load_persona(cf.path)
                result.append(pv if pv else cf)
                seen.add(cf.path)
            elif cf.path == "MEMORY.md":
                # Drop the shared root MEMORY.md; persona's own (if any) added below.
                seen.add("MEMORY.md")
            else:
                result.append(cf)
        # Persona may provide AGENT/USER the shared workspace lacks
        for fname in char_files:
            if fname not in seen:
                pv = load_persona(fname)
                if pv:
                    result.append(pv)
        # Persona-specific MEMORY.md only — never the shared root one
        mem = load_persona("MEMORY.md")
        if mem:
            result.append(mem)
        return result

    def initialize_agent(self, session_id: Optional[str] = None, channel_id: Optional[str] = None) -> Agent:
        """
        Initialize agent for a session

        Args:
            session_id: Session ID (None for default agent)
            channel_id: Channel identifier (e.g. "telegram", "weixin"). When set,
                        memory files (MEMORY.md, daily diaries, index.db) are isolated
                        under ~/cow/channels/{channel_id}/. Character background files
                        (AGENT.md, USER.md, RULE.md) remain shared at ~/cow/.

        Returns:
            Initialized agent instance
        """
        from config import conf

        # Get workspace from config
        workspace_root = expand_path(conf().get("agent_workspace", "~/cow"))

        # Active persona takes precedence: when set, both character files
        # (AGENT.md / USER.md) and ALL memory live under personas/{persona}/.
        # When unset, fall back to the per-channel memory isolation.
        persona = self._active_persona()
        if persona:
            memory_workspace = self._persona_root(workspace_root, persona)
            os.makedirs(memory_workspace, exist_ok=True)
        else:
            # Channel memory workspace: ~/cow/channels/{channel_id}/ when set
            memory_workspace = self._channel_workspace(workspace_root, channel_id)
            if channel_id and memory_workspace != workspace_root:
                os.makedirs(memory_workspace, exist_ok=True)

        # Agent home: when a persona is active, the file-tool cwd and the prompt's
        # workspace view follow the persona dir, so the agent's own edits to
        # AGENT.md / USER.md / MEMORY.md land where they are actually loaded from
        # (instead of the shared root, where they'd be silently overridden).
        # ensure_workspace / skills / config stay shared at workspace_root.
        agent_home = memory_workspace if persona else workspace_root
        if persona and not os.path.exists(os.path.join(memory_workspace, "AGENT.md")):
            self._warn_missing_persona(persona, memory_workspace)

        # Migrate API keys
        self._migrate_config_to_env(workspace_root)

        # Load environment variables
        self._load_env_file()

        # Initialize workspace
        from agent.prompt import ensure_workspace, load_context_files, PromptBuilder
        workspace_files = ensure_workspace(workspace_root, create_templates=True)

        if session_id is None:
            logger.info(f"[AgentInitializer] Workspace initialized at: {workspace_root}"
                        + (f" (channel memory: {memory_workspace})" if channel_id else ""))

        # Setup memory system (uses channel-specific workspace when channel_id is set)
        memory_manager, memory_tools = self._setup_memory_system(workspace_root, session_id,
                                                                  memory_workspace=memory_workspace)

        # Load tools
        tools = self._load_tools(agent_home, memory_manager, memory_tools, session_id)

        # Initialize scheduler if needed
        self._initialize_scheduler(tools, session_id)

        # Load shared context files (AGENT.md / USER.md / RULE.md from workspace_root),
        # then override the character + memory files for the active scope:
        #   - persona active  -> AGENT.md / USER.md / MEMORY.md from personas/{persona}/
        #   - else channel set -> MEMORY.md from channels/{channel}/
        context_files = load_context_files(workspace_root)
        if persona:
            context_files = self._override_persona_files(context_files, memory_workspace)
        elif channel_id and memory_workspace != workspace_root:
            context_files = self._override_channel_memory(context_files, memory_workspace)
        
        # Initialize skill manager
        skill_manager = self._initialize_skill_manager(workspace_root, session_id)
        
        # Build system prompt
        prompt_builder = PromptBuilder(workspace_dir=agent_home, language="zh")
        runtime_info = self._get_runtime_info(workspace_root)
        
        system_prompt = prompt_builder.build(
            tools=tools,
            context_files=context_files,
            skill_manager=skill_manager,
            memory_manager=memory_manager,
            runtime_info=runtime_info,
        )
        
        # Get cost control parameters
        from config import conf
        max_steps = conf().get("agent_max_steps", 20)
        max_context_tokens = conf().get("agent_max_context_tokens", 50000)
        
        # Create agent
        agent = self.agent_bridge.create_agent(
            system_prompt=system_prompt,
            tools=tools,
            max_steps=max_steps,
            output_mode="logger",
            workspace_dir=agent_home,
            skill_manager=skill_manager,
            enable_skills=True,
            max_context_tokens=max_context_tokens,
            runtime_info=runtime_info  # Pass runtime_info for dynamic time updates
        )
        
        # Attach a context-files provider so per-turn prompt rebuilds keep the
        # shared RULE.md (root) AND the active scope's AGENT/USER/MEMORY overrides.
        # Without this, get_full_system_prompt would load straight from agent_home
        # (the persona dir) and silently drop RULE.md, which lives at the root.
        def _context_files_provider():
            cf = load_context_files(workspace_root)
            if persona:
                return self._override_persona_files(cf, memory_workspace)
            elif channel_id and memory_workspace != workspace_root:
                return self._override_channel_memory(cf, memory_workspace)
            return cf
        agent.context_files_provider = _context_files_provider

        # Attach memory manager and share LLM model for summarization
        if memory_manager:
            agent.memory_manager = memory_manager
            if hasattr(agent, 'model') and agent.model:
                memory_manager.flush_manager.llm_model = agent.model

        # Restore persisted conversation history for this session
        if session_id:
            self._restore_conversation_history(agent, session_id)

        # Catch up a daily flush that was missed while the process was down
        # (e.g. machine off at 23:5x). Runs at most once per process.
        self._maybe_catchup_flush(agent)

        # Start daily memory flush timer (once, on first agent init regardless of session)
        self._start_daily_flush_timer()

        return agent

    def _restore_conversation_history(self, agent, session_id: str) -> None:
        """
        Load persisted conversation messages from SQLite and inject them
        into the agent's in-memory message list.

        Only user text and assistant text are restored. Tool call chains
        (tool_use / tool_result) are stripped out because:
        1. They are intermediate process, the value is already in the final
           assistant text reply.
        2. They consume massive context tokens (often 80%+ of history).
        3. Different models have incompatible tool message formats, so
           restoring tool chains across model switches causes 400 errors.
        4. Eliminates the entire class of tool_use/tool_result pairing bugs.
        """
        from config import conf
        if not conf().get("conversation_persistence", True):
            return

        try:
            from agent.memory import get_conversation_store
            store = get_conversation_store()
            max_turns = conf().get("agent_max_context_turns", 20)
            # Scheduler tasks run on a stable isolated session per task and
            # can fire many times a day; a smaller restore window keeps prompt
            # cost bounded while still letting the agent see "last few" runs
            # for trend / dedup style logic. Regular chat sessions keep the
            # original heuristic so user dialogues feel continuous.
            if session_id.startswith("scheduler_"):
                restore_turns = max(1, max_turns // 5)
            else:
                restore_turns = max(3, max_turns // 6)
            saved = store.load_messages(session_id, max_turns=restore_turns)
            if saved:
                filtered = self._filter_text_only_messages(saved)
                if filtered:
                    with agent.messages_lock:
                        agent.messages = filtered
                    logger.debug(
                        f"[AgentInitializer] Restored {len(filtered)} text messages "
                        f"(from {len(saved)} total, {restore_turns} turns cap) "
                        f"for session={session_id}"
                    )
        except Exception as e:
            logger.warning(
                f"[AgentInitializer] Failed to restore conversation history for "
                f"session={session_id}: {e}"
            )

    @staticmethod
    def _filter_text_only_messages(messages: list) -> list:
        """
        Extract clean user/assistant turn pairs from raw message history.

        Groups messages into turns (each starting with a real user query),
        then keeps only:
        - The first user text in each turn (the actual user input)
        - The last assistant text in each turn (the final answer)

        All tool_use, tool_result, intermediate assistant thoughts, and
        internal hint messages injected by the agent loop are discarded.
        """

        def _extract_text(content) -> str:
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                parts = [
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                return "\n".join(p for p in parts if p).strip()
            return ""

        def _is_real_user_msg(msg: dict) -> bool:
            """True for actual user input, False for tool_result or internal hints."""
            if msg.get("role") != "user":
                return False
            content = msg.get("content")
            if isinstance(content, list):
                has_tool_result = any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content
                )
                if has_tool_result:
                    return False
            text = _extract_text(content)
            return bool(text)

        # Group into turns: each turn starts with a real user message
        turns = []
        current_turn = None
        for msg in messages:
            if _is_real_user_msg(msg):
                if current_turn is not None:
                    turns.append(current_turn)
                current_turn = {"user": msg, "assistants": []}
            elif current_turn is not None and msg.get("role") == "assistant":
                text = _extract_text(msg.get("content"))
                if text:
                    current_turn["assistants"].append(text)
        if current_turn is not None:
            turns.append(current_turn)

        # Build result: one user msg + one assistant msg per turn
        filtered = []
        for turn in turns:
            user_text = _extract_text(turn["user"].get("content"))
            if not user_text:
                continue
            filtered.append({
                "role": "user",
                "content": [{"type": "text", "text": user_text}]
            })
            if turn["assistants"]:
                final_reply = turn["assistants"][-1]
                filtered.append({
                    "role": "assistant",
                    "content": [{"type": "text", "text": final_reply}]
                })

        return filtered
    
    def _load_env_file(self):
        """Load environment variables from .env file"""
        env_file = expand_path("~/.cow/.env")
        if os.path.exists(env_file):
            try:
                from dotenv import load_dotenv
                load_dotenv(env_file, override=True)
            except ImportError:
                logger.warning("[AgentInitializer] python-dotenv not installed")
            except Exception as e:
                logger.warning(f"[AgentInitializer] Failed to load .env file: {e}")
    
    def _setup_memory_system(self, workspace_root: str, session_id: Optional[str] = None,
                             memory_workspace: Optional[str] = None):
        """
        Setup memory system

        Args:
            workspace_root: Shared workspace root (~/cow)
            session_id: Session ID for conversation history
            memory_workspace: Optional channel-specific workspace for memory isolation.
                              When provided, MEMORY.md / daily diaries / index.db are
                              stored here instead of workspace_root.

        Returns:
            (memory_manager, memory_tools) tuple
        """
        memory_manager = None
        memory_tools = []

        try:
            from agent.memory import MemoryManager, MemoryConfig
            from agent.tools import MemorySearchTool, MemoryGetTool
            from config import conf

            # Use channel-specific workspace for all memory files when set,
            # otherwise fall back to the shared workspace root.
            effective_memory_root = memory_workspace if memory_workspace else workspace_root
            memory_config = MemoryConfig(workspace_root=effective_memory_root)

            embedding_provider = self._init_embedding_provider(
                memory_config, session_id=session_id
            )

            memory_manager = MemoryManager(memory_config, embedding_provider=embedding_provider)
            self._sync_memory(memory_manager, session_id)

            memory_tools = [
                MemorySearchTool(memory_manager),
                MemoryGetTool(memory_manager)
            ]
            
            if session_id is None:
                logger.info("[AgentInitializer] Memory system initialized")
        
        except Exception as e:
            logger.warning(f"[AgentInitializer] Memory system not available: {e}")
        
        return memory_manager, memory_tools

    def _init_embedding_provider(self, memory_config, session_id: Optional[str] = None):
        """
        Initialize the embedding provider for memory.

        Two paths:
          A. Default (no `embedding_provider` in config.json):
             Auto-init OpenAI -> LinkAI fallback. Existing 1536-dim indices
             keep working.
          B. Explicit (`embedding_provider` is set):
             Initialize the requested vendor with unified dim (default 1024).
             If the index was built with a different dim, vector search will
             quietly return no results (cosine returns 0) and keyword search
             takes over until the user runs /memory rebuild-index.
        """
        from agent.memory import create_embedding_provider
        from config import conf

        explicit_provider = (conf().get("embedding_provider") or "").strip().lower()

        if not explicit_provider:
            return self._init_embedding_provider_legacy(session_id=session_id)

        return self._init_embedding_provider_explicit(
            memory_config, explicit_provider, session_id=session_id,
        )

    def _init_embedding_provider_legacy(self, session_id: Optional[str] = None):
        """Legacy auto-init path: OpenAI -> LinkAI. Preserved verbatim for compat."""
        from agent.memory import create_embedding_provider
        from config import conf

        embedding_provider = None
        embedding_model = None

        openai_api_key = conf().get("open_ai_api_key", "")
        openai_api_base = conf().get("open_ai_api_base", "")
        if openai_api_key and openai_api_key not in ["", "YOUR API KEY", "YOUR_API_KEY"]:
            try:
                model = "text-embedding-3-small"
                embedding_provider = create_embedding_provider(
                    provider="openai",
                    model=model,
                    api_key=openai_api_key,
                    api_base=openai_api_base or "https://api.openai.com/v1"
                )
                embedding_model = f"openai/{model}"
            except Exception as e:
                logger.warning(f"[AgentInitializer] OpenAI embedding failed: {e}")

        if embedding_provider is None:
            linkai_api_key = conf().get("linkai_api_key", "") or os.environ.get("LINKAI_API_KEY", "")
            linkai_api_base = conf().get("linkai_api_base", "https://api.link-ai.tech")
            if linkai_api_key and linkai_api_key not in ["", "YOUR API KEY", "YOUR_API_KEY"]:
                try:
                    model = "text-embedding-3-small"
                    embedding_provider = create_embedding_provider(
                        provider="linkai",
                        model=model,
                        api_key=linkai_api_key,
                        api_base=f"{linkai_api_base}/v1"
                    )
                    embedding_model = f"linkai/{model}"
                except Exception as e:
                    logger.warning(f"[AgentInitializer] LinkAI embedding failed: {e}")

        if embedding_provider is not None and embedding_model:
            global _embedding_logged
            if not _embedding_logged:
                logger.info(
                    f"[AgentInitializer] Embedding model in use: {embedding_model} "
                    f"(dim={embedding_provider.dimensions})"
                )
                _embedding_logged = True

        return embedding_provider

    def _init_embedding_provider_explicit(
        self,
        memory_config,
        provider_key: str,
        session_id: Optional[str] = None,
    ):
        """Explicit-provider path: build the configured vendor.

        If the index was built with a different dim, vector search will
        silently return no results (cosine returns 0 for mismatched dims)
        and keyword search takes over. Users switch vendors by running
        /memory rebuild-index — see docs.
        """
        from agent.memory import create_embedding_provider
        from agent.memory.embedding import EMBEDDING_VENDORS
        from config import conf

        meta = EMBEDDING_VENDORS.get(provider_key)
        if meta is None:
            logger.error(
                f"[AgentInitializer] Unknown embedding_provider '{provider_key}'. "
                f"Supported: {sorted(EMBEDDING_VENDORS.keys())}. "
                f"Memory will run in keyword-only mode."
            )
            return None

        api_key = self._resolve_embedding_api_key(provider_key)
        api_base = self._resolve_embedding_api_base(provider_key, meta["default_base_url"])

        if not api_key:
            logger.error(
                f"[AgentInitializer] embedding_provider='{provider_key}' is set but its "
                f"API key is missing. Memory will run in keyword-only mode."
            )
            return None

        model = (conf().get("embedding_model") or "").strip() or meta["default_model"]
        try:
            cfg_dim = int(conf().get("embedding_dimensions") or 0)
        except (TypeError, ValueError):
            cfg_dim = 0
        dim = cfg_dim if cfg_dim > 0 else meta["default_dimensions"]

        try:
            provider = create_embedding_provider(
                provider=provider_key,
                model=model,
                api_key=api_key,
                api_base=api_base,
                dimensions=dim,
            )
        except Exception as e:
            logger.error(
                f"[AgentInitializer] Failed to init embedding provider "
                f"'{provider_key}/{model}': {e}"
            )
            return None

        global _embedding_logged
        if not _embedding_logged:
            logger.info(
                f"[AgentInitializer] Embedding model in use: "
                f"{provider_key}/{model} (dim={provider.dimensions})"
            )
            _embedding_logged = True
        return provider

    @staticmethod
    def _resolve_embedding_api_key(provider_key: str) -> str:
        """Pick the API key for an explicit embedding provider from config."""
        from config import conf

        key_map = {
            "openai":    "open_ai_api_key",
            "linkai":    "linkai_api_key",
            "dashscope": "dashscope_api_key",
            "doubao":    "ark_api_key",
            "zhipu":     "zhipu_ai_api_key",
        }
        field = key_map.get(provider_key)
        if not field:
            return ""
        value = conf().get(field, "") or ""
        if value in ["", "YOUR API KEY", "YOUR_API_KEY"]:
            return ""
        return value

    @staticmethod
    def _resolve_embedding_api_base(provider_key: str, default_base: str) -> str:
        """Pick the API base for an explicit embedding provider from config."""
        from config import conf

        base_map = {
            "openai":    "open_ai_api_base",
            "linkai":    "linkai_api_base",
            "doubao":    "ark_base_url",
            "zhipu":     "zhipu_ai_api_base",
        }
        field = base_map.get(provider_key)
        if not field:
            return default_base
        value = (conf().get(field) or "").strip()
        if not value:
            return default_base
        if provider_key == "linkai" and not value.rstrip("/").endswith("/v1"):
            return f"{value.rstrip('/')}/v1"
        return value
    
    def _sync_memory(self, memory_manager, session_id: Optional[str] = None):
        """Sync memory database"""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                raise RuntimeError("Event loop is closed")
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        try:
            if loop.is_running():
                asyncio.create_task(memory_manager.sync())
            else:
                loop.run_until_complete(memory_manager.sync())
        except Exception as e:
            logger.warning(f"[AgentInitializer] Memory sync failed: {e}")
    
    def _load_tools(self, workspace_root: str, memory_manager, memory_tools: List, session_id: Optional[str] = None):
        """Load all tools"""
        tool_manager = ToolManager()
        tool_manager.load_tools()
        
        tools = []
        file_config = {
            "cwd": workspace_root,
            "memory_manager": memory_manager
        } if memory_manager else {"cwd": workspace_root}
        
        for tool_name in tool_manager.tool_classes.keys():
            try:
                # Skip web_search if no API key is available
                if tool_name == "web_search":
                    from agent.tools.web_search.web_search import WebSearch
                    if not WebSearch.is_available():
                        logger.debug("[AgentInitializer] WebSearch skipped - no search provider configured")
                        continue

                # Skip evolution_undo when self-evolution is disabled: with no
                # evolution there is nothing to roll back, so the tool is dead weight.
                if tool_name == "evolution_undo":
                    from agent.evolution.config import get_evolution_config
                    if not get_evolution_config().enabled:
                        logger.debug("[AgentInitializer] evolution_undo skipped - self-evolution disabled")
                        continue

                # Special handling for EnvConfig tool
                if tool_name == "env_config":
                    from agent.tools import EnvConfig
                    tool = EnvConfig({"agent_bridge": self.agent_bridge})
                else:
                    tool = tool_manager.create_tool(tool_name)

                if tool:
                    # Apply workspace config to file operation tools.
                    # Merge into the existing tool.config (set by ToolManager from
                    # config.json's `tools.<name>` section) instead of replacing
                    # it, otherwise per-tool user configs (e.g. browser.cdp_endpoint)
                    # would be silently dropped.
                    if tool_name in ['read', 'write', 'edit', 'bash', 'grep', 'find', 'ls', 'web_fetch', 'send', 'browser']:
                        merged_config = dict(getattr(tool, 'config', None) or {})
                        merged_config.update(file_config)
                        tool.config = merged_config
                        tool.cwd = merged_config.get("cwd", getattr(tool, 'cwd', None))
                        if 'memory_manager' in merged_config:
                            tool.memory_manager = merged_config['memory_manager']
                    tools.append(tool)
            except Exception as e:
                logger.warning(f"[AgentInitializer] Failed to load tool {tool_name}: {e}")

        # Add MCP tools (snapshot to avoid races with the background loader)
        mcp_tools_snapshot = list(tool_manager._mcp_tool_instances.items())
        if mcp_tools_snapshot:
            for _, mcp_tool in mcp_tools_snapshot:
                tools.append(mcp_tool)
            if session_id is None:
                names = [name for name, _ in mcp_tools_snapshot]
                logger.info(
                    f"[AgentInitializer] Added {len(names)} MCP tool(s): {names}"
                )

        # Add memory tools
        if memory_tools:
            tools.extend(memory_tools)
            if session_id is None:
                logger.info(f"[AgentInitializer] Added {len(memory_tools)} memory tools")
        
        if session_id is None:
            logger.info(f"[AgentInitializer] Loaded {len(tools)} tools: {[t.name for t in tools]}")
        
        return tools
    
    def _initialize_scheduler(self, tools: List, session_id: Optional[str] = None):
        """Initialize scheduler service if needed.

        Serialize the check-and-set under a module-level lock so concurrent
        first-time session inits cannot each create a new SchedulerService
        (which would leak background scanning threads).
        """
        if not self.agent_bridge.scheduler_initialized:
            with _scheduler_init_lock:
                if not self.agent_bridge.scheduler_initialized:
                    try:
                        from agent.tools.scheduler.integration import init_scheduler
                        if init_scheduler(self.agent_bridge):
                            self.agent_bridge.scheduler_initialized = True
                            if session_id is None:
                                logger.info("[AgentInitializer] Scheduler service initialized")
                    except Exception as e:
                        logger.warning(f"[AgentInitializer] Failed to initialize scheduler: {e}")
        
        # Inject scheduler dependencies
        if self.agent_bridge.scheduler_initialized:
            try:
                from agent.tools.scheduler.integration import get_task_store, get_scheduler_service
                from agent.tools import SchedulerTool
                from config import conf
                
                task_store = get_task_store()
                scheduler_service = get_scheduler_service()
                
                for tool in tools:
                    if isinstance(tool, SchedulerTool):
                        tool.task_store = task_store
                        tool.scheduler_service = scheduler_service
                        if not tool.config:
                            tool.config = {}
                        raw_ct = conf().get("channel_type", "unknown")
                        if isinstance(raw_ct, list):
                            ct = raw_ct[0] if raw_ct else "unknown"
                        elif isinstance(raw_ct, str) and "," in raw_ct:
                            ct = raw_ct.split(",")[0].strip()
                        else:
                            ct = raw_ct
                        tool.config["channel_type"] = ct
            except Exception as e:
                logger.warning(f"[AgentInitializer] Failed to inject scheduler dependencies: {e}")
    
    def _initialize_skill_manager(self, workspace_root: str, session_id: Optional[str] = None):
        """Initialize skill manager"""
        try:
            from agent.skills import SkillManager
            skill_manager = SkillManager(custom_dir=os.path.join(workspace_root, "skills"))
            return skill_manager
        except Exception as e:
            logger.warning(f"[AgentInitializer] Failed to initialize SkillManager: {e}")
            return None
    
    def _get_runtime_info(self, workspace_root: str):
        """Get runtime information with dynamic time support"""
        from config import conf
        
        def get_current_time():
            """Get current time dynamically - called each time system prompt is accessed"""
            now = datetime.datetime.now()
            
            # Get timezone info
            try:
                offset = -time.timezone if not time.daylight else -time.altzone
                hours = offset // 3600
                minutes = (offset % 3600) // 60
                timezone_name = f"UTC{hours:+03d}:{minutes:02d}" if minutes else f"UTC{hours:+03d}"
            except Exception:
                timezone_name = "UTC"
            
            # Weekday: English name in en, Chinese mapping otherwise
            weekday_en = now.strftime("%A")
            try:
                from common import i18n
                is_en = i18n.get_language() == "en"
            except Exception:
                is_en = False
            if is_en:
                weekday = weekday_en
            else:
                weekday_map = {
                    'Monday': '星期一', 'Tuesday': '星期二', 'Wednesday': '星期三',
                    'Thursday': '星期四', 'Friday': '星期五', 'Saturday': '星期六', 'Sunday': '星期日'
                }
                weekday = weekday_map.get(weekday_en, weekday_en)

            return {
                'time': now.strftime("%Y-%m-%d %H:%M:%S"),
                'weekday': weekday,
                'timezone': timezone_name
            }
        
        def get_model():
            """Get current model name dynamically from config"""
            return conf().get("model", "unknown")

        return {
            "_get_model": get_model,
            "workspace": workspace_root,
            "channel": ", ".join(conf().get("channel_type")) if isinstance(conf().get("channel_type"), list) else conf().get("channel_type", "unknown"),
            "_get_current_time": get_current_time  # Dynamic time function
        }
    
    def _migrate_config_to_env(self, workspace_root: str):
        """Migrate API keys from config.json to .env file"""
        from config import conf
        
        key_mapping = {
            "open_ai_api_key": "OPENAI_API_KEY",
            "open_ai_api_base": "OPENAI_API_BASE",
            "gemini_api_key": "GEMINI_API_KEY",
            "claude_api_key": "CLAUDE_API_KEY",
            "linkai_api_key": "LINKAI_API_KEY",
        }
        
        env_file = expand_path("~/.cow/.env")
        
        # Read existing env vars (key -> value)
        existing_env_vars = {}
        if os.path.exists(env_file):
            try:
                with open(env_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            key, val = line.split('=', 1)
                            existing_env_vars[key.strip()] = val.strip()
            except Exception as e:
                logger.warning(f"[AgentInitializer] Failed to read .env file: {e}")
        
        # Sync config.json values into .env (add/update/remove)
        updated = False
        for config_key, env_key in key_mapping.items():
            raw = conf().get(config_key, "")
            value = raw.strip() if raw else ""
            old_value = existing_env_vars.get(env_key)

            if value:
                if old_value == value:
                    continue
                existing_env_vars[env_key] = value
                os.environ[env_key] = value
                updated = True
            else:
                if old_value is None:
                    continue
                existing_env_vars.pop(env_key, None)
                os.environ.pop(env_key, None)
                updated = True

        if updated:
            try:
                env_dir = os.path.dirname(env_file)
                os.makedirs(env_dir, exist_ok=True)

                # Rewrite the entire .env file to ensure consistency
                with open(env_file, 'w', encoding='utf-8') as f:
                    f.write('# Environment variables for agent\n')
                    f.write('# Auto-managed - synced from config.json on startup\n\n')
                    for key, value in sorted(existing_env_vars.items()):
                        f.write(f'{key}={value}\n')

                logger.info(f"[AgentInitializer] Synced API keys from config.json to .env")
            except Exception as e:
                logger.warning(f"[AgentInitializer] Failed to sync API keys: {e}")

    def _start_daily_flush_timer(self):
        """Start a background thread that flushes all agents' memory daily at 23:55."""
        if getattr(self.agent_bridge, '_daily_flush_started', False):
            return
        self.agent_bridge._daily_flush_started = True

        import threading

        def _daily_flush_loop():
            import random
            last_run_date = None  # Track last successful run date to prevent same-day re-trigger
            while True:
                try:
                    now = datetime.datetime.now()
                    jitter_min = random.randint(50, 55)
                    jitter_sec = random.randint(0, 59)
                    target = now.replace(hour=23, minute=jitter_min, second=jitter_sec, microsecond=0)
                    # Always schedule for tomorrow if we already ran today, or if target time has passed
                    if target <= now or (last_run_date == now.date()):
                        target += datetime.timedelta(days=1)
                    wait_seconds = (target - now).total_seconds()
                    logger.info(f"[DailyFlush] Next flush at {target.strftime('%Y-%m-%d %H:%M:%S')} (in {wait_seconds/3600:.1f}h)")
                    time.sleep(wait_seconds)

                    self._flush_all_agents()
                    self._record_flush_date()
                    last_run_date = datetime.datetime.now().date()
                except Exception as e:
                    logger.warning(f"[DailyFlush] Error in daily flush loop: {e}")
                    time.sleep(3600)

        t = threading.Thread(target=_daily_flush_loop, daemon=True)
        t.start()

    def _flush_state_file(self):
        """Path of the small JSON marking the last successful daily flush date."""
        from common.utils import expand_path
        from config import conf
        workspace = expand_path(conf().get("agent_workspace", "~/cow"))
        return os.path.join(workspace, "memory", ".daily_flush_state.json")

    def _record_flush_date(self, d=None):
        """Persist the date of the last successful daily flush so a startup
        catch-up can tell whether a scheduled flush was missed."""
        import datetime as _dt
        import json as _json
        try:
            d = d or _dt.date.today()
            path = self._flush_state_file()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                _json.dump({"last_flush_date": d.isoformat()}, f)
        except Exception as e:
            logger.warning(f"[DailyFlush] Failed to record flush date: {e}")

    def _maybe_catchup_flush(self, agent):
        """Run a one-shot daily flush at startup when the previous day's
        scheduled flush was missed (process was down at 23:5x).

        Reuses the freshly-built agent's flush_manager (LLM already attached on
        line ~122) and the conversation just restored from the store, so a day's
        memory isn't silently lost when the machine is off overnight. Runs at
        most once per process and only when a flush was actually missed.
        """
        try:
            if getattr(self.agent_bridge, "_catchup_flush_done", False):
                return
            if not (agent and getattr(agent, "memory_manager", None)):
                return

            import datetime as _dt
            import json as _json
            import threading as _th

            today = _dt.date.today()
            last_flush = None
            try:
                with open(self._flush_state_file(), "r", encoding="utf-8") as f:
                    raw = _json.load(f).get("last_flush_date", "")
                last_flush = _dt.date.fromisoformat(raw) if raw else None
            except Exception:
                last_flush = None

            # Nothing missed if we already flushed yesterday or later.
            if last_flush is not None and last_flush >= today - _dt.timedelta(days=1):
                self.agent_bridge._catchup_flush_done = True
                return

            # Attempt at most once per process, regardless of outcome.
            self.agent_bridge._catchup_flush_done = True

            with agent.messages_lock:
                messages = list(agent.messages)
            if not messages:
                return

            def _worker():
                try:
                    logger.info(
                        f"[DailyFlush] Missed flush detected (last={last_flush}); "
                        f"running startup catch-up over {len(messages)} messages"
                    )
                    fm = agent.memory_manager.flush_manager
                    if fm.create_daily_summary(messages):
                        ft = fm._last_flush_thread
                        if ft:
                            ft.join(timeout=60)
                    try:
                        fm.deep_dream()
                    except Exception as e:
                        logger.warning(f"[DailyFlush] catch-up deep_dream failed: {e}")
                    # Mark the missed day (yesterday) as handled; tonight's
                    # scheduled flush still runs and records today.
                    self._record_flush_date(today - _dt.timedelta(days=1))
                    logger.info("[DailyFlush] Startup catch-up complete")
                except Exception as e:
                    logger.warning(f"[DailyFlush] Startup catch-up failed: {e}")

            _th.Thread(target=_worker, daemon=True, name="daily-flush-catchup").start()
        except Exception as e:
            logger.warning(f"[DailyFlush] catch-up check failed: {e}")

    def _flush_all_agents(self):
        """Flush memory for all active agent sessions, then run Deep Dream."""
        agents = []
        if self.agent_bridge.default_agent:
            agents.append(("default", self.agent_bridge.default_agent))
        for sid, agent in self.agent_bridge.agents.items():
            agents.append((sid, agent))

        if not agents:
            return

        # Phase 1: flush daily summaries
        flushed = 0
        flush_threads = []
        dream_candidate = None
        for label, agent in agents:
            try:
                if not agent.memory_manager:
                    continue
                with agent.messages_lock:
                    messages = list(agent.messages)
                if not messages:
                    continue
                result = agent.memory_manager.flush_manager.create_daily_summary(messages)
                if result:
                    flushed += 1
                    t = agent.memory_manager.flush_manager._last_flush_thread
                    if t:
                        flush_threads.append(t)
                if dream_candidate is None:
                    dream_candidate = agent.memory_manager.flush_manager
            except Exception as e:
                logger.warning(f"[DailyFlush] Failed for session {label}: {e}")

        if flushed:
            logger.info(f"[DailyFlush] Flushed {flushed}/{len(agents)} agent session(s)")

        # Wait for all flush threads to finish before dreaming
        for t in flush_threads:
            t.join(timeout=60)

        # Phase 2: Deep Dream — distill daily memories → MEMORY.md + dream diary
        if dream_candidate:
            try:
                result = dream_candidate.deep_dream()
                if result:
                    logger.info("[DeepDream] Memory distillation completed successfully")
            except Exception as e:
                logger.warning(f"[DeepDream] Failed: {e}")
