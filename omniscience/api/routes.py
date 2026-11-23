"""Compatibility facade for aiohttp route registration.

Handlers are grouped by responsibility in the sibling route registrars. This
module keeps the historical import path and registration entry point stable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import types
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
from ..region_router import RegionRouter
from ..util import ProjectFolderExistsError
from ..workflow_diagnostics import static_diagnostic
from ..workflow_executor import WorkflowExecutionError
from . import deps
from .route_common import FEATURES, HEALTH_VERSION, _body, _composer_workspace, _response
from .route_health import register_health_routes
from .route_skills import register_skills_routes
from .route_system import register_system_routes
from .route_tasks import register_tasks_routes
from .route_workflows import register_workflows_routes

logger = logging.getLogger("Comfyui-omniscience")


class _RoutesFacadeModule(types.ModuleType):
    """Forward historical facade patches to the split registrar modules."""

    _registrar_names = (
        "omniscience.api.route_common",
        "omniscience.api.route_health",
        "omniscience.api.route_workflows",
        "omniscience.api.route_tasks",
        "omniscience.api.route_skills",
        "omniscience.api.route_system",
    )

    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        for module_name in self._registrar_names:
            module = sys.modules.get(module_name)
            if module is not None and hasattr(module, name):
                setattr(module, name, value)


def register_routes(prompt_server: Any) -> None:
    """Register all routes in the historical order."""
    routes = prompt_server.routes
    register_health_routes(routes)
    register_workflows_routes(routes)
    register_tasks_routes(routes)
    register_skills_routes(routes)
    register_system_routes(routes)


sys.modules[__name__].__class__ = _RoutesFacadeModule