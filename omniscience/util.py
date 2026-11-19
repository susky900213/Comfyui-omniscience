"""Shared low-level helpers: validation, naming, workspace defaults."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from . import constants, paths


def now() -> float:
    return time.time()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_text(value: Any, limit: int = constants.MAX_MESSAGE_CHARS) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2) if not isinstance(value, str) else value
    if len(text) > limit:
        return truncate_middle(text, limit)
    return text


def truncate_middle(text: str, limit: int) -> str:
    """Keep the head and the tail of oversized output.

    Test failures and tracebacks usually live at the end of command output,
    so tail-only truncation hides the most actionable part (DESIGN 5.4).
    """
    if len(text) <= limit:
        return text
    marker = f"\n...[middle truncated, {len(text)} characters total]...\n"
    keep = max(0, limit - len(marker))
    head = keep // 2
    tail = keep - head
    return text[:head] + marker + text[len(text) - tail:] if tail else text[:head] + marker


def safe_relative(path: str) -> str:
    """Normalize a user-supplied path without allowing absolute traversal."""

    value = str(path or ".").replace("\\", "/")
    if value.startswith("/") or re.match(r"^[A-Za-z]:/", value):
        raise ValueError("Absolute paths are not allowed; use a path relative to the workspace.")
    normalized = os.path.normpath(value)
    if normalized == ".." or normalized.startswith(f"..{os.sep}"):
        raise ValueError("Path must stay inside the workspace.")
    return "." if normalized in {"", "."} else normalized


class TaskCancelled(Exception):
    """Internal control-flow exception for a user-cancelled run."""


class ProjectFolderExistsError(ValueError):
    """Raised when a new task would overwrite an existing node directory."""


def project_folder_name(value: Any) -> str:
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


def workflow_template(project_name: str) -> dict[str, Any]:
    """Build a portable ComfyUI UI workflow for testing a new node.

    A custom node's class type and inputs are project-specific, so the starter
    workflow deliberately uses only ComfyUI core image nodes.  Developers can
    insert their node between LoadImage and PreviewImage without requiring a
    checkpoint or any other model-specific asset.
    """
    note = (
        f"Comfyui-omniscience 开发工作流：{project_name}\n"
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


def write_workflow_if_missing(project: Path, project_name: str) -> tuple[Path, bool]:
    """Create the starter workflow without ever replacing an existing path."""
    workflow = project.resolve() / constants.WORKFLOW_FILENAME
    if workflow.exists() or workflow.is_symlink():
        return workflow, False
    try:
        with workflow.open("x", encoding="utf-8") as handle:
            json.dump(workflow_template(project_name), handle, ensure_ascii=False, indent=2)
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


def default_workspace() -> Path:
    configured = paths.env("OMNISCIENCE_WORKSPACE", "CLINE_WORKSPACE")
    workspace = Path(configured).expanduser() if configured else paths.DEFAULT_WORKSPACE
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace.resolve()


def max_context_tokens(value: Any, default: int = constants.DEFAULT_MAX_CONTEXT_TOKENS) -> int:
    """Validate the configured model context budget."""
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if result < constants.MIN_MAX_CONTEXT_TOKENS or result > constants.MAX_MAX_CONTEXT_TOKENS:
        raise ValueError(
            f"最大上下文 Token 数必须在 {constants.MIN_MAX_CONTEXT_TOKENS} 到 {constants.MAX_MAX_CONTEXT_TOKENS} 之间。"
        )
    return result


def compaction_threshold_percent(
    value: Any,
    default: int = constants.DEFAULT_COMPACTION_THRESHOLD_PERCENT,
) -> int:
    """Validate the percentage of the context budget that triggers compaction."""
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if result < constants.MIN_COMPACTION_THRESHOLD_PERCENT or result > constants.MAX_COMPACTION_THRESHOLD_PERCENT:
        raise ValueError(
            f"自动压缩阈值必须在 {constants.MIN_COMPACTION_THRESHOLD_PERCENT}% 到 "
            f"{constants.MAX_COMPACTION_THRESHOLD_PERCENT}% 之间。"
        )
    return result
