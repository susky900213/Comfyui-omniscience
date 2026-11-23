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

def register_health_routes(routes: Any) -> None:
    from aiohttp import web

    @routes.get("/cline/health")
    async def cline_health(request):
        return _response({"name": PRODUCT_NAME, "status": "ok", "version": HEALTH_VERSION, "features": FEATURES})
    @routes.get("/cline/tasks")
    async def cline_tasks(request):
        try:
            return _response({"tasks": deps.task_agent().store.list_tasks(int(request.query.get("limit", "100")))})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)
