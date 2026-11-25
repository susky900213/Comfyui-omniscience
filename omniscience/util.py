"""Shared low-level helpers: validation, naming, workspace defaults."""

from __future__ import annotations

import errno
import json
import os
import posixpath
import re
import stat
import subprocess
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
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise ValueError("Absolute paths are not allowed; use a path relative to the workspace.")
    normalized = posixpath.normpath(value)
    if normalized == ".." or normalized.startswith("../"):
        raise ValueError("Path must stay inside the workspace.")
    for component in normalized.replace("\\", "/").split("/"):
        if component not in {"", ".", ".."}:
            validate_windows_path_component(component, "路径")
    return "." if normalized in {"", "."} else normalized


class TaskCancelled(Exception):
    """Internal control-flow exception for a user-cancelled run."""


class ProjectFolderExistsError(ValueError):
    """Raised when a new task would overwrite an existing node directory."""


WINDOWS_RESERVED_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
    "com¹",
    "com²",
    "com³",
    "lpt¹",
    "lpt²",
    "lpt³",
}


def validate_windows_path_component(value: Any, label: str = "文件名") -> str:
    """Validate a single path component that may be created on Windows.

    This validation intentionally runs on every platform. Workspaces and
    archives can be prepared on macOS/Linux and later used on Windows, where
    device names, trailing dots/spaces, and alternate data streams have
    different semantics.
    """

    component = str(value)
    if not component or component in {".", ".."}:
        raise ValueError(f"{label}无效。")
    if any(ord(char) < 32 for char in component):
        raise ValueError(f"{label}不能包含控制字符。")
    if any(char in '<>:"/\\|?*' for char in component):
        raise ValueError(f"{label}包含 Windows 不支持的字符。")
    if component.endswith((".", " ")):
        raise ValueError(f"{label}不能以空格或点结尾。")
    stem = component.split(".", 1)[0].casefold()
    if stem in WINDOWS_RESERVED_NAMES:
        raise ValueError(f"{label}不能使用 Windows 保留设备名。")
    return component


def secure_file_permissions(path: Path | str) -> None:
    """Restrict a local configuration file to its owning account.

    POSIX uses mode ``0600``. Windows does not map that mode to an ACL, so use
    the native ``icacls`` utility to remove inherited permissions and grant the
    current service/user account full control. Failure is surfaced to callers;
    silently continuing would make a secrets file appear protected when it is
    not.
    """

    file_path = Path(path)
    if os.name != "nt":
        file_path.chmod(0o600)
        return
    identity = str(os.environ.get("USERDOMAIN") or "").strip()
    username = str(os.environ.get("USERNAME") or os.environ.get("USER") or "").strip()
    account = f"{identity}\\{username}" if identity and username else username
    if not account:
        try:
            result = subprocess.run(
                ["whoami"], capture_output=True, check=True, text=True, timeout=5
            )
            account = result.stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise OSError("无法确定当前 Windows 账户，不能安全设置配置文件 ACL。") from exc
    try:
        subprocess.run(
            [
                "icacls",
                str(file_path),
                "/inheritance:r",
                "/grant:r",
                f"{account}:F",
            ],
            capture_output=True,
            check=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError(f"无法限制 Windows 配置文件 ACL：{file_path}") from exc


def is_directory_junction(path: Path) -> bool:
    """Return whether *path* is a Windows directory Junction.

    ``Path.is_junction`` was added after the minimum Python version supported by
    the extension, so keep a small ``st_file_attributes`` fallback for Python
    versions that do not expose it.  A Junction is a directory link, but unlike
    a POSIX symlink it is not always reported by ``Path.is_symlink``.
    """

    try:
        checker = getattr(path, "is_junction", None)
        if callable(checker):
            return bool(checker())
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return bool(attributes & reparse_point) and path.is_dir()
    except OSError:
        return False


def is_filesystem_link(path: Path) -> bool:
    """Return whether *path* is a POSIX symlink or a Windows Junction."""

    try:
        return path.is_symlink() or is_directory_junction(path)
    except OSError:
        return False


def _run_windows_mklink(link: Path, target: Path, flag: str = "") -> None:
    """Create a Windows link through the shell builtin ``mklink``."""

    command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "mklink"]
    if flag:
        command.append(flag)
    command.extend((str(link), str(target)))
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise OSError("Windows cmd.exe/mklink 不可用，无法创建文件链接。") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        lowered = detail.casefold()
        if flag == "/H" and any(term in lowered for term in ("different volume", "different drive", "different disk")):
            raise OSError(errno.EXDEV, detail) from exc
        suffix = f"：{detail}" if detail else "。"
        raise OSError(f"Windows mklink 创建链接失败{suffix}") from exc


def create_filesystem_link(link: Path | str, target: Path | str, *, target_is_directory: bool = False) -> None:
    """Create a link using the native mechanism for the current operating system.

    POSIX keeps using a symbolic link.  Windows uses a Junction for directories
    (``mklink /J``), which normally does not require Developer Mode or an
    elevated process, and a hard link for files (``mklink /H``).
    """

    link_path = Path(link)
    target_path = Path(target)
    if os.name == "nt":
        _run_windows_mklink(link_path, target_path, "/J" if target_is_directory else "/H")
        return
    link_path.symlink_to(target_path, target_is_directory=target_is_directory)


def remove_filesystem_link(path: Path | str) -> None:
    """Remove a symlink or Junction without touching its target directory."""

    link_path = Path(path)
    if os.name == "nt" and is_directory_junction(link_path):
        link_path.rmdir()
    else:
        link_path.unlink()


def project_folder_name(value: Any) -> str:
    """Validate a new project's direct child folder name.

    Projects are intentionally limited to one directory below the configured
    workspace.  This prevents a task from escaping the workspace and keeps a
    task history entry unambiguously mapped to one ComfyUI node directory.
    """
    raw_name = str(value or "")
    name = raw_name.strip()
    if not name:
        raise ValueError("请输入项目文件夹名称。")
    if raw_name != name:
        raise ValueError("项目文件夹名称不能以空格开头或结尾。")
    if name in {".", ".."} or len(name) > 128 or any(char.isspace() and char in "\r\n\t" for char in name):
        raise ValueError("项目文件夹名称无效。")
    if "/" in name or "\\" in name or "\x00" in name or Path(name).name != name:
        raise ValueError("项目文件夹必须是工作区下的直接文件夹名称，不能包含路径。")
    if any(ord(char) < 32 for char in name):
        raise ValueError("项目文件夹名称不能包含控制字符。")
    validate_windows_path_component(name, "项目文件夹名称")
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
            "omniscience": {
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
