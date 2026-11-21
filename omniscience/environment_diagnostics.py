"""Generic, evidence-based diagnostics for failed ComfyUI workflows.

The diagnostic is independent of any particular custom node. It classifies the
original ComfyUI error, collects read-only runtime evidence, and describes
repair actions that the service may execute. It never edits a workflow, runs a
shell command, installs a package, updates a repository, or guesses a source.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any


_CATEGORY_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("memory", ("out of memory", "cuda out of memory", "outofmemory", "显存不足", "内存不足", "memoryerror")),
    ("model", ("model not found", "checkpoint not found", "vae not found", "lora not found", "missing model", "缺少模型", "模型不存在", "未找到模型", "权重不存在")),
    ("node", ("node not found", "unknown node", "missing node", "节点不存在", "未知节点", "缺少节点", "custom_nodes")),
    ("timeout", ("timed out", "timeout", "超时")),
    ("connection", ("connection refused", "connection reset", "cannot connect", "connection error", "连接被拒绝", "无法连接")),
    ("parameters", ("prompt_invalid", "validation", "invalid", "missing input", "缺少输入", "参数错误", "校验失败", "输入无效")),
    ("environment", ("importerror", "modulenotfounderror", "runtimeerror", "dll", "动态库", "依赖", "版本不兼容")),
)


def _compact_json(value: Any) -> str:
    try:
        import json

        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _text(value: Any, limit: int = 4_000) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, (int, float, bool)):
        text = str(value)
    else:
        text = _compact_json(value)
    text = text.strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _classify(code: Any, message: Any, hint: Any = "", details: Any = None) -> str:
    value = " ".join(_text(item).casefold() for item in (code, message, hint, details))
    for category, terms in _CATEGORY_TERMS:
        if any(term in value for term in terms):
            return category
    return "unknown"


def _node_ids(details: Any) -> list[str]:
    if not isinstance(details, dict):
        return []
    values = [details.get("node_id"), details.get("node_ids")]
    node_errors = details.get("node_errors")
    if isinstance(node_errors, dict):
        values.append(list(node_errors))
    result: list[str] = []
    for value in values:
        items = value if isinstance(value, (list, tuple, set)) else [value]
        for item in items:
            text = str(item or "").strip()
            if text and text not in result:
                result.append(text)
    return result[:12]


def _repair_actions(
    category: str,
    *,
    retryable: bool,
    allow_retry: bool,
    dependency_plan: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    plan = dependency_plan or {}
    node_action = bool(plan.get("nodes")) and all(
        item.get("source") for item in plan.get("nodes", []) if isinstance(item, dict)
    )
    pending_models = [
        item
        for item in plan.get("models", [])
        if isinstance(item, dict) and item.get("status") not in {"installed", "selected_local"}
    ]
    model_action = len(pending_models) == 1 and bool(
        pending_models[0].get("source")
    )
    actions: list[dict[str, Any]] = []
    if allow_retry and (retryable or category in {"connection", "timeout"}):
        actions.append({"id": "retry_execution", "label": "重试工作流", "automatic": True, "requires_confirmation": True})
    if category == "node" and node_action:
        actions.append({"id": "install_selected_nodes", "label": "安装已选择来源的缺失节点", "automatic": True, "requires_confirmation": True})
    if category == "model" and model_action:
        actions.append({"id": "download_selected_models", "label": "下载已选择来源的缺失模型", "automatic": True, "requires_confirmation": True})
    return actions


def diagnose_environment(
    settings: Any = None,
    *,
    error_text: Any = "",
    workflow: Any = None,
    code: Any = "execution_error",
    details: Any = None,
    hint: Any = "",
    retryable: bool = False,
    prompt_id: Any = "",
    phase: str = "execution",
    runtime: dict[str, Any] | None = None,
    dependency_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a generic diagnosis and a server-side repair plan.

    ``settings`` is accepted for compatibility with older callers; diagnosis
    does not read or mutate settings. ``workflow`` is only used for evidence.
    """

    message = _text(error_text) or "工作流执行失败。"
    category = _classify(code, message, hint, details)
    node_ids = _node_ids(details)
    node_count = 0
    if isinstance(workflow, dict):
        prompt = workflow.get("prompt")
        nodes = workflow.get("nodes")
        node_count = len(prompt) if isinstance(prompt, dict) else len(nodes) if isinstance(nodes, list) else 0

    summary_by_category = {
        "node": "工作流引用的节点未找到或当前没有成功加载。",
        "model": "工作流需要的模型或权重未找到。",
        "memory": "执行过程中的显存或内存不足。",
        "parameters": "工作流输入或节点参数没有通过 ComfyUI 校验。",
        "connection": "服务与 ComfyUI 的连接失败。",
        "timeout": "ComfyUI 在限定时间内没有完成执行。",
        "environment": "运行环境或第三方依赖在执行时抛出了错误。",
        "unknown": "无法仅凭当前错误安全确定唯一原因。",
    }
    advice_by_category = {
        "node": "检查节点是否已安装并在当前 ComfyUI 启动日志中成功加载；不要直接修改工作流中的节点类型。",
        "model": "检查模型文件名、类别和 ComfyUI 注册目录；确认文件完整且与节点要求匹配。",
        "memory": "降低分辨率或批量大小，关闭其他任务，或更换满足资源要求的模型。",
        "parameters": "根据原始错误定位节点输入，补齐缺失输入或修正参数；服务不会擅自猜测参数。",
        "connection": "确认 ComfyUI 服务正在运行并检查地址、端口和网络策略。",
        "timeout": "检查队列和服务负载，降低任务规模后再重试；不要重复提交已经取得 prompt_id 的任务。",
        "environment": "查看 ComfyUI 启动日志中的完整 traceback，并由用户确认依赖或节点版本的处理方式。",
        "unknown": "查看完整 traceback、失败节点和输入值，再针对实际组件处理；当前没有足够证据自动修改。",
    }
    actions = _repair_actions(
        category,
        retryable=retryable,
        allow_retry=not bool(prompt_id),
        dependency_plan=dependency_plan,
    )
    if not actions:
        actions = [{"id": "manual_review", "label": advice_by_category[category], "automatic": False, "requires_confirmation": False}]
    evidence = [f"ComfyUI 原始错误：{message}"]
    if code:
        evidence.append(f"错误代码：{code}。")
    if node_ids:
        evidence.append(f"错误详情涉及节点：{', '.join(node_ids)}。")
    if node_count:
        evidence.append(f"已保存的工作流包含 {node_count} 个节点；诊断未修改其内容。")
    if runtime:
        loaded = runtime.get("loaded_nodes")
        if isinstance(loaded, list):
            evidence.append(f"当前 ComfyUI object_info 返回 {len(loaded)} 个已加载节点。")
        if runtime.get("error"):
            evidence.append(f"运行状态读取失败：{_text(runtime['error'], 500)}")

    automatic = [item for item in actions if item.get("automatic")]
    blocker = "" if automatic else "当前没有已确认且可安全执行的修复动作；请按建议人工处理。"
    return {
        "kind": "workflow_failure",
        "phase": phase,
        "code": str(code or "execution_error"),
        "message": message,
        "details": details,
        "prompt_id": str(prompt_id or ""),
        "retryable": bool(retryable),
        "category": category,
        "summary": summary_by_category[category],
        "cause": advice_by_category[category],
        "evidence": evidence[:12],
        "actions": actions[:6],
        "repair_actions": actions[:6],
        "repair_blocker": blocker,
        "can_auto_repair": bool(automatic),
        "workflow_unchanged": True,
        "external_files_changed": False,
        "runtime": runtime or {},
        "checked_at": time.time(),
    }


async def diagnose_running_environment(
    settings: Any,
    *,
    error_text: Any = "",
    workflow: Any = None,
    code: Any = "execution_error",
    details: Any = None,
    hint: Any = "",
    retryable: bool = False,
    prompt_id: Any = "",
    phase: str = "execution",
    dependency_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add best-effort read-only facts from the local ComfyUI API."""

    runtime: dict[str, Any] = {}
    try:
        from .workflow_executor import ComfyUIWorkflowExecutor

        executor = ComfyUIWorkflowExecutor(settings, request_timeout=3.0)
        stats, object_info = await asyncio.gather(
            asyncio.to_thread(executor.system_stats),
            asyncio.to_thread(executor.object_info),
            return_exceptions=True,
        )
        if isinstance(stats, dict):
            runtime["system_stats"] = stats
        elif isinstance(stats, BaseException):
            runtime["error"] = str(stats)
        if isinstance(object_info, dict):
            runtime["loaded_nodes"] = sorted(str(name) for name in object_info if name)
        elif isinstance(object_info, BaseException) and not runtime.get("error"):
            runtime["error"] = str(object_info)
    except Exception as exc:
        runtime["error"] = str(exc)
    return diagnose_environment(
        settings,
        error_text=error_text,
        workflow=workflow,
        code=code,
        details=details,
        hint=hint,
        retryable=retryable,
        prompt_id=prompt_id,
        phase=phase,
        runtime=runtime,
        dependency_plan=dependency_plan,
    )