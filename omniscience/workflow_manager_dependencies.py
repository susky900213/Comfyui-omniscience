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


class WorkflowDependencyMixin:
    """WorkflowDependencyMixin implementation."""

    def dependency_plan(self, job_id: Any) -> dict[str, Any]:
        job = self.workflow_job(job_id)
        job_metadata = dict(job.get("metadata") or {})
        inspection = job_metadata.get("inspection") or {}
        # A dependency request can arrive before the browser has restored the
        # card and called the async inspection endpoint.  Do not let an old
        # cached report keep the install card out of sync with ComfyUI.
        if (
            isinstance(job_metadata.get("workflow"), dict)
            and inspection.get("inspection_version") != WORKFLOW_INSPECTION_VERSION
        ):
            try:
                loaded = ComfyUIWorkflowExecutor(self.settings, request_timeout=3.0).object_info()
                refreshed = inspect_workflow(
                    job_metadata["workflow"],
                    loaded_nodes=loaded,
                    loaded_nodes_checked=True,
                    loader_choices=loaded,
                )
                old_models = [item for item in inspection.get("models") or [] if isinstance(item, dict)]
                for model in refreshed.get("models") or []:
                    previous = self._find_previous_model(model, old_models)
                    if isinstance(previous, dict):
                        self._merge_persisted_model_state(model, previous)
                inspection = refreshed
                job_metadata["inspection"] = inspection
                self.store.update_workflow_job(job["id"], metadata=job_metadata)
            except Exception:
                # The async inspection endpoint will report the connection
                # problem to the UI.  A dependency read must remain usable for
                # legacy jobs when ComfyUI is temporarily unavailable.
                pass
        inspection = job_metadata.get("inspection") or {}
        # ``missing_nodes`` is the historical executable-only report.  The
        # install card must instead use the canvas dependency set so a bypassed
        # node is not silently omitted from the list ComfyUI needs to render.
        missing_nodes = [
            str(name)
            for name in (
                inspection.get("installable_missing_nodes")
                or inspection.get("missing_nodes")
                or []
            )
            if str(name).strip()
        ]
        missing_node_instances = [
            item for item in (
                inspection.get("installable_missing_node_instances")
                or inspection.get("missing_node_instances")
                or []
            )
            if isinstance(item, dict) and str(item.get("class_type") or "").strip()
        ]
        # Reports created before instance-level attribution only contain the
        # de-duplicated type list.  Reconstruct a minimal instance record for
        # those jobs so dependency planning remains resumable.
        if not missing_node_instances:
            missing_node_instances = [
                {"id": "", "class_type": name, "aux_id": "", "properties": {}}
                for name in missing_nodes
            ]
        unverified_nodes = inspection.get("unverified_nodes") or []
        prepared_models = (job.get("metadata") or {}).get("prepared_models") or []
        prepared_names = {
            str(item.get("name") or "")
            for item in prepared_models
            if isinstance(item, dict) and item.get("selection_type") != "local"
        }
        local_model_selections = dict((job.get("metadata") or {}).get("local_model_selections") or {})
        models = [
            model for model in (inspection.get("models") or [])
            if str(model.get("name") or "") not in prepared_names
        ]
        recognized_models = [
            model for model in (inspection.get("models") or [])
            if model.get("loader_status") == "recognized"
        ]
        candidate = (job.get("metadata") or {}).get("candidate") or {}
        node_sources = candidate.get("node_sources") or []
        selected_node_sources = job_metadata.get("node_source_selections") or {}
        node_search_results = job_metadata.get("node_search_results") or {}
        node_manager_results = job_metadata.get("node_manager_results") or {}
        settings_data = self.settings.load() if hasattr(self.settings, "load") else dict(self.settings or {})
        comfyui_root = str(settings_data.get("comfyui_directory") or "").strip()
        models_root = Path(comfyui_root).expanduser().resolve() / "models" if comfyui_root else Path("/__missing_comfyui_models__")
        nodes = []
        plugin_groups: dict[str, dict[str, Any]] = {}
        unresolved_nodes: list[dict[str, Any]] = []
        from .agent.node_install import NodeInstallerMixin
        manager_client = ComfyUIManagerClient(self.settings)

        for name in missing_nodes:
            instances = [
                dict(item)
                for item in missing_node_instances
                if str(item.get("class_type") or "") == name
            ]
            aux_ids = sorted(
                {
                    str(item.get("aux_id") or "").strip()
                    for item in instances
                    if str(item.get("aux_id") or "").strip()
                }
            )
            workflow_source = next(
                (
                    item
                    for item in node_sources
                    if isinstance(item, dict)
                    and (name == item.get("class_type") or name in (item.get("class_types") or []))
                ),
                {},
            )
            if not isinstance(workflow_source, dict):
                workflow_source = {}
            selected = selected_node_sources.get(name)
            aux_id_for_lookup = ""
            if len(aux_ids) == 1:
                aux_id_for_lookup = aux_ids[0]
            local_manager_candidates = manager_client.preferred_candidates(name, aux_id=aux_id_for_lookup)
            if local_manager_candidates:
                # A persisted search result may have selected another package
                # for the same class name.  The installed Manager extension map
                # is stronger evidence and must replace stale/ambiguous search
                # results, while an explicit user selection remains available
                # for validation below.
                cached_candidates = self._merge_node_source_candidates(
                    local_manager_candidates,
                    selected,
                )
            else:
                cached_candidates = self._merge_node_source_candidates(
                    node_manager_results.get(name),
                    node_search_results.get(name),
                    workflow_source,
                    selected,
                )
            selected_by_instance = selected.get("instances") if isinstance(selected, dict) else None
            resolved_instances = []
            for instance in instances:
                instance_key = str(instance.get("id") or "")
                instance_selection = selected
                if isinstance(selected_by_instance, dict):
                    instance_selection = selected_by_instance.get(instance_key)
                    if not isinstance(instance_selection, dict):
                        instance_selection = None
                resolved_instances.append(
                    self._resolve_node_instance(instance, cached_candidates, instance_selection)
                )
            confirmed_instances = [
                item for item in resolved_instances if item.get("source_selected")
            ]
            confirmed_identities = {
                self._node_plugin_identity(
                    item.get("candidate") if isinstance(item.get("candidate"), dict) else item
                )
                for item in confirmed_instances
            }
            confirmed_identities.discard("")
            source = ""
            manager_id = ""
            manager_candidate: dict[str, Any] = {}
            selected_commit = str(selected.get("commit") or "") if isinstance(selected, dict) else ""
            if len(confirmed_instances) == len(resolved_instances) and confirmed_instances and (
                len(confirmed_identities) == 1 or isinstance(selected, dict)
            ):
                first = confirmed_instances[0]
                source = str(first.get("source") or "").strip()
                manager_id = str(first.get("manager_id") or "").strip()
                if isinstance(first.get("candidate"), dict):
                    manager_candidate = dict(first["candidate"])
                if isinstance(selected, dict):
                    selected_commit = str(selected.get("commit") or selected_commit)
            if source:
                try:
                    host, owner, repository, prefix, _ref = NodeInstallerMixin._node_repository_source(source)
                    if prefix:
                        raise ValueError("节点来源必须指向仓库根目录。")
                    source = f"https://{host}/{owner}/{repository}"
                except (TypeError, ValueError):
                    source = None
            if source and selected_commit and re.fullmatch(r"[0-9a-fA-F]{40}", selected_commit):
                source = f"{source}/tree/{selected_commit}"
            source_is_ambiguous = any(item.get("status") == "ambiguous" for item in resolved_instances)
            source_is_partial = bool(resolved_instances) and len(confirmed_instances) != len(resolved_instances)
            node_item = {
                "class_type": name,
                "status": "missing",
                "source": source,
                "source_candidates": self._annotated_node_candidates(cached_candidates, ""),
                "manager": manager_candidate if manager_id else None,
                "manager_id": manager_id,
                "source_type": "comfyui_manager" if manager_id else "manual" if source else "unresolved",
                "source_selected": bool(source or manager_id) and not source_is_partial,
                "download_allowed": bool(source or manager_id) and not source_is_partial,
                "instances": resolved_instances,
                "aux_ids": aux_ids,
                "source_ambiguous": source_is_ambiguous,
                "source_partial": source_is_partial,
            }
            nodes.append(node_item)
            unresolved_for_type = [
                item for item in resolved_instances if not item.get("source_selected")
            ]
            if unresolved_for_type:
                unresolved_nodes.append(
                    {
                        "class_type": name,
                        "instances": unresolved_for_type,
                        "reason": "ambiguous" if source_is_ambiguous else "unresolved",
                    }
                )
            # Aggregate confirmed occurrences, not merely class types.  A
            # partially attributed type can therefore contribute its confirmed
            # instances while the remaining occurrences stay unresolved.
            for resolved in confirmed_instances:
                candidate_for_group = resolved.get("candidate")
                group_item = candidate_for_group if isinstance(candidate_for_group, dict) else resolved
                plugin_identity = self._node_plugin_identity(group_item)
                if not plugin_identity:
                    continue
                group = plugin_groups.setdefault(
                    plugin_identity,
                    {
                        "id": plugin_identity,
                        "source": resolved.get("source"),
                        "manager_id": resolved.get("manager_id"),
                        "manager": group_item if str(group_item.get("manager_id") or "").strip() else None,
                        "package_id": str(
                            group_item.get("install_id")
                            or group_item.get("id")
                            or group_item.get("manager_id")
                            or ""
                        ).strip(),
                        "package_name": str(
                            group_item.get("title")
                            or group_item.get("name")
                            or group_item.get("manager_id")
                            or resolved.get("source")
                            or plugin_identity
                        ).strip(),
                        "class_types": [],
                        "node_instances": [],
                        "node_count": 0,
                    },
                )
                if name not in group["class_types"]:
                    group["class_types"].append(name)
                group["node_instances"].append(resolved)
                group["node_count"] += 1
        folders, folder_source, folder_warning = self._model_folders(comfyui_root)
        installable_models = []
        for model in models:
            if model.get("local_status") != "installed":
                installable_models.append(model)
                continue
            category = self._model_category(model, job_metadata.get("workflow"))
            # Older inspection reports treated quantized filename variants as
            # installed. Re-check those reports against exact paths so the
            # variant can be offered as a user-selectable local candidate.
            if not folders.get(category) or self._local_model_exact_matches(
                folders, category, model.get("name")
            ):
                continue
            installable_models.append(model)
        models = installable_models
        planned_models = []
        model_source_selections = dict(job_metadata.get("model_source_selections") or {})
        model_search_results = job_metadata.get("model_search_results") or {}
        candidate_model_sources = candidate.get("model_sources") or []
        if not isinstance(candidate_model_sources, list):
            candidate_model_sources = []
        for model in models:
            model_name = str(model.get("name") or "")
            basename = Path(model_name.replace("\\", "/")).name
            category = self._model_category(model, job_metadata.get("workflow"))
            metadata_directory_invalid = bool(model.get("metadata_directory_invalid")) or self._model_directory_invalid(model)
            directory_invalid = metadata_directory_invalid and not self._model_directory_resolved(
                model, category, folders, folder_source
            )
            source = model_source_selections.get(model_name)
            if isinstance(source, dict):
                source = dict(source)
                source.setdefault("filename", basename)
                # The workflow's directory is authoritative for both download
                # classification and the eventual ComfyUI link destination.
                source["category"] = category
                repo_id = str(source.get("repo_id") or "")
                source_filename = str(source.get("filename") or "").replace("\\", "/")
                digest = str(source.get("sha256") or "").lower()
                if (
                    not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo_id)
                    or not model_filenames_match(Path(source_filename).name, basename)
                    or ".." in Path(source_filename).parts
                    or digest and not re.fullmatch(r"[0-9a-f]{64}", digest)
                ):
                    source = None
            local_matches = [] if directory_invalid else self._local_model_exact_matches(
                folders, category, model.get("name")
            )
            local_candidates = [] if directory_invalid else self._local_model_candidates(folders, category)
            similar_local_candidates = [] if directory_invalid else self._local_model_similar_candidates(
                folders, category, model.get("name")
            )
            source_candidates = model_search_results.get(model_name, [])
            if not isinstance(source_candidates, list):
                source_candidates = []
            expected_hashes = {
                digest
                for item in [source, *source_candidates, *candidate_model_sources]
                if isinstance(item, dict)
                and self._model_source_matches_name(item, basename)
                for digest in [self._valid_model_hash(item.get("sha256"))]
                if digest
            }
            matching_local_candidates = [] if directory_invalid else self._local_model_candidates_with_hash(
                folders, category, expected_hashes, basename
            )
            local_selection = local_model_selections.get(model_name)
            if directory_invalid:
                local_selection = None
                local_model_selections.pop(model_name, None)
            elif isinstance(local_selection, dict):
                selected_path = str(local_selection.get("workflow_path") or "").replace("\\", "/")
                selected_category = str(local_selection.get("category") or "")
                selected_file = self._selected_local_file(folders, selected_category, selected_path)
                if selected_category != category or selected_file is None:
                    local_selection = None
            if not local_selection and not local_matches and matching_local_candidates:
                selected_path = matching_local_candidates[0]
                selected_category, separator, category_relative_path = selected_path.partition("/")
                selected_file = self._selected_local_file(
                    folders, selected_category, category_relative_path
                )
                if (
                    separator
                    and selected_category == category
                    and selected_file is not None
                    and isinstance(job_metadata.get("workflow"), dict)
                    and self._replace_workflow_model_reference(
                        job_metadata["workflow"], model, category_relative_path
                    )
                ):
                    local_selection = {
                        "path": str(selected_file),
                        "workflow_path": category_relative_path,
                        "category": category,
                    }
                    local_model_selections[model_name] = local_selection
                    model_source_selections.pop(model_name, None)
                    source = None
            planned = {
                **model,
                "directory_invalid": directory_invalid,
                "metadata_directory_invalid": metadata_directory_invalid,
                "directory_resolved": bool(category and folders.get(category)),
                "status": "missing_directory" if directory_invalid else "selected_local" if local_selection else "installed" if local_matches else "ready" if source else "source_required",
                "source": source,
                "source_candidates": source_candidates,
                "local_paths": [str(item) for item in local_matches],
                "local_candidates": local_candidates,
                "similar_local_candidates": similar_local_candidates,
                "local_selection": local_selection,
                "category": category,
                "download_allowed": bool(source) and not directory_invalid,
            }
            if similar_local_candidates:
                planned["similar_local_candidates"] = similar_local_candidates
            else:
                planned.pop("similar_local_candidates", None)
            # A loader-recognized model is normally omitted from the dependency
            # card because ComfyUI already knows how to load it.  Keep it only
            # when the current filesystem contains a selectable quantized/local
            # variant; otherwise the old omission behavior remains unchanged.
            if model.get("loader_status") == "recognized" and not similar_local_candidates:
                continue
            planned_models.append(planned)
        # ``models`` is also used below to decide whether the card requires user
        # confirmation.  It must describe the same visible model set as the
        # serialized plan, not the pre-filter inspection list.
        models = planned_models
        plan = {
            "job_id": job["id"],
            "state": job.get("state"),
            "nodes": nodes,
            "workflow_node_count": inspection.get("node_count", 0),
            "workflow_node_types": inspection.get("canvas_class_types") or inspection.get("class_types") or [],
            "canvas_node_count": inspection.get("canvas_node_count", inspection.get("node_count", 0)),
            "canvas_missing_nodes": inspection.get("canvas_missing_nodes") or missing_nodes,
            "canvas_missing_node_count": inspection.get("canvas_missing_node_count", len(missing_node_instances)),
            "installable_missing_nodes": missing_nodes,
            "installable_missing_type_count": len(missing_nodes),
            "installable_missing_node_count": len(missing_node_instances),
            "installable_plugins": list(plugin_groups.values()),
            "installable_plugin_count": len(plugin_groups),
            "frontend_only_nodes": inspection.get("frontend_only_nodes") or [],
            "frontend_only_type_count": inspection.get("frontend_only_type_count", 0),
            "frontend_only_node_count": inspection.get("frontend_only_node_count", 0),
            "plugin_groups": list(plugin_groups.values()),
            "unresolved_nodes": unresolved_nodes,
            "unresolved_node_types": [item["class_type"] for item in unresolved_nodes],
            "unresolved_instances": [
                instance
                for item in unresolved_nodes
                for instance in item.get("instances") or []
            ],
            "node_instance_count": sum(len(item.get("instances") or []) for item in nodes),
            "resolved_node_instance_count": sum(
                sum(1 for instance in item.get("instances") or [] if instance.get("source_selected"))
                for item in nodes
            ),
            "unverified_nodes": [
                {"class_type": name, "status": "unverified", "download_allowed": False}
                for name in unverified_nodes
            ],
            "models": planned_models,
            "recognized_models": recognized_models,
            "missing_models": [item for item in planned_models if item.get("status") not in {"installed", "selected_local", "recognized"}],
            "model_download_ready": bool(planned_models) and all(
                item.get("status") in {"installed", "selected_local", "recognized"} or item.get("download_allowed")
                for item in planned_models
            ),
            "restart_required": bool(missing_nodes),
            "requires_user_confirmation": bool(missing_nodes or models),
            "folder_paths_source": folder_source,
            "folder_paths": {
                category: [str(path) for path in paths]
                for category, paths in folders.items()
                if paths
            },
            "folder_paths_warning": folder_warning,
            "message": "缺失节点优先使用 ComfyUI-Manager 的精确映射和安装队列；没有映射时只能由用户手动提供 GitHub 地址。手动来源会在安装前进行安全扫描和节点导出校验。",
        }
        metadata = dict(job_metadata)
        if local_model_selections != (metadata.get("local_model_selections") or {}):
            metadata["local_model_selections"] = local_model_selections
            metadata["model_source_selections"] = model_source_selections
            metadata["workflow"] = job_metadata.get("workflow")
        metadata["dependency_plan"] = plan
        self.store.update_workflow_job(job["id"], state="selected", metadata=metadata)
        return plan
