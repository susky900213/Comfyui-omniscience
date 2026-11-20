"""Submit and verify ComfyUI API-format workflows."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class WorkflowExecutionError(ValueError):
    pass


class ComfyUIWorkflowExecutor:
    def __init__(self, settings: Any, *, request_timeout: float = 20.0):
        self.settings = settings
        self.request_timeout = request_timeout

    def _data(self) -> dict[str, Any]:
        return self.settings.load() if hasattr(self.settings, "load") else dict(self.settings or {})

    def _base_url(self) -> str:
        value = str(self._data().get("comfyui_server_url") or "http://127.0.0.1:8188").strip().rstrip("/")
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise WorkflowExecutionError("ComfyUI 服务地址无效。")
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} and not self._data().get("allow_remote_comfyui"):
            raise WorkflowExecutionError("工作流执行默认只允许本机 ComfyUI 服务。")
        return value

    def _json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        max_bytes: int = 2_000_000,
    ) -> Any:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(f"{self._base_url()}{path}", data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.request_timeout) as response:  # noqa: S310 - validated local endpoint
                status = getattr(response, "status", None)
                content_type = str(response.headers.get("Content-Type") or "")
                raw = response.read(max_bytes + 1)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise WorkflowExecutionError(f"ComfyUI 请求失败：{method} {path}：{exc}") from exc
        if len(raw) > max_bytes:
            raise WorkflowExecutionError(
                f"ComfyUI 接口 {method} {path} 响应超过 {max_bytes} 字节，可能被截断；"
                f"status={status} content_type={content_type or 'unknown'}。"
            )
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            preview = raw[:500].decode("utf-8", errors="replace").replace("\n", " ")
            raise WorkflowExecutionError(
                f"ComfyUI 接口 {method} {path} 返回了无法解析的 JSON；"
                f"status={status} content_type={content_type or 'unknown'} bytes={len(raw)} preview={preview!r}"
            ) from exc

    def system_stats(self) -> dict[str, Any]:
        payload = self._json("GET", "/system_stats")
        if not isinstance(payload, dict):
            raise WorkflowExecutionError("ComfyUI /system_stats 返回格式无效。")
        return payload

    def loaded_nodes(self) -> list[str]:
        payload = self._json("GET", "/object_info", max_bytes=50_000_000)
        if not isinstance(payload, dict):
            raise WorkflowExecutionError("ComfyUI /object_info 返回格式无效。")
        return sorted(str(name) for name in payload if name)

    def folder_paths(self) -> dict[str, list[str]]:
        """Return the model and asset folders registered by ComfyUI.

        ``/internal/folder_paths`` is an internal endpoint and is not present
        in older ComfyUI releases.  Keep the response contract deliberately
        narrow here so callers can safely fall back when a version returns a
        different shape or does not expose the endpoint.
        """
        payload = self._json("GET", "/internal/folder_paths", max_bytes=5_000_000)
        if not isinstance(payload, dict):
            raise WorkflowExecutionError("ComfyUI /internal/folder_paths 返回格式无效。")
        result: dict[str, list[str]] = {}
        for name, values in payload.items():
            if not isinstance(name, str) or not isinstance(values, list):
                continue
            paths = [str(value).strip() for value in values if isinstance(value, str) and value.strip()]
            result[name] = paths
        if not result and payload:
            raise WorkflowExecutionError("ComfyUI /internal/folder_paths 返回的目录列表格式无效。")
        return result

    def submit(self, prompt: dict[str, Any]) -> str:
        if not isinstance(prompt, dict) or not prompt:
            raise WorkflowExecutionError("只有 API 格式工作流才能自动执行。")
        result = self._json("POST", "/prompt", {"prompt": prompt, "client_id": uuid.uuid4().hex})
        prompt_id = str(result.get("prompt_id") or "") if isinstance(result, dict) else ""
        if not prompt_id:
            error = result.get("error") if isinstance(result, dict) else ""
            raise WorkflowExecutionError(f"ComfyUI 没有接受工作流：{error or '缺少 prompt_id'}")
        return prompt_id

    def wait(self, prompt_id: str, *, timeout: float = 1800.0, interval: float = 1.0) -> dict[str, Any]:
        deadline = time.monotonic() + max(1.0, timeout)
        while time.monotonic() < deadline:
            history = self._json("GET", f"/history/{prompt_id}")
            item = history.get(prompt_id) if isinstance(history, dict) else None
            if isinstance(item, dict):
                status = item.get("status") or {}
                if status.get("status_str") in {"error", "failed"} or status.get("completed") is False and status.get("messages"):
                    raise WorkflowExecutionError(f"工作流执行失败：{json.dumps(status, ensure_ascii=False)[:2000]}")
                if status.get("completed") or item.get("outputs"):
                    outputs = item.get("outputs") or {}
                    if not outputs:
                        raise WorkflowExecutionError("ComfyUI 报告执行完成，但没有输出。")
                    return {"prompt_id": prompt_id, "history": item, "outputs": outputs, "status": "completed"}
            time.sleep(max(0.1, interval))
        raise WorkflowExecutionError("工作流执行超时，请在 ComfyUI 队列或历史记录中检查。")

    def execute(self, prompt: dict[str, Any], *, timeout: float = 1800.0) -> dict[str, Any]:
        prompt_id = self.submit(prompt)
        return self.wait(prompt_id, timeout=timeout)
