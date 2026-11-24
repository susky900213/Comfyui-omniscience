from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from .. import node_review, node_security, skill_review, skill_security, uploads
from ..agent.node_install import NodeSecurityBlockedError
from ..constants import (
    MAX_CURRENT_WORKFLOW_BYTES,
    MAX_NODE_BATCH_SCANS,
    MAX_NODE_PACKAGE_BYTES,
    PRODUCT_NAME,
    WORKFLOW_FILENAME,
)
from ..host import BrowserUnavailable, MCPProtocolError
from ..util import ProjectFolderExistsError
from ..workflow_diagnostics import static_diagnostic
from ..workflow_executor import WorkflowExecutionError
from . import deps
from .route_common import FEATURES, HEALTH_VERSION, _body, _composer_workspace, _response

def register_system_routes(routes: Any) -> None:
    from aiohttp import web

    @routes.get("/omniscience/settings")
    async def omniscience_settings(request):
        return _response(deps.legacy_agent().settings.public())
    @routes.post("/omniscience/settings")
    async def omniscience_update_settings(request):
        try:
            values = await _body(request)
            return _response(deps.legacy_agent().settings.update(values))
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)
    @routes.get("/omniscience/files")
    async def omniscience_files(request):
        try:
            path = request.query.get("path", ".")
            recursive = request.query.get("recursive", "false").lower() == "true"
            workspace = _composer_workspace(request)
            return _response(
                {
                    "workspace": str(workspace.root),
                    "path": path,
                    "entries": workspace.list_files(path, recursive),
                }
            )
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)
    @routes.get("/omniscience/file")
    async def omniscience_file(request):
        try:
            path = request.query.get("path", "")
            workspace = _composer_workspace(request)
            return _response({"path": path, "content": workspace.read_file(path)})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)
    @routes.post("/omniscience/chat")
    async def omniscience_chat(request):
        try:
            values = await _body(request)
            result = await deps.legacy_agent().chat(values.get("messages", []), str(values.get("context") or ""))
            return _response(result)
        except (ValueError, RuntimeError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)
    @routes.post("/omniscience/confirm")
    async def omniscience_confirm(request):
        try:
            values = await _body(request)
            action_id = str(values.get("id") or "")
            if not action_id:
                raise ValueError("Confirmation id is required.")
            result = await deps.legacy_agent().confirm(action_id, bool(values.get("approved")))
            return _response(result)
        except (ValueError, RuntimeError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)
    @routes.post("/omniscience/approvals/{approval_id}")
    async def omniscience_task_confirm(request):
        try:
            values = await _body(request)
            result = await deps.task_agent().confirm(request.match_info["approval_id"], bool(values.get("approved")))
            return _response(result)
        except (ValueError, RuntimeError, OSError, MCPProtocolError, BrowserUnavailable) as exc:
            return _response({"error": str(exc)}, status=400)
