"""Shared helpers for the split aiohttp route registrars."""

from __future__ import annotations

from typing import Any

from . import deps

HEALTH_VERSION = "0.3.0"
FEATURES = [
    "tasks", "global_chat", "cancellation", "checkpoints", "mcp", "browser",
    "live_workflow", "upload", "network_switch", "global_workspace", "skill_security",
    "custom_node_projects", "node_security", "node_install", "workflow_node_search",
    "workflow_diagnostics", "workflow_install",
]


def _composer_workspace(request) -> Any:
    """Workspace backing @ mentions: global, a task workspace, or the default."""
    if request.query.get("scope") == "global":
        return deps.global_chat_agent().chat_workspace()
    task_id = request.query.get("task_id")
    return deps.task_agent().workspace(task_id) if task_id else deps.legacy_agent().workspace()


def _response(payload: Any, status: int = 200):
    from aiohttp import web

    return web.json_response(payload, status=status)


async def _body(request) -> dict[str, Any]:
    try:
        value = await request.json()
    except (TypeError, ValueError) as exc:
        raise ValueError("Request body must be valid JSON.") from exc
    if not isinstance(value, dict):
        raise ValueError("Request body must be a JSON object.")
    return value
