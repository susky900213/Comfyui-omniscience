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


class WorkflowModelSourcesMixin:
    """WorkflowModelSourcesMixin implementation."""

    async def search_workflow_model_sources(self, job_id: Any, model_name: Any = None) -> dict[str, Any]:
        """Inspect local files first, then search exact filenames via web search and Hugging Face.

        When ``model_name`` is supplied, only that unresolved model is searched.
        The empty/default value preserves the original batch-search behavior.
        """
        job = self.workflow_job(job_id)
        metadata = dict(job.get("metadata") or {})
        plan = metadata.get("dependency_plan") or self.dependency_plan(job_id)
        metadata = dict((self.store.get_workflow_job(job["id"]) or job).get("metadata") or {})
        requested_name = str(model_name or "").strip()
        if requested_name:
            requested_model = next(
                (item for item in plan.get("models", []) if str(item.get("name") or "") == requested_name),
                None,
            )
            if requested_model is None:
                raise ValueError("该工作流没有此缺失模型，不能重新搜索。")
            if requested_model.get("source") or requested_model.get("status") in {"installed", "selected_local"}:
                raise ValueError("该模型已有可用来源，无需重新搜索。")
        selections = metadata.get("model_source_selections") or {}
        prior_results = dict(metadata.get("model_search_results") or {})
        results: dict[str, list[dict[str, Any]]] = {}
        local_results: dict[str, list[str]] = {}
        warnings: dict[str, str] = {}
        search_urls: dict[str, str] = {}
        for model in plan.get("models", []):
            name = str(model.get("name") or "")
            if requested_name and name != requested_name:
                continue
            if model.get("source") or model.get("status") in {"installed", "selected_local"} or name in selections:
                continue
            local_candidates = [str(path) for path in model.get("local_candidates") or [] if str(path).strip()]
            if local_candidates:
                local_results[name] = local_candidates
            searcher = ModelSourceSearcher(self.settings)
            basename = Path(name.replace("\\", "/")).name
            try:
                candidates = await asyncio.to_thread(searcher.search, basename)
            except ValueError as exc:
                candidates = []
                warnings[name] = str(exc)
            if not isinstance(candidates, list):
                candidates = []
            if candidates and local_candidates:
                try:
                    candidates = await asyncio.to_thread(searcher.enrich_sha256, candidates)
                except ValueError as exc:
                    warnings.setdefault(name, str(exc))
            if basename:
                search_urls[name] = searcher.bing_search_url(basename)
            prior_results[name] = candidates
            results[name] = candidates
        metadata["model_search_results"] = prior_results
        self.store.update_workflow_job(job["id"], metadata=metadata)
        refreshed_plan = self.dependency_plan(job["id"])
        message = "已先检查 ComfyUI 本地模型，并搜索未解决模型的 Bing/Google Hugging Face 第一页结果，再回退 Hugging Face API；本地存在相同 SHA-256 的模型时会自动替换工作流中的模型文件名，只有没有本地匹配时才需要选择远程来源。选择远程来源后仍需单独确认下载。"
        if warnings:
            message += " 部分远程搜索受限或失败，不影响查看和选择本地模型。"
        return {
            "job_id": job["id"], "results": results, "local_results": local_results,
            "models": refreshed_plan.get("models", []),
            "warnings": warnings, "search_urls": search_urls, "message": message,
        }


    def select_workflow_model_source(self, job_id: Any, model_name: Any, source: Any) -> dict[str, Any]:
        name = str(model_name or "").strip()
        job = self.workflow_job(job_id)
        metadata = dict(job.get("metadata") or {})
        inspection = metadata.get("inspection") or {}
        models = inspection.get("models") or []
        match = next((model for model in models if str(model.get("name") or "") == name), None)
        if match is None or match.get("local_status") == "installed" or match.get("loader_status") == "recognized":
            raise ValueError("该工作流没有此缺失模型，不能选择下载来源。")
        if not name or name in {".", ".."}:
            raise ValueError("模型名称无效。")
        candidates = (metadata.get("model_search_results") or {}).get(name, [])
        manual_url = str((source or {}).get("url") or "").strip() if isinstance(source, dict) else ""
        repo_id = str((source or {}).get("repo_id") or "") if isinstance(source, dict) else ""
        filename = str((source or {}).get("filename") or "") if isinstance(source, dict) else ""
        if manual_url:
            selected = ModelSourceSearcher(self.settings).manual_source(manual_url, Path(name.replace("\\", "/")).name)
        else:
            selected = next((item for item in candidates if item.get("repo_id") == repo_id and item.get("filename") == filename), None)
            if selected is None:
                raise ValueError("模型来源不在本次 Hugging Face 搜索结果中，请重新搜索并从列表中选择。")
            repo_id = str(selected.get("repo_id") or "")
            filename = str(selected.get("filename") or "")
        repo_id = str(selected.get("repo_id") or repo_id)
        filename = str(selected.get("filename") or filename)
        normalized_filename = filename.replace("\\", "/")
        if not model_filenames_match(
            Path(normalized_filename).name,
            Path(name.replace("\\", "/")).name,
        ) or ".." in Path(normalized_filename).parts:
            raise ValueError("所选仓库中的模型文件名与工作流依赖不匹配。")
        safe_source = {
            "repo_id": repo_id,
            "filename": filename,
            "category": self._model_category(match, metadata.get("workflow")),
            "sha256": str(selected.get("sha256") or ""),
        }
        for key in ("source_type", "source_url", "region", "revision", "title", "snippet"):
            value = selected.get(key)
            if value:
                safe_source[key] = str(value)
        selections = dict(metadata.get("model_source_selections") or {})
        selections[name] = safe_source
        metadata["model_source_selections"] = selections
        local_selections = dict(metadata.get("local_model_selections") or {})
        local_selections.pop(name, None)
        metadata["local_model_selections"] = local_selections
        self.store.update_workflow_job(job["id"], metadata=metadata)
        plan = self.dependency_plan(job["id"])
        return {"status": "source_selected", "job_id": job["id"], "name": name, "source": safe_source, "plan": plan}


    def select_workflow_local_model(self, job_id: Any, model_name: Any, selected_path: Any) -> dict[str, Any]:
        """Use a listed local ComfyUI model and update the workflow's model reference."""
        name = str(model_name or "").strip()
        relative_path = str(selected_path or "").strip().replace("\\", "/")
        job = self.workflow_job(job_id)
        metadata = dict(job.get("metadata") or {})
        inspection = metadata.get("inspection") or {}
        model = next((item for item in inspection.get("models", []) if str(item.get("name") or "") == name), None)
        if model is None or model.get("local_status") == "installed" or model.get("loader_status") == "recognized":
            raise ValueError("该工作流没有此缺失模型，不能选择本地模型文件。")
        if not relative_path or relative_path.startswith("/") or ".." in Path(relative_path).parts:
            raise ValueError("本地模型文件路径无效。")
        plan = metadata.get("dependency_plan") or self.dependency_plan(job["id"])
        planned = next((item for item in plan.get("models", []) if item.get("name") == name), None)
        if planned is None or relative_path not in (planned.get("local_candidates") or []):
            raise ValueError("所选文件不在本次 ComfyUI 模型列表中，请刷新依赖计划。")
        settings_data = self.settings.load() if hasattr(self.settings, "load") else dict(self.settings or {})
        comfyui_root = str(settings_data.get("comfyui_directory") or "").strip()
        if not comfyui_root:
            raise ValueError("请先配置 ComfyUI 目录。")
        category, separator, category_relative_path = relative_path.partition("/")
        if not separator or category not in CATEGORIES or not category_relative_path:
            raise ValueError("本地模型文件路径必须包含有效类别和文件名。")
        folders, _folder_source, _folder_warning = self._model_folders(comfyui_root)
        model_file = self._selected_local_file(folders, category, category_relative_path)
        if model_file is None:
            raise ValueError("所选本地模型文件已不存在，请刷新依赖计划。")
        workflow = metadata.get("workflow")
        if not isinstance(workflow, dict) or not self._replace_workflow_model_reference(workflow, model, category_relative_path):
            raise ValueError("无法在工作流中安全替换该模型引用。")
        metadata["workflow"] = workflow
        local_selections = dict(metadata.get("local_model_selections") or {})
        local_selections[name] = {"path": str(model_file), "workflow_path": category_relative_path, "category": category}
        metadata["local_model_selections"] = local_selections
        model_sources = dict(metadata.get("model_source_selections") or {})
        model_sources.pop(name, None)
        metadata["model_source_selections"] = model_sources
        metadata.pop("dependency_plan", None)
        updated = self.store.update_workflow_job(job["id"], state="selected", metadata=metadata)
        return {"status": "local_model_selected", "job_id": job["id"], "name": name, "path": str(model_file), "job": updated}


    @staticmethod
    def _replace_workflow_model_reference(workflow: dict[str, Any], model: dict[str, Any], selected_path: str) -> bool:
        sources: list[Any] = []
        if isinstance(workflow.get("prompt"), dict):
            sources.append(workflow["prompt"])
        if isinstance(workflow.get("nodes"), list):
            sources.append(workflow["nodes"])
        if not sources:
            sources.append(workflow.get("nodes"))
        target_nodes = {str(node_id) for node_id in (model.get("nodes") or [])}
        field = str(model.get("field") or "")
        old_value = str(model.get("name") or "")
        changed = False
        for source in sources:
            if isinstance(source, dict):
                entries = [(str(node_id), node) for node_id, node in source.items() if isinstance(node, dict)]
            elif isinstance(source, list):
                entries = [(str(node.get("id") or index), node) for index, node in enumerate(source) if isinstance(node, dict)]
            else:
                continue
            for node_id, node in entries:
                if node_id not in target_nodes:
                    continue
                if field.startswith("widget_"):
                    try:
                        index = int(field.removeprefix("widget_"))
                        widgets = node.get("widgets_values")
                        if isinstance(widgets, list) and index < len(widgets) and str(widgets[index]) == old_value:
                            widgets[index] = selected_path
                            changed = True
                        # The API prompt has named inputs after conversion,
                        # while the inspection report may still identify the
                        # original canvas value as widget_N.  Update the
                        # matching value in the same node without guessing a
                        # field name.
                        values = node.get("inputs") if isinstance(node.get("inputs"), dict) else None
                        if values is not None:
                            for name, value in list(values.items()):
                                if str(value) == old_value:
                                    values[name] = selected_path
                                    changed = True
                    except ValueError:
                        pass
                else:
                    values = node.get("inputs") if isinstance(node.get("inputs"), dict) else None
                    if values is not None and str(values.get(field) or "") == old_value:
                        values[field] = selected_path
                        changed = True
        return changed
