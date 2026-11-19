"""Process-wide agent singletons behind the HTTP surface.

Agents are created lazily so tests (and ComfyUI reloads) that patch
``omniscience.paths`` before the first request get consistent state.
"""

from __future__ import annotations

from typing import Any

from ..settings import SettingsStore

_LEGACY_AGENT: Any = None
_TASK_AGENT: Any = None
_GLOBAL_CHAT_AGENT: Any = None
_CONTROLLER: Any = None


def legacy_agent() -> Any:
    global _LEGACY_AGENT
    if _LEGACY_AGENT is None:
        from ..agent.legacy import ClineAgent

        _LEGACY_AGENT = ClineAgent(SettingsStore())
    return _LEGACY_AGENT


def task_agent() -> Any:
    global _TASK_AGENT, _CONTROLLER
    if _TASK_AGENT is None:
        from ..agent.runtime import PersistentClineAgent
        from .turns import TaskTurnController

        _TASK_AGENT = PersistentClineAgent(SettingsStore())
        _CONTROLLER = TaskTurnController(_TASK_AGENT)
        _TASK_AGENT.followup_provider = _CONTROLLER.drain
    return _TASK_AGENT


def controller() -> Any:
    task_agent()
    return _CONTROLLER


def global_chat_agent() -> Any:
    global _GLOBAL_CHAT_AGENT
    if _GLOBAL_CHAT_AGENT is None:
        from ..agent.global_chat import GlobalChatAgent

        _GLOBAL_CHAT_AGENT = GlobalChatAgent(SettingsStore())
    return _GLOBAL_CHAT_AGENT
