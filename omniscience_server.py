"""Compatibility facade for the Comfyui-omniscience backend.

The implementations now live in the ``omniscience`` package (DESIGN.md
section 4).  This module keeps ``from omniscience_server import ...``
working for ComfyUI startup, integrations, and tests: it re-exports the
public classes, prompts, tool definitions, and constants, and registers the
aiohttp routes once through :func:`omniscience.api.routes.register_routes`.

Mutable state (database paths, defaults) lives in ``omniscience.paths``;
patch that module before constructing agents, not the aliases below.
"""

from __future__ import annotations

from typing import Any

try:  # ComfyUI loads custom nodes as packages; direct unittest does not.
    from .omniscience import constants, paths, prompts
    from .omniscience.agent.global_chat import GlobalChatAgent
    from .omniscience.agent.legacy import ClineAgent, PendingAction
    from .omniscience.agent.runtime import PersistentClineAgent
    from .omniscience.api import deps
    from .omniscience.api import routes as _routes
    from .omniscience.checkpointing import CheckpointManager
    from .omniscience.context import ContextManager
    from .omniscience.host import BrowserUnavailable, MCPProtocolError
    from .omniscience.settings import SettingsStore
    from .omniscience.skills import SkillStore
    from .omniscience.storage import TaskStore
    from .omniscience.tools.defs import (
        BROWSER_TOOL_DEFINITIONS,
        LIVE_WORKFLOW_TOOL_DEFINITIONS,
        TOOL_DEFINITIONS,
    )
    from .omniscience.tools.policy import PLAN_SAFE_TOOLS
    from .omniscience.util import (
        ProjectFolderExistsError,
        TaskCancelled,
        project_folder_name as _project_folder_name,
    )
    from .omniscience.workspace import Workspace, apply_unified_patch
except ImportError:
    from omniscience import constants, paths, prompts
    from omniscience.agent.global_chat import GlobalChatAgent
    from omniscience.agent.legacy import ClineAgent, PendingAction
    from omniscience.agent.runtime import PersistentClineAgent
    from omniscience.api import deps
    from omniscience.api import routes as _routes
    from omniscience.checkpointing import CheckpointManager
    from omniscience.context import ContextManager
    from omniscience.host import BrowserUnavailable, MCPProtocolError
    from omniscience.settings import SettingsStore
    from omniscience.skills import SkillStore
    from omniscience.storage import TaskStore
    from omniscience.tools.defs import (
        BROWSER_TOOL_DEFINITIONS,
        LIVE_WORKFLOW_TOOL_DEFINITIONS,
        TOOL_DEFINITIONS,
    )
    from omniscience.tools.policy import PLAN_SAFE_TOOLS
    from omniscience.util import (
        ProjectFolderExistsError,
        TaskCancelled,
        project_folder_name as _project_folder_name,
    )
    from omniscience.workspace import Workspace, apply_unified_patch

PRODUCT_NAME = constants.PRODUCT_NAME
PLUGIN_NAME = constants.PRODUCT_NAME
PLAN_TOOL_NAMES = set(PLAN_SAFE_TOOLS)

# Convenience aliases for callers that still read module-level defaults.
# These mirror omniscience.paths at import time; patch omniscience.paths.
DEFAULT_WORKSPACE = paths.DEFAULT_WORKSPACE
DEFAULT_MODEL = paths.DEFAULT_MODEL
DEFAULT_API_BASE_URL = paths.DEFAULT_API_BASE_URL
DEFAULT_COMFYUI_DIRECTORY = paths.DEFAULT_COMFYUI_DIRECTORY
SYSTEM_PROMPT = prompts.SYSTEM_PROMPT
COMPACTION_SYSTEM_PROMPT = prompts.COMPACTION_SYSTEM_PROMPT
GLOBAL_CHAT_SYSTEM_PROMPT = prompts.GLOBAL_CHAT_SYSTEM_PROMPT


def _agent() -> ClineAgent:
    return deps.legacy_agent()


def _task_agent() -> PersistentClineAgent:
    return deps.task_agent()


def _global_chat_agent() -> GlobalChatAgent:
    return deps.global_chat_agent()


_REGISTERED = False


def register_routes(prompt_server: Any) -> None:
    """Register routes once when ComfyUI imports this custom node."""

    global _REGISTERED
    if _REGISTERED:
        return
    _routes.register_routes(prompt_server)
    _REGISTERED = True


__all__ = [
    "BROWSER_TOOL_DEFINITIONS",
    "CheckpointManager",
    "ClineAgent",
    "ContextManager",
    "GLOBAL_CHAT_SYSTEM_PROMPT",
    "GlobalChatAgent",
    "LIVE_WORKFLOW_TOOL_DEFINITIONS",
    "PersistentClineAgent",
    "PendingAction",
    "ProjectFolderExistsError",
    "SettingsStore",
    "SkillStore",
    "SYSTEM_PROMPT",
    "TaskCancelled",
    "TaskStore",
    "TOOL_DEFINITIONS",
    "Workspace",
    "apply_unified_patch",
    "register_routes",
]
