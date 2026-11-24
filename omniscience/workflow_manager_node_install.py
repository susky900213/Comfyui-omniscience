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
from .region_router import RegionRouter
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


class WorkflowNodeInstallMixin:
    """WorkflowNodeInstallMixin implementation."""

    async def confirm_node_installation(
        self,
        job_id: Any,
        *,
        confirmed: bool = False,
        acknowledge_security_for: str = "",
        progress_callback: Any = None,
        class_types: Any = None,
    ) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("必须由用户明确确认后才能安装缺少的节点。")
        job = self.workflow_job(job_id)
        plan = (job.get("metadata") or {}).get("dependency_plan")
        if not plan:
            plan = await asyncio.to_thread(self.dependency_plan, job_id)
        if not plan.get("nodes"):
            return {"status": "nodes_prepared", "job_id": job["id"], "nodes": [], "models_pending": bool(plan.get("models"))}
        install_nodes = [item for item in plan.get("nodes", []) if isinstance(item, dict)]
        requested_types = {str(name).strip() for name in class_types} if isinstance(class_types, list) else None
        if requested_types is not None:
            available_types = {str(item.get("class_type") or "") for item in install_nodes}
            if not requested_types or not requested_types.issubset(available_types):
                raise ValueError("单节点安装请求包含无效或不属于当前工作流的节点类型。")
            install_nodes = [item for item in install_nodes if str(item.get("class_type") or "") in requested_types]
        unresolved = [
            item.get("class_type")
            for item in install_nodes
            if not item.get("source") and not item.get("manager_id")
        ]
        if unresolved:
            raise ValueError("以下缺失节点尚未选择 ComfyUI-Manager 安装包或手动 GitHub 来源：" + "、".join(str(item) for item in unresolved[:8]))
        metadata = dict(job.get("metadata") or {})
        prepared = dict(metadata.get("prepared_node_results") or {})
        installed = []

        async def verify_loaded(class_type: str, result: dict[str, Any]) -> dict[str, Any]:
            """Report the live ComfyUI registration without importing code here."""
            try:
                loaded = await asyncio.to_thread(ComfyUIWorkflowExecutor(self.settings).object_info)
                registered = class_type in loaded
                result["node_registered"] = registered
                result["verification_status"] = "loaded" if registered else "restart_required"
                if not registered:
                    result["message"] = (
                        f"节点文件已安装并完成安全扫描，但当前 ComfyUI /object_info 尚未注册“{class_type}”。"
                        "请重启 ComfyUI 后重新检查工作流。"
                    )
            except Exception as exc:
                result["node_registered"] = None
                result["verification_status"] = "restart_required"
                result["verification_error"] = str(exc)
            return result

        total_nodes = len(install_nodes)
        for index, item in enumerate(install_nodes):
            if not item.get("source") and not item.get("manager_id"):
                raise ValueError(f"节点 {item.get('class_type')} 没有可信安装来源。")
            class_type = str(item.get("class_type") or "")
            if callable(progress_callback):
                progress_callback(
                    {
                        "phase": "installing",
                        "current": index + 1,
                        "completed": index,
                        "current_node": class_type,
                        "current_plugin": self._node_install_package_name(item),
                        "total": total_nodes,
                    }
                )
            existing = prepared.get(class_type)
            if existing:
                installed.append(existing)
                if callable(progress_callback):
                    progress_callback(
                        {
                            "phase": "completed",
                            "completed": index + 1,
                            "completed_plugin": self._node_install_package_name(item),
                            "installed_path": result.get("path") or "",
                        }
                    )
                continue
            selected_sources = dict((self.store.get_workflow_job(job["id"]) or {}).get("metadata", {}).get("node_source_selections") or {})
            selected = dict(selected_sources.get(class_type) or {})
            provider = str(selected.get("provider") or item.get("source_type") or "").strip()
            manager_id = str(selected.get("manager_id") or item.get("manager_id") or "").strip()
            if provider == "comfyui_manager" or manager_id:
                manager = selected.get("manager") if isinstance(selected.get("manager"), dict) else item.get("manager")
                manager = manager if isinstance(manager, dict) else {}
                candidate = selected.get("candidate") if isinstance(selected.get("candidate"), dict) else manager
                if not isinstance(candidate, dict):
                    candidate = {}
                manager_source = str(item.get("source") or manager.get("repository") or "")
                self.store.update_workflow_job(job["id"], state="installing_nodes")
                try:
                    try:
                        manager_result = await asyncio.to_thread(
                            ComfyUIManagerClient(self.settings).install,
                            manager_id,
                            version=str(manager.get("selected_version") or manager.get("version") or selected.get("selected_version") or selected.get("version") or "latest"),
                            source=manager_source,
                            candidate=candidate,
                            on_progress=(
                                lambda update, node=class_type, plugin=self._node_install_package_name(item): progress_callback(
                                    {
                                        "phase": str(update.get("phase") or "installing"),
                                        "current_node": node,
                                        "current_plugin": plugin,
                                        "manager_task_id": update.get("manager_task_id"),
                                        "manager_id": update.get("manager_id"),
                                        "manager_status": update.get("manager_status"),
                                        "manager_messages": update.get("manager_messages") or [],
                                    }
                                )
                                if callable(progress_callback)
                                else None
                            ),
                        )
                    except Exception as manager_error:
                        try:
                            host, owner, repository, prefix, ref = self._node_repository_source(manager_source)
                        except (TypeError, ValueError) as exc:
                            raise ValueError(f"ComfyUI-Manager 安装失败，且没有可安全回退的 GitHub 仓库来源：{manager_error}") from exc
                        if host != "github.com" or prefix or ref:
                            raise ValueError(f"ComfyUI-Manager 安装失败；回退只允许已确认的 GitHub 仓库根目录：{manager_error}") from manager_error
                        from .agent.node_install import NodeInstallerMixin
                        existing_install = await NodeInstallerMixin.scan_installed_manager_node(
                            self,
                            manager_id=manager_id,
                            repository=manager_source,
                            expected_class_type=class_type,
                        )
                        if existing_install.get("status") == "manager_installed":
                            raise ValueError(
                                f"ComfyUI-Manager 报告安装失败，但已在 custom_nodes 找到 {existing_install.get('path') or manager_source}；"
                                "为避免重复覆盖，已跳过 GitHub 后备安装。"
                            ) from manager_error
                        install_region = str(selected.get("region") or "").strip() or RegionRouter(self.settings).decide().selected_region
                        try:
                            resolved_ref = await asyncio.to_thread(
                                self._repository_default_branch,
                                host,
                                owner,
                                repository,
                                region=install_region,
                                settings=self.settings,
                            )
                            commit = await asyncio.to_thread(
                                self._repository_commit,
                                host,
                                owner,
                                repository,
                                resolved_ref,
                                region=install_region,
                                settings=self.settings,
                            )
                        except Exception as metadata_error:
                            try:
                                fallback_result = await self.install_comfyui_node(
                                    manager_source,
                                    acknowledged=class_type == acknowledge_security_for,
                                    expected_class_type=class_type,
                                    region=install_region,
                                    on_progress=(
                                        lambda update, node=class_type, plugin=self._node_install_package_name(item): progress_callback(
                                            {**update, "current_node": node, "current_plugin": plugin}
                                        ) if callable(progress_callback) else None
                                    ),
                                    allow_unpinned_zip_fallback=True,
                                )
                            except Exception as zip_error:
                                from .agent.node_install import NodeSecurityBlockedError
                                if isinstance(zip_error, NodeSecurityBlockedError):
                                    raise
                                raise ValueError(f"ComfyUI-Manager 安装失败，GitHub 元数据与 main/master ZIP 后备均失败：{zip_error}") from metadata_error
                        else:
                            pinned_source = f"https://{host}/{owner}/{repository}/tree/{commit}"
                            try:
                                fallback_result = await self.install_comfyui_node(
                                    pinned_source,
                                    pin_commit=True,
                                    acknowledged=class_type == acknowledge_security_for,
                                    expected_class_type=class_type,
                                    region=install_region,
                                    on_progress=(
                                        lambda update, node=class_type, plugin=self._node_install_package_name(item): progress_callback(
                                            {**update, "current_node": node, "current_plugin": plugin}
                                        ) if callable(progress_callback) else None
                                    ),
                                )
                            except Exception as pinned_error:
                                raise ValueError(f"ComfyUI-Manager 安装失败，固定 commit 后备安装也失败：{pinned_error}") from manager_error
                        result = await verify_loaded(
                            class_type,
                            {
                                **fallback_result,
                                "class_type": class_type,
                                "manager_fallback": True,
                                "manager_error": str(manager_error),
                            },
                        )
                    else:
                        from .agent.node_install import NodeInstallerMixin

                        verification = await NodeInstallerMixin.scan_installed_manager_node(
                            self,
                            manager_id=manager_id,
                            repository=manager_source,
                            expected_class_type=class_type,
                        )
                        result = {**manager_result, **verification, "class_type": class_type}
                        if result.get("status") != "manager_installed":
                            result["requires_restart"] = True
                        result = await verify_loaded(class_type, result)
                except Exception as exc:
                    partial = dict(self.store.get_workflow_job(job["id"]) or job)
                    partial_metadata = dict(partial.get("metadata") or {})
                    partial_metadata["prepared_node_results"] = prepared
                    self.store.update_workflow_job(job["id"], state="selected", error=str(exc), metadata=partial_metadata)
                    raise
                prepared[class_type] = result
                partial_metadata = dict(self.store.get_workflow_job(job["id"]) or job)
                saved_metadata = dict(partial_metadata.get("metadata") or {})
                saved_metadata["prepared_node_results"] = prepared
                self.store.update_workflow_job(job["id"], state="selected", error="", metadata=saved_metadata)
                installed.append(result)
                if callable(progress_callback):
                    progress_callback(
                        {
                            "phase": "completed",
                            "completed": index + 1,
                            "completed_plugin": self._node_install_package_name(item),
                            "installed_path": result.get("path") or "",
                        }
                    )
                continue
            try:
                host, owner, repository, _prefix, ref = self._node_repository_source(item["source"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"节点 {class_type} 的 GitHub 来源无效：{exc}") from exc
            commit = str(selected.get("commit") or (ref if re.fullmatch(r"[0-9a-fA-F]{40}", str(ref or "")) else ""))
            if not commit:
                install_region = selected.get("region") if provider in {"web_search", "github_proxy"} else None
                resolved_ref = str(ref or await asyncio.to_thread(
                    self._repository_default_branch,
                    host,
                    owner,
                    repository,
                    region=install_region,
                    settings=self.settings,
                ))
                commit = await asyncio.to_thread(
                    self._repository_commit,
                    host,
                    owner,
                    repository,
                    resolved_ref,
                    region=install_region,
                    settings=self.settings,
                )
                selected.update({"source": f"https://{host}/{owner}/{repository}", "commit": commit, "pinned_at": time.time()})
                selected_sources[class_type] = selected
                fresh_job = self.store.get_workflow_job(job["id"]) or job
                fresh_metadata = dict(fresh_job.get("metadata") or {})
                fresh_metadata["node_source_selections"] = selected_sources
                self.store.update_workflow_job(job["id"], metadata=fresh_metadata)
            pinned_source = f"https://{host}/{owner}/{repository}/tree/{commit}"
            item["source"] = pinned_source
            self.store.update_workflow_job(job["id"], state="installing_nodes")
            try:
                install_kwargs = {
                    "pin_commit": True,
                    "acknowledged": class_type == acknowledge_security_for,
                    "expected_class_type": class_type,
                }
                if provider == "web_search" and selected.get("region"):
                    install_kwargs["region"] = selected["region"]
                result = await self.install_comfyui_node(item["source"], **install_kwargs)
                result = await verify_loaded(class_type, result)
            except Exception as exc:
                from .agent.node_install import NodeSecurityBlockedError

                if isinstance(exc, NodeSecurityBlockedError):
                    exc.report["class_type"] = class_type
                partial = dict(self.store.get_workflow_job(job["id"]) or job)
                partial_metadata = dict(partial.get("metadata") or {})
                partial_metadata["prepared_node_results"] = prepared
                self.store.update_workflow_job(job["id"], state="selected", error=str(exc), metadata=partial_metadata)
                raise
            prepared[class_type] = result
            partial_metadata = dict(self.store.get_workflow_job(job["id"]) or job)
            saved_metadata = dict(partial_metadata.get("metadata") or {})
            saved_metadata["prepared_node_results"] = prepared
            self.store.update_workflow_job(job["id"], state="selected", error="", metadata=saved_metadata)
            installed.append(result)
            if callable(progress_callback):
                progress_callback(
                    {
                        "phase": "completed",
                        "completed": index + 1,
                        "completed_plugin": self._node_install_package_name(item),
                    }
                )
        metadata = dict(self.store.get_workflow_job(job["id"]) or job).get("metadata") or {}
        metadata["prepared_nodes"] = installed
        self.store.update_workflow_job(job["id"], state="selected", metadata=metadata)
        return {"status": "nodes_prepared", "job_id": job["id"], "nodes": installed, "models_pending": bool(plan.get("models"))}
