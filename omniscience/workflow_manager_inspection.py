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


class WorkflowInspectionMixin:
    """WorkflowInspectionMixin implementation."""

    async def search_runninghub_workflows(
        self,
        query: Any,
        *,
        limit: int = 10,
        filters: dict[str, Any] | None = None,
        region: str | None = None,
    ) -> dict[str, Any]:
        text = str(query or "").strip()
        if not text:
            raise ValueError("请输入工作流需求。")
        search_text = text
        job = self.store.create_workflow_job(text)
        provider = RunningHubProvider(self.settings)
        region_override = region if region in {"domestic", "international"} else self.requested_region(text)
        logging.getLogger("Comfyui-omniscience").info(
            "[runninghub-search] requested_region=%s query=%s search_query=%s",
            region_override,
            text[:120],
            search_text[:120],
        )
        try:
            candidates, decision = await asyncio.to_thread(
                provider.search, search_text, limit=limit, filters=filters, region=region_override
            )
            self.store.update_workflow_job(
                job["id"],
                state="awaiting_selection" if candidates else "not_found",
                region={
                    "mode": decision.mode,
                    "detected_region": decision.detected_region,
                    "selected_region": decision.selected_region,
                    "confidence": decision.confidence,
                    "detected_by": decision.detected_by,
                    "failover_used": decision.failover_used,
                },
            )
            saved = self.store.save_workflow_candidates(job["id"], candidates)
            try:
                stats = await asyncio.to_thread(ComfyUIWorkflowExecutor(self.settings).system_stats)
            except Exception:
                stats = None
            settings_data = self.settings.load() if hasattr(self.settings, "load") else dict(self.settings or {})
            snapshot = local_snapshot(storage_root=settings_data.get("workspace"), comfy_stats=stats)
            for item in saved:
                item["resource_assessment"] = assess(
                    snapshot, item.get("resource_requirements") or {}
                ).as_dict()
            saved.sort(
                key=lambda item: -rank_resource(
                    item, assess(snapshot, item.get("resource_requirements") or {})
                )
            )
            saved = self.store.save_workflow_candidates(job["id"], saved)
            self.store.update_workflow_job(job["id"], state="awaiting_selection" if saved else "not_found")
        except Exception as exc:
            self.store.update_workflow_job(job["id"], state="failed", error=str(exc))
            raise
        return {
            "status": "candidates_found" if saved else "not_found",
            "job_id": job["id"],
            "query": text,
            "region": self.store.get_workflow_job(job["id"])["region"],
            "candidates": saved,
            "message": f"已按核心关键词“{search_text}”搜索，请选择一个候选工作流后再安装。" if saved else f"没有找到核心关键词“{search_text}”对应的工作流。",
        }


    def select_workflow(self, job_id: Any, candidate_id: Any) -> dict[str, Any]:
        job_key = str(job_id or "").strip()
        candidate_key = str(candidate_id or "").strip()
        job = self.store.get_workflow_job(job_key)
        if job is None:
            raise ValueError("工作流搜索任务不存在。")
        candidate = self.store.workflow_candidate(job_key, candidate_key)
        if candidate is None:
            raise ValueError("工作流候选不存在或已过期。")
        # Selecting the same candidate again is also how the UI restores a
        # workflow card after it has been rebuilt.  Do not throw away the
        # inspection, source selections, or resumable download snapshot when
        # that happens.
        metadata = dict(job.get("metadata") or {})
        metadata["candidate"] = candidate
        has_execution_failure = bool(
            isinstance(metadata.get("execution_diagnostic"), dict)
            and metadata.get("execution_diagnostic")
        ) or bool(str(metadata.get("execution_error") or "").strip())
        updated = self.store.update_workflow_job(
            job_key,
            # Restoring a card after a failed execution calls this endpoint
            # again.  Keep the failure terminal state so a reload cannot hide
            # the persisted diagnostic by turning it back into "selected".
            state="failed" if has_execution_failure else "selected",
            selected_candidate_id=candidate_key,
            metadata=metadata,
        )
        return {"status": "selected", "job": updated, "candidate": candidate}


    async def inspect_selected_workflow(self, job_id: Any, *, loaded_nodes: list[str] | None = None) -> dict[str, Any]:
        job = self.store.get_workflow_job(str(job_id or "").strip())
        if job is None:
            raise ValueError("工作流搜索任务不存在。")
        candidate = self.store.workflow_candidate(job["id"], job.get("selected_candidate_id"))
        if candidate is None:
            raise ValueError("请先选择一个工作流候选。")
        node_info: dict[str, Any] | None = None
        if loaded_nodes is None:
            loaded_nodes_error = ""
            loaded_nodes_checked = False
            try:
                comfy_executor = ComfyUIWorkflowExecutor(self.settings)
                node_info = await asyncio.to_thread(comfy_executor.object_info)
                loaded_nodes_checked = True
                loaded_nodes = sorted(str(name) for name in node_info if name)
            except Exception as exc:
                loaded_nodes = None
                loaded_nodes_error = str(exc)
        else:
            loaded_nodes_checked = True
            loaded_nodes_error = ""
        if candidate.get("source_type") == "uploaded":
            workflow = (job.get("metadata") or {}).get("workflow")
            if not isinstance(workflow, dict):
                raise ValueError("上传工作流的内容已不存在，无法重新检查。")
        else:
            provider = RunningHubProvider(self.settings)
            workflow = await asyncio.to_thread(provider.get_workflow, candidate)
        report = inspect_workflow(
            workflow,
            loaded_nodes=loaded_nodes,
            loaded_nodes_error=loaded_nodes_error,
            loaded_nodes_checked=loaded_nodes_checked,
            loader_choices=node_info,
        )
        report["loaded_node_types"] = sorted(str(name) for name in (loaded_nodes or [])) if loaded_nodes_checked else []
        # Keep the provider's workflow untouched during selection/preparation.
        # UI canvas exports may contain widget values for nodes that are not
        # present in the current ComfyUI /object_info response yet (for
        # example, a custom SetNode).  The user must still be able to reach
        # the dependency and confirmation UI; API-prompt conversion is an
        # execution preflight and is performed by execute_workflow instead.
        settings_data = self.settings.load() if hasattr(self.settings, "load") else dict(self.settings or {})
        comfyui_root = str(settings_data.get("comfyui_directory") or "").strip()
        # ``_model_folders`` may call ComfyUI's /internal/folder_paths endpoint.
        # The workflow inspection handler itself runs on ComfyUI's aiohttp event
        # loop, so doing this synchronously here would deadlock that loop while
        # waiting for a request served by the same process and surface as a
        # browser-side "Fetch timeout".  Keep all blocking local HTTP/filesystem
        # work off the event loop, just like the /object_info request above.
        folders, folder_source, folder_warning = await asyncio.to_thread(
            self._model_folders,
            comfyui_root,
        )
        for model in report.get("models", []):
            category = self._model_category(model, workflow)
            metadata_directory_invalid = bool(model.get("metadata_directory_invalid")) or self._model_directory_invalid(model)
            directory_invalid = metadata_directory_invalid and not self._model_directory_resolved(
                model, category, folders, folder_source
            )
            if metadata_directory_invalid:
                model["metadata_directory_invalid"] = True
            model["directory_invalid"] = directory_invalid
            local_paths = [] if directory_invalid else [
                str(path) for path in self._local_model_exact_matches(folders, category, model.get("name"))
            ]
            similar_local_candidates = [] if directory_invalid else self._local_model_similar_candidates(
                folders, category, model.get("name")
            )
            model["local_paths"] = local_paths
            if similar_local_candidates:
                model["similar_local_candidates"] = similar_local_candidates
            else:
                model.pop("similar_local_candidates", None)
            model["local_status"] = "missing_directory" if directory_invalid else "installed" if local_paths else "missing"
            model["download_status"] = "missing_directory" if directory_invalid else "ready" if local_paths else "source_required"
            model["category"] = category
            model["directory_resolved"] = bool(category and folders.get(category))
        report["folder_paths_source"] = folder_source
        report["folder_paths"] = {
            category: [str(path) for path in paths]
            for category, paths in folders.items()
            if paths
        }
        if folder_warning:
            report["folder_paths_warning"] = folder_warning
        metadata = dict(job.get("metadata") or {})
        prepared = dict(metadata.get("prepared_node_results") or {})
        progress = metadata.get("node_install_progress") or {}
        failed_node = str(progress.get("current_node") or "").strip()
        terminal_install_failure = progress.get("status") == "failed" or progress.get("interrupted") is True
        loaded_set = {str(name) for name in (loaded_nodes or [])} if loaded_nodes_checked else set()
        prepared_changed = False
        if loaded_nodes_checked and terminal_install_failure and failed_node and failed_node not in loaded_set:
            failed_instances = [
                dict(instance)
                for instance in report.get("node_instances") or []
                if str(instance.get("class_type") or "") == failed_node
            ]
            if failed_instances:
                for name_field, instance_field, details_field in (
                    ("missing_nodes", "missing_node_instances", "missing_node_details"),
                    ("canvas_missing_nodes", "canvas_missing_node_instances", "canvas_missing_node_details"),
                    ("installable_missing_nodes", "installable_missing_node_instances", "installable_missing_node_details"),
                ):
                    names = list(dict.fromkeys([*(report.get(name_field) or []), failed_node]))
                    instances = [
                        dict(instance)
                        for instance in report.get(instance_field) or []
                        if str(instance.get("class_type") or "") != failed_node
                    ]
                    instances.extend(failed_instances)
                    report[name_field] = sorted(str(name) for name in names)
                    report[instance_field] = instances
                    details = [
                        dict(detail)
                        for detail in report.get(details_field) or []
                        if str(detail.get("class_type") or "") != failed_node
                    ]
                    details.append({
                        "class_type": failed_node,
                        "count": len(failed_instances),
                        "node_ids": [str(instance.get("id") or "") for instance in failed_instances],
                    })
                    report[details_field] = sorted(details, key=lambda detail: str(detail.get("class_type") or ""))
                report["missing_node_count"] = len(report.get("missing_node_instances") or [])
                report["canvas_missing_node_count"] = len(report.get("canvas_missing_node_instances") or [])
                report["installable_missing_node_count"] = len(report.get("installable_missing_node_instances") or [])
                report["missing_type_count"] = len(report.get("missing_node_details") or [])
                report["canvas_missing_type_count"] = len(report.get("canvas_missing_node_details") or [])
                report["installable_missing_type_count"] = len(report.get("installable_missing_node_details") or [])
                prepared.pop(failed_node, None)
                prepared_changed = True
        if loaded_nodes_checked:
            for class_type, result in prepared.items():
                if not isinstance(result, dict) or result.get("verification_status") != "restart_required":
                    continue
                result["node_registered"] = class_type in loaded_set
                result["verification_status"] = "loaded" if class_type in loaded_set else "restart_required"
                prepared_changed = True
        if prepared_changed:
            metadata["prepared_node_results"] = prepared
        metadata["inspection"] = report
        metadata["workflow"] = workflow
        updated = self.store.update_workflow_job(job["id"], state="checking_dependencies", metadata=metadata)
        return {"status": "checked", "job": updated, "candidate": candidate, "inspection": report, "workflow": workflow}


    async def inspect_latest_selected_workflow(self) -> dict[str, Any]:
        job = self.store.latest_selected_workflow_job()
        if job is None:
            raise ValueError("当前没有用户已选择的工作流候选。")
        return await self.inspect_selected_workflow(job["id"])
