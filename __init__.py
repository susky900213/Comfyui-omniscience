"""Comfyui-omniscience coding assistant extension for ComfyUI.

The extension deliberately implements the assistant against ComfyUI's HTTP
and frontend extension APIs instead of importing Cline's VS Code extension.
This keeps it installable as a normal custom node and avoids changing the
ComfyUI core application.
"""

WEB_DIRECTORY = "./web"

try:
    from server import PromptServer

    from .omniscience_server import register_routes

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