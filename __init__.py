"""Comfyui-omniscience coding assistant extension for ComfyUI.

The extension deliberately implements the assistant against ComfyUI's HTTP
and frontend extension APIs instead of importing Cline's VS Code extension.
This keeps it installable as a normal custom node and avoids changing the
ComfyUI core application.
"""

import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))

WEB_DIRECTORY = "./web"

try:
    from server import PromptServer

    try:  # ComfyUI's pack loader may run this file without package context.
        from .omniscience_server import register_routes
    except ImportError:
        from omniscience_server import register_routes

    register_routes(PromptServer.instance)
except Exception as exc:  # pragma: no cover - exercised by ComfyUI startup
    # A custom node must not prevent ComfyUI from starting when an optional
    # integration is unavailable. The error remains visible in the server log.
    print(f"[Comfyui-omniscience] Failed to register routes: {exc}")


NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "WEB_DIRECTORY",
]