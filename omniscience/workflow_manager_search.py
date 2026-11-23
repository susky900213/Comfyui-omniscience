"""Shared dependencies for workflow manager mixins."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from pathlib import Path
from threading import Event
from typing import Any

from . import paths
from .environment_diagnostics import diagnose_environment, diagnose_running_environment
from .model_downloader import (
    CATEGORIES,
    ModelDownloadCancelled,
    ModelDownloadRetryableError,
    ModelDownloader,
)
from .model_source import ModelSourceSearcher, model_filenames_match
from .node_search import ComfyUIManagerClient
from .resource_assessor import assess, local_snapshot, rank_resource
from .util import json_text
from .workflow_diagnostics import diagnose_execution_failure, static_diagnostic
from .workflow_executor import ComfyUIWorkflowExecutor, WorkflowExecutionError
from .workflow_parser import (
    WORKFLOW_INSPECTION_VERSION,
    WorkflowParseError,
    inspect_workflow,
    to_api_workflow,
)
from .workflow_provider import RunningHubProvider

MODEL_SUFFIXES = {".safetensors", ".sft", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx", ".pkl"}
MODEL_HASH_CHUNK_SIZE = 1024 * 1024
MODEL_FOLDER_ALIASES = {
    "checkpoints": ("checkpoints",),
    "vae": ("vae",),
    "loras": ("loras",),
    "controlnet": ("controlnet",),
    "clip": ("clip", "text_encoders", "clip_gguf"),
    "unet": ("unet", "diffusion_models", "unet_gguf"),
    "diffusion_models": ("diffusion_models", "unet", "unet_gguf"),
    "upscale_models": ("upscale_models",),
    "embeddings": ("embeddings",),
}
MODEL_DIRECTORY_CATEGORIES = {
    "checkpoints": "checkpoints",
    "checkpoint": "checkpoints",
    "vae": "vae",
    "loras": "loras",
    "lora": "loras",
    "controlnet": "controlnet",
    "controlnets": "controlnet",
    "clip": "clip",
    "clip_gguf": "clip",
    "text_encoders": "clip",
    "text_encoder": "clip",
    "unet": "unet",
    "unet_gguf": "unet",
    "diffusion_models": "diffusion_models",
    "diffusion_model": "diffusion_models",
    "upscale_models": "upscale_models",
    "embeddings": "embeddings",
}

__all__ = [name for name in globals() if not name.startswith("__")]


class WorkflowSearchMixin:
    """WorkflowSearchMixin implementation."""

    def workflow_search_requested(self, content: Any) -> bool:
        text = str(content or "").casefold()
        workflow = bool(re.search(r"工作流|workflow|comfyui[\s_-]*workflow", text))
        if not workflow:
            return False
        # A named workflow request such as “Moss 工作流” should expose the
        # search tool even when the user did not use a verb like “搜索”.
        return not bool(re.search(r"不要|无需|不需要|别|禁止|don't|do not", text))


    def _record_workflow_execution_failure(
        self,
        job_id: str,
        error: BaseException,
        *,
        code: str = "unexpected_error",
        details: Any = None,
        retryable: bool = False,
        prompt_id: str = "",
        hint: str = "",
        recovery: str = "",
        phase: str = "execution",
    ) -> None:
        """Keep an actionable execution diagnosis without losing job metadata."""
        if isinstance(error, WorkflowExecutionError):
            code = error.code
            details = error.details
            retryable = error.retryable
            prompt_id = error.prompt_id
            hint = error.hint
        if details is None:
            parser_diagnostic = getattr(error, "diagnostic", None)
            if isinstance(parser_diagnostic, dict) and parser_diagnostic:
                details = parser_diagnostic
        job = self.store.get_workflow_job(str(job_id))
        if job is None:
            return
        metadata = dict(job.get("metadata") or {})
        diagnostic = static_diagnostic(
            code=code,
            message=str(error),
            details=details,
            retryable=retryable,
            prompt_id=prompt_id,
            hint=hint,
            recovery=recovery,
        )
        diagnostic["phase"] = phase
        diagnostic.update(
            {
                key: value
                for key, value in diagnose_environment(
                    self.settings,
                    error_text=str(error),
                    workflow=metadata.get("workflow"),
                    code=code,
                    details=details,
                    hint=hint,
                    retryable=retryable,
                    prompt_id=prompt_id,
                    phase=phase,
                    dependency_plan=self._diagnostic_dependency_plan(metadata),
                ).items()
                if key not in {
                    "message",
                    "details",
                    "prompt_id",
                    "retryable",
                    "code",
                    "phase",
                    "actions",
                    "summary",
                    "cause",
                }
            }
        )
        metadata["execution_error"] = str(error)
        metadata["execution_diagnostic"] = diagnostic
        self.store.update_workflow_job(str(job_id), state="failed", error=str(error), metadata=metadata)


    async def _enrich_workflow_execution_failure(self, job_id: Any) -> None:
        """Ask the configured model for a read-only explanation of one failure.

        The persisted static diagnosis is written before this call, so a model
        timeout, malformed response, or missing provider can never hide the
        original ComfyUI error.
        """
        job = self.store.get_workflow_job(str(job_id))
        if job is None:
            return
        metadata = dict(job.get("metadata") or {})
        diagnostic = metadata.get("execution_diagnostic")
        if not isinstance(diagnostic, dict):
            return
        candidate = metadata.get("candidate") or {}
        inspection = metadata.get("inspection") or {}
        workflow = metadata.get("workflow") or {}
        prompt = workflow.get("prompt") if isinstance(workflow, dict) else None
        context = {
            "phase": diagnostic.get("phase") or "execution",
            "candidate": {
                "title": candidate.get("title"),
                "execution_mode": candidate.get("execution_mode"),
                "resource_assessment": candidate.get("resource_assessment"),
            },
            "workflow": {
                "format": inspection.get("format"),
                "node_count": inspection.get("node_count"),
                "missing_nodes": inspection.get("missing_nodes") or [],
                "models": [
                    {"name": item.get("name"), "local_status": item.get("local_status")}
                    for item in (inspection.get("models") or [])
                    if isinstance(item, dict)
                ][:40],
                "prompt_nodes": list(prompt.keys())[:120] if isinstance(prompt, dict) else [],
            },
        }
        environment = await diagnose_running_environment(
            self.settings,
            error_text=diagnostic.get("message"),
            workflow=workflow,
            code=diagnostic.get("code"),
            details=diagnostic.get("details"),
            hint=diagnostic.get("hint"),
            retryable=diagnostic.get("retryable", False),
            prompt_id=diagnostic.get("prompt_id"),
            phase=diagnostic.get("phase") or "execution",
            dependency_plan=self._diagnostic_dependency_plan(metadata),
        )
        latest = self.store.get_workflow_job(str(job_id)) or job
        latest_metadata = dict(latest.get("metadata") or {})
        current = latest_metadata.get("execution_diagnostic") or {}
        if current.get("message") != diagnostic.get("message"):
            return
        current.update(
            {
                "runtime": environment.get("runtime") or {},
                "evidence": list(dict.fromkeys((current.get("evidence") or []) + (environment.get("evidence") or [])))[:12],
                "repair_actions": environment.get("repair_actions") or current.get("repair_actions") or [],
                "can_auto_repair": bool(environment.get("can_auto_repair") or current.get("can_auto_repair")),
                "repair_blocker": environment.get("repair_blocker") or current.get("repair_blocker") or "",
            }
        )
        latest_metadata["execution_diagnostic"] = current
        self.store.update_workflow_job(str(job_id), metadata=latest_metadata)
        # Preserve the historical facade's monkey-patch surface.  Before the
        # responsibility split this function was resolved from
        # ``workflow_manager``; callers may still replace that compatibility
        # symbol at runtime.
        diagnosis_function = diagnose_execution_failure
        try:
            from . import workflow_manager as compatibility_module

            diagnosis_function = getattr(compatibility_module, "diagnose_execution_failure", diagnosis_function)
        except ImportError:
            pass
        enriched = await diagnosis_function(
            self.settings,
            diagnostic=diagnostic,
            workflow_context=context,
        )
        latest = self.store.get_workflow_job(str(job_id)) or job
        latest_metadata = dict(latest.get("metadata") or {})
        # Do not overwrite a newer failure if a future execution path updates it
        # while the provider request is in flight.
        current = latest_metadata.get("execution_diagnostic") or {}
        if current.get("message") != diagnostic.get("message"):
            return
        persisted_repairs = current.get("repair_actions") or []
        enriched["repair_actions"] = persisted_repairs
        enriched["can_auto_repair"] = bool(persisted_repairs)
        latest_metadata["execution_diagnostic"] = enriched
        self.store.update_workflow_job(str(job_id), metadata=latest_metadata)


    async def repair_workflow(
        self,
        job_id: Any,
        *,
        action_id: Any,
        confirmed: bool = False,
        timeout: float = 1800.0,
    ) -> dict[str, Any]:
        """Execute one persisted, user-confirmed repair action.

        The browser sends only an action id. All sources, paths and workflow
        data are loaded from the persisted job so it cannot turn this endpoint
        into an arbitrary command or download proxy.
        """
        if not confirmed:
            raise ValueError("必须由用户明确确认后才能执行修复。")
        action = str(action_id or "").strip()
        if action not in {"retry_execution", "install_selected_nodes", "download_selected_models"}:
            raise ValueError("修复动作无效或不支持自动执行。")
        job = self.workflow_job(job_id)
        metadata = dict(job.get("metadata") or {})
        diagnostic = metadata.get("execution_diagnostic") or {}
        allowed = {
            str(item.get("id"))
            for item in diagnostic.get("repair_actions") or diagnostic.get("actions") or []
            if isinstance(item, dict) and item.get("automatic")
        }
        if action not in allowed:
            raise ValueError("该修复动作没有被当前错误诊断授权，或依赖来源尚未由用户确认。")

        if action == "retry_execution":
            result = await self.execute_workflow(job["id"], confirmed=True, timeout=timeout)
            return {"status": "repaired", "action": action, "result": result}

        plan = metadata.get("dependency_plan") or self.dependency_plan(job["id"])
        if action == "install_selected_nodes":
            result = await self.confirm_node_installation(job["id"], confirmed=True)
            return {
                "status": "restart_required",
                "action": action,
                "result": result,
                "message": "节点文件已安装并通过安全检查；ComfyUI 需要重启后才能加载节点，服务不会强制重启当前进程。",
            }

        model_names = [
            str(item.get("name"))
            for item in plan.get("models", [])
            if isinstance(item, dict) and item.get("status") not in {"installed", "selected_local"}
        ]
        if len(model_names) != 1:
            raise ValueError("自动下载要求当前只有一个已选择来源的缺失模型。")
        result = await self.confirm_model_download(
            job["id"], confirmed=True, model_names=model_names, wait_for_completion=True
        )
        refreshed = self.workflow_job(job["id"])
        if refreshed.get("state") != "ready":
            return {"status": "repaired", "action": action, "result": result, "message": "模型已处理，请重新执行工作流。"}
        execution = await self.execute_workflow(job["id"], confirmed=True, timeout=timeout)
        return {"status": "repaired", "action": action, "result": result, "execution": execution}


    async def _refresh_workflow_resource_assessment(
        self, job_id: Any, metadata: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Refresh advisory resource information immediately before execution.

        Candidate cards are persisted for search ranking and can be minutes or
        hours old by the time preparation finishes.  The result is advisory:
        ComfyUI remains the authority during actual execution and a stale
        assessment must not replace its error.
        """
        candidate = dict(metadata.get("candidate") or {})
        if not candidate:
            return metadata, None
        requirements = candidate.get("resource_requirements")
        if not isinstance(requirements, dict) or not requirements:
            previous = candidate.get("resource_assessment")
            if isinstance(previous, dict):
                candidate["resource_assessment"] = {
                    **previous,
                    "stale": True,
                    "source": "search_time_stale",
                    "recommendation": "该工作流未声明可量化的资源需求；此评估仅用于搜索排序，执行时以 ComfyUI 返回的真实错误为准。",
                }
                metadata["candidate"] = candidate
            return metadata, None
        try:
            stats = await asyncio.to_thread(
                ComfyUIWorkflowExecutor(self.settings, request_timeout=2.0).system_stats
            )
        except Exception:
            stats = None
        settings_data = self.settings.load() if hasattr(self.settings, "load") else dict(self.settings or {})
        snapshot = local_snapshot(storage_root=settings_data.get("workspace"), comfy_stats=stats)
        assessment = assess(snapshot, requirements).as_dict()
        assessment["checked_at"] = time.time()
        assessment["source"] = "execution_preflight"
        candidate["resource_assessment"] = assessment
        metadata["candidate"] = candidate
        return metadata, assessment


    @staticmethod
    def simplify_workflow_query(query: str) -> str:
        value = str(query or "").strip()
        value = re.sub(r"(?:我想要|我需要|帮我|请帮我|请搜索|搜索|查找|推荐|安装(?:下|一个|这个)?)", " ", value, flags=re.I)
        value = re.sub(r"(?:的)\s*", " ", value)
        value = re.sub(r"(?:工作流|workflow)\b", " ", value, flags=re.I)
        value = re.sub(r"\s+", " ", value).strip(" ，,。.!！")
        return value or str(query or "").strip()


    @staticmethod
    def requested_region(content: Any) -> str | None:
        text = str(content or "")
        if re.search(r"国内|中国站|国内站", text):
            return "domestic"
        if re.search(r"国外|国际站|海外", text):
            return "international"
        return None
