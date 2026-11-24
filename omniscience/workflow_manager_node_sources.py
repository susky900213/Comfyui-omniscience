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
from .node_search import ComfyUIManagerClient, WebNodeSearcher
from .region_router import RegionRouter
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

    @staticmethod
    def _node_candidate_id(job_id: Any, class_type: Any, candidate: dict[str, Any], index: int) -> str:
        """Create a stable opaque id for one persisted candidate in one job."""
        identity = {
            key: candidate.get(key)
            for key in (
                "provider",
                "source",
                "repository",
                "reference",
                "manager_id",
                "install_id",
                "cnr_id",
                "aux_id",
                "version",
                "selected_version",
                "install_type",
                "region",
                "search_engine",
            )
            if candidate.get(key) not in (None, "", [], {})
        }
        identity["job_id"] = str(job_id)
        identity["class_type"] = str(class_type)
        identity["index"] = index
        encoded = json_text(identity).encode("utf-8")
        return "node_" + hashlib.sha256(encoded).hexdigest()[:24]

    @classmethod
    def _annotate_node_candidate_ids(
        cls, job_id: Any, class_type: Any, candidates: Any
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for index, candidate in enumerate(candidates if isinstance(candidates, list) else []):
            if not isinstance(candidate, dict):
                continue
            item = dict(candidate)
            item["provider"] = str(item.get("provider") or "comfyui_manager").strip()
            item["candidate_id"] = str(item.get("candidate_id") or cls._node_candidate_id(job_id, class_type, item, index))
            result.append(item)
        return result

    async def search_workflow_node_sources(self, job_id: Any) -> dict[str, Any]:
        """Resolve missing nodes through ComfyUI-Manager only."""
        job = self.workflow_job(job_id)
        plan = (job.get("metadata") or {}).get("dependency_plan") or self.dependency_plan(job_id)
        metadata = dict(job.get("metadata") or {})
        prior_results = dict(metadata.get("node_search_results") or {})
        manager_results = dict(metadata.get("node_manager_results") or {})
        results: dict[str, list[dict[str, Any]]] = {}
        manager_errors: dict[str, str] = {}
        manager_client = ComfyUIManagerClient(self.settings, request_timeout=5.0)
        for item in plan.get("nodes", []):
            class_type = str(item.get("class_type") or "")
            aux_ids = [
                str(instance.get("aux_id") or "").strip()
                for instance in item.get("instances") or []
                if str(instance.get("aux_id") or "").strip()
            ]
            try:
                if len(set(aux_ids)) == 1:
                    candidate = await asyncio.wait_for(
                        asyncio.to_thread(manager_client.resolve, class_type, aux_id=aux_ids[0]),
                        timeout=5.0,
                    )
                else:
                    candidate = await asyncio.wait_for(
                        asyncio.to_thread(manager_client.resolve, class_type),
                        timeout=5.0,
                    )
                candidates = getattr(manager_client, "_last_candidates", None)
                if not isinstance(candidates, list) or not candidates:
                    candidates = [candidate] if candidate else []
            except (ValueError, OSError, TimeoutError) as exc:
                manager_errors[class_type] = str(exc) or "Fetch timeout"
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
                    "retryable": True,
                    "message": manager_errors.get(class_type) or "ComfyUI-Manager 没有找到该节点的精确映射。",
                }
        metadata["node_search_results"] = {**prior_results, **results}
        metadata["node_manager_results"] = manager_results
        metadata.pop("dependency_plan", None)
        self.store.update_workflow_job(job["id"], metadata=metadata)
        return {"job_id": job["id"], "results": results, "manager_errors": manager_errors}

    async def search_workflow_node_web_sources(
        self, job_id: Any, class_types: Any = None
    ) -> dict[str, Any]:
        """Search the configured regional web engine for unresolved node types."""
        job = self.workflow_job(job_id)
        plan = (job.get("metadata") or {}).get("dependency_plan") or self.dependency_plan(job_id)
        metadata = dict(job.get("metadata") or {})
        prior_results = dict(metadata.get("node_search_results") or {})
        manager_results = dict(metadata.get("node_manager_results") or {})
        results: dict[str, list[dict[str, Any]]] = {}
        search_errors: dict[str, str] = {}
        search_urls: dict[str, str] = {}
        search_engines: dict[str, str] = {}
        requested_types = {str(name).strip() for name in class_types} if isinstance(class_types, list) else None
        for node in plan.get("nodes", []):
            class_type = str(node.get("class_type") or "")
            if requested_types is not None and class_type not in requested_types:
                continue
            existing = prior_results.get(class_type) or []
            if requested_types is not None and class_type not in requested_types:
                continue
            manager_candidates = [
                item for item in existing
                if isinstance(item, dict) and str(item.get("provider") or "") == "comfyui_manager"
            ]
            if manager_candidates:
                results[class_type] = manager_candidates
                continue
            try:
                decision = RegionRouter(self.settings).decide()
                engine, search_url = WebNodeSearcher.search_url(class_type, region=decision.selected_region)
                search_urls[class_type] = search_url
                search_engines[class_type] = engine
                web_candidates = await asyncio.wait_for(
                    asyncio.to_thread(WebNodeSearcher.search, class_type, region=decision.selected_region),
                    timeout=8.0,
                )
                results[class_type] = self._annotate_node_candidate_ids(job["id"], class_type, web_candidates[:1])
                for candidate in results[class_type]:
                    candidate["search_url"] = search_url
                    candidate["search_engine"] = engine
            except (ValueError, OSError, TimeoutError) as exc:
                search_errors[class_type] = str(exc) or "Web search timeout"
                results[class_type] = []
            if results[class_type]:
                primary = dict(results[class_type][0])
                primary["candidates"] = results[class_type]
                manager_results[class_type] = primary
            else:
                previous = manager_results.get(class_type) or {}
                previous.update({
                    "class_type": class_type,
                    "provider": "web_search",
                    "verified": False,
                    "match": "missing",
                    "retryable": True,
                    "search_error": search_errors.get(class_type, "无 GitHub 候选"),
                })
                manager_results[class_type] = previous
        metadata["node_search_results"] = {**prior_results, **results}
        metadata["node_manager_results"] = manager_results
        metadata.pop("dependency_plan", None)
        self.store.update_workflow_job(job["id"], metadata=metadata)
        return {
            "job_id": job["id"],
            "results": results,
            "search_errors": search_errors,
            "search_urls": search_urls,
            "search_engines": search_engines,
        }

    async def _search_workflow_node_sources_legacy(self, job_id: Any) -> dict[str, Any]:
        """Compatibility orchestration for callers that still request both phases."""
        manager = await self.search_workflow_node_sources(job_id)
        if any(not candidates for candidates in manager.get("results", {}).values()):
            web = await self.search_workflow_node_web_sources(job_id)
            return {**manager, **web}
        return manager

    async def search_workflow_node_sources_legacy(self, job_id: Any) -> dict[str, Any]:
        """Placeholder for the former combined search implementation."""
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
        manager_errors: dict[str, str] = {}
        search_errors: dict[str, str] = {}
        manager_client = ComfyUIManagerClient(self.settings, request_timeout=5.0)
        for item in searched:
            class_type = str(item.get("class_type") or "")
            node_manager_error = ""
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
                    candidate = await asyncio.wait_for(
                        asyncio.to_thread(manager_client.resolve, class_type, aux_id=aux_ids[0]),
                        timeout=5.0,
                    )
                else:
                    candidate = await asyncio.wait_for(
                        asyncio.to_thread(manager_client.resolve, class_type),
                        timeout=5.0,
                    )
                candidates = getattr(manager_client, "_last_candidates", None)
                if not isinstance(candidates, list) or not candidates:
                    candidates = [candidate] if candidate else []
            except (ValueError, OSError, TimeoutError) as exc:
                node_manager_error = str(exc)
                manager_error = node_manager_error
                manager_errors[class_type] = node_manager_error
                candidate = None
                candidates = []
            manager_resolution_error = str(getattr(manager_client, "_last_resolution_error", "") or "")
            manager_candidates = [item for item in candidates if isinstance(item, dict)]
            results[class_type] = manager_candidates
            if not results[class_type]:
                try:
                    decision = RegionRouter(self.settings).decide()
                    web_candidates = await asyncio.to_thread(
                        WebNodeSearcher.search,
                        class_type,
                        region=decision.selected_region,
                    )
                    results[class_type] = self._annotate_node_candidate_ids(
                        job["id"], class_type, web_candidates[:1]
                    )
                except (ValueError, OSError) as exc:
                    search_errors[class_type] = str(exc)
            if results[class_type]:
                primary = dict(results[class_type][0])
                primary["candidates"] = results[class_type]
                manager_results[class_type] = primary
            else:
                manager_results[class_type] = {
                    "class_type": class_type,
                    "provider": "comfyui_manager" if not search_errors.get(class_type) else "web_search",
                    "verified": False,
                    "match": "missing",
                    "retryable": bool(node_manager_error or manager_resolution_error or search_errors.get(class_type)),
                    "message": (
                        "ComfyUI-Manager 请求失败；已继续区域 Web 搜索，但没有找到 GitHub 候选。"
                        if node_manager_error or manager_resolution_error
                        else "ComfyUI-Manager 没有找到该节点的精确映射，区域 Web 搜索也没有找到候选。"
                    ),
                }
                if manager_resolution_error:
                    manager_results[class_type]["error"] = manager_resolution_error
                if search_errors.get(class_type):
                    manager_results[class_type]["search_error"] = search_errors[class_type]
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
            "manager_errors": manager_errors,
            "search_errors": search_errors,
            "message": "已先按 ComfyUI-Manager 精确解析；Manager 无候选时再使用区域 Web 搜索。请确认候选来源，安装前会执行安全扫描和 NODE_CLASS_MAPPINGS 校验。"
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
        candidate_id: Any = "",
        provider: Any = "",
        region: Any = "",
        branch: Any = "",
        commit: Any = "",
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
        requested_candidate_id = str(candidate_id or "").strip()
        selected_candidate = None
        if requested_candidate_id:
            selected_candidate = next(
                (
                    item for item in manager_candidates
                    if str(item.get("candidate_id") or "").strip() == requested_candidate_id
                ),
                None,
            )
            if not selected_candidate:
                raise ValueError("节点候选已过期或不属于本次搜索结果，请重新搜索。")
            persisted_provider = str(selected_candidate.get("provider") or "").strip()
            requested_provider = str(provider or persisted_provider).strip()
            if requested_provider != persisted_provider:
                raise ValueError("节点候选 provider 与本次搜索结果不一致，请重新搜索。")
            if requested_provider not in {"comfyui_manager", "web_search", "github_proxy"}:
                raise ValueError("节点候选 provider 无效。")
            if requested_provider == "web_search":
                if str(region or selected_candidate.get("region") or "").strip() != str(selected_candidate.get("region") or "").strip():
                    raise ValueError("区域节点候选的 region 与本次搜索结果不一致。")
                if str(selected_candidate.get("search_engine") or "").strip() not in {"bing", "google"}:
                    raise ValueError("区域节点候选缺少有效搜索引擎信息。")
            if value and self._node_candidate_source(selected_candidate).strip().rstrip("/").casefold() != value.casefold():
                raise ValueError("节点来源与已保存候选不一致，请重新搜索。")
            value = self._node_candidate_source(selected_candidate).strip().rstrip("/")
            provider = requested_provider
            manager_id = selected_candidate.get("manager_id") or manager_id
            aux_id = selected_candidate.get("aux_id") or aux_id
            cnr_id = selected_candidate.get("cnr_id") or cnr_id
            install_id = selected_candidate.get("install_id") or install_id
            version = selected_candidate.get("version") or selected_candidate.get("selected_version") or version
            install_type = selected_candidate.get("install_type") or install_type
        chosen_manager_id = str(manager_id or "").strip()
        if requested_candidate_id and str(provider or "").strip() == "web_search":
            try:
                host, owner, repository, prefix, ref = NodeInstallerMixin._node_repository_source(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Web 搜索节点来源无效：{exc}") from exc
            if host != "github.com" or prefix or ref:
                raise ValueError("Web 搜索候选必须是 GitHub 仓库根目录 HTTPS 地址。")
            canonical = f"https://{host}/{owner}/{repository}"
            selection = {
                "source": canonical,
                "provider": "web_search",
                "candidate_id": requested_candidate_id,
                "region": str(selected_candidate.get("region") or region or "").strip(),
                "search_engine": str(selected_candidate.get("search_engine") or "").strip(),
                "search_query": str(selected_candidate.get("search_query") or "").strip(),
                "candidate": selected_candidate,
                "branch": str(branch or selected_candidate.get("branch") or "").strip(),
                "commit": str(commit or selected_candidate.get("commit") or "").strip(),
                "selected_at": time.time(),
            }
        elif chosen_manager_id and not manual:
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
                "candidate_id": str(manager.get("candidate_id") or requested_candidate_id).strip(),
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
