"""Backend for the ComfyUI-Cline custom node.

This module contains the workspace guard, model adapter, tool loop, and
aiohttp route registration. It intentionally uses only Python's standard
library plus aiohttp, which is already a ComfyUI dependency.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlparse
from urllib.request import Request, urlopen
from urllib.request import HTTPRedirectHandler, build_opener

try:
    from .cline_browser import BrowserManager, BrowserUnavailable
    from .cline_core import CheckpointManager, ContextManager, SkillStore, TaskStore
    from .cline_mcp import MCPManager, MCPProtocolError
except ImportError:  # Support the repository's direct unittest invocation.
    from cline_browser import BrowserManager, BrowserUnavailable
    from cline_core import CheckpointManager, ContextManager, SkillStore, TaskStore
    from cline_mcp import MCPManager, MCPProtocolError


PLUGIN_NAME = "ComfyUI-Cline"
MAX_FILE_BYTES = 1_000_000
MAX_WRITE_BYTES = 2_000_000
MAX_COMMAND_OUTPUT = 20_000
MAX_SEARCH_RESULTS = 100
MAX_AGENT_STEPS = 8
MAX_MESSAGE_CHARS = 120_000
MAX_GLOBAL_SKILL_PACKAGE_FILES = 40
MAX_GLOBAL_SKILL_PACKAGE_BYTES = 200_000
DEFAULT_MAX_CONTEXT_TOKENS = 32_000
MIN_MAX_CONTEXT_TOKENS = 1_024
MAX_MAX_CONTEXT_TOKENS = 1_000_000
DEFAULT_COMPACTION_THRESHOLD_PERCENT = 80
MIN_COMPACTION_THRESHOLD_PERCENT = 1
MAX_COMPACTION_THRESHOLD_PERCENT = 99
MAX_CURRENT_WORKFLOW_BYTES = 2_000_000
PLAN_TOOL_NAMES = {"list_files", "read_file", "search_files", "browser_navigate", "browser_content", "get_current_workflow"}
STATE_DIRECTORY = Path(
    os.environ.get(
        "CLINE_STATE_DIRECTORY",
        str(Path.home() / ".local" / "share" / "comfyui-cline"),
    )
).expanduser()
TASK_DATABASE = Path(os.environ.get("CLINE_TASK_DATABASE", str(STATE_DIRECTORY / "tasks.sqlite3"))).expanduser()
SKILL_DATABASE = Path(os.environ.get("CLINE_SKILL_DATABASE", str(STATE_DIRECTORY / "skills.sqlite3"))).expanduser()
MCP_SETTINGS_PATH = Path(
    os.environ.get("CLINE_MCP_SETTINGS_PATH", str(Path.home() / ".cline" / "data" / "settings" / "cline_mcp_settings.json"))
).expanduser()
DEFAULT_MODEL = os.environ.get("CLINE_MODEL", "")
DEFAULT_API_BASE_URL = os.environ.get("CLINE_API_BASE_URL", "https://api.openai.com/v1")
DEFAULT_WORKSPACE = Path.home() / ".Comfyui-node"
DEFAULT_COMFYUI_DIRECTORY = os.environ.get("CLINE_COMFYUI_DIRECTORY", "").strip()
WORKFLOW_FILENAME = "workflow.json"
CONFIG_PATH = Path(
    os.environ.get(
        "CLINE_CONFIG_FILE",
        str(Path.home() / ".config" / "comfyui-cline" / "config.json"),
    )
).expanduser()
logger = logging.getLogger(__name__)


def _max_context_tokens(value: Any, default: int = DEFAULT_MAX_CONTEXT_TOKENS) -> int:
    """Validate the configured model context budget."""
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if result < MIN_MAX_CONTEXT_TOKENS or result > MAX_MAX_CONTEXT_TOKENS:
        raise ValueError(
            f"最大上下文 Token 数必须在 {MIN_MAX_CONTEXT_TOKENS} 到 {MAX_MAX_CONTEXT_TOKENS} 之间。"
        )
    return result


def _compaction_threshold_percent(value: Any, default: int = DEFAULT_COMPACTION_THRESHOLD_PERCENT) -> int:
    """Validate the percentage of the context budget that triggers compaction."""
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if result < MIN_COMPACTION_THRESHOLD_PERCENT or result > MAX_COMPACTION_THRESHOLD_PERCENT:
        raise ValueError(
            f"自动压缩阈值必须在 {MIN_COMPACTION_THRESHOLD_PERCENT}% 到 {MAX_COMPACTION_THRESHOLD_PERCENT}% 之间。"
        )
    return result


class TaskCancelled(Exception):
    """Internal control-flow exception for a user-cancelled run."""


class ProjectFolderExistsError(ValueError):
    """Raised when a new task would overwrite an existing node directory."""


def _project_folder_name(value: Any) -> str:
    """Validate a new project's direct child folder name.

    Projects are intentionally limited to one directory below the configured
    workspace.  This prevents a task from escaping the workspace and keeps a
    task history entry unambiguously mapped to one ComfyUI node directory.
    """
    name = str(value or "").strip()
    if not name:
        raise ValueError("请输入项目文件夹名称。")
    if name in {".", ".."} or len(name) > 128 or any(char.isspace() and char in "\r\n\t" for char in name):
        raise ValueError("项目文件夹名称无效。")
    if "/" in name or "\\" in name or "\x00" in name or Path(name).name != name:
        raise ValueError("项目文件夹必须是工作区下的直接文件夹名称，不能包含路径。")
    if any(ord(char) < 32 for char in name):
        raise ValueError("项目文件夹名称不能包含控制字符。")
    return name


def _workflow_template(project_name: str) -> dict[str, Any]:
    """Build a portable ComfyUI UI workflow for testing a new node.

    A custom node's class type and inputs are project-specific, so the starter
    workflow deliberately uses only ComfyUI core image nodes.  Developers can
    insert their node between LoadImage and PreviewImage without requiring a
    checkpoint or any other model-specific asset.
    """
    note = (
        f"ComfyUI-Cline 开发工作流：{project_name}\n"
        "在 LoadImage 选择测试图片，然后把新开发的节点接在 LoadImage 和 PreviewImage 之间。\n"
        "如果节点不是图像处理节点，请按节点的输入输出修改这条测试链。"
    )
    return {
        "last_node_id": 3,
        "last_link_id": 1,
        "nodes": [
            {
                "id": 1,
                "type": "LoadImage",
                "pos": [80, 180],
                "size": [315, 314],
                "flags": {},
                "order": 0,
                "mode": 0,
                "inputs": [],
                "outputs": [
                    {"name": "IMAGE", "type": "IMAGE", "links": [1], "slot_index": 0},
                    {"name": "MASK", "type": "MASK", "links": None, "slot_index": 1},
                ],
                "properties": {"Node name for S&R": "LoadImage"},
                "widgets_values": ["", "image"],
            },
            {
                "id": 2,
                "type": "PreviewImage",
                "pos": [760, 180],
                "size": [315, 246],
                "flags": {},
                "order": 1,
                "mode": 0,
                "inputs": [{"name": "images", "type": "IMAGE", "link": 1}],
                "outputs": [],
                "properties": {"Node name for S&R": "PreviewImage"},
                "widgets_values": [],
            },
            {
                "id": 3,
                "type": "Note",
                "pos": [80, 40],
                "size": [650, 100],
                "flags": {},
                "order": 2,
                "mode": 0,
                "inputs": [],
                "outputs": [],
                "properties": {},
                "widgets_values": [note],
            },
        ],
        "links": [[1, 1, 0, 2, 0, "IMAGE"]],
        "groups": [],
        "config": {},
        "extra": {
            "cline": {
                "project": project_name,
                "purpose": "ComfyUI custom node development test workflow",
            },
            "ds": {"scale": 1, "offset": [0, 0]},
        },
        "version": 0.4,
    }


def _write_workflow_if_missing(project: Path, project_name: str) -> tuple[Path, bool]:
    """Create the starter workflow without ever replacing an existing path."""
    workflow = project.resolve() / WORKFLOW_FILENAME
    if workflow.exists() or workflow.is_symlink():
        return workflow, False
    try:
        with workflow.open("x", encoding="utf-8") as handle:
            json.dump(_workflow_template(project_name), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except FileExistsError:
        # Another process won the race.  It is still important not to inspect
        # or replace that file: it may be a user-authored workflow.
        return workflow, False
    except Exception:
        # Do not leave a truncated starter file if the filesystem rejects the
        # write after the exclusive open succeeds.
        try:
            workflow.unlink()
        except OSError:
            pass
        raise
    return workflow, True

IGNORED_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "__pycache__",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}
BLOCKED_COMMANDS = re.compile(
    r"(?:^|[;&|])\s*(?:sudo|shutdown|reboot|mkfs(?:\.|\s)|dd\s+if=)|"
    r"(?:rm\s+-[^\n]*r[^\n]*f|git\s+reset\s+--hard|git\s+clean\s+-fd)",
    re.IGNORECASE,
)

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and directories in the ComfyUI workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative directory. Use '.' for the root.",
                    },
                    "recursive": {"type": "boolean"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file from the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative file path."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search text in workspace files, excluding build and dependency directories.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string", "description": "Optional directory or file."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or replace a UTF-8 text file inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": "Apply a unified diff to one workspace file inside the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "patch": {"type": "string"},
                },
                "required": ["path", "patch"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a development command in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "minimum": 1, "maximum": 120},
                },
                "required": ["command"],
            },
        },
    },
]

BROWSER_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "browser_navigate",
            "description": "Open an HTTP or HTTPS URL in the task browser.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_content",
            "description": "Read visible text from the current browser page.",
            "parameters": {
                "type": "object",
                "properties": {"selector": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_screenshot",
            "description": "Take a screenshot of the current browser page and save it in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": "Click a CSS selector in the current browser page.",
            "parameters": {
                "type": "object",
                "properties": {"selector": {"type": "string"}},
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_fill",
            "description": "Fill a CSS selector in the current browser page.",
            "parameters": {
                "type": "object",
                "properties": {"selector": {"type": "string"}, "value": {"type": "string"}},
                "required": ["selector", "value"],
            },
        },
    },
]

LIVE_WORKFLOW_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_current_workflow",
            "description": "Read the latest workflow serialized from the currently open ComfyUI canvas. This includes unsaved canvas changes and is separate from the project's workflow.json file.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]


def _json_text(value: Any, limit: int = MAX_MESSAGE_CHARS) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2) if not isinstance(value, str) else value
    if len(text) > limit:
        return text[:limit] + f"\n...[truncated at {limit} characters]"
    return text


def _safe_relative(path: str) -> str:
    """Normalize a user-supplied path without allowing absolute traversal."""

    value = str(path or ".").replace("\\", "/")
    if value.startswith("/") or re.match(r"^[A-Za-z]:/", value):
        raise ValueError("Absolute paths are not allowed; use a path relative to the workspace.")
    normalized = os.path.normpath(value)
    if normalized == ".." or normalized.startswith(f"..{os.sep}"):
        raise ValueError("Path must stay inside the workspace.")
    return "." if normalized in {"", "."} else normalized


class Workspace:
    """Filesystem operations restricted to one resolved workspace directory."""

    def __init__(self, root: str | os.PathLike[str]):
        candidate = Path(root).expanduser().resolve()
        if not candidate.is_dir():
            raise ValueError(f"Workspace does not exist or is not a directory: {candidate}")
        self.root = candidate

    def resolve(self, relative_path: str) -> Path:
        relative = _safe_relative(relative_path)
        resolved = (self.root / relative).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("Path must stay inside the workspace.") from exc
        return resolved

    def display_path(self, path: Path) -> str:
        return "." if path == self.root else path.relative_to(self.root).as_posix()

    def stays_inside(self, path: Path) -> bool:
        """Return whether a directory entry remains inside the workspace after resolving links."""

        try:
            path.resolve().relative_to(self.root)
            return True
        except (OSError, ValueError):
            return False

    def list_files(self, relative_path: str = ".", recursive: bool = False) -> list[dict[str, Any]]:
        directory = self.resolve(relative_path)
        if not directory.is_dir():
            raise ValueError(f"Not a directory: {relative_path}")
        entries: list[dict[str, Any]] = []
        iterator: Iterable[Path] = directory.rglob("*") if recursive else directory.iterdir()
        for item in sorted(iterator, key=lambda value: (not value.is_dir(), value.name.lower())):
            # A custom node is often installed as a symlink. Do not expose a
            # link whose target escapes the configured workspace: clicking it
            # would otherwise produce a confusing path-boundary error.
            if not self.stays_inside(item):
                continue
            if any(part in IGNORED_DIRECTORIES for part in item.relative_to(self.root).parts):
                continue
            try:
                is_dir = item.is_dir()
                entries.append(
                    {
                        "path": self.display_path(item),
                        "name": item.name,
                        "type": "directory" if is_dir else "file",
                        "size": None if is_dir else item.stat().st_size,
                    }
                )
            except OSError:
                continue
            if len(entries) >= 500:
                break
        return entries

    def read_file(self, relative_path: str) -> str:
        path = self.resolve(relative_path)
        if not path.is_file():
            raise ValueError(f"Not a file: {relative_path}")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(f"File is larger than the {MAX_FILE_BYTES} byte limit.")
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Only UTF-8 text files can be read by the assistant.") from exc

    def search_files(self, query: str, relative_path: str = ".") -> list[dict[str, Any]]:
        query = str(query or "")
        if not query:
            raise ValueError("Search query cannot be empty.")
        start = self.resolve(relative_path)
        candidates = [start] if start.is_file() else start.rglob("*")
        results: list[dict[str, Any]] = []
        for path in candidates:
            if len(results) >= MAX_SEARCH_RESULTS:
                break
            if not self.stays_inside(path):
                continue
            if not path.is_file() or any(part in IGNORED_DIRECTORIES for part in path.relative_to(self.root).parts):
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                if query.lower() in line.lower():
                    results.append(
                        {
                            "path": self.display_path(path),
                            "line": line_number,
                            "text": line[:500],
                        }
                    )
                    if len(results) >= MAX_SEARCH_RESULTS:
                        break
        return results

    def write_file(self, relative_path: str, content: str) -> str:
        path = self.resolve(relative_path)
        encoded = str(content).encode("utf-8")
        if len(encoded) > MAX_WRITE_BYTES:
            raise ValueError(f"File is larger than the {MAX_WRITE_BYTES} byte limit.")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(path, str(content))
        return self.display_path(path)

    def apply_patch(self, relative_path: str, patch: str) -> str:
        path = self.resolve(relative_path)
        original = self.read_file(relative_path) if path.exists() else ""
        updated = apply_unified_patch(original, str(patch))
        if len(updated.encode("utf-8")) > MAX_WRITE_BYTES:
            raise ValueError(f"Patched file is larger than the {MAX_WRITE_BYTES} byte limit.")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(path, updated)
        return self.display_path(path)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as temporary:
            temporary.write(content)
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)


def apply_unified_patch(original: str, patch: str) -> str:
    """Apply a small unified diff, including the common Cline patch wrapper."""

    raw_lines = str(patch).replace("\r\n", "\n").splitlines(keepends=True)
    lines = [line for line in raw_lines if not line.startswith(("*** Begin Patch", "*** End Patch"))]
    lines = [line for line in lines if not line.startswith("*** Update File:")]
    hunk_indexes = [index for index, line in enumerate(lines) if line.startswith("@@")]
    if not hunk_indexes:
        raise ValueError("Patch must contain at least one unified-diff hunk starting with '@@'.")

    source = original.splitlines(keepends=True)
    output: list[str] = []
    source_position = 0
    for hunk_number, header_index in enumerate(hunk_indexes):
        header = lines[header_index]
        match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", header)
        if not match:
            raise ValueError(f"Invalid unified diff hunk header: {header.rstrip()}")
        old_line = int(match.group(1))
        old_start = 0 if old_line == 0 else old_line - 1
        if old_start < source_position or old_start > len(source):
            raise ValueError("Patch context does not match the current file.")
        output.extend(source[source_position:old_start])
        end = hunk_indexes[hunk_number + 1] if hunk_number + 1 < len(hunk_indexes) else len(lines)
        for line in lines[header_index + 1 : end]:
            if line.startswith("\\ No newline at end of file"):
                continue
            if not line:
                raise ValueError("Malformed empty patch line.")
            marker, value = line[0], line[1:]
            if marker == " ":
                if source_position >= len(source) or source[source_position] != value:
                    raise ValueError("Patch context does not match the current file.")
                output.append(value)
                source_position += 1
            elif marker == "-":
                if source_position >= len(source) or source[source_position] != value:
                    raise ValueError("Patch removal does not match the current file.")
                source_position += 1
            elif marker == "+":
                output.append(value)
            else:
                raise ValueError(f"Unsupported patch line: {line.rstrip()}")
    output.extend(source[source_position:])
    return "".join(output)


def _default_workspace() -> Path:
    configured = os.environ.get("CLINE_WORKSPACE")
    workspace = Path(configured).expanduser() if configured else DEFAULT_WORKSPACE
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace.resolve()


class SettingsStore:
    """Small local settings file; API keys are never returned by GET settings."""

    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}

    def public(self) -> dict[str, Any]:
        data = self.load()
        workspace = data.get("workspace") or str(_default_workspace())
        workspace_path = Path(workspace).expanduser()
        workspace_path.mkdir(parents=True, exist_ok=True)
        workspace = str(workspace_path.resolve())
        comfyui_value = data["comfyui_directory"] if "comfyui_directory" in data else DEFAULT_COMFYUI_DIRECTORY
        comfyui_path = Path(str(comfyui_value).strip()).expanduser() if str(comfyui_value or "").strip() else None
        comfyui = str(comfyui_path.resolve()) if comfyui_path and comfyui_path.is_dir() else str(comfyui_value or "").strip()
        return {
            "api_base_url": data.get("api_base_url") or DEFAULT_API_BASE_URL,
            "model": data.get("model") or DEFAULT_MODEL,
            "max_context_tokens": _max_context_tokens(data.get("max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS)),
            "compaction_threshold_percent": _compaction_threshold_percent(
                data.get("compaction_threshold_percent", DEFAULT_COMPACTION_THRESHOLD_PERCENT)
            ),
            "workspace": workspace,
            "comfyui_directory": comfyui,
            "api_key_configured": bool(data.get("api_key") or os.environ.get("CLINE_API_KEY")),
            "api_key_from_environment": bool(os.environ.get("CLINE_API_KEY")),
        }

    def update(self, values: dict[str, Any]) -> dict[str, Any]:
        current = self.load()
        for key in ("api_base_url", "model", "workspace", "comfyui_directory"):
            if key in values and values[key] is not None:
                current[key] = str(values[key]).strip()
        if "max_context_tokens" in values and values["max_context_tokens"] is not None:
            current["max_context_tokens"] = _max_context_tokens(values["max_context_tokens"])
        if "compaction_threshold_percent" in values and values["compaction_threshold_percent"] is not None:
            current["compaction_threshold_percent"] = _compaction_threshold_percent(values["compaction_threshold_percent"])
        if "api_key" in values:
            key = str(values.get("api_key") or "").strip()
            if key:
                current["api_key"] = key
            # An empty field means "leave the stored key unchanged". This
            # prevents opening Settings and clicking Save from deleting it.
        workspace = current.get("workspace") or str(_default_workspace())
        workspace_path = Path(workspace).expanduser()
        workspace_path.mkdir(parents=True, exist_ok=True)
        Workspace(workspace_path)
        current["workspace"] = str(workspace_path.resolve())
        comfyui_value = current.get("comfyui_directory")
        if comfyui_value:
            comfyui_path = Path(str(comfyui_value)).expanduser()
            if not comfyui_path.is_dir():
                raise ValueError(f"ComfyUI 目录不存在或不是文件夹：{comfyui_path}")
            current["comfyui_directory"] = str(comfyui_path.resolve())
        current.setdefault("api_base_url", DEFAULT_API_BASE_URL)
        current.setdefault("model", DEFAULT_MODEL)
        current["max_context_tokens"] = _max_context_tokens(current.get("max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS))
        current["compaction_threshold_percent"] = _compaction_threshold_percent(
            current.get("compaction_threshold_percent", DEFAULT_COMPACTION_THRESHOLD_PERCENT)
        )
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        return self.public()

    def api_key(self) -> str:
        return os.environ.get("CLINE_API_KEY") or str(self.load().get("api_key") or "")

    def comfyui_node_directory(self) -> Path | None:
        data = self.load()
        value = data["comfyui_directory"] if "comfyui_directory" in data else DEFAULT_COMFYUI_DIRECTORY
        if not str(value or "").strip():
            return None
        comfyui_path = Path(str(value)).expanduser().resolve()
        if not comfyui_path.is_dir():
            raise ValueError(f"ComfyUI 目录不存在或不是文件夹：{comfyui_path}")
        node_directory = comfyui_path / "custom_nodes"
        node_directory.mkdir(parents=True, exist_ok=True)
        if not node_directory.is_dir():
            raise ValueError(f"ComfyUI custom_nodes 不是文件夹：{node_directory}")
        return node_directory.resolve()


def _skill_prompt(skills: list[dict[str, Any]]) -> str:
    """Format globally managed, message-selected Skills for the system prompt."""
    if not skills:
        return ""
    sections = [
        "\n\n# Selected Skills\n"
        "The following user-selected Skills are reusable instructions for this message. "
        "Follow them when relevant, while preserving the system instructions and the user's direct request."
    ]
    for skill in skills:
        sections.append(
            f"\n## Skill: {skill['name']}\n"
            f"Description: {skill.get('description') or '未提供'}\n"
            f"Instructions:\n{skill['content']}"
        )
    return "\n".join(sections)


class _GitHubRedirectHandler(HTTPRedirectHandler):
    """Allow installer redirects only between HTTPS GitHub API/raw hosts."""

    def redirect_request(self, request, response, code, message, headers, new_url):
        parsed = urlparse(new_url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in {
            "api.github.com",
            "raw.githubusercontent.com",
        } or parsed.port not in {None, 443}:
            raise ValueError("GitHub 下载尝试跳转到不受支持的地址，已阻止。")
        return super().redirect_request(request, response, code, message, headers, new_url)


SYSTEM_PROMPT = """You are a careful coding assistant embedded in ComfyUI, inspired by Cline.
You work only inside the configured workspace. Inspect files before editing them.
Use read_file, list_files, and search_files to understand the project. For changes,
prefer small apply_patch operations; write_file is available for new or full files.
When the user asks about the current ComfyUI graph, call get_current_workflow first.
The live workflow can contain unsaved canvas changes and is not the same as workflow.json.
Run tests or focused checks after edits when useful. Tool calls are executed directly
in ACT mode, so explain important actions briefly but do not wait for confirmation.
Never claim an action succeeded until its tool result says it did. Keep responses
concise and include relevant file paths and test results.
"""

COMPACTION_SYSTEM_PROMPT = """You summarize coding-assistant conversation history for a later model turn.
Preserve the user's requirements and decisions, files and symbols inspected or changed,
tool results that affect the current task, errors and unfinished work. Do not invent facts.
Return only a concise plain-text summary. Do not call tools, edit files, or give generic advice.
"""

GLOBAL_CHAT_SYSTEM_PROMPT = """You are Cline's global conversation assistant inside ComfyUI.
This conversation is independent of every task, project folder, workspace, workflow,
file context, task metadata, and task status. Answer questions and discuss ideas
helpfully, but do not claim to inspect or modify local files or the current ComfyUI
workflow. Keep responses concise. When the user explicitly requests installation of
a Skill from a public GitHub source, call install_github_skill with that exact source.
After receiving the tool result, explain whether installation succeeded and summarize
the returned result. Never claim success before the tool confirms it. Skill files are
stored as text instructions/references; never execute bundled scripts or install
software, plugins, dependencies, or arbitrary repository code.
"""

GLOBAL_CHAT_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "install_github_skill",
            "description": (
                "Install one public GitHub Cline Skill package into the global Skill library. "
                "Call only when the user explicitly asked to install a Skill. Use the exact "
                "repository, folder, or SKILL.md source the user provided; if they gave owner/repo "
                "shorthand, use https://github.com/owner/repo. Never install arbitrary plugins or software."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "github_url": {
                        "type": "string",
                        "description": "The user's public GitHub URL, or https://github.com/OWNER/REPO for owner/repo shorthand.",
                    }
                },
                "required": ["github_url"],
                "additionalProperties": False,
            },
        },
    }
]

@dataclass
class PendingAction:
    messages: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    action_index: int
    name: str
    arguments: dict[str, Any]


class ClineAgent:
    def __init__(self, settings: SettingsStore):
        self.settings = settings
        self.pending: dict[str, PendingAction] = {}
        settings_data = self.settings.load()
        configured = settings_data.get("max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS)
        threshold = settings_data.get("compaction_threshold_percent", DEFAULT_COMPACTION_THRESHOLD_PERCENT)
        try:
            configured = _max_context_tokens(configured)
        except ValueError:
            configured = DEFAULT_MAX_CONTEXT_TOKENS
        try:
            threshold = _compaction_threshold_percent(threshold)
        except ValueError:
            threshold = DEFAULT_COMPACTION_THRESHOLD_PERCENT
        self.context = ContextManager(configured, compaction_threshold=threshold / 100)

    def workspace(self) -> Workspace:
        configured = self.settings.load().get("workspace") or str(_default_workspace())
        return Workspace(configured)

    async def chat(self, messages: list[dict[str, Any]], context: str = "") -> dict[str, Any]:
        clean_messages = self._clean_messages(messages)
        if context:
            clean_messages.append(
                {
                    "role": "user",
                    "content": f"Additional workspace context:\n{context}",
                }
            )
        return await self._run_loop(clean_messages)

    async def confirm(self, action_id: str, approved: bool) -> dict[str, Any]:
        action = self.pending.pop(action_id, None)
        if action is None:
            raise ValueError("This confirmation has expired. Please send the request again.")
        if approved:
            result = self._execute_tool(action.name, action.arguments)
        else:
            result = "User denied this action. Do not retry it unless the user asks again."
        tool_call = action.tool_calls[action.action_index]
        action.messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.get("id", "cline-tool"),
                "content": _json_text(result),
            }
        )
        return await self._run_loop(action.messages, action.tool_calls, action.action_index + 1)

    async def _run_loop(
        self,
        messages: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]] | None = None,
        tool_index: int = 0,
    ) -> dict[str, Any]:
        for _ in range(MAX_AGENT_STEPS):
            if tool_calls is not None:
                for index in range(tool_index, len(tool_calls)):
                    name, arguments = self._parse_tool_call(tool_calls[index])
                    result = self._execute_tool(name, arguments)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_calls[index].get("id", f"cline-tool-{index}"),
                            "content": _json_text(result),
                        }
                    )
                tool_calls = None
                tool_index = 0

            messages, _ = await self._compact_messages(messages, TOOL_DEFINITIONS)
            response = await self._call_model(messages, tools=TOOL_DEFINITIONS)
            choice = (response.get("choices") or [{}])[0]
            assistant = choice.get("message") or {}
            calls = assistant.get("tool_calls") or []
            if not calls:
                return {
                    "status": "ok",
                    "message": {
                        "role": "assistant",
                        "content": assistant.get("content") or "模型没有返回文本。",
                    },
                }
            messages.append(
                {
                    "role": "assistant",
                    "content": assistant.get("content"),
                    "tool_calls": calls,
                }
            )
            tool_calls = calls
            tool_index = 0
        raise RuntimeError("The assistant reached the tool-call step limit. Please continue the conversation.")

    async def _compact_messages(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], bool]:
        try:
            configured = _max_context_tokens(
                self.settings.load().get("max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS)
            )
            threshold = _compaction_threshold_percent(
                self.settings.load().get("compaction_threshold_percent", DEFAULT_COMPACTION_THRESHOLD_PERCENT)
            )
        except ValueError:
            configured = DEFAULT_MAX_CONTEXT_TOKENS
            threshold = DEFAULT_COMPACTION_THRESHOLD_PERCENT
        if self.context.max_tokens != configured or self.context.compaction_threshold != threshold / 100:
            self.context = ContextManager(
                configured,
                keep_messages=self.context.keep_messages,
                compaction_threshold=threshold / 100,
            )
        reserved = ContextManager.estimate_tokens(SYSTEM_PROMPT) + ContextManager.estimate_tokens(_json_text(tools))
        if not self.context.needs_compaction(messages, reserved):
            return messages, False
        summary_input = self.context.bounded_summary_messages(messages)
        if not summary_input:
            return self.context.fallback(messages, reserved)
        summary_input.append(
            {
                "role": "user",
                "content": "Summarize the preceding conversation now. Preserve concrete task state and unresolved work.",
            }
        )
        try:
            response = await self._call_model(
                summary_input,
                tools=[],
                system_prompt=COMPACTION_SYSTEM_PROMPT,
                max_tokens=2048,
            )
            summary = ((response.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            if not str(summary).strip():
                raise RuntimeError("模型摘要响应为空。")
            compacted, changed = self.context.with_summary(messages, summary, reserved)
            if changed:
                return compacted, True
            return messages, False
        except Exception as exc:
            # The persisted/original transcript remains untouched. The local
            # fallback keeps the request usable while making the provider error
            # visible in the server log.
            logger.warning("Context summary request failed; using local fallback: %s", exc)
            return self.context.fallback(messages, reserved)

    async def _call_model(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        system_prompt: str = SYSTEM_PROMPT,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        data = self.settings.load()
        api_key = self.settings.api_key()
        model = str(data.get("model") or DEFAULT_MODEL).strip()
        if not model:
            raise ValueError("No model configured. Open Cline settings and enter a model name.")
        base_url = str(data.get("api_base_url") or DEFAULT_API_BASE_URL).rstrip("/")
        endpoint = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system_prompt}, *messages],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        request = Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            body = await asyncio.to_thread(self._urlopen_json, request)
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:2_000]
            raise RuntimeError(f"模型 API 返回 HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"无法连接模型 API: {exc}") from exc
        if not isinstance(body, dict) or not body.get("choices"):
            raise RuntimeError(f"模型 API 返回了无法识别的响应: {_json_text(body, 2_000)}")
        return body

    @staticmethod
    def _urlopen_json(request: Request) -> dict[str, Any]:
        with urlopen(request, timeout=120) as response:  # noqa: S310 - configured local/provider URL
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _clean_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(messages, list) or not messages:
            raise ValueError("At least one user message is required.")
        cleaned: list[dict[str, Any]] = []
        for message in messages[-40:]:
            if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
                continue
            content = str(message.get("content") or "")
            # Do not reject a long transcript here: the model-input compactor
            # must see the old turns before deciding what can be summarized.
            # Keep the per-message safety bound for an accidentally huge input.
            if len(content) > MAX_MESSAGE_CHARS:
                raise ValueError("A single conversation message is too large.")
            cleaned.append({"role": message["role"], "content": content})
        if not cleaned or cleaned[-1]["role"] != "user":
            raise ValueError("The last conversation message must be from the user.")
        return cleaned


    @staticmethod
    def _parse_tool_call(tool_call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        function = tool_call.get("function") or {}
        name = str(function.get("name") or "")
        raw_arguments = function.get("arguments") or "{}"
        try:
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
        except json.JSONDecodeError as exc:
            raise ValueError(f"模型返回了无效的工具参数: {raw_arguments}") from exc
        if not name or not isinstance(arguments, dict):
            raise ValueError("模型返回了无效的工具调用。")
        return name, arguments

    @staticmethod
    def _requires_confirmation(name: str) -> bool:
        """Compatibility hook retained for callers of the original endpoint.

        Tool calls are intentionally executed directly. Safety is provided by
        the workspace boundary, blocked-command policy, and task cancellation,
        not by a confirmation prompt for every action.
        """
        return False

    def _confirmation(self, action_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "run_command":
            description = f"在工作区执行命令：{arguments.get('command', '')}"
        elif name == "apply_patch":
            description = f"修改文件：{arguments.get('path', '')}（应用补丁）"
        else:
            description = f"写入文件：{arguments.get('path', '')}"
        return {
            "id": action_id,
            "tool": name,
            "description": description,
            "arguments": {
                key: (value[:2_000] + "...[truncated]" if isinstance(value, str) and len(value) > 2_000 else value)
                for key, value in arguments.items()
            },
        }

    def _execute_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        workspace = self.workspace()
        if name == "list_files":
            return workspace.list_files(arguments.get("path", "."), bool(arguments.get("recursive", False)))
        if name == "read_file":
            return {"path": arguments.get("path"), "content": workspace.read_file(arguments["path"])}
        if name == "search_files":
            return workspace.search_files(arguments["query"], arguments.get("path", "."))
        if name == "write_file":
            return {"written": workspace.write_file(arguments["path"], arguments.get("content", ""))}
        if name == "apply_patch":
            return {"patched": workspace.apply_patch(arguments["path"], arguments["patch"])}
        if name == "run_command":
            return self._run_command(str(arguments.get("command") or ""), arguments.get("timeout", 120), workspace)
        raise ValueError(f"Unknown assistant tool: {name}")

    @staticmethod
    def _run_command(command: str, timeout: Any, workspace: Workspace) -> dict[str, Any]:
        if not command or len(command) > 2_000:
            raise ValueError("Command must be between 1 and 2,000 characters.")
        if "\x00" in command or BLOCKED_COMMANDS.search(command):
            raise ValueError("This command is blocked by the local safety policy.")
        try:
            timeout_seconds = min(120, max(1, int(timeout)))
        except (TypeError, ValueError):
            timeout_seconds = 120
        try:
            completed = subprocess.run(
                command,
                cwd=workspace.root,
                shell=True,  # noqa: S602 - command execution is bounded by local policy
                executable=os.environ.get("SHELL", "/bin/sh"),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            output = (completed.stdout + completed.stderr)[-MAX_COMMAND_OUTPUT:]
            return {"exit_code": completed.returncode, "output": output}
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode(errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            return {"exit_code": None, "timed_out": True, "output": (stdout + stderr)[-MAX_COMMAND_OUTPUT:]}


class GlobalChatAgent:
    """Task-independent conversation runtime.

    The global conversation has no task id, workspace, task metadata, or task
    status. Its only tool installs text-only Skill packages from public GitHub
    repositories; package scripts and other executable files are never run.
    """

    def __init__(self, settings: SettingsStore):
        self.settings = settings
        self.store = TaskStore(TASK_DATABASE)
        self.skills = SkillStore(SKILL_DATABASE)
        self.model = ClineAgent(settings)
        self._lock = asyncio.Lock()

    @staticmethod
    def _model_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {key: value for key, value in message.items() if key not in {"seq", "created_at", "skill_ids"}}
            for message in messages
            if message.get("role") in {"user", "assistant", "tool"}
        ]

    @staticmethod
    def _github_request(
        url: str,
        accept: str = "application/vnd.github+json",
        max_bytes: int = MAX_GLOBAL_SKILL_PACKAGE_BYTES,
    ) -> Any:
        request = Request(
            url,
            headers={"Accept": accept, "User-Agent": "ComfyUI-Cline-Skill-Installer"},
        )
        opener = build_opener(_GitHubRedirectHandler())
        with opener.open(request, timeout=20) as response:  # noqa: S310 - URL is constructed from validated public GitHub URL
            final_host = (urlparse(response.geturl()).hostname or "").lower()
            if final_host not in {"api.github.com", "raw.githubusercontent.com"}:
                raise ValueError("GitHub 请求被重定向到不受支持的主机，已拒绝安装。")
            body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError("GitHub 返回的数据超过安全大小限制。")
        if "raw.githubusercontent.com" in url:
            return body.decode("utf-8")
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("GitHub 返回了无法识别的 Skill 数据。") from exc

    @staticmethod
    def _github_skill_source(source: Any) -> tuple[str, str, str, str | None]:
        value = str(source or "").strip()
        parsed = urlparse(value)
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").lower() != "github.com"
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
            or len(parts) < 2
        ):
            raise ValueError("请提供公开 GitHub HTTPS 仓库或 Skill 文件夹链接。")
        owner, repository = parts[0], parts[1].removesuffix(".git")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repository):
            raise ValueError("GitHub 仓库地址无效。")
        ref: str | None = None
        prefix = ""
        if len(parts) > 2:
            if parts[2] == "tree" and len(parts) >= 4:
                ref = parts[3]
                prefix = "/".join(parts[4:]).strip("/")
            elif parts[2] == "blob" and len(parts) >= 5:
                ref = parts[3]
                file_path = "/".join(parts[4:]).strip("/")
                if Path(file_path).name.lower() != "skill.md":
                    raise ValueError("GitHub 文件链接必须指向 SKILL.md。")
                prefix = str(Path(file_path).parent).replace("\\", "/")
                if prefix == ".":
                    prefix = ""
            else:
                raise ValueError("链接应指向 GitHub 仓库、tree 文件夹或 SKILL.md 文件。")
        if any(part in {".", ".."} for part in prefix.split("/") if part):
            raise ValueError("Skill 文件夹路径无效。")
        return owner, repository, prefix, ref

    @staticmethod
    def _skill_metadata(skill_markdown: str, folder_name: str) -> tuple[str, str, str]:
        text = str(skill_markdown).replace("\r\n", "\n")
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            raise ValueError("Skill 包缺少 SKILL.md YAML frontmatter。")
        try:
            end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
        except StopIteration as exc:
            raise ValueError("SKILL.md YAML frontmatter 未闭合。") from exc
        fields: dict[str, str] = {}
        for line in lines[1:end]:
            match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):\s*(.*?)\s*$", line)
            if match:
                fields[match.group(1).lower()] = match.group(2).strip().strip("\"'")
        name = fields.get("name") or folder_name
        description = fields.get("description") or ""
        content = "\n".join(lines[end + 1 :]).strip()
        if not description:
            raise ValueError("SKILL.md frontmatter 必须包含 description。")
        if not content:
            raise ValueError("SKILL.md 没有 Skill 指令正文。")
        return name, description, content

    async def install_github_skill(self, source: Any) -> dict[str, Any]:
        owner, repository, requested_prefix, requested_ref = self._github_skill_source(source)
        api_root = f"https://api.github.com/repos/{owner}/{repository}"

        def request_json(url: str) -> Any:
            return self._github_request(url, max_bytes=10_000_000)

        try:
            if requested_ref is None:
                repo_info = await asyncio.to_thread(request_json, api_root)
                ref = str(repo_info.get("default_branch") or "").strip()
                if not ref:
                    raise ValueError("GitHub 仓库没有可用的默认分支。")
            else:
                ref = requested_ref
            tree_url = f"{api_root}/git/trees/{quote(ref, safe='')}?recursive=1"
            tree = await asyncio.to_thread(request_json, tree_url)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise ValueError(f"无法读取公开 GitHub Skill：{exc}") from exc
        entries = tree.get("tree") if isinstance(tree, dict) else None
        if not isinstance(entries, list) or tree.get("truncated"):
            if not requested_prefix:
                raise ValueError("GitHub 仓库过大，无法安全自动查找 Skill；请提供具体 Skill 文件夹链接。")
            content_directory_requests = 0

            async def read_contents(directory: str) -> list[dict[str, Any]]:
                nonlocal content_directory_requests
                content_directory_requests += 1
                if content_directory_requests > MAX_GLOBAL_SKILL_PACKAGE_FILES:
                    raise ValueError("Skill 包目录过于复杂，最多读取 40 个目录。")
                encoded_directory = "/".join(quote(part, safe="") for part in directory.split("/") if part)
                endpoint = f"{api_root}/contents/{encoded_directory}?ref={quote(ref, safe='')}"
                try:
                    result = await asyncio.to_thread(request_json, endpoint)
                except (HTTPError, URLError, TimeoutError, OSError) as exc:
                    raise ValueError(f"无法读取 GitHub Skill 文件夹 {directory}: {exc}") from exc
                if not isinstance(result, list):
                    raise ValueError(f"GitHub 路径不是文件夹：{directory}")
                return result

            root_listing = await read_contents(requested_prefix)
            skill_candidates: list[str] = []
            root_has_skill = any(
                item.get("type") == "file" and item.get("name", "").lower() == "skill.md"
                for item in root_listing
            )
            if root_has_skill:
                skill_candidates.append(requested_prefix)
            else:
                child_dirs = [item for item in root_listing if item.get("type") == "dir"]
                if len(child_dirs) > MAX_GLOBAL_SKILL_PACKAGE_FILES:
                    raise ValueError("Skill 文件夹包含过多子目录，请直接链接到 Skill 所在的目录。")
                for item in child_dirs:
                    child = f"{requested_prefix}/{item['name']}"
                    child_listing = await read_contents(child)
                    if any(
                        entry.get("type") == "file" and entry.get("name", "").lower() == "skill.md"
                        for entry in child_listing
                    ):
                        skill_candidates.append(child)
            if len(skill_candidates) != 1:
                raise ValueError("提供的 GitHub 文件夹中没有唯一的 Skill 包，请链接到包含单个 SKILL.md 的目录。")
            package_dir = skill_candidates[0]
            package_listing = await read_contents(package_dir)
            entries = []

            def add_regular_file(item: dict[str, Any], parent: str) -> None:
                if item.get("type") != "file" or item.get("mode") not in {None, "100644"}:
                    return
                entries.append({
                    "path": f"{parent}/{item['name']}" if parent else item["name"],
                    "type": "blob",
                    "mode": "100644",
                    "size": int(item.get("size") or 0),
                })

            for item in package_listing:
                add_regular_file(item, package_dir)
            allowed_reference_dirs = {"docs", "references", "templates", "examples"}

            async def add_reference_tree(directory: str, depth: int = 0) -> None:
                if depth > 5 or len(entries) > MAX_GLOBAL_SKILL_PACKAGE_FILES:
                    return
                for item in await read_contents(directory):
                    if item.get("type") == "file":
                        if Path(str(item.get("name") or "")).suffix.lower() in {".md", ".txt"}:
                            add_regular_file(item, directory)
                    elif item.get("type") == "dir":
                        await add_reference_tree(f"{directory}/{item['name']}", depth + 1)

            for item in package_listing:
                if item.get("type") == "dir" and item.get("name", "").lower() in allowed_reference_dirs:
                    await add_reference_tree(f"{package_dir}/{item['name']}")

        def regular_blob(item: dict[str, Any]) -> bool:
            return item.get("type") == "blob" and item.get("mode") == "100644"

        if requested_prefix and not (isinstance(tree, dict) and tree.get("truncated")):
            skill_path = f"{requested_prefix}/SKILL.md"
            if not any(item.get("path") == skill_path and regular_blob(item) for item in entries):
                # Allow a link to a parent folder containing exactly one skill package.
                candidates = [
                    item["path"]
                    for item in entries
                    if regular_blob(item)
                    and item.get("path", "").startswith(requested_prefix.rstrip("/") + "/")
                    and item.get("path", "").endswith("/SKILL.md")
                ]
                if len(candidates) != 1:
                    raise ValueError("提供的 GitHub 文件夹中没有唯一的 SKILL.md，请链接到具体 Skill 文件夹。")
                skill_path = candidates[0]
        elif not requested_prefix:
            candidates = [
                item["path"]
                for item in entries
                if regular_blob(item)
                and (item.get("path") == "SKILL.md" or item.get("path", "").endswith("/SKILL.md"))
            ]
            if not candidates:
                raise ValueError("该 GitHub 仓库中没有找到 SKILL.md。")
            if len(candidates) != 1:
                raise ValueError("仓库包含多个 Skill，请提供要安装的具体 Skill 文件夹链接。")
            skill_path = candidates[0]
        else:
            skill_path = f"{package_dir}/SKILL.md"

        package_dir = str(Path(skill_path).parent).replace("\\", "/")
        if package_dir == ".":
            package_dir = ""
        folder_name = package_dir.split("/")[-1] if package_dir else repository
        allowed_reference_dirs = {"docs", "references", "templates", "examples"}
        package_files = []
        for item in entries:
            path = str(item.get("path") or "")
            if not regular_blob(item) or item.get("size", 0) > MAX_GLOBAL_SKILL_PACKAGE_BYTES:
                continue
            if path != skill_path and not path.startswith(package_dir.rstrip("/") + "/"):
                continue
            relative = path[len(package_dir.rstrip("/") + "/") :] if package_dir else path
            relative_parts = relative.split("/")
            if path != skill_path and (
                len(relative_parts) < 2
                or relative_parts[0].lower() not in allowed_reference_dirs
                or Path(path).suffix.lower() not in {".md", ".txt"}
            ):
                continue
            package_files.append((path, relative, int(item.get("size") or 0)))
        if len(package_files) > MAX_GLOBAL_SKILL_PACKAGE_FILES:
            raise ValueError("Skill 包文本文件超过数量限制，最多导入 40 个文件。")
        total_size = sum(size for _, _, size in package_files)
        if total_size > MAX_GLOBAL_SKILL_PACKAGE_BYTES:
            raise ValueError("Skill 包文本文件总大小超过 200 KB 限制。")

        async def fetch_file(path: str) -> str:
            encoded_path = "/".join(quote(part, safe="") for part in path.split("/"))
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repository}/{quote(ref, safe='')}/{encoded_path}"
            try:
                return await asyncio.to_thread(self._github_request, raw_url, "text/plain")
            except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError) as exc:
                raise ValueError(f"无法下载 Skill 文本文件 {path}: {exc}") from exc

        downloaded = await asyncio.gather(*(fetch_file(path) for path, _, _ in package_files))
        files = {relative: body for (_, relative, _), body in zip(package_files, downloaded)}
        name, description, instructions = self._skill_metadata(files["SKILL.md"], folder_name)
        if name != folder_name:
            raise ValueError("SKILL.md frontmatter 的 name 必须与 GitHub Skill 文件夹名称一致。")
        if len(name) > SkillStore.MAX_NAME_CHARS:
            raise ValueError(f"Skill 名称不能超过 {SkillStore.MAX_NAME_CHARS} 个字符。")
        if any(skill["name"].casefold() == name.casefold() for skill in self.skills.list()):
            raise ValueError(f"Skill“{name}”已存在；为避免覆盖现有 Skill，未安装。")
        references = [
            f"\n\n---\nReference file: {path}\n---\n{body.strip()}"
            for path, body in sorted(files.items())
            if path != "SKILL.md" and body.strip()
        ]
        content = instructions + "".join(references)
        if len(content.encode("utf-8")) > SkillStore.MAX_CONTENT_BYTES:
            raise ValueError("Skill 指令和参考文档超过 200 KB 限制。")
        skill = await asyncio.to_thread(
            self.skills.create,
            {"name": name, "description": description, "content": content, "enabled": True},
        )
        return {
            "status": "installed",
            "skill": skill,
            "source": f"https://github.com/{owner}/{repository}/tree/{quote(ref, safe='')}/{package_dir}".rstrip("/"),
            "files_imported": len(files),
            "scripts_executed": False,
        }

    @staticmethod
    def _explicit_install_request(content: str) -> bool:
        text = str(content or "")
        request = text.casefold()
        install_requested = bool(re.search(
            r"安装|下载|添加|导入|\b(?:install|download|add|import)\b",
            request,
        ))
        negated = bool(re.search(
            r"(?:不要|无需|不需要|别|禁止|不能|无法|没法).{0,24}(?:安装|下载|添加|导入)|"
            r"(?:don't|dont|do not|no need to|cannot|can't|unable to).{0,32}(?:install|download|add|import)",
            request,
        ))
        plugin_requested = bool(re.search(r"插件|\bplugins?\b|\bsoftware\b|\bdependencies\b", request))
        source_present = bool(
            re.search(r"https://github\.com/[^\s<>\"']+", text, re.IGNORECASE)
            or re.search(r"(?<![A-Za-z0-9_.-])[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?![A-Za-z0-9_.-])", text)
        )
        return install_requested and source_present and not negated and not plugin_requested

    @staticmethod
    def _install_source_allowed(content: str, source: Any) -> bool:
        if not GlobalChatAgent._explicit_install_request(content):
            return False
        explicit_url = GlobalChatAgent._requested_skill_source(content)
        if not explicit_url:
            return False
        try:
            explicit_source = GlobalChatAgent._github_skill_source(explicit_url)
            proposed_source = GlobalChatAgent._github_skill_source(source)
        except (TypeError, ValueError):
            return False
        explicit_owner, explicit_repo, explicit_prefix, explicit_ref = explicit_source
        proposed_owner, proposed_repo, proposed_prefix, proposed_ref = proposed_source
        return (
            explicit_owner.casefold() == proposed_owner.casefold()
            and explicit_repo.casefold() == proposed_repo.casefold()
            and (not explicit_prefix or explicit_prefix == proposed_prefix)
            and (explicit_ref is None or explicit_ref == proposed_ref)
        )

    @classmethod
    def _requested_skill_source(cls, content: str) -> str | None:
        if not cls._explicit_install_request(content):
            return None
        match = re.search(r"https://github\.com/[^\s<>\"']+", str(content or ""), re.IGNORECASE)
        if match:
            source = match.group(0).rstrip(".,;:!?)\"]")
            try:
                cls._github_skill_source(source)
            except (TypeError, ValueError):
                return None
            return source
        shorthand = re.search(
            r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?![A-Za-z0-9_.-])",
            str(content or ""),
        )
        if not shorthand:
            return None
        return f"https://github.com/{shorthand.group(1)}/{shorthand.group(2)}"

    def _validated_skill_ids(self, skill_ids: Any) -> list[str]:
        if not isinstance(skill_ids, list):
            raise ValueError("skill_ids 必须是数组。")
        available = {skill["id"]: skill for skill in self.skills.list()}
        selected: list[str] = []
        for value in skill_ids:
            skill_id = str(value or "").strip()
            if not skill_id or skill_id in selected:
                continue
            skill = available.get(skill_id)
            if skill is None:
                raise ValueError("选择的 Skill 不存在。")
            if not skill.get("enabled"):
                raise ValueError("不能选择已停用的 Skill。")
            selected.append(skill_id)
        return selected

    def selected_skills(self, skill_ids: Any) -> list[dict[str, Any]]:
        available = {skill["id"]: skill for skill in self.skills.list()}
        return [available[skill_id] for skill_id in self._validated_skill_ids(skill_ids) if skill_id in available]

    async def chat(self, content: str, skill_ids: Any = None) -> dict[str, Any]:
        text = str(content or "").strip()
        if not text:
            raise ValueError("请输入对话内容。")
        if len(text) > MAX_MESSAGE_CHARS:
            raise ValueError("单条对话消息过大。")
        selected_skill_ids = self._validated_skill_ids([] if skill_ids is None else skill_ids)
        async with self._lock:
            self.store.append_global_message({"role": "user", "content": text, "skill_ids": selected_skill_ids})
            system_prompt = GLOBAL_CHAT_SYSTEM_PROMPT + _skill_prompt(self.selected_skills(selected_skill_ids))
            install_requested = self._explicit_install_request(text)
            tools = GLOBAL_CHAT_TOOL_DEFINITIONS if install_requested else []
            for _ in range(4):
                model_messages = self._model_messages(self.store.global_messages())
                reserved = ContextManager.estimate_tokens(system_prompt) + ContextManager.estimate_tokens(
                    _json_text(tools)
                )
                model_messages, _ = self.model.context.fallback(model_messages, reserved)
                response = await self.model._call_model(
                    model_messages,
                    tools=tools,
                    tool_choice="required" if install_requested else None,
                    system_prompt=system_prompt,
                )
                choice = (response.get("choices") or [{}])[0]
                assistant = choice.get("message") or {}
                calls = assistant.get("tool_calls") or []
                if not calls:
                    assistant_message = {
                        "role": "assistant",
                        "content": str(assistant.get("content") or "模型没有返回文本。"),
                    }
                    self.store.append_global_message(assistant_message)
                    return {"status": "ok", "message": assistant_message, "messages": self.store.global_messages()}

                self.store.append_global_message(
                    {"role": "assistant", "content": assistant.get("content"), "tool_calls": calls}
                )
                for index, call in enumerate(calls):
                    call_id = str(call.get("id") or f"global-skill-{index}")
                    try:
                        if not install_requested:
                            raise ValueError("只有在用户明确请求安装 Skill 时才允许调用此工具。")
                        tool_name, arguments = self.model._parse_tool_call(call)
                        if tool_name != "install_github_skill":
                            raise ValueError("全局对话不支持此工具。")
                        source = arguments.get("github_url")
                        if not self._install_source_allowed(text, source):
                            raise ValueError("工具参数必须对应用户明确提供的公开 GitHub Skill 来源。")
                        result = await self.install_github_skill(source)
                    except Exception as exc:
                        logger.warning("Global Skill tool failed: %s", exc)
                        result = {"error": str(exc)}
                    self.store.append_global_message(
                        {"role": "tool", "tool_call_id": call_id, "content": _json_text(result)}
                    )
            raise RuntimeError("全局对话的 Skill 工具调用超过步骤限制。")


class PersistentClineAgent:
    """Task-oriented runtime used by the ComfyUI panel.

        ``ClineAgent`` above remains as a compatibility adapter for the original
        endpoint. This runtime is the real application boundary: all task input,
        assistant output, tool results, and checkpoints are persisted. Tool calls
        execute directly; approval records are retained only for old databases
        and API compatibility.
    """

    def __init__(self, settings: SettingsStore):
        self.settings = settings
        self.store = TaskStore(TASK_DATABASE)
        self.skills = SkillStore(SKILL_DATABASE)
        self.checkpoints = CheckpointManager(STATE_DIRECTORY)
        settings_data = self.settings.load()
        try:
            max_context_tokens = _max_context_tokens(
                settings_data.get("max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS)
            )
        except ValueError:
            max_context_tokens = DEFAULT_MAX_CONTEXT_TOKENS
        try:
            compaction_threshold = _compaction_threshold_percent(
                settings_data.get("compaction_threshold_percent", DEFAULT_COMPACTION_THRESHOLD_PERCENT)
            )
        except ValueError:
            compaction_threshold = DEFAULT_COMPACTION_THRESHOLD_PERCENT
        self.context = ContextManager(max_context_tokens, compaction_threshold=compaction_threshold / 100)
        self.mcp = MCPManager(MCP_SETTINGS_PATH)
        self.browser = BrowserManager()
        self._lock = asyncio.Lock()
        self._running_tasks: dict[str, asyncio.Task[Any]] = {}
        # Cancellation interrupts the current async run without making the
        # persisted task terminal.  The marker is cleared after that run has
        # fully unwound, immediately before the task can accept the next turn.
        self._cancel_requested: set[str] = set()
        # A compaction is a model call. Keep its result for subsequent tool
        # steps and later user turns instead of summarizing the same prefix on
        # every request. The cache is intentionally process-local; the durable
        # transcript remains the source of truth after a restart.
        self._context_cache: dict[str, dict[str, Any]] = {}
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._process_lock = threading.RLock()

    def workspace(self, task_id: str | None = None) -> Workspace:
        if task_id:
            task = self.task(task_id)
            return Workspace(task["workspace"])
        configured = self.settings.load().get("workspace") or str(_default_workspace())
        return Workspace(configured)

    def task(self, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError("Task does not exist.")
        return task

    def _validated_skill_ids(self, skill_ids: Any) -> list[str]:
        """Validate message-selected IDs against the global enabled Skill library."""
        if not isinstance(skill_ids, list):
            raise ValueError("skill_ids 必须是数组。")
        available = {skill["id"]: skill for skill in self.skills.list()}
        selected: list[str] = []
        for value in skill_ids:
            skill_id = str(value or "").strip()
            if not skill_id or skill_id in selected:
                continue
            skill = available.get(skill_id)
            if skill is None:
                raise ValueError("选择的 Skill 不存在。")
            if not skill.get("enabled"):
                raise ValueError("不能选择已停用的 Skill。")
            selected.append(skill_id)
        return selected

    def selected_skills(self, task_id: str, skill_ids: Any = None) -> list[dict[str, Any]]:
        """Return globally managed, enabled Skills selected for one message."""
        self.task(task_id)
        if skill_ids is None:
            skill_ids = self._latest_message_skill_ids(task_id)
        available = {skill["id"]: skill for skill in self.skills.list()}
        if not isinstance(skill_ids, list):
            return []
        selected: list[dict[str, Any]] = []
        for value in skill_ids:
            skill_id = str(value or "").strip()
            skill = available.get(skill_id)
            if skill is None:
                continue
            if not skill.get("enabled"):
                continue
            selected.append(skill)
        return selected

    def _latest_message_skill_ids(self, task_id: str) -> list[str]:
        for message in reversed(self.store.messages(task_id)):
            if message.get("role") == "user":
                values = message.get("skill_ids", [])
                return values if isinstance(values, list) else []
        return []

    def update_task_skills(self, task_id: str, skill_ids: Any) -> dict[str, Any]:
        """Compatibility adapter: validate IDs without persisting them on a task."""
        self.task(task_id)
        self._validated_skill_ids(skill_ids)
        return self.task(task_id)

    def remove_skill(self, skill_id: str) -> dict[str, Any]:
        """Delete a Skill from the global library."""
        return self.skills.delete(skill_id)

    def system_prompt(self, task_id: str, mode: str | None = None, skill_ids: Any = None) -> str:
        task = self.task(task_id)
        task_mode = mode or task["mode"]
        return (
            SYSTEM_PROMPT
            + f"\nYou are currently in {task_mode.upper()} mode. In PLAN mode, do not modify files or run commands."
            + _skill_prompt(self.selected_skills(task_id, skill_ids))
        )

    def update_current_workflow(self, task_id: str, workflow: Any, source: str = "comfyui_canvas") -> dict[str, Any]:
        """Store a validated snapshot of the workflow currently visible in ComfyUI."""
        self.task(task_id)
        if not isinstance(workflow, dict):
            raise ValueError("当前工作流必须是 JSON 对象。")
        try:
            serialized = json.dumps(workflow, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("当前工作流必须只包含有效的 JSON 值。") from exc
        if len(serialized.encode("utf-8")) > MAX_CURRENT_WORKFLOW_BYTES:
            raise ValueError(f"当前工作流过大，不能超过 {MAX_CURRENT_WORKFLOW_BYTES // 1_000_000} MB。")
        return self.store.set_current_workflow(task_id, workflow, source)

    def _event(self, task_id: str, kind: str, payload: Any) -> dict[str, Any]:
        return self.store.append_event(task_id, kind, payload)

    def _check_cancelled(self, task_id: str) -> None:
        if task_id in self._cancel_requested:
            raise TaskCancelled()

    def _cancelled_result(self, task_id: str) -> dict[str, Any]:
        return {
            "status": "cancelled",
            "message": {"role": "system", "content": "本次运行已被强制取消，可以继续提问。"},
            "task": self.task(task_id),
        }

    def _register_running_task(self, task_id: str) -> None:
        current = asyncio.current_task()
        if current is not None:
            self._running_tasks[task_id] = current

    def _unregister_running_task(self, task_id: str) -> None:
        current = asyncio.current_task()
        if self._running_tasks.get(task_id) is current:
            self._running_tasks.pop(task_id, None)

    def _terminate_process(self, task_id: str) -> None:
        with self._process_lock:
            process = self._processes.get(task_id)
        if process is None or process.poll() is not None:
            return
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except (OSError, ProcessLookupError):
            pass

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        """Immediately stop the current agent operation without ending the task."""
        task = self.task(task_id)
        if task["status"] not in {"running", "waiting_approval"}:
            return task
        self._cancel_requested.add(task_id)
        cancelled = self.store.cancel_task(task_id)
        self._event(task_id, "task_cancelled", {"reason": "user", "message": "任务已被强制取消。"})
        self._terminate_process(task_id)
        running = self._running_tasks.get(task_id)
        current = asyncio.current_task()
        if running is not None and running is not current and not running.done():
            running.cancel()
        return cancelled

    def clear_history(self, task_id: str) -> dict[str, Any]:
        task = self.task(task_id)
        if task["status"] in {"running", "waiting_approval"} or task_id in self._running_tasks:
            raise ValueError("任务正在运行，请先停止任务后再清理历史对话。")
        cleared = self.store.clear_history(task_id)
        self._context_cache.pop(task_id, None)
        self.pending_cleanup(task_id)
        return cleared

    def pending_cleanup(self, task_id: str) -> None:
        """Remove in-memory approval state if a compatibility caller created it."""
        # Persistent approvals live in SQLite.  This hook also makes cleanup
        # explicit for future in-memory task extensions without exposing the
        # pending map as part of the HTTP API.
        return None

    def _comfyui_link_path(self, project: Path, folder_name: str) -> Path | None:
        """Return the custom_nodes link path for a project, if configured."""
        comfyui_nodes = self.settings.comfyui_node_directory()
        if comfyui_nodes is None:
            return None
        link = comfyui_nodes / _project_folder_name(folder_name)
        # A workspace configured directly as custom_nodes needs no self-link.
        if link == project.resolve():
            return None
        return link

    def _ensure_comfyui_link(self, project: Path | str, folder_name: str) -> tuple[str, bool]:
        """Ensure a project is available from ComfyUI's custom_nodes directory.

        The boolean reports whether this call created the symlink.  Keeping it
        separate lets task creation roll back only a link that it owns, while
        completion can safely repair a link that was removed after creation.
        """
        project_path = Path(project).expanduser().resolve()
        link = self._comfyui_link_path(project_path, folder_name)
        if link is None:
            return "", False

        if link.is_symlink() and link.resolve(strict=False) == project_path:
            return str(link), False
        if link.exists() or link.is_symlink():
            raise ProjectFolderExistsError(f"ComfyUI custom_nodes 中已存在：{link.name}。请换一个文件夹名称。")
        try:
            link.symlink_to(project_path, target_is_directory=True)
        except FileExistsError as exc:
            # Another process may have created the correct link between the
            # collision check and symlink creation. Never replace its target.
            if link.is_symlink() and link.resolve(strict=False) == project_path:
                return str(link), False
            raise ProjectFolderExistsError(f"ComfyUI custom_nodes 中已存在：{link.name}。请换一个文件夹名称。") from exc
        return str(link), True

    def _ensure_task_comfyui_link(self, task_id: str) -> str:
        """Repair the ComfyUI link when a task reaches a successful finish."""
        task = self.task(task_id)
        metadata = dict(task.get("metadata") or {})
        folder_name = metadata.get("project_folder") or Path(str(task["workspace"])).name
        link, _ = self._ensure_comfyui_link(task["workspace"], folder_name)
        if link and metadata.get("comfyui_link") != link:
            metadata["comfyui_link"] = link
            self.store.update_task(task_id, metadata=metadata)
        return link

    def _ensure_task_workflow(self, task_id: str) -> tuple[str, bool]:
        """Create the starter workflow if a task project does not have one."""
        task = self.task(task_id)
        metadata = dict(task.get("metadata") or {})
        folder_name = metadata.get("project_folder") or Path(str(task["workspace"])).name
        workflow, created = _write_workflow_if_missing(Path(task["workspace"]), folder_name)
        workflow_value = str(workflow)
        if metadata.get("workflow") != workflow_value:
            metadata["workflow"] = workflow_value
            self.store.update_task(task_id, metadata=metadata)
        return workflow_value, created

    def create_task(self, title: str = "新任务", mode: str = "act", folder: str | None = None) -> dict[str, Any]:
        root = self.workspace().root
        # Direct Python callers from older integrations did not provide a
        # folder. Keep them working with a unique folder, while the HTTP API
        # requires the user-facing folder field explicitly.
        folder_name = _project_folder_name(folder) if folder is not None else f"task-{uuid.uuid4().hex[:12]}"
        project = root / folder_name
        link = self._comfyui_link_path(project, folder_name)
        if link is not None and (link.exists() or link.is_symlink()):
            raise ProjectFolderExistsError(f"ComfyUI custom_nodes 中已存在：{folder_name}。请换一个文件夹名称。")
        try:
            project.mkdir()
        except FileExistsError as exc:
            raise ProjectFolderExistsError(f"项目文件夹已存在：{folder_name}。请换一个文件夹名称。") from exc
        link_created = False
        link_value = ""
        workflow_created = False
        try:
            link_value, link_created = self._ensure_comfyui_link(project, folder_name)
            workflow, workflow_created = _write_workflow_if_missing(project, folder_name)
            task = self.store.create_task(
                str(project.resolve()),
                title or folder_name,
                mode,
                {
                    "project_folder": folder_name,
                    "workspace_root": str(root),
                    "comfyui_link": link_value,
                    "workflow": str(workflow),
                },
            )
        except Exception:
            if link_created:
                try:
                    Path(link_value).unlink()
                except OSError:
                    pass
            # Do not leave an orphan directory if persistence fails before the
            # task becomes visible in history.
            try:
                if workflow_created:
                    (project / WORKFLOW_FILENAME).unlink()
                project.rmdir()
            except OSError:
                pass
            raise
        self._event(task["id"], "task_created", task)
        return task

    async def chat(self, task_id: str, content: str, context: str = "", skill_ids: Any = None) -> dict[str, Any]:
        self.task(task_id)
        if not str(content).strip():
            raise ValueError("Message cannot be empty.")
        selected_skill_ids = self._validated_skill_ids(skill_ids if skill_ids is not None else [])
        # The cancel endpoint returns before the cancelled coroutine has
        # necessarily finished unwinding.  Wait for that same task to release
        # its slot, then let this new turn use the same task and workspace.
        current = asyncio.current_task()
        while (running := self._running_tasks.get(task_id)) is not None and running is not current:
            await asyncio.sleep(0)
        self._cancel_requested.discard(task_id)
        pending = self.store.pending_approval(task_id)
        if pending:
            # Tasks created by an older version may still contain one paused
            # approval. Resume that action automatically so the task does not
            # remain blocked after upgrading to direct execution.
            return await self.confirm(pending["id"], True)
        message = {"role": "user", "content": str(content).strip(), "skill_ids": selected_skill_ids}
        if context:
            message["content"] += f"\n\n--- Additional workspace context ---\n{context}"
        self.store.append_message(task_id, message)
        self._event(task_id, "user_message", {"content": message["content"], "skill_ids": selected_skill_ids})
        self._register_running_task(task_id)
        try:
            return await self._run_loop(task_id)
        except TaskCancelled:
            return self._cancelled_result(task_id)
        except asyncio.CancelledError:
            if task_id in self._cancel_requested:
                return self._cancelled_result(task_id)
            raise
        finally:
            self._unregister_running_task(task_id)
            self._cancel_requested.discard(task_id)

    async def confirm(self, approval_id: str, approved: bool) -> dict[str, Any]:
        approval = self.store.approval(approval_id)
        if not approval or approval["status"] != "pending":
            raise ValueError("This confirmation has expired or was already resolved.")
        task_id = approval["task_id"]
        self.task(task_id)
        self.store.resolve_approval(approval_id, approved)
        self._event(task_id, "approval_resolved", {"id": approval_id, "approved": bool(approved)})
        self._register_running_task(task_id)
        try:
            self._check_cancelled(task_id)
            if approved:
                try:
                    result = await self._execute_tool(task_id, approval["tool_name"], approval["arguments"])
                except Exception as exc:  # Tool errors are part of the agent transcript.
                    result = {"error": str(exc)}
            else:
                result = "User denied this action. Do not retry it unless the user asks again."
            self._check_cancelled(task_id)
            call = approval["call_payload"]
            tool_message = {
                "role": "tool",
                "tool_call_id": call.get("id", f"cline-tool-{approval['call_index']}"),
                "content": _json_text(result),
            }
            self.store.append_message(task_id, tool_message)
            self._event(task_id, "tool_result", {"tool": approval["tool_name"], "result": result})
            await self._checkpoint_after_tool(task_id, approval["tool_name"])
            messages = self.store.messages(task_id)
            calls: list[dict[str, Any]] = []
            for message in reversed(messages):
                if message.get("role") == "assistant" and message.get("tool_calls"):
                    calls = message["tool_calls"]
                    break
            next_result = await self._process_tool_calls(task_id, calls, int(approval["call_index"]) + 1, self.task(task_id)["mode"])
            if next_result is not None:
                return next_result
            return await self._run_loop(task_id)
        except TaskCancelled:
            return self._cancelled_result(task_id)
        except asyncio.CancelledError:
            if task_id in self._cancel_requested:
                return self._cancelled_result(task_id)
            raise
        finally:
            self._unregister_running_task(task_id)
            self._cancel_requested.discard(task_id)

    async def _tool_definitions(self, mode: str) -> list[dict[str, Any]]:
        base_definitions = TOOL_DEFINITIONS + BROWSER_TOOL_DEFINITIONS + LIVE_WORKFLOW_TOOL_DEFINITIONS
        if mode == "plan":
            definitions = [
                definition
                for definition in base_definitions
                if definition["function"]["name"] in PLAN_TOOL_NAMES
            ]
        else:
            definitions = base_definitions
        for group in await self.mcp.list_tools():
            if mode == "plan":
                continue
            for tool in group.get("tools", []):
                definitions.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool["name"],
                            "description": tool.get("description") or f"Call MCP tool {tool.get('original_name')}",
                            "parameters": tool.get("inputSchema") or {"type": "object"},
                        },
                    }
                )
        return definitions

    @staticmethod
    def _messages_for_model(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        clean: list[dict[str, Any]] = []
        for message in messages:
            value = {
                key: value
                for key, value in message.items()
                if key not in {"seq", "created_at", "skill_ids"}
            }
            if value.get("role") in {"user", "assistant", "tool"}:
                clean.append(value)
        return clean

    async def _compact_messages(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        mode: str,
        task_id: str | None = None,
        skill_ids: Any = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        system_prompt = self.system_prompt(task_id, mode, skill_ids) if task_id else (
            SYSTEM_PROMPT + f"\nYou are currently in {mode.upper()} mode. In PLAN mode, do not modify files or run commands."
        )
        settings_data = self.settings.load()
        try:
            configured = _max_context_tokens(
                settings_data.get("max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS)
            )
            threshold = _compaction_threshold_percent(
                settings_data.get("compaction_threshold_percent", DEFAULT_COMPACTION_THRESHOLD_PERCENT)
            )
        except ValueError:
            configured = DEFAULT_MAX_CONTEXT_TOKENS
            threshold = DEFAULT_COMPACTION_THRESHOLD_PERCENT
        if self.context.max_tokens != configured or self.context.compaction_threshold != threshold / 100:
            self.context = ContextManager(
                configured,
                keep_messages=self.context.keep_messages,
                compaction_threshold=threshold / 100,
            )
        reserved = ContextManager.estimate_tokens(system_prompt) + ContextManager.estimate_tokens(_json_text(tools))
        if not self.context.needs_compaction(messages, reserved):
            return messages, False
        summary_input = self.context.bounded_summary_messages(messages)
        if not summary_input:
            return self.context.fallback(messages, reserved)
        summary_input.append(
            {
                "role": "user",
                "content": "Summarize the preceding conversation now. Preserve concrete task state and unresolved work.",
            }
        )
        try:
            summary_system_prompt = COMPACTION_SYSTEM_PROMPT + (_skill_prompt(self.selected_skills(task_id, skill_ids)) if task_id else "")
            response = await self._call_model(
                summary_input,
                [],
                mode,
                system_prompt=summary_system_prompt,
                max_tokens=2048,
            )
            summary = ((response.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            if not str(summary).strip():
                raise RuntimeError("模型摘要响应为空。")
            compacted, changed = self.context.with_summary(messages, summary, reserved)
            if changed:
                return compacted, True
            return messages, False
        except Exception as exc:
            logger.warning("Context summary request failed; using local fallback: %s", exc)
            return self.context.fallback(messages, reserved)

    async def context_status(self, task_id: str, skill_ids: Any = None) -> dict[str, Any]:
        """Return the estimated prompt size using the current message Skills."""
        task = self.task(task_id)
        settings_data = self.settings.load()
        try:
            max_context_tokens = _max_context_tokens(
                settings_data.get("max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS)
            )
        except ValueError:
            max_context_tokens = DEFAULT_MAX_CONTEXT_TOKENS
        try:
            threshold = _compaction_threshold_percent(
                settings_data.get("compaction_threshold_percent", DEFAULT_COMPACTION_THRESHOLD_PERCENT)
            )
        except ValueError:
            threshold = DEFAULT_COMPACTION_THRESHOLD_PERCENT

        stored_messages = self.store.messages(task_id)
        model_messages = self._cached_context(task_id, stored_messages)
        if model_messages is None:
            model_messages = self._messages_for_model(stored_messages)
        tools = await self._tool_definitions(task["mode"])
        system_prompt = self.system_prompt(task_id, skill_ids=skill_ids)
        system_tokens = ContextManager.estimate_tokens(system_prompt)
        tools_tokens = ContextManager.estimate_tokens(_json_text(tools))
        message_tokens = ContextManager.estimate_messages_tokens(model_messages)
        prompt_tokens = message_tokens + system_tokens + tools_tokens
        prompt_chars = len(system_prompt) + len(_json_text(tools)) + sum(
            len(_json_text(message)) for message in model_messages
        )
        prompt_percent = prompt_tokens / max_context_tokens * 100
        return {
            "task_id": task_id,
            "message_count": len(model_messages),
            "prompt_chars": prompt_chars,
            "prompt_tokens": prompt_tokens,
            "message_tokens": message_tokens,
            "system_tokens": system_tokens,
            "tools_tokens": tools_tokens,
            "max_context_tokens": max_context_tokens,
            "prompt_percent": round(prompt_percent, 1),
            "compaction_threshold_percent": threshold,
            "above_compaction_threshold": prompt_percent > threshold,
        }

    def _cached_context(
        self,
        task_id: str,
        stored_messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]] | None:
        cached = self._context_cache.get(task_id)
        if not cached:
            return None
        source_seq = int(cached.get("source_seq", -1))
        sequence = [int(message.get("seq", -1)) for message in stored_messages]
        if source_seq < 0 or source_seq not in sequence:
            self._context_cache.pop(task_id, None)
            return None
        source_index = sequence.index(source_seq)
        new_messages = self._messages_for_model(stored_messages[source_index + 1 :])
        return [dict(message) for message in cached.get("messages", [])] + new_messages

    def _remember_context(
        self,
        task_id: str,
        stored_messages: list[dict[str, Any]],
        model_messages: list[dict[str, Any]],
        compacted: bool,
    ) -> None:
        if not compacted or not stored_messages:
            return
        self._context_cache[task_id] = {
            "source_seq": int(stored_messages[-1].get("seq", -1)),
            "messages": [dict(message) for message in model_messages],
        }

    async def _run_loop(self, task_id: str) -> dict[str, Any]:
        async with self._lock:
            task = self.task(task_id)
            task = self.store.start_task(task_id)
            self._event(task_id, "task_started", {"mode": task["mode"]})
            try:
                for _ in range(MAX_AGENT_STEPS):
                    self._check_cancelled(task_id)
                    stored_messages = self.store.messages(task_id)
                    model_messages = self._cached_context(task_id, stored_messages)
                    if model_messages is None:
                        model_messages = self._messages_for_model(stored_messages)
                    active_skill_ids = self._latest_message_skill_ids(task_id)
                    tools = await self._tool_definitions(task["mode"])
                    model_messages, compacted = await self._compact_messages(
                        model_messages, tools, task["mode"], task_id, active_skill_ids
                    )
                    if compacted:
                        self._event(task_id, "context_compacted", {"message_count": len(model_messages)})
                        self._remember_context(task_id, stored_messages, model_messages, compacted)
                    response = await self._call_model(
                        model_messages,
                        tools,
                        task["mode"],
                        system_prompt=self.system_prompt(task_id, skill_ids=active_skill_ids),
                    )
                    self._check_cancelled(task_id)
                    choice = (response.get("choices") or [{}])[0]
                    assistant = choice.get("message") or {}
                    calls = assistant.get("tool_calls") or []
                    assistant_message = {
                        "role": "assistant",
                        "content": assistant.get("content") or "",
                    }
                    if calls:
                        assistant_message["tool_calls"] = calls
                    self.store.append_message(task_id, assistant_message)
                    if assistant_message["content"]:
                        self._event(task_id, "assistant_text", {"text": assistant_message["content"]})
                    if not calls:
                        comfyui_link = self._ensure_task_comfyui_link(task_id)
                        workflow, workflow_created = self._ensure_task_workflow(task_id)
                        result = {
                            "status": "ok",
                            "message": {"role": "assistant", "content": assistant_message["content"] or "模型没有返回文本。"},
                            "comfyui_link": comfyui_link,
                            "workflow": workflow,
                            "workflow_created": workflow_created,
                        }
                        self.store.update_task(task_id, status="idle")
                        self._event(task_id, "task_finished", result)
                        return result
                    pending = await self._process_tool_calls(task_id, calls, 0, task["mode"])
                    if pending is not None:
                        return pending
                raise RuntimeError("The assistant reached the tool-call step limit. Please continue the task.")
            except TaskCancelled:
                return self._cancelled_result(task_id)
            except Exception as exc:
                self.store.update_task(task_id, status="idle")
                self._event(task_id, "task_failed", {"error": str(exc)[:2_000]})
                raise
            finally:
                # Cancellation and other BaseException paths must not leave a
                # task permanently marked as running after ComfyUI resumes.
                current = self.task(task_id)
                if current["status"] == "running":
                    self.store.update_task(task_id, status="idle")

    async def _process_tool_calls(
        self,
        task_id: str,
        calls: list[dict[str, Any]],
        start_index: int,
        mode: str,
    ) -> dict[str, Any] | None:
        """Execute a model's calls in order without interactive approvals."""

        for index in range(start_index, len(calls)):
            self._check_cancelled(task_id)
            call = calls[index]
            name, arguments = ClineAgent._parse_tool_call(call)
            if mode == "plan" and name not in PLAN_TOOL_NAMES:
                tool_result = {"error": f"工具 {name} 在 PLAN 模式下不可用。"}
            else:
                try:
                    tool_result = await self._execute_tool(task_id, name, arguments)
                except Exception as exc:
                    tool_result = {"error": str(exc)}
            self._check_cancelled(task_id)
            tool_message = {
                "role": "tool",
                "tool_call_id": call.get("id", f"cline-tool-{index}"),
                "content": _json_text(tool_result),
            }
            self.store.append_message(task_id, tool_message)
            self._event(task_id, "tool_result", {"tool": name, "result": tool_result})
            await self._checkpoint_after_tool(task_id, name)
        return None

    @staticmethod
    def _validate_command(command: str, timeout: Any) -> int:
        if not command or len(command) > 2_000:
            raise ValueError("Command must be between 1 and 2,000 characters.")
        if "\x00" in command or BLOCKED_COMMANDS.search(command):
            raise ValueError("This command is blocked by the local safety policy.")
        try:
            return min(120, max(1, int(timeout)))
        except (TypeError, ValueError):
            return 120

    def _run_command_process(self, task_id: str, command: str, timeout: Any, workspace: Workspace) -> dict[str, Any]:
        timeout_seconds = self._validate_command(command, timeout)
        self._check_cancelled(task_id)
        process = subprocess.Popen(
            command,
            cwd=workspace.root,
            shell=True,  # noqa: S602 - command execution is bounded by local policy
            executable=os.environ.get("SHELL", "/bin/sh"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=(os.name != "nt"),
        )
        with self._process_lock:
            self._processes[task_id] = process
        if task_id in self._cancel_requested:
            self._terminate_process(task_id)
        try:
            try:
                stdout, stderr = process.communicate(timeout=timeout_seconds)
                return {"exit_code": process.returncode, "output": (stdout + stderr)[-MAX_COMMAND_OUTPUT:]}
            except subprocess.TimeoutExpired as exc:
                self._terminate_process(task_id)
                stdout, stderr = process.communicate()
                partial_stdout = exc.stdout or stdout or ""
                partial_stderr = exc.stderr or stderr or ""
                if isinstance(partial_stdout, bytes):
                    partial_stdout = partial_stdout.decode(errors="replace")
                if isinstance(partial_stderr, bytes):
                    partial_stderr = partial_stderr.decode(errors="replace")
                return {"exit_code": None, "timed_out": True, "output": (partial_stdout + partial_stderr)[-MAX_COMMAND_OUTPUT:]}
        finally:
            with self._process_lock:
                if self._processes.get(task_id) is process:
                    self._processes.pop(task_id, None)

    async def _run_command(self, task_id: str, command: str, timeout: Any, workspace: Workspace) -> dict[str, Any]:
        return await asyncio.to_thread(self._run_command_process, task_id, command, timeout, workspace)

    async def _call_model(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        mode: str,
        system_prompt: str | None = None,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        data = self.settings.load()
        api_key = self.settings.api_key()
        model = str(data.get("model") or DEFAULT_MODEL).strip()
        if not model:
            raise ValueError("No model configured. Open Cline settings and enter a model name.")
        base_url = str(data.get("api_base_url") or DEFAULT_API_BASE_URL).rstrip("/")
        endpoint = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        prompt = system_prompt or (SYSTEM_PROMPT + f"\nYou are currently in {mode.upper()} mode. In PLAN mode, do not modify files or run commands.")
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": prompt}, *messages],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        # Use aiohttp here rather than wrapping urllib in a worker thread. A
        # cancelled Agent task can then cancel the provider request itself,
        # instead of leaving a blocking network thread alive until timeout.
        try:
            from aiohttp import ClientError, ClientSession, ClientTimeout

            timeout = ClientTimeout(total=120)
            async with ClientSession(timeout=timeout) as session:
                async with session.post(endpoint, json=payload, headers=headers) as response:
                    if response.status >= 400:
                        detail = (await response.text())[:2_000]
                        raise RuntimeError(f"模型 API 返回 HTTP {response.status}: {detail}")
                    body = await response.json(content_type=None)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("模型 API 请求超时。") from exc
        except ClientError as exc:
            raise RuntimeError(f"无法连接模型 API: {exc}") from exc
        if not isinstance(body, dict) or not body.get("choices"):
            raise RuntimeError(f"模型 API 返回了无法识别的响应: {_json_text(body, 2_000)}")
        return body

    def _requires_confirmation(self, mode: str, name: str) -> bool:
        """Return whether a tool should pause for user approval.

        Kept as a compatibility hook for integrations that call this method.
        The current runtime executes every tool exposed for the task mode
        directly, while mode-specific tool exposure remains the safety boundary.
        """
        return False

    def _confirmation(self, action_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "run_command":
            description = f"在工作区执行命令：{arguments.get('command', '')}"
        elif name.startswith("browser_"):
            description = f"使用浏览器操作：{name}"
        elif name.startswith("mcp__"):
            description = f"调用 MCP 工具：{name}"
        elif name == "apply_patch":
            description = f"修改文件：{arguments.get('path', '')}（应用补丁）"
        else:
            description = f"写入文件：{arguments.get('path', '')}"
        return {
            "id": action_id,
            "tool": name,
            "description": description,
            "arguments": {
                key: (value[:2_000] + "...[truncated]" if isinstance(value, str) and len(value) > 2_000 else value)
                for key, value in arguments.items()
            },
        }

    async def _execute_tool(self, task_id: str, name: str, arguments: dict[str, Any]) -> Any:
        if name == "get_current_workflow":
            current = self.store.current_workflow(task_id)
            if current is None:
                return {
                    "available": False,
                    "message": "当前 ComfyUI 画布尚未同步到 Cline。请用户在 ComfyUI 中打开 Cline 后重试，或点击“读取当前画布”。",
                }
            return {"available": True, **current}
        workspace = self.workspace(task_id)
        if name == "list_files":
            return workspace.list_files(arguments.get("path", "."), bool(arguments.get("recursive", False)))
        if name == "read_file":
            return {"path": arguments.get("path"), "content": workspace.read_file(arguments["path"])}
        if name == "search_files":
            return workspace.search_files(arguments["query"], arguments.get("path", "."))
        if name == "write_file":
            return {"written": workspace.write_file(arguments["path"], arguments.get("content", ""))}
        if name == "apply_patch":
            return {"patched": workspace.apply_patch(arguments["path"], arguments["patch"])}
        if name == "run_command":
            return await self._run_command(task_id, str(arguments.get("command") or ""), arguments.get("timeout", 120), workspace)
        if name == "browser_navigate":
            return await self.browser.navigate(task_id, str(arguments["url"]))
        if name == "browser_content":
            return await self.browser.content(task_id, str(arguments.get("selector") or ""))
        if name == "browser_screenshot":
            destination = workspace.resolve(str(arguments.get("path") or f".cline-browser/{task_id}.png"))
            return await self.browser.screenshot(task_id, destination)
        if name == "browser_click":
            return await self.browser.click(task_id, str(arguments["selector"]))
        if name == "browser_fill":
            return await self.browser.fill(task_id, str(arguments["selector"]), str(arguments["value"]))
        if name.startswith("mcp__"):
            return await self.mcp.call_tool(name, arguments)
        raise ValueError(f"Unknown assistant tool: {name}")

    async def _checkpoint_after_tool(self, task_id: str, tool_name: str) -> None:
        if tool_name in {"list_files", "read_file", "search_files", "browser_content"}:
            return
        task = self.task(task_id)
        backend, reference, metadata = self.checkpoints.create(task_id, Path(task["workspace"]), f"After {tool_name}")
        messages = self.store.messages(task_id)
        checkpoint = self.store.add_checkpoint(task_id, f"After {tool_name}", reference, backend, messages[-1].get("seq", 0) if messages else 0, metadata)
        self._event(task_id, "checkpoint_created", checkpoint)

    async def restore_checkpoint(self, checkpoint_id: str, restore_task: bool = False, restore_files: bool = True) -> dict[str, Any]:
        checkpoint = self.store.checkpoint(checkpoint_id)
        if not checkpoint:
            raise ValueError("Checkpoint does not exist.")
        task = self.task(checkpoint["task_id"])
        if restore_files:
            self.checkpoints.restore(task["id"], Path(task["workspace"]), checkpoint["backend"], checkpoint["reference"])
        if restore_task:
            self.store.truncate_messages(task["id"], checkpoint["message_seq"])
        self._event(task["id"], "checkpoint_restored", {"checkpoint": checkpoint_id, "restore_files": restore_files, "restore_task": restore_task})
        return {"status": "ok", "task": self.task(task["id"]), "checkpoint": checkpoint}


_AGENT: ClineAgent | None = None
_TASK_AGENT: PersistentClineAgent | None = None
_GLOBAL_CHAT_AGENT: GlobalChatAgent | None = None
_REGISTERED = False


def _agent() -> ClineAgent:
    global _AGENT
    if _AGENT is None:
        _AGENT = ClineAgent(SettingsStore())
    return _AGENT


def _task_agent() -> PersistentClineAgent:
    global _TASK_AGENT
    if _TASK_AGENT is None:
        _TASK_AGENT = PersistentClineAgent(SettingsStore())
    return _TASK_AGENT


def _global_chat_agent() -> GlobalChatAgent:
    global _GLOBAL_CHAT_AGENT
    if _GLOBAL_CHAT_AGENT is None:
        _GLOBAL_CHAT_AGENT = GlobalChatAgent(SettingsStore())
    return _GLOBAL_CHAT_AGENT


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


def register_routes(prompt_server: Any) -> None:
    """Register routes once when ComfyUI imports this custom node."""

    global _REGISTERED
    if _REGISTERED:
        return
    routes = prompt_server.routes

    @routes.get("/cline/health")
    async def cline_health(request):
        return _response({"name": PLUGIN_NAME, "status": "ok", "version": "0.2.0", "features": ["tasks", "global_chat", "cancellation", "checkpoints", "mcp", "browser", "live_workflow"]})

    @routes.get("/cline/tasks")
    async def cline_tasks(request):
        try:
            return _response({"tasks": _task_agent().store.list_tasks(int(request.query.get("limit", "100")))})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.get("/cline/global-chat")
    async def cline_global_chat(request):
        try:
            return _response({"messages": _global_chat_agent().store.global_messages()})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.post("/cline/global-chat/messages")
    async def cline_global_chat_message(request):
        try:
            values = await _body(request)
            return _response(await _global_chat_agent().chat(values.get("content", ""), values.get("skill_ids", [])))
        except (ValueError, RuntimeError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.delete("/cline/global-chat/history")
    async def cline_clear_global_chat(request):
        try:
            _global_chat_agent().store.clear_global_messages()
            return _response({"messages": []})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.post("/cline/tasks")
    async def cline_create_task(request):
        try:
            values = await _body(request)
            if "folder" not in values:
                raise ValueError("请输入项目文件夹名称。")
            return _response(
                _task_agent().create_task(
                    str(values.get("title") or values.get("folder") or "新任务"),
                    str(values.get("mode") or "act"),
                    str(values.get("folder") or ""),
                )
            )
        except ProjectFolderExistsError as exc:
            return _response({"error": str(exc), "code": "folder_exists"}, status=409)
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.get("/cline/tasks/{task_id}")
    async def cline_task(request):
        try:
            agent = _task_agent()
            task = agent.task(request.match_info["task_id"])
            return _response({"task": task, "messages": agent.store.messages(task["id"]), "pending_approval": agent.store.pending_approval(task["id"])})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=404)

    @routes.get("/cline/tasks/{task_id}/workflow")
    async def cline_task_workflow(request):
        try:
            agent = _task_agent()
            task_id = request.match_info["task_id"]
            workflow_path, created = agent._ensure_task_workflow(task_id)
            task = agent.task(task_id)
            content = agent.workspace(task_id).read_file(WORKFLOW_FILENAME)
            return _response({
                "path": workflow_path,
                "content": content,
                "created": created,
                "current": agent.store.current_workflow(task_id),
                "task": task,
            })
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=404)

    @routes.get("/cline/tasks/{task_id}/current-workflow")
    async def cline_current_workflow(request):
        try:
            agent = _task_agent()
            task_id = request.match_info["task_id"]
            agent.task(task_id)
            return _response({"current": agent.store.current_workflow(task_id), "task": agent.task(task_id)})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=404)

    @routes.get("/cline/tasks/{task_id}/context")
    async def cline_task_context(request):
        try:
            raw_skill_ids = request.query.get("skill_ids", "[]")
            skill_ids = json.loads(raw_skill_ids)
            return _response(await _task_agent().context_status(request.match_info["task_id"], skill_ids))
        except (ValueError, OSError, RuntimeError, MCPProtocolError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.post("/cline/tasks/{task_id}/current-workflow")
    async def cline_update_current_workflow(request):
        try:
            if request.content_length and request.content_length > MAX_CURRENT_WORKFLOW_BYTES + 10_000:
                return _response({"error": f"当前工作流请求过大，不能超过 {MAX_CURRENT_WORKFLOW_BYTES // 1_000_000} MB。"}, status=413)
            values = await _body(request)
            workflow = values.get("workflow")
            source = str(values.get("source") or "comfyui_canvas")
            current = _task_agent().update_current_workflow(request.match_info["task_id"], workflow, source)
            return _response({"current": current, "task": _task_agent().task(request.match_info["task_id"])})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.patch("/cline/tasks/{task_id}")
    async def cline_update_task(request):
        try:
            values = await _body(request)
            return _response(_task_agent().store.update_task(request.match_info["task_id"], **values))
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.post("/cline/tasks/{task_id}/cancel")
    async def cline_cancel_task(request):
        try:
            return _response(_task_agent().cancel_task(request.match_info["task_id"]))
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=404)

    @routes.delete("/cline/tasks/{task_id}/history")
    async def cline_clear_task_history(request):
        try:
            task_id = request.match_info["task_id"]
            task = _task_agent().clear_history(task_id)
            return _response({"task": task, "messages": [], "cleared": True})
        except ValueError as exc:
            return _response({"error": str(exc)}, status=400)
        except OSError as exc:
            return _response({"error": str(exc)}, status=404)

    @routes.get("/cline/tasks/{task_id}/events")
    async def cline_task_events(request):
        try:
            task_id = request.match_info["task_id"]
            _task_agent().task(task_id)
            return _response({"events": _task_agent().store.events(task_id, int(request.query.get("after", "-1")))})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=404)

    @routes.get("/cline/tasks/{task_id}/skills")
    async def cline_task_skills(request):
        """Compatibility response for clients that still request task Skills.

        Skills are global and selected per message now, so this route never
        exposes or persists a task-level selection.
        """
        try:
            agent = _task_agent()
            task_id = request.match_info["task_id"]
            task = agent.task(task_id)
            return _response({"task": task, "skills": agent.skills.list(), "selected_ids": [], "selected": []})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=404)

    @routes.post("/cline/tasks/{task_id}/skills")
    async def cline_update_task_skills(request):
        """Validate legacy task-Skill updates without persisting them."""
        try:
            values = await _body(request)
            agent = _task_agent()
            task_id = request.match_info["task_id"]
            task = agent.task(task_id)
            agent._validated_skill_ids(values.get("skill_ids", []))
            return _response({"task": task, "skills": agent.skills.list(), "selected_ids": [], "selected": []})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.post("/cline/tasks/{task_id}/messages")
    async def cline_task_message(request):
        try:
            values = await _body(request)
            result = await _task_agent().chat(
                request.match_info["task_id"],
                str(values.get("content") or ""),
                str(values.get("context") or ""),
                values.get("skill_ids", []),
            )
            return _response(result)
        except (ValueError, RuntimeError, OSError, MCPProtocolError, BrowserUnavailable) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.post("/cline/tasks/{task_id}/mode")
    async def cline_task_mode(request):
        try:
            values = await _body(request)
            return _response(_task_agent().store.update_task(request.match_info["task_id"], mode=str(values.get("mode") or "act")))
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.get("/cline/tasks/{task_id}/checkpoints")
    async def cline_task_checkpoints(request):
        try:
            task_id = request.match_info["task_id"]
            _task_agent().task(task_id)
            return _response({"checkpoints": _task_agent().store.checkpoints(task_id)})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=404)

    @routes.get("/cline/checkpoints/{checkpoint_id}/diff")
    async def cline_checkpoint_diff(request):
        try:
            agent = _task_agent()
            checkpoint = agent.store.checkpoint(request.match_info["checkpoint_id"])
            if not checkpoint:
                raise ValueError("Checkpoint does not exist.")
            task = agent.task(checkpoint["task_id"])
            diff = agent.checkpoints.compare(task["id"], Path(task["workspace"]), checkpoint["backend"], checkpoint["reference"])
            return _response({"diff": diff})
        except (ValueError, OSError, RuntimeError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.post("/cline/checkpoints/{checkpoint_id}/restore")
    async def cline_checkpoint_restore(request):
        try:
            values = await _body(request)
            return _response(await _task_agent().restore_checkpoint(request.match_info["checkpoint_id"], bool(values.get("restore_task")), bool(values.get("restore_files", True))))
        except (ValueError, OSError, RuntimeError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.get("/cline/mcp")
    async def cline_mcp(request):
        return _response({"servers": _task_agent().mcp.public()})

    @routes.post("/cline/mcp")
    async def cline_update_mcp(request):
        try:
            values = await _body(request)
            return _response({"servers": _task_agent().mcp.update(values)})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.get("/cline/skills")
    async def cline_skills(request):
        return _response({"skills": _task_agent().skills.list()})

    @routes.post("/cline/skills")
    async def cline_create_skill(request):
        try:
            values = await _body(request)
            return _response({"skill": _task_agent().skills.create(values)})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.patch("/cline/skills/{skill_id}")
    async def cline_update_skill(request):
        try:
            values = await _body(request)
            return _response({"skill": _task_agent().skills.update(request.match_info["skill_id"], values)})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.delete("/cline/skills/{skill_id}")
    async def cline_delete_skill(request):
        try:
            return _response({"skill": _task_agent().remove_skill(request.match_info["skill_id"])})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=404)

    @routes.post("/cline/skills/import")
    async def cline_import_skills(request):
        try:
            values = await _body(request)
            imported = _task_agent().skills.import_items(values.get("skills", values))
            return _response({"skills": _task_agent().skills.list(), "imported": imported})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.get("/cline/skills/export")
    async def cline_export_skills(request):
        return _response({"version": 1, "skills": _task_agent().skills.export_items()})

    @routes.get("/cline/mcp/tools")
    async def cline_mcp_tools(request):
        try:
            return _response({"groups": await _task_agent().mcp.list_tools()})
        except (ValueError, RuntimeError, OSError, MCPProtocolError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.get("/cline/settings")
    async def cline_settings(request):
        return _response(_agent().settings.public())

    @routes.post("/cline/settings")
    async def cline_update_settings(request):
        try:
            values = await _body(request)
            return _response(_agent().settings.update(values))
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.get("/cline/files")
    async def cline_files(request):
        try:
            path = request.query.get("path", ".")
            recursive = request.query.get("recursive", "false").lower() == "true"
            task_id = request.query.get("task_id")
            workspace = _task_agent().workspace(task_id) if task_id else _agent().workspace()
            return _response(
                {
                    "workspace": str(workspace.root),
                    "path": path,
                    "entries": workspace.list_files(path, recursive),
                }
            )
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.get("/cline/file")
    async def cline_file(request):
        try:
            path = request.query.get("path", "")
            task_id = request.query.get("task_id")
            workspace = _task_agent().workspace(task_id) if task_id else _agent().workspace()
            return _response({"path": path, "content": workspace.read_file(path)})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.post("/cline/chat")
    async def cline_chat(request):
        try:
            values = await _body(request)
            result = await _agent().chat(values.get("messages", []), str(values.get("context") or ""))
            return _response(result)
        except (ValueError, RuntimeError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.post("/cline/confirm")
    async def cline_confirm(request):
        try:
            values = await _body(request)
            action_id = str(values.get("id") or "")
            if not action_id:
                raise ValueError("Confirmation id is required.")
            result = await _agent().confirm(action_id, bool(values.get("approved")))
            return _response(result)
        except (ValueError, RuntimeError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.post("/cline/approvals/{approval_id}")
    async def cline_task_confirm(request):
        try:
            values = await _body(request)
            result = await _task_agent().confirm(request.match_info["approval_id"], bool(values.get("approved")))
            return _response(result)
        except (ValueError, RuntimeError, OSError, MCPProtocolError, BrowserUnavailable) as exc:
            return _response({"error": str(exc)}, status=400)

    _REGISTERED = True


__all__ = [
    "ClineAgent",
    "GlobalChatAgent",
    "SettingsStore",
    "Workspace",
    "apply_unified_patch",
    "register_routes",
]