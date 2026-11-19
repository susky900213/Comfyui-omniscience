"""Validated file uploads into a workspace (panel "上传文件" button).

Uploads always land in ``uploads/`` and keep only the basename of the
client-supplied name, so a crafted path cannot escape the workspace.
"""

from __future__ import annotations

import base64
from pathlib import PurePosixPath
from typing import Any

from .constants import MAX_WRITE_BYTES
from .workspace import Workspace


def decode_upload(name: Any, content_base64: Any, *, max_bytes: int = MAX_WRITE_BYTES) -> tuple[str, bytes]:
    """Validate a client upload and return its safe basename plus decoded bytes."""
    filename = PurePosixPath(str(name or "").replace("\\", "/")).name
    if not filename or filename.startswith("."):
        raise ValueError("文件名无效。")
    encoded = str(content_base64 or "")
    if len(encoded) > (max_bytes + 4) // 3 * 4 + 1024:
        raise ValueError(f"上传文件超过 {max_bytes} 字节限制。")
    try:
        data = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ValueError("上传内容不是有效的 base64。") from exc
    if not data or len(data) > max_bytes:
        raise ValueError(f"上传文件为空或超过 {max_bytes} 字节限制。")
    return filename, data


def store_upload(workspace: Workspace, name: Any, content_base64: Any) -> dict[str, Any]:
    filename, data = decode_upload(name, content_base64)
    path = workspace.resolve(f"uploads/{filename}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"path": workspace.display_path(path), "size": len(data)}
