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


class WorkflowModelFilesMixin:
    """WorkflowModelFilesMixin implementation."""

    @staticmethod
    def _valid_model_hash(value: Any) -> str:
        digest = str(value or "").strip().lower()
        return digest if re.fullmatch(r"[0-9a-f]{64}", digest) else ""


    @classmethod
    def _model_source_matches_name(cls, source: dict[str, Any], basename: str) -> bool:
        filename = str(source.get("filename") or "").replace("\\", "/")
        return bool(filename) and model_filenames_match(Path(filename).name, basename) and ".." not in Path(filename).parts


    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(MODEL_HASH_CHUNK_SIZE), b""):
                digest.update(chunk)
        return digest.hexdigest()


    @classmethod
    def _local_model_candidates_with_hash(
        cls,
        folders: dict[str, list[Path]],
        category: str,
        expected_hashes: set[str],
        workflow_basename: str,
    ) -> list[str]:
        """Return only differently named local files with a trusted matching hash."""
        if not expected_hashes:
            return []
        candidates: list[str] = []
        seen: set[str] = set()
        for folder in folders.get(category, []):
            for path, relative in cls._local_model_files(folder):
                # An exact filename is already handled as an installed model;
                # it must never be presented as a warning or alternate choice.
                if path.name.casefold() == workflow_basename.casefold():
                    continue
                try:
                    digest = cls._file_sha256(path)
                except (OSError, PermissionError):
                    continue
                if digest not in expected_hashes:
                    continue
                value = f"{category}/{relative}"
                if value not in seen:
                    seen.add(value)
                    candidates.append(value)
        return candidates


    @classmethod
    def _selected_local_file(
        cls, folders: dict[str, list[Path]], category: Any, relative_path: Any
    ) -> Path | None:
        category_name = str(category or "")
        if category_name not in CATEGORIES:
            return None
        relative = cls._safe_model_relative(relative_path)
        if not relative:
            return None
        for folder in folders.get(category_name, []):
            # Validate the lexical path against the registered folder without
            # resolving a model-file symlink into its cache target.
            candidate = folder / relative
            try:
                candidate.relative_to(folder)
            except ValueError:
                continue
            if candidate.is_file():
                return candidate
        return None


    def local_model_inventory(self, job_id: Any) -> dict[str, Any]:
        """List only local files relevant to the selected workflow dependencies."""
        job = self.workflow_job(job_id)
        settings_data = self.settings.load() if hasattr(self.settings, "load") else dict(self.settings or {})
        comfyui_root = str(settings_data.get("comfyui_directory") or "").strip()
        if not comfyui_root:
            return {
                "status": "unconfigured",
                "job_id": job["id"],
                "models": [],
                "dependencies": [],
                "missing": [],
                "by_category": {},
                "message": "尚未配置 ComfyUI 目录，无法查看本地模型。",
            }

        folders, folder_source, folder_warning = self._model_folders(comfyui_root)
        plan = self.dependency_plan(job["id"])
        # dependency_plan may refresh a legacy inspection report before
        # returning. Reload the job so historical cards use that refreshed
        # complete dependency set rather than the stale pre-refresh snapshot.
        job = self.workflow_job(job["id"])
        metadata = job.get("metadata") or {}
        workflow = metadata.get("workflow")
        dependency_items: list[dict[str, Any]] = []
        seen_dependencies: set[tuple[str, str]] = set()
        # ``dependency_plan["models"]`` is intentionally an install-only list
        # and therefore omits dependencies already installed in ComfyUI.  The
        # persisted inspection report is the complete workflow dependency set;
        # plan results are retained as a fallback for legacy jobs that have no
        # inspection report yet.
        inspection = metadata.get("inspection") or {}
        dependency_sources = [
            inspection.get("models") or [],
            plan.get("models") or [],
            plan.get("missing_models") or [],
            plan.get("recognized_models") or [],
        ]
        for source in dependency_sources:
            for item in source:
                if not isinstance(item, dict):
                    continue
                item = dict(item)
                item["category"] = str(item.get("category") or self._model_category(item, workflow) or "").strip()
                item["name"] = str(item.get("name") or "").strip()
                name = item["name"]
                category = item["category"]
                key = (category.casefold(), name.casefold())
                if name and key not in seen_dependencies:
                    seen_dependencies.add(key)
                    dependency_items.append(item)

        dependencies: list[dict[str, Any]] = []
        models: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        by_category: dict[str, list[str]] = {category: [] for category in sorted(CATEGORIES)}
        seen_paths: set[str] = set()
        for item in dependency_items:
            name = str(item.get("name") or "").strip()
            category = str(item.get("category") or self._model_category(item, workflow) or "").strip()
            local_paths = [
                str(path)
                for path in self._local_model_matches(folders, category, name)
            ] if category in CATEGORIES else []
            dependency = {
                "name": name,
                "category": category,
                "status": "matched" if local_paths else "missing",
                "local_paths": local_paths,
            }
            dependencies.append(dependency)
            if local_paths:
                for local_path in local_paths:
                    relative_path = next(
                        (
                            f"{category}/{relative}"
                            for folder in folders.get(category, [])
                            for path, relative in self._local_model_files(folder)
                            if str(path) == local_path
                        ),
                        "",
                    )
                    if relative_path and relative_path not in seen_paths:
                        seen_paths.add(relative_path)
                        by_category.setdefault(category, []).append(relative_path)
                        models.append(
                            {
                                "category": category,
                                "path": relative_path,
                                "filename": Path(relative_path).name,
                                "dependency": name,
                            }
                        )
            else:
                missing.append(dependency)

        return {
            "status": "ok",
            "job_id": job["id"],
            "models_root": str(Path(comfyui_root).expanduser().resolve() / "models"),
            "folder_paths_source": folder_source,
            "folder_paths": {
                category: [str(path) for path in paths]
                for category, paths in folders.items()
                if paths
            },
            "dependencies": dependencies,
            "models": models,
            "missing": missing,
            "by_category": by_category,
            "count": len(models),
            "message": (
                "已按当前工作流依赖匹配 ComfyUI 实际注册的本地模型文件。"
                if folder_source == "comfyui"
                else "已按当前工作流依赖匹配 ComfyUI models 目录中的本地模型文件。"
            ) + (f" {folder_warning}" if folder_warning else ""),
        }
