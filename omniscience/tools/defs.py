"""Tool schemas and descriptions (DESIGN Appendix A.4).

Environment-specific constraints live in each tool's own description -- not
only in the system prompt -- and descriptions are re-evaluated per request
through ``build_definitions``.
"""

from __future__ import annotations

from typing import Any


def _function(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


FILE_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    _function(
        "list_files",
        "List files and directories in the task workspace. Paths are relative to the workspace root; symlinked entries that escape the workspace are hidden.",
        {
            "path": {"type": "string", "description": "Workspace-relative directory. Use '.' for the root."},
            "recursive": {"type": "boolean"},
        },
        ["path"],
    ),
    _function(
        "read_file",
        "Read a UTF-8 text file from the workspace (max 1 MB).",
        {"path": {"type": "string", "description": "Workspace-relative file path."}},
        ["path"],
    ),
    _function(
        "search_files",
        "Case-insensitive text search across workspace files, excluding build and dependency directories. Results are capped at 100 line matches.",
        {"query": {"type": "string"}, "path": {"type": "string", "description": "Optional directory or file."}},
        ["query"],
    ),
]

WRITE_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    _function(
        "editor",
        "An editor for controlled text edits on the file at `path`. Provide `insert_line` to insert `new_text` at a line number; otherwise replace `old_text` with `new_text`, or create the file with `new_text` if it does not exist. "
        "Use this tool for small, precise edits over full-file writes or shell commands. If several edits to different files or non-overlapping regions are already known, emit multiple editor calls in the same response instead of serializing them across turns. "
        "old_text must match exactly one location; add surrounding context when unsure. Failed edits do not touch the file.",
        {
            "path": {"type": "string", "description": "Workspace-relative file path."},
            "old_text": {"type": "string", "description": "Exact text to replace. Must occur once."},
            "new_text": {"type": "string", "description": "Replacement or inserted text."},
            "insert_line": {"type": "integer", "minimum": 0, "description": "Line index for insertion instead of replacement."},
        },
        ["path"],
    ),
    _function(
        "write_file",
        "Create or fully replace a UTF-8 text file inside the workspace (max 2 MB). Prefer `editor` or `apply_patch` for changes to existing files.",
        {"path": {"type": "string"}, "content": {"type": "string"}},
        ["path", "content"],
    ),
    _function(
        "apply_patch",
        "Apply a context-based unified diff to one workspace file. Hunk headers use the old file's line numbers; context lines are matched verbatim and a mismatch rejects the whole patch without writing. "
        "The Cline-style wrapper lines (*** Begin Patch / *** Update File: path) are tolerated and stripped.",
        {"path": {"type": "string"}, "patch": {"type": "string", "description": "Unified diff with @@ hunk headers."}},
        ["path", "patch"],
    ),
]


def build_command_definition(mode: str, max_timeout: int = 120) -> dict[str, Any]:
    return _function(
        "run_command",
        (
            "Run a non-interactive shell command from the root of the task workspace using the POSIX shell "
            "(single command per call; chain with '&&' only when the steps must share state). "
            "Commands must be non-interactive: skip pagers and prompts with flags or env (git --no-pager, DEBIAN_FRONTEND=noninteractive, -y). "
            "Output beyond ~20k characters is middle-truncated (start and end preserved); filter output with grep/head/tail when you need specific sections. "
            "State-changing commands (file edits, installs, git mutations) are rejected in plan mode. "
            "Include multiple independent commands as separate tool calls in the same response."
        ),
        {
            "command": {"type": "string", "description": "Shell command, max 2,000 characters."},
            "timeout": {"type": "integer", "minimum": 1, "maximum": max_timeout},
        },
        ["command"],
    )


BROWSER_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    _function(
        "browser_navigate",
        "Open an http/https page in this task's browser session. Use it to load the ComfyUI web UI (http://127.0.0.1:8188) and verify a node you are developing end-to-end.",
        {"url": {"type": "string"}},
        ["url"],
    ),
    _function(
        "browser_content",
        "Extract visible text from the page body or a CSS selector. Results over 100k characters are truncated with a marker -- narrow with a selector instead of re-fetching the whole page.",
        {"selector": {"type": "string"}},
        [],
    ),
    _function(
        "browser_screenshot",
        "Capture the full page into the task workspace and return the file path. Use after wiring a workflow (import the project workflow.json) to verify the node graph and outputs visually.",
        {"path": {"type": "string"}},
        [],
    ),
    _function(
        "browser_click",
        "Click a CSS selector in the current browser page.",
        {"selector": {"type": "string"}},
        ["selector"],
    ),
    _function(
        "browser_fill",
        "Fill a CSS selector in the current browser page.",
        {"selector": {"type": "string"}, "value": {"type": "string"}},
        ["selector", "value"],
    ),
]

LIVE_WORKFLOW_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    _function(
        "get_current_workflow",
        "Read the latest workflow serialized from the currently open ComfyUI canvas. This includes unsaved canvas changes and is separate from the project's workflow.json file.",
        {},
        [],
    ),
]

GLOBAL_CHAT_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    _function(
        "install_github_skill",
        (
            "Install Cline Skills from one public GitHub repository into the global Skill library. "
            "Call only when the user explicitly asked to install a Skill. Use the exact repository, "
            "folder, or SKILL.md source the user provided; if they gave owner/repo shorthand or only a "
            "repository URL, pass that repository -- every Skill folder it publishes is installed and "
            "the result lists each one. Each Skill is security-checked before it is stored, with the "
            "static rule engine or the model review engine the user configured; report the verdict, "
            "findings and engine in the result to the user. Packages judged unsafe are refused and "
            "never installed. Never install arbitrary plugins or software."
        ),
        {
            "github_url": {
                "type": "string",
                "description": "The user's public GitHub URL, or https://github.com/OWNER/REPO for owner/repo shorthand.",
            }
        },
        ["github_url"],
    )
]

# Read-only browsing subset for the global conversation (no click/fill).
GLOBAL_CHAT_BROWSER_TOOL_DEFINITIONS: list[dict[str, Any]] = BROWSER_TOOL_DEFINITIONS[:3]

GLOBAL_CHAT_WORKFLOW_DEFINITIONS: list[dict[str, Any]] = [
    _function(
        "search_runninghub_workflows",
        "Search RunningHub domestic or international sites for ComfyUI workflow candidates. Call only when the user explicitly asks to find or recommend a workflow. Return ranked candidates and wait for the user to select one; never install before selection.",
        {
            "query": {"type": "string", "description": "Your chosen concise RunningHub search keywords based on the user's workflow requirement. Preserve the important project/model/task terms; omit polite wording and generic wrapper words."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            "filters": {"type": "object", "additionalProperties": True},
        },
        ["query"],
    ),
    _function(
        "inspect_runninghub_workflow",
        "Download and inspect a user-selected RunningHub workflow using its persisted job_id and candidate_id. Return workflow format, node classes, missing loaded nodes, and model references. Call only after the user explicitly selects a candidate; do not install dependencies or execute it.",
        {
            "job_id": {"type": "string"},
            "candidate_id": {"type": "string"},
        },
        ["job_id", "candidate_id"],
    ),
]

GLOBAL_CHAT_WORKFLOW_INSTALL_DEFINITIONS: list[dict[str, Any]] = [
    _function(
        "install_uploaded_workflow",
        (
            "Install one user-uploaded ComfyUI workflow JSON into ComfyUI's user workflow library. "
            "Call only when the user explicitly asked to install/import a workflow or used @workflow. "
            "The source must be the exact uploaded path under uploads/ supplied for this message. "
            "The JSON is parsed and validated before it is copied; this never executes the workflow, "
            "installs custom nodes, downloads models, or overwrites an existing same-named file."
        ),
        {
            "source": {
                "type": "string",
                "description": "Exact global workspace path of the uploaded JSON, for example uploads/my-workflow.json.",
            }
        },
        ["source"],
    )
]


GLOBAL_CHAT_FILE_TOOL_DEFINITIONS: list[dict[str, Any]] = FILE_TOOL_DEFINITIONS

GLOBAL_CHAT_NODE_INSTALL_DEFINITIONS: list[dict[str, Any]] = [
    _function(
        "install_comfyui_node",
        (
            "Install one ComfyUI custom node project into ComfyUI/custom_nodes. Call only when the user "
            "explicitly asked to install a node. Pass the exact public GitHub/GitCode repository or tree-folder link "
            "they provided, or the workspace path of a node archive they uploaded (for example "
            "uploads/my-node.zip). The package is staged outside custom_nodes, security-scanned with the "
            "engine the user configured, then copied in; the result carries the verdict and findings. "
            "Dependencies are never installed and bundled scripts never run, so report requirements.txt and "
            "the ComfyUI restart yourself. A project judged unsafe is refused and cannot be overridden from "
            "here -- tell the user to confirm 仍然安装 in the panel. Never use this for Skills, plugins or "
            "arbitrary software."
        ),
        {
            "source": {
                "type": "string",
                "description": "The user's https://github.com/OWNER/REPO or https://gitcode.com/OWNER/REPO (optionally /tree/REF/FOLDER), or an uploaded archive path such as uploads/name.zip.",
            }
        },
        ["source"],
    )
]


def global_chat_definitions(
    *,
    install_requested: bool,
    network_enabled: bool,
    node_install_requested: bool = False,
    workflow_requested: bool = False,
    workflow_install_requested: bool = False,
) -> list[dict[str, Any]]:
    """Tool surface for the task-independent conversation."""
    tools: list[dict[str, Any]] = list(GLOBAL_CHAT_FILE_TOOL_DEFINITIONS)
    if install_requested:
        tools += GLOBAL_CHAT_TOOL_DEFINITIONS
    if node_install_requested:
        tools += GLOBAL_CHAT_NODE_INSTALL_DEFINITIONS
    if workflow_requested:
        tools += GLOBAL_CHAT_WORKFLOW_DEFINITIONS
    if workflow_install_requested:
        tools += GLOBAL_CHAT_WORKFLOW_INSTALL_DEFINITIONS
    if network_enabled:
        tools += GLOBAL_CHAT_BROWSER_TOOL_DEFINITIONS
    return tools


# Historical module-level lists kept for facade compatibility.
TOOL_DEFINITIONS: list[dict[str, Any]] = [
    *FILE_TOOL_DEFINITIONS,
    WRITE_TOOL_DEFINITIONS[1],  # write_file (historic order)
    WRITE_TOOL_DEFINITIONS[2],  # apply_patch
    build_command_definition("act"),
]

BROWSER_TOOL_NAMES = {item["function"]["name"] for item in BROWSER_TOOL_DEFINITIONS}
FILE_TOOL_NAMES = {item["function"]["name"] for item in FILE_TOOL_DEFINITIONS}
WRITE_TOOL_NAMES = {item["function"]["name"] for item in WRITE_TOOL_DEFINITIONS}


def definitions_for_mode(mode: str, mcp_groups: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Per-request tool set (DESIGN 9 enhancement 4: rebuild at each boundary)."""
    tools: list[dict[str, Any]] = [*FILE_TOOL_DEFINITIONS, build_command_definition(mode)]
    if mode == "plan":
        tools += BROWSER_TOOL_DEFINITIONS[:2]
    else:
        tools += [*WRITE_TOOL_DEFINITIONS, *BROWSER_TOOL_DEFINITIONS]
    tools += LIVE_WORKFLOW_TOOL_DEFINITIONS
    # MCP tools may have arbitrary side effects: ACT mode only (DESIGN 6).
    for group in (mcp_groups or [] if mode != "plan" else []):
        for tool in group.get("tools") or []:
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description") or tool["name"],
                        "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
                    },
                }
            )
    return tools
