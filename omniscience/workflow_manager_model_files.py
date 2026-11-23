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
        """List files in the model folders currently registered by ComfyUI."""
        job = self.workflow_job(job_id)
        settings_data = self.settings.load() if hasattr(self.settings, "load") else dict(self.settings or {})
        comfyui_root = str(settings_data.get("comfyui_directory") or "").strip()
        if not comfyui_root:
            return {
                "status": "unconfigured",
                "job_id": job["id"],
                "models": [],
                "by_category": {},
                "message": "尚未配置 ComfyUI 目录，无法查看本地模型。",
            }

        folders, folder_source, folder_warning = self._model_folders(comfyui_root)
        by_category: dict[str, list[str]] = {category: [] for category in sorted(CATEGORIES)}
        for category in sorted(CATEGORIES):
            by_category[category] = self._local_model_candidates(folders, category)

        models = [
            {"category": category, "path": model_path, "filename": Path(model_path).name}
            for category, paths in by_category.items()
            for model_path in paths
        ]
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
            "models": models,
            "by_category": by_category,
            "count": len(models),
            "message": (
                "已读取 ComfyUI 实际注册的模型目录中的本地模型文件。"
                if folder_source == "comfyui"
                else "已读取 ComfyUI models 目录中的本地模型文件。"
            ) + (f" {folder_warning}" if folder_warning else ""),
        }
