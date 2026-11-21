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
    """A workflow execution failure with the diagnostic returned by ComfyUI."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "execution_error",
        details: Any = None,
        retryable: bool = False,
        prompt_id: str = "",
        hint: str = "",
    ):
        super().__init__(message)
        self.code = code
        self.details = details
        self.retryable = retryable
        self.prompt_id = prompt_id
        self.hint = hint


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

    @staticmethod
    def _error_text(value: Any, *, limit: int = 3000) -> str:
        """Turn ComfyUI's nested validation/history payload into readable text."""
        if value is None:
            return ""
        if isinstance(value, str):
            text = value.strip()
        elif isinstance(value, dict):
            parts: list[str] = []
            for key in ("error", "message", "exception_message", "detail", "description"):
                item = value.get(key)
                if isinstance(item, (str, int, float)) and str(item).strip():
                    parts.append(str(item).strip())
            node_errors = value.get("node_errors")
            if isinstance(node_errors, dict):
                for node_id, node_error in node_errors.items():
                    description = ComfyUIWorkflowExecutor._error_text(node_error, limit=1000)
                    if description:
                        parts.append(f"节点 {node_id}：{description}")
            if not parts and value:
                parts.append(json.dumps(value, ensure_ascii=False, sort_keys=True))
            text = "；".join(dict.fromkeys(parts))
        elif isinstance(value, (list, tuple)):
            parts = []
            for item in value:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    name = str(item[0] or "").strip()
                    description = ComfyUIWorkflowExecutor._error_text(item[1], limit=1000)
                    if name and description:
                        parts.append(f"{name}：{description}")
                    elif description:
                        parts.append(description)
                else:
                    description = ComfyUIWorkflowExecutor._error_text(item, limit=1000)
                    if description:
                        parts.append(description)
            text = "；".join(dict.fromkeys(parts))
        else:
            text = str(value).strip()
        return text[:limit] + ("…" if len(text) > limit else "")

    @staticmethod
    def _hint_for_error(text: str) -> str:
        lowered = text.casefold()
        if any(term in lowered for term in ("unknown node", "node not found", "不存在节点", "未找到节点")):
            return "请安装或加载报错中的节点，并在必要时重启 ComfyUI 后重新准备工作流。"
        if any(term in lowered for term in ("model", "checkpoint", "vae", "lora")) and any(
            term in lowered for term in ("not found", "not exist", "missing", "不存在", "未找到")
        ):
            return "请检查该模型是否已放入正确的 ComfyUI 模型目录，并重新准备工作流。"
        if any(term in lowered for term in ("out of memory", "cuda out of memory", "显存不足", "内存不足")):
            return "请降低分辨率/批量大小，或关闭其他占用显存的任务后重试。"
        if any(term in lowered for term in ("validation", "invalid prompt", "invalid workflow", "校验失败", "参数无效")):
            return "请根据报错节点检查工作流输入参数；当前错误不能安全地自动修改工作流。"
        return ""

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
        except HTTPError as exc:
            try:
                raw = exc.read(max_bytes + 1)
            except OSError:
                raw = b""
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            detail = self._error_text(payload) or raw[:1000].decode("utf-8", errors="replace").strip() or str(exc)
            request_details = dict(payload) if isinstance(payload, dict) else {}
            request_details.update({"status": exc.code, "method": method, "path": path})
            raise WorkflowExecutionError(
                f"ComfyUI 请求失败：{method} {path}（HTTP {exc.code}）：{detail}",
                code="http_error",
                details=request_details or {"status": exc.code, "message": detail, "method": method, "path": path},
                retryable=exc.code >= 500,
                hint=self._hint_for_error(detail),
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise WorkflowExecutionError(
                f"ComfyUI 请求失败：{method} {path}：{exc}",
                code="connection_error",
                details={"method": method, "path": path},
                retryable=True,
            ) from exc
        if len(raw) > max_bytes:
            raise WorkflowExecutionError(
                f"ComfyUI 接口 {method} {path} 响应超过 {max_bytes} 字节，可能被截断；"
                f"status={status} content_type={content_type or 'unknown'}。",
                code="response_too_large",
            )
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            preview = raw[:500].decode("utf-8", errors="replace").replace("\n", " ")
            raise WorkflowExecutionError(
                f"ComfyUI 接口 {method} {path} 返回了无法解析的 JSON；"
                f"status={status} content_type={content_type or 'unknown'} bytes={len(raw)} preview={preview!r}",
                code="invalid_response",
            ) from exc

    def system_stats(self) -> dict[str, Any]:
        payload = self._json("GET", "/system_stats")
        if not isinstance(payload, dict):
            raise WorkflowExecutionError("ComfyUI /system_stats 返回格式无效。")
        return payload

    def loaded_nodes(self) -> list[str]:
        return sorted(str(name) for name in self.object_info() if name)

    def object_info(self) -> dict[str, Any]:
        """Return ComfyUI node schemas used to restore canvas widget values."""
        payload = self._json("GET", "/object_info", max_bytes=50_000_000)
        if not isinstance(payload, dict):
            raise WorkflowExecutionError("ComfyUI /object_info 返回格式无效。")
        return payload

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

    @classmethod
    def _history_failure(cls, history: dict[str, Any], prompt_id: str) -> WorkflowExecutionError:
        status = history.get("status")
        error: Any = None
        if isinstance(status, dict):
            messages = status.get("messages")
            if isinstance(messages, list):
                for message in messages:
                    if not isinstance(message, (list, tuple)) or len(message) < 2:
                        continue
                    event_name = str(message[0] or "").strip().lower()
                    if event_name in {"execution_error", "execution_interrupted"}:
                        error = message[1]
                        break
        if error is None:
            error = history.get("error") or history
        detail = cls._error_text(error) or "ComfyUI 任务执行失败，但没有提供具体错误信息"
        if isinstance(error, dict):
            node_id = str(error.get("node_id") or "").strip()
            node_type = str(error.get("node_type") or "").strip()
            node_label = "节点 " + node_id if node_id else ""
            if node_type:
                node_label = f"{node_label}（{node_type}）" if node_label else node_type
            if node_label:
                detail = f"{node_label}：{detail}"
        details = error if isinstance(error, dict) else history
        interrupted = False
        if isinstance(status, dict):
            interrupted = any(
                isinstance(message, (list, tuple))
                and len(message) >= 1
                and str(message[0] or "").strip().lower() == "execution_interrupted"
                for message in status.get("messages") or []
            )
        return WorkflowExecutionError(
            f"工作流执行失败：{detail}",
            code="execution_cancelled" if interrupted else "execution_error",
            details=details,
            prompt_id=prompt_id,
            hint=cls._hint_for_error(detail),
        )

    def _history_result(self, history: dict[str, Any], prompt_id: str) -> dict[str, Any] | None:
        status = history.get("status")
        status_str = str(status.get("status_str") or "").strip().lower() if isinstance(status, dict) else ""
        if status_str == "error":
            raise self._history_failure(history, prompt_id)
        if status_str not in {"", "success"}:
            return None
        if isinstance(status, dict) and not status.get("completed"):
            return None

        outputs = history.get("outputs") or {}
        if not isinstance(outputs, dict) or not outputs:
            raise WorkflowExecutionError(
                "ComfyUI 报告任务完成，但没有输出。",
                code="empty_outputs",
                details={"prompt_id": prompt_id, "history": history},
                prompt_id=prompt_id,
            )
        return {"prompt_id": prompt_id, "history": history, "outputs": outputs, "status": "completed"}

    def submit(self, prompt: dict[str, Any]) -> str:
        if not isinstance(prompt, dict) or not prompt:
            raise WorkflowExecutionError("只有 API 格式工作流才能自动执行。")
        result = self._json("POST", "/prompt", {"prompt": prompt, "client_id": uuid.uuid4().hex})
        prompt_id = str(result.get("prompt_id") or "") if isinstance(result, dict) else ""
        if not prompt_id:
            error = self._error_text(result.get("error") if isinstance(result, dict) else result)
            if isinstance(result, dict) and result.get("node_errors"):
                node_errors = self._error_text({"node_errors": result["node_errors"]})
                error = "；".join(item for item in (error, node_errors) if item)
            detail = error or "缺少 prompt_id"
            raise WorkflowExecutionError(
                f"ComfyUI 没有接受工作流：{detail}",
                code="prompt_rejected",
                details=result,
                hint=self._hint_for_error(detail),
            )
        return prompt_id

    def wait(self, prompt_id: str, *, timeout: float = 1800.0, interval: float = 1.0) -> dict[str, Any]:
        deadline = time.monotonic() + max(1.0, timeout)
        while time.monotonic() < deadline:
            payload = self._json("GET", f"/history/{prompt_id}", max_bytes=50_000_000)
            history = payload.get(prompt_id) if isinstance(payload, dict) else None
            if isinstance(history, dict):
                result = self._history_result(history, prompt_id)
                if result is not None:
                    return result

            # ComfyUI returns an empty object until the prompt has finished.
            # Keep polling the standard history endpoint until it is available.
            time.sleep(max(0.1, interval))
        raise WorkflowExecutionError(
            "工作流执行超时，请在 ComfyUI 队列或历史记录中检查。",
            code="timeout",
            details={"prompt_id": prompt_id},
            retryable=True,
            prompt_id=prompt_id,
        )

    def execute(self, prompt: dict[str, Any], *, timeout: float = 1800.0) -> dict[str, Any]:
        prompt_id = self.submit(prompt)
        return self.wait(prompt_id, timeout=timeout)
