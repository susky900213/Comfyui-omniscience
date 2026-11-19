"""Unified tool-result envelope (DESIGN.md section 5.6).

Every tool branch returns a content-ized result rather than raising: failures
go back into the transcript so the model can self-correct, and the write of
the tool message and the event keep the assistant/tool pair adjacent.
"""

from __future__ import annotations

from typing import Any

TOOL_KEY = "tool"


def ok(tool: str, output: Any) -> dict[str, Any]:
    return {TOOL_KEY: tool, "success": True, "output": output}


def fail(tool: str, error: Any) -> dict[str, Any]:
    message = error if isinstance(error, str) else str(error)
    return {TOOL_KEY: tool, "success": False, "error": message}


def normalize(tool: str, value: Any) -> dict[str, Any]:
    """Wrap a raw branch return value, preserving explicit failure dicts."""
    if isinstance(value, dict) and set(value.keys()) <= {"error"} and "error" in value:
        return fail(tool, value["error"])
    return ok(tool, value)
