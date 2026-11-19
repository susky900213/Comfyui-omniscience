"""Import bridge to the node-level sibling modules (browser and MCP drivers).

ComfyUI loads this repository as a package, so the sibling modules live one
level above the ``omniscience`` package; direct unittest runs import them
from the repository root instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:  # ComfyUI package context: siblings resolve one level up.
    from omniscience_browser import BrowserManager, BrowserUnavailable
    from omniscience_mcp import MCPManager, MCPProtocolError
except (ImportError, ValueError):  # direct import inside the loaded node package
    from ...omniscience_browser import BrowserManager, BrowserUnavailable  # type: ignore[attr-defined]
    from ...omniscience_mcp import MCPManager, MCPProtocolError  # type: ignore[attr-defined]

__all__ = [
    "BrowserManager",
    "BrowserUnavailable",
    "MCPManager",
    "MCPProtocolError",
    "browser_manager",
    "mcp_manager",
]


def mcp_manager(settings_path: Path) -> Any:
    return MCPManager(settings_path)


def browser_manager(config: Any = None) -> Any:
    return BrowserManager(config)
