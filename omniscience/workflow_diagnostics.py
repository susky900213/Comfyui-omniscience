"""AI-assisted, read-only diagnosis for failed ComfyUI workflow runs.

The execution error returned by ComfyUI is authoritative.  This module only
adds a short explanation and possible next steps; it never changes a workflow,
installs a node, downloads a model, or retries execution.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .model_client import complete
from .util import json_text

logger = logging.getLogger("Comfyui-omniscience")

MAX_DIAGNOSTIC_CONTEXT_CHARS = 18_000
MAX_DIAGNOSTIC_TEXT_CHARS = 2_000
AI_DIAGNOSTIC_TIMEOUT_SECONDS = 15.0

DIAGNOSTIC_SYSTEM_PROMPT = """你是 ComfyUI 工作流执行失败诊断助手。
你只能分析输入，不能安装节点、下载模型、修改工作流、执行命令或声称已经修复问题。
ComfyUI 返回的原始错误是事实来源，不能被泛化提示替换；如果证据不足，必须明确说无法确定。
请只返回 JSON 对象，不要 Markdown，不要代码块，字段必须是：
{
  "category": "node|model|memory|parameters|connection|timeout|environment|unknown",
  "summary": "一句中文结论",
  "cause": "基于原始错误的具体原因；不要编造",
  "actions": ["按优先级排列的具体、可执行建议"],
  "confidence": "high|medium|low"
}
建议必须区分“可以检查/手动处理”和“当前无法自动修复”。不要建议执行本身自动安装或下载。
"""


def _clip(value: Any, limit: int = MAX_DIAGNOSTIC_TEXT_CHARS) -> str:
    text = value if isinstance(value, str) else json_text(value, limit=limit)
    text = str(text or "").strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _fallback_category(code: Any, message: Any, hint: Any = "") -> str:
    text = f"{code} {message} {hint}".casefold()
    if any(item in text for item in ("out of memory", "cuda", "显存", "内存不足")):
        return "memory"
    if any(item in text for item in ("model", "checkpoint", "vae", "lora", "模型", "权重")) and any(
        item in text for item in ("not found", "missing", "不存在", "未找到", "缺少")
    ):
        return "model"
    if any(item in text for item in ("node", "节点", "custom_nodes")) and any(
        item in text for item in ("not found", "unknown", "不存在", "未找到", "缺少")
    ):
        return "node"
    if any(item in text for item in ("timeout", "timed out", "超时")):
        return "timeout"
    if any(item in text for item in ("connect", "connection", "拒绝", "无法连接", "http ")):
        return "connection"
    if any(item in text for item in ("validation", "invalid", "参数", "校验", "prompt_rejected")):
        return "parameters"
    return "unknown"


def static_diagnostic(
    *,
    code: Any,
    message: Any,
    hint: Any = "",
    details: Any = None,
    retryable: bool = False,
    prompt_id: Any = "",
    recovery: Any = "",
) -> dict[str, Any]:
    """Build a useful diagnosis without requiring a model provider."""
    category = _fallback_category(code, message, hint)
    actions = [str(hint).strip()] if str(hint or "").strip() else []
    if not actions:
        actions = ["保留并查看 ComfyUI 原始错误详情，再针对报错节点或服务状态处理。"]
    return {
        "code": str(code or "unexpected_error"),
        "message": str(message or "工作流执行失败。"),
        "details": details,
        "retryable": bool(retryable),
        "prompt_id": str(prompt_id or ""),
        "hint": str(hint or ""),
        "recovery": str(recovery or ""),
        "category": category,
        "summary": "暂未获得 AI 分析，已保留 ComfyUI 原始错误。",
        "cause": str(message or "工作流执行失败。"),
        "actions": actions,
        "confidence": "low",
        "ai_status": "unavailable",
    }


def _normalise_ai_result(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        raw = value.strip()
        if raw.startswith("```"):
            raw = raw.strip("`").strip()
            if raw.startswith("json"):
                raw = raw[4:].lstrip()
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, dict):
        return None
    category = str(value.get("category") or "unknown").strip().lower()
    if category not in {"node", "model", "memory", "parameters", "connection", "timeout", "environment", "unknown"}:
        category = "unknown"
    confidence = str(value.get("confidence") or "low").strip().lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"
    actions = value.get("actions")
    if not isinstance(actions, list):
        actions = [str(actions)] if str(actions or "").strip() else []
    actions = [str(item).strip() for item in actions if str(item).strip()][:5]
    return {
        "category": category,
        "summary": _clip(value.get("summary"), 600),
        "cause": _clip(value.get("cause"), 1_500),
        "actions": actions,
        "confidence": confidence,
    }


async def diagnose_execution_failure(
    settings: Any,
    *,
    diagnostic: dict[str, Any],
    workflow_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Enrich one persisted diagnostic with a best-effort model analysis."""
    result = dict(diagnostic)
    result.setdefault("ai_status", "unavailable")
    context = {
        "failure": {
            "code": result.get("code"),
            "message": result.get("message"),
            "hint": result.get("hint"),
            "retryable": result.get("retryable"),
            "prompt_id": result.get("prompt_id"),
            "details": result.get("details"),
        },
        "workflow": workflow_context or {},
    }
    prompt = json_text(context, limit=MAX_DIAGNOSTIC_CONTEXT_CHARS)
    try:
        data = settings.load() if hasattr(settings, "load") else dict(settings or {})
        if not str(data.get("model") or "").strip():
            return result
        response = await asyncio.wait_for(
            complete(
                model=str(data.get("model") or "").strip(),
                api_base_url=str(data.get("api_base_url") or ""),
                api_key=settings.api_key() if hasattr(settings, "api_key") else "",
                messages=[
                    {"role": "system", "content": DIAGNOSTIC_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": "请诊断下面这次 ComfyUI 工作流失败，只根据证据返回指定 JSON：\n" + prompt,
                    }
                ],
                temperature=0.0,
                max_tokens=1_500,
            ),
            timeout=AI_DIAGNOSTIC_TIMEOUT_SECONDS,
        )
        choices = response.get("choices") or []
        content = (choices[0].get("message") or {}).get("content") if choices else ""
        parsed = _normalise_ai_result(content)
        if parsed is None:
            raise ValueError("AI 诊断返回的 JSON 无法解析。")
        result.update(parsed)
        result["ai_status"] = "completed"
        return result
    except Exception as exc:  # AI is an enhancement; never hide the real failure.
        logger.warning("Workflow AI diagnosis unavailable: %s", exc)
        result["ai_error"] = str(exc)
        return result