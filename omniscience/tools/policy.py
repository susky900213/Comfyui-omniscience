"""Tool policy (DESIGN.md section 6): one declarative layer replaces if-else.

The interactive approval chain is deliberately not revived (README: Act mode
executes directly); the safety boundary is the workspace sandbox plus this
policy.  Plan mode keeps read-only investigation tools *and* run_command for
inspection, while command-guard hard-blocks mutations -- matching Omniscience's
behavior rather than the old PLAN_TOOL_NAMES hiding strategy.
"""

from __future__ import annotations

from typing import Any

from . import guards

# Read-only tools that keep their exact names from the historical whitelist.
PLAN_SAFE_TOOLS = {
    "list_files",
    "read_file",
    "search_files",
    "get_current_workflow",
    "browser_navigate",
    "browser_content",
}
# run_command stays exposed in plan mode but is inspected by the guard below.
PLAN_GUARDED_TOOLS = {"run_command"}

WRITE_TOOL_NAMES = {"write_file", "apply_patch", "editor"}


def policy_error(mode: str, name: str, arguments: dict[str, Any]) -> str | None:
    """Return the refusal message when the tool may not run, else None."""
    if mode != "plan":
        return None
    if name in PLAN_SAFE_TOOLS:
        return None
    if name in PLAN_GUARDED_TOOLS:
        return guards.guard_error(str(arguments.get("command") or ""), "plan")
    if name.startswith("mcp__"):
        return f"工具 {name} 在 PLAN 模式下不可用。MCP 工具可能产生副作用，只能在 ACT 模式调用。"
    return f"工具 {name} 在 PLAN 模式下不可用。"
