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


class WorkflowNodeProgressMixin:
    """WorkflowNodeProgressMixin implementation."""

    @staticmethod
    def _node_install_package_name(item: dict[str, Any]) -> str:
        manager = item.get("manager") if isinstance(item.get("manager"), dict) else {}
        return str(
            item.get("package_name")
            or manager.get("title")
            or manager.get("name")
            or item.get("manager_id")
            or item.get("source")
            or item.get("class_type")
            or "未知插件"
        ).strip()


    def _save_node_install_progress(self, job_id: Any, progress: dict[str, Any]) -> dict[str, Any]:
        """Persist node-install progress so a browser reload can resume polling."""
        job = self.store.get_workflow_job(str(job_id))
        if not job:
            return progress
        metadata = dict(job.get("metadata") or {})
        metadata["node_install_progress"] = dict(progress)
        status = str(progress.get("status") or "")
        self.store.update_workflow_job(
            str(job_id),
            state="installing_nodes" if status == "installing" else "selected",
            error=str(progress.get("error") or "") if status == "failed" else "",
            metadata=metadata,
        )
        return progress


    def _migrate_legacy_manager_security_progress(
        self, job_id: str, progress: dict[str, Any]
    ) -> dict[str, Any]:
        """Convert an old Manager security failure into a terminal snapshot."""
        security_value = progress.get("security")
        if progress.get("status") != "failed" or (
            isinstance(security_value, dict) and security_value.get("post_install")
        ):
            return progress
        security = progress.get("security") if isinstance(progress.get("security"), dict) else {}
        root = str(security.get("root") or "").strip()
        try:
            nodes_root = self.settings.comfyui_node_directory()
            is_manager_package = bool(
                root
                and nodes_root is not None
                and Path(root).expanduser().resolve().is_relative_to(nodes_root.expanduser().resolve())
            )
        except (OSError, RuntimeError, ValueError):
            is_manager_package = False
        if not is_manager_package:
            return progress
        migrated = dict(progress)
        migrated["status"] = "security_blocked"
        migrated["phase"] = "security_blocked"
        migrated["terminal"] = True
        migrated["manager_installed"] = True
        security = dict(security)
        security["post_install"] = True
        security["manager_managed"] = True
        security.setdefault("class_type", str(progress.get("current_node") or "").strip())
        migrated["security"] = security
        return self._save_node_install_progress(job_id, migrated)


    async def start_node_installation(
        self,
        job_id: Any,
        *,
        confirmed: bool = False,
        acknowledge_security_for: str = "",
        class_types: Any = None,
    ) -> dict[str, Any]:
        """Queue node installation and return immediately with a durable progress snapshot."""
        if not confirmed:
            raise ValueError("必须由用户明确确认后才能安装缺少的节点。")
        job = self.workflow_job(job_id)
        plan = (job.get("metadata") or {}).get("dependency_plan")
        if not plan:
            plan = await asyncio.to_thread(self.dependency_plan, job_id)
        nodes = [item for item in plan.get("nodes") or [] if isinstance(item, dict)]
        requested_types = {str(name).strip() for name in class_types} if isinstance(class_types, list) else None
        if requested_types is not None:
            available_types = {str(item.get("class_type") or "") for item in nodes}
            if not requested_types or not requested_types.issubset(available_types):
                raise ValueError("单节点安装请求包含无效或不属于当前工作流的节点类型。")
            nodes = [item for item in nodes if str(item.get("class_type") or "") in requested_types]
        if not nodes:
            return {
                "status": "completed",
                "job_id": job["id"],
                "progress": {"status": "completed", "current": 0, "completed": 0, "total": 0},
            }
        unresolved = [
            str(item.get("class_type") or "未知节点")
            for item in nodes
            if not item.get("source") and not item.get("manager_id")
        ]
        if unresolved:
            raise ValueError("以下缺失节点尚未选择 ComfyUI-Manager 安装包或手动 GitHub 来源：" + "、".join(unresolved[:8]))
        tasks = getattr(self, "_node_install_tasks", None)
        if tasks is None:
            tasks = {}
            self._node_install_tasks = tasks
        key = str(job["id"])
        existing = tasks.get(key)
        if existing is not None and not existing.done():
            return {"status": "installing", "job_id": key, "progress": self.node_install_status(key)["progress"]}
        previous = dict((job.get("metadata") or {}).get("node_install_progress") or {})
        previous = self._migrate_legacy_manager_security_progress(key, previous)
        if previous.get("status") == "completed" and not previous.get("partial_install"):
            return {"status": "completed", "job_id": key, "progress": previous}
        if previous.get("status") == "security_blocked":
            # Manager has already put this project in custom_nodes.  A security
            # refusal is terminal for this installation attempt: retrying here
            # would enqueue the same Manager package again and show the same
            # report on every refresh.  Once the user removes/disables that
            # package, allow a fresh attempt while retaining other completed
            # node results.
            security = previous.get("security") if isinstance(previous.get("security"), dict) else {}
            installed_root = str(security.get("root") or "").strip()
            if installed_root and Path(installed_root).is_dir():
                return {"status": "security_blocked", "job_id": key, "progress": previous}
            metadata = dict(job.get("metadata") or {})
            prepared = dict(metadata.get("prepared_node_results") or {})
            blocked_class_type = str(security.get("class_type") or "").strip()
            if blocked_class_type:
                prepared.pop(blocked_class_type, None)
                metadata["prepared_node_results"] = prepared
            metadata.pop("node_install_progress", None)
            self.store.update_workflow_job(key, state="selected", error="", metadata=metadata)
        progress = {
            "status": "installing",
            "phase": "starting",
            "current": 0,
            "completed": 0,
            "total": len(nodes),
            "requested_class_types": [str(item.get("class_type") or "") for item in nodes]
                if requested_types is not None
                else None,
            "current_node": "",
            "current_plugin": "",
            "completed_plugins": [],
            "completed_installations": [],
            "events": [],
            "error": "",
        }
        self._save_node_install_progress(key, progress)
        task = asyncio.create_task(
            self._run_background_node_installation(
                key,
                acknowledge_security_for=acknowledge_security_for,
                total=len(nodes),
                class_types=[str(item.get("class_type") or "") for item in nodes],
            )
        )
        tasks[key] = task
        task.add_done_callback(lambda completed, task_key=key: self._discard_node_install_task(task_key, completed))
        return {"status": "installing", "job_id": key, "progress": progress}


    async def _run_background_node_installation(
        self, job_id: str, *, acknowledge_security_for: str, total: int, class_types: list[str]
    ) -> dict[str, Any]:
        try:
            result = await self.confirm_node_installation(
                job_id,
                confirmed=True,
                acknowledge_security_for=acknowledge_security_for,
                progress_callback=lambda update: self._merge_node_install_progress(job_id, update, total),
                class_types=class_types,
            )
            progress = dict((self.store.get_workflow_job(job_id) or {}).get("metadata", {}).get("node_install_progress") or {})
            partial = bool(progress.get("requested_class_types"))
            progress.update({
                "status": "completed",
                "phase": "completed",
                "current": total,
                "completed": total,
                "total": total,
                "error": "",
                "partial_install": partial,
            })
            self._save_node_install_progress(job_id, progress)
            return result
        except Exception as exc:
            progress = dict((self.store.get_workflow_job(job_id) or {}).get("metadata", {}).get("node_install_progress") or {})
            report = getattr(exc, "report", None)
            if isinstance(report, dict):
                progress["security"] = report
            from .agent.node_install import NodeSecurityBlockedError

            manager_managed = isinstance(report, dict) and bool(
                report.get("post_install") or report.get("manager_managed")
            )
            node_security_blocked = isinstance(exc, NodeSecurityBlockedError)
            security_requires_review = node_security_blocked and not manager_managed
            progress.update(
                {
                    "status": "security_review_required" if security_requires_review else "security_blocked" if node_security_blocked else "failed",
                    "phase": "security_review_required" if security_requires_review else "security_blocked" if node_security_blocked else "failed",
                    "terminal": manager_managed,
                    "manager_installed": manager_managed,
                    "error": str(exc),
                    "total": total,
                }
            )
            if node_security_blocked:
                blocked_node = str(report.get("class_type") or progress.get("current_node") or "").strip()
                if blocked_node:
                    progress["blocked_node"] = blocked_node
                events = list(progress.get("events") or [])
                event = f"等待用户确认风险：{blocked_node or '节点包'}" if security_requires_review else f"安全复核阻止：{blocked_node or '节点包'}"
                if event not in events:
                    events.append(event)
                progress["events"] = events[-20:]
            self._save_node_install_progress(job_id, progress)
            raise


    def _merge_node_install_progress(self, job_id: str, update: dict[str, Any], total: int) -> dict[str, Any]:
        job = self.store.get_workflow_job(job_id) or {}
        metadata = job.get("metadata") or {}
        previous = metadata.get("node_install_progress") or {}
        merged = {**dict(previous), **dict(update), "status": "installing", "total": total}
        completed_plugins = list(previous.get("completed_plugins") or [])
        completed_plugin = str(update.get("completed_plugin") or "").strip()
        if completed_plugin and completed_plugin not in completed_plugins:
            completed_plugins.append(completed_plugin)
        merged["completed_plugins"] = completed_plugins
        completed_installations = list(previous.get("completed_installations") or [])
        if completed_plugin:
            installation = {
                "plugin": completed_plugin,
                "path": str(update.get("installed_path") or ""),
            }
            completed_installations = [
                item for item in completed_installations
                if str(item.get("plugin") or "") != completed_plugin
            ]
            completed_installations.append(installation)
        merged["completed_installations"] = completed_installations

        events = list(previous.get("events") or [])
        manager_messages = [
            str(message).strip()
            for message in update.get("manager_messages") or []
            if str(message).strip()
        ]
        event = ""
        if completed_plugin:
            event = f"已完成：{completed_plugin}"
        elif manager_messages:
            event = "；".join(manager_messages)
        else:
            phase = str(update.get("phase") or "installing").strip()
            current_plugin = str(update.get("current_plugin") or "").strip()
            current_node = str(update.get("current_node") or "").strip()
            target = current_plugin or current_node
            if target:
                event = f"{phase}：{target}"
        if event and (not events or events[-1] != event):
            events.append(event)
        merged["events"] = events[-20:]
        return self._save_node_install_progress(
            job_id,
            merged,
        )


    def _discard_node_install_task(self, key: str, task: asyncio.Task) -> None:
        tasks = getattr(self, "_node_install_tasks", {})
        if tasks.get(key) is task:
            tasks.pop(key, None)
        try:
            error = task.exception()
        except asyncio.CancelledError:
            error = None
        if error is not None:
            logging.getLogger("Comfyui-omniscience").warning("Background node installation failed: %s", error)


    def node_install_status(self, job_id: Any) -> dict[str, Any]:
        """Return the durable node-install progress for a workflow job."""
        job = self.workflow_job(job_id)
        key = str(job["id"])
        progress = dict((job.get("metadata") or {}).get("node_install_progress") or {})
        if not progress:
            progress = {"status": "idle", "current": 0, "completed": 0, "total": 0}
        progress = self._migrate_legacy_manager_security_progress(key, progress)
        tasks = getattr(self, "_node_install_tasks", {})
        active_task = tasks.get(key)
        if progress.get("status") == "installing" and (
            active_task is None or active_task.done()
        ):
            # The progress snapshot survives a browser/ComfyUI restart, but
            # asyncio tasks do not.  Do not leave the client polling a task
            # which can no longer make progress; the next explicit confirmation
            # may safely start the installation again.
            progress.update(
                {
                    "status": "failed",
                    "phase": "interrupted",
                    "interrupted": True,
                    "error": progress.get("error")
                    or "ComfyUI 重启后，原节点安装任务已中断，请重试。",
                }
            )
            self._save_node_install_progress(key, progress)
        return {"status": progress.get("status", "idle"), "job_id": job["id"], "progress": progress}
