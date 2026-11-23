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


class WorkflowNodeSourcesMixin:
    """WorkflowNodeSourcesMixin implementation."""

    async def search_workflow_node_sources(self, job_id: Any) -> dict[str, Any]:
        """Resolve missing nodes through ComfyUI-Manager's exact mappings.

        GitHub repository text search is intentionally not used here: matching a
        class name in a README is not evidence that the package registers it in
        ``NODE_CLASS_MAPPINGS``.  The panel can provide a manual GitHub URL when
        Manager has no mapping.
        """
        job = self.workflow_job(job_id)
        plan = (job.get("metadata") or {}).get("dependency_plan") or self.dependency_plan(job_id)
        metadata = dict(job.get("metadata") or {})
        prior_results = dict(metadata.get("node_search_results") or {})
        manager_results = dict(metadata.get("node_manager_results") or {})
        unsearched = [
            item
            for item in plan.get("nodes", [])
            if (
                str(item.get("class_type") or "") not in manager_results
                or bool(
                    isinstance(manager_results.get(str(item.get("class_type") or "")), dict)
                    and (manager_results.get(str(item.get("class_type") or "")) or {}).get("retryable")
                )
            )
        ]
        results: dict[str, list[dict[str, Any]]] = {
            str(item.get("class_type")): self._merge_node_source_candidates(
                manager_results.get(str(item.get("class_type"))),
                prior_results.get(str(item.get("class_type"))),
            )
            for item in plan.get("nodes", [])
        }
        # Resolve every unresolved class type in one request.  The Manager client
        # caches its mapping payload for this search pass, so large workflows do
        # not cause one full mapping download per node type.
        searched = unsearched
        manager_error = ""
        manager_client = ComfyUIManagerClient(self.settings)
        for item in searched:
            class_type = str(item.get("class_type") or "")
            aux_ids = [
                str(instance.get("aux_id") or "").strip()
                for instance in item.get("instances") or []
                if str(instance.get("aux_id") or "").strip()
            ]
            try:
                # ``resolve`` remains the compatibility entry point.  The
                # current client stores every candidate in ``_last_candidates``
                # so this response no longer discards ambiguous ownership.
                if len(set(aux_ids)) == 1:
                    candidate = await asyncio.to_thread(
                        manager_client.resolve, class_type, aux_id=aux_ids[0]
                    )
                else:
                    candidate = await asyncio.to_thread(manager_client.resolve, class_type)
                candidates = getattr(manager_client, "_last_candidates", None)
                if not isinstance(candidates, list) or not candidates:
                    candidates = [candidate] if candidate else []
            except ValueError as exc:
                manager_error = str(exc)
                candidate = None
                candidates = []
            results[class_type] = [item for item in candidates if isinstance(item, dict)]
            if results[class_type]:
                primary = dict(results[class_type][0])
                primary["candidates"] = results[class_type]
                manager_results[class_type] = primary
            else:
                manager_results[class_type] = {
                    "class_type": class_type,
                    "provider": "comfyui_manager",
                    "verified": False,
                    "match": "missing",
                    "retryable": bool(manager_error),
                    "message": "ComfyUI-Manager 没有找到该节点的精确映射。",
                }
        metadata["node_search_results"] = {
            **prior_results,
            **{class_type: results[class_type] for class_type in (str(item.get("class_type") or "") for item in searched)},
        }
        metadata["node_manager_results"] = manager_results
        metadata.pop("dependency_plan", None)
        self.store.update_workflow_job(job["id"], metadata=metadata)
        deferred = max(len(unsearched) - len(searched), 0)
        return {
            "job_id": job["id"],
            "results": results,
            "deferred": deferred,
            "manager_error": manager_error,
            "message": "已按 ComfyUI-Manager 节点映射进行精确解析；没有映射的节点不会展示 GitHub 模糊搜索结果。请手动输入并确认 GitHub 地址，安装前会执行安全扫描和 NODE_CLASS_MAPPINGS 校验。"
            + (f" 另有 {deferred} 个节点可再次解析。" if deferred else ""),
        }


    def select_workflow_node_source(
        self,
        job_id: Any,
        class_type: Any,
        source: Any,
        *,
        manager_id: Any = "",
        aux_id: Any = "",
        cnr_id: Any = "",
        install_id: Any = "",
        version: Any = "",
        install_type: Any = "",
        reference: Any = "",
        pip: Any = "",
        instance_id: Any = "",
        manual: bool = False,
    ) -> dict[str, Any]:
        """Persist a Manager mapping or an explicitly entered GitHub source."""
        name = str(class_type or "").strip()
        value = str(source or "").strip().rstrip("/")
        job = self.workflow_job(job_id)
        metadata = dict(job.get("metadata") or {})
        inspection = metadata.get("inspection") or {}
        installable_missing_nodes = (
            inspection.get("installable_missing_nodes")
            or inspection.get("missing_nodes")
            or []
        )
        if name not in installable_missing_nodes:
            raise ValueError("该工作流中没有此缺失节点，不能选择安装来源。")
        if name in (job.get("metadata") or {}).get("prepared_node_results", {}):
            raise ValueError("该节点已准备完成，不能再次更换来源。")
        from .agent.node_install import NodeInstallerMixin

        manager_result = (metadata.get("node_manager_results") or {}).get(name)
        manager_candidates = self._merge_node_source_candidates(
            manager_result,
            (metadata.get("node_search_results") or {}).get(name),
        )
        chosen_manager_id = str(manager_id or "").strip()
        if chosen_manager_id and not manual:
            matching_candidates = [
                item for item in manager_candidates
                if str(item.get("manager_id") or "").strip().casefold() == chosen_manager_id.casefold()
            ]
            requested_identity = {
                "cnr_id": str(cnr_id or "").strip(),
                "aux_id": str(aux_id or "").strip(),
                "install_id": str(install_id or "").strip(),
                "version": str(version or "").strip(),
                "install_type": str(install_type or "").strip(),
            }
            for field, requested in requested_identity.items():
                if requested:
                    matching_candidates = [
                        item for item in matching_candidates
                        if (
                            self._node_candidate_value(item, "version").casefold()
                            if field == "version"
                            else self._node_candidate_value(item, field).casefold()
                        ) == requested.casefold()
                    ]
            if aux_id:
                matching_candidates = [
                    item for item in matching_candidates
                    if self._node_candidate_matches_aux_id(item, aux_id)
                ]
            if not matching_candidates:
                raise ValueError("节点来源不是本次 ComfyUI-Manager 映射结果，请重新解析。")
            manager = matching_candidates[0]
            candidate_source = self._node_candidate_source(manager).strip().rstrip("/")
            submitted_source = str(source or "").strip().rstrip("/")
            if submitted_source and not candidate_source:
                raise ValueError("节点来源无法与本次 ComfyUI-Manager 映射候选核对，请重新解析。")
            if submitted_source and candidate_source:
                if submitted_source.casefold() != candidate_source.casefold():
                    raise ValueError("节点来源与本次 ComfyUI-Manager 映射候选不一致，请重新解析。")
            canonical = candidate_source
            selection = {
                "source": canonical,
                "manager_id": str(manager.get("manager_id") or chosen_manager_id).strip(),
                "manager": manager,
                "candidate": manager,
                "provider": "comfyui_manager",
                "selected_at": time.time(),
            }
            for key in ("install_id", "cnr_id", "aux_id", "version", "selected_version", "install_type", "reference", "pip", "title"):
                if manager.get(key) not in (None, "", [], {}):
                    selection[key] = manager[key]
        else:
            if not manual:
                raise ValueError("没有可用的 ComfyUI-Manager 节点包，请手动提供 GitHub 地址。")
            try:
                host, owner, repository, prefix, _ref = NodeInstallerMixin._node_repository_source(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"手动节点来源无效：{exc}") from exc
            if host != "github.com" or prefix:
                raise ValueError("手动兜底来源必须是 GitHub 仓库根目录 HTTPS 地址。")
            canonical = f"https://{host}/{owner}/{repository}"
            selection = {"source": canonical, "provider": "manual_github", "unverified": True, "selected_at": time.time()}
        selections = dict(metadata.get("node_source_selections") or {})
        if instance_id:
            existing = selections.get(name)
            if not isinstance(existing, dict):
                existing = {}
            instance_selections = existing.get("instances")
            if not isinstance(instance_selections, dict):
                instance_selections = {}
            instance_selections[str(instance_id)] = selection
            existing["instances"] = instance_selections
            selections[name] = existing
        else:
            selections[name] = selection
        metadata["node_source_selections"] = selections
        results = dict(metadata.get("node_search_results") or {})
        results.pop(name, None)
        metadata["node_search_results"] = results
        metadata.pop("dependency_plan", None)
        updated = self.store.update_workflow_job(job["id"], metadata=metadata)
        return {"status": "source_selected", "job_id": job["id"], "class_type": name, "source": canonical, "job": updated}
