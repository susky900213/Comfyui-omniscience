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


class WorkflowModelInventoryMixin:
    """WorkflowModelInventoryMixin implementation."""

    @staticmethod
    def _model_compatibility_key(model: dict[str, Any]) -> tuple[str, str]:
        """Build a refresh/deduplication key from directory and model basename."""
        directory = str(model.get("directory") or "").strip().replace("\\", "/").strip("/")
        parts = [part for part in directory.split("/") if part]
        if parts and parts[0].casefold() in {"model", "models"}:
            parts = parts[1:]
        directory_key = "/".join(parts).casefold()
        category = WorkflowModelInventoryMixin._model_directory_category(directory)
        if category:
            directory_key = category.casefold()
        name = str(model.get("name") or "").strip().replace("\\", "/")
        return directory_key, (Path(name).name or name).casefold()


    @staticmethod
    def _model_legacy_key(model: dict[str, Any]) -> tuple[str, str]:
        return (
            str(model.get("field") or ""),
            str(model.get("name") or ""),
        )


    @classmethod
    def _find_previous_model(
        cls, model: dict[str, Any], previous_models: list[dict[str, Any]]
    ) -> dict[str, Any] | None:
        """Find persisted state across inspection schema versions.

        New metadata uses ``directory`` and may omit the old input ``field``.
        Prefer the normalized directory/basename key; then retain the exact
        legacy ``(field, name)`` lookup.  A unique basename-only match bridges
        old field-based reports to new directory-based metadata without merging
        two same-named models from different categories.
        """
        key = cls._model_compatibility_key(model)
        for previous in previous_models:
            if cls._model_compatibility_key(previous) == key:
                return previous
        legacy_key = cls._model_legacy_key(model)
        if legacy_key != ("", ""):
            for previous in previous_models:
                previous_key = cls._model_compatibility_key(previous)
                if cls._model_legacy_key(previous) == legacy_key and (
                    not previous_key[0] or (key[0] and previous_key[0] == key[0])
                ):
                    return previous
        basename = key[1]
        candidates = [
            previous
            for previous in previous_models
            if cls._model_compatibility_key(previous)[1] == basename
            and not cls._model_compatibility_key(previous)[0]
            and (not key[0] or not cls._model_compatibility_key(previous)[0])
        ]
        return candidates[0] if len(candidates) == 1 else None


    @staticmethod
    def _merge_persisted_model_state(model: dict[str, Any], previous: dict[str, Any]) -> None:
        """Retain user/source state when an old inspection is refreshed."""
        for key in (
            "url",
            "source",
            "source_url",
            "source_candidates",
            "local_paths",
            "local_status",
            "download_status",
            "local_selection",
            "download_allowed",
        ):
            if key in previous and previous[key] not in (None, "", [], {}):
                model[key] = previous[key]
        if "directory_invalid" in previous:
            model["directory_invalid"] = bool(model.get("directory_invalid")) or bool(previous["directory_invalid"])
        if model.get("directory_invalid"):
            model["local_paths"] = []
            model["local_status"] = "missing_directory"
            model["download_status"] = "missing_directory"
            model["local_selection"] = None
            model["download_allowed"] = False


    @staticmethod
    def _model_category(model: dict[str, Any], workflow: dict[str, Any] | None = None) -> str:
        directory_category = WorkflowModelInventoryMixin._model_directory_category(model.get("directory"))
        if directory_category:
            return directory_category
        field = str(model.get("field") or "").casefold()
        name = str(model.get("name") or "").casefold()
        node_ids = {str(item) for item in model.get("nodes") or []}
        nodes = workflow.get("prompt") if isinstance(workflow, dict) and isinstance(workflow.get("prompt"), dict) else workflow.get("nodes") if isinstance(workflow, dict) else None
        if isinstance(nodes, dict):
            node_types = [str(node.get("class_type") or node.get("type") or "").casefold() for node_id, node in nodes.items() if str(node_id) in node_ids and isinstance(node, dict)]
        elif isinstance(nodes, list):
            node_types = [str(node.get("type") or node.get("class_type") or "").casefold() for index, node in enumerate(nodes) if str(node.get("id") or index) in node_ids and isinstance(node, dict)]
        else:
            node_types = []
        for node_type in node_types:
            if "vae" in node_type and "loader" in node_type:
                return "vae"
            if "lora" in node_type:
                return "loras"
            if "controlnet" in node_type:
                return "controlnet"
            if "clip" in node_type and "loader" in node_type:
                return "clip"
            if "unet" in node_type or "diffusionmodel" in node_type:
                return "diffusion_models"
            if "upscale" in node_type and "loader" in node_type:
                return "upscale_models"
            if "checkpoint" in node_type and "loader" in node_type:
                return "checkpoints"
        if "vae" in field or "vae" in name:
            return "vae"
        if "lora" in field or "lora" in name:
            return "loras"
        if "controlnet" in field or "controlnet" in name:
            return "controlnet"
        if "clip" in field or "clip" in name or "text_encoder" in field:
            return "clip"
        if "upscale" in field or "upscale" in name:
            return "upscale_models"
        if "unet" in field or "diffusion_models" in field:
            return "diffusion_models"
        return "checkpoints"


    @staticmethod
    def _model_directory_category(directory: Any) -> str:
        """Map ComfyUI's frontend model directory to a supported category.

        ``properties.models`` is the frontend's authoritative dependency
        metadata.  The value is normally a folder name, but older/custom node
        exports can include a ``models/`` prefix or use a registered alias.
        Keep the normalization deliberately category-only so an arbitrary path
        can never become a filesystem destination.
        """
        value = str(directory or "").strip().replace("\\", "/").strip("/")
        if not value:
            return ""
        parts = [part for part in value.split("/") if part]
        if parts and parts[0].casefold() in {"models", "model"}:
            parts = parts[1:]
        if not parts:
            return ""
        return MODEL_DIRECTORY_CATEGORIES.get(parts[-1].casefold(), "")


    @staticmethod
    def _model_directory_invalid(model: dict[str, Any]) -> bool:
        value = model.get("directory_invalid")
        if isinstance(value, str):
            return value.strip().casefold() in {"1", "true", "yes", "y", "on"}
        return bool(value)


    @staticmethod
    def _model_directory_resolved(
        model: dict[str, Any],
        category: str,
        folders: dict[str, list[Path]],
        folder_source: str = "",
    ) -> bool:
        """Return whether current ComfyUI folder registration validates a directory.

        ``directory_invalid`` is frontend export-time state.  A current
        ``/internal/folder_paths`` response is stronger evidence: it confirms
        that the named category is registered, including extra model paths.
        Unknown or unregistered categories remain blocked from unsafe downloads.
        """
        directory = str(model.get("directory") or "").strip()
        return bool(
            directory
            and folder_source != "legacy"
            and WorkflowModelInventoryMixin._model_directory_category(directory) == category
            and folders.get(category)
        )


    def _model_folders(self, comfyui_root: str) -> tuple[dict[str, list[Path]], str, str]:
        """Resolve model folders from ComfyUI, with the legacy layout as fallback.

        The internal endpoint reflects ``folder_paths.folder_names_and_paths``
        after extra-model-paths and custom nodes have registered their folders.
        It may not exist on older ComfyUI versions, so failure is intentionally
        non-fatal and never prevents the legacy ``models/<category>`` scan.
        """
        root = Path(comfyui_root).expanduser().resolve() if comfyui_root else None
        legacy: dict[str, list[Path]] = {
            category: [(root / "models" / category).resolve()] if root else []
            for category in sorted(CATEGORIES)
        }
        if not comfyui_root:
            return legacy, "legacy", "尚未配置 ComfyUI 目录，无法读取 /internal/folder_paths。"

        try:
            payload = ComfyUIWorkflowExecutor(self.settings, request_timeout=2.0).folder_paths()
        except Exception as exc:
            return legacy, "legacy", f"无法读取 ComfyUI /internal/folder_paths，已回退默认 models 目录：{exc}"

        folders: dict[str, list[Path]] = {category: [] for category in sorted(CATEGORIES)}
        registered_any = False
        for category, aliases in MODEL_FOLDER_ALIASES.items():
            seen: set[Path] = set()
            for alias in aliases:
                for value in payload.get(alias, []):
                    path = Path(value).expanduser()
                    # The ComfyUI endpoint returns absolute filesystem paths.
                    # Ignore malformed relative values rather than resolving
                    # them against the plugin process working directory.
                    if not path.is_absolute():
                        continue
                    resolved = path.resolve()
                    if resolved not in seen:
                        seen.add(resolved)
                        folders[category].append(resolved)
                        registered_any = True
            # Keep the default folder even when ComfyUI reports an extra-model
            # path.  Some versions/configurations expose only the extra path via
            # /internal/folder_paths, although files in models/<category> are
            # still valid ComfyUI models.
            for path in legacy.get(category, []):
                if path not in seen:
                    seen.add(path)
                    folders[category].append(path)
        if not registered_any:
            return legacy, "legacy", "ComfyUI /internal/folder_paths 未返回可用模型目录，已回退默认 models 目录。"
        return folders, "comfyui", ""


    @staticmethod
    def _safe_model_relative(value: Any) -> str:
        relative = str(value or "").strip().replace("\\", "/")
        if not relative or relative.startswith("/") or ".." in Path(relative).parts:
            return ""
        return relative


    @classmethod
    def _normalise_model_reference(cls, value: Any, category: str) -> str:
        """Normalize workflow model references without leaving the category root."""
        relative = cls._safe_model_relative(value)
        if not relative:
            return ""
        parts = relative.split("/")
        aliases = {
            str(category).casefold(),
            *(str(item).casefold() for item in MODEL_FOLDER_ALIASES.get(category, ())),
        }
        if len(parts) >= 2 and parts[0].casefold() == "models" and parts[1].casefold() in aliases:
            parts = parts[2:]
        elif len(parts) >= 2 and parts[0].casefold() in aliases:
            parts = parts[1:]
        return "/".join(parts)


    @classmethod
    def _local_model_files(cls, folder: Path) -> list[tuple[Path, str]]:
        """Return files and safe paths relative to one registered model folder."""
        if not folder.is_dir():
            return []
        files: list[tuple[Path, str]] = []
        for path in sorted(folder.rglob("*"), key=lambda value: value.as_posix().casefold()):
            if not path.is_file() or path.suffix.lower() not in MODEL_SUFFIXES:
                continue
            try:
                # Keep the path as registered by ComfyUI. Model directories
                # commonly contain symlinks into a model cache outside the
                # registered directory; resolving the file first would make a
                # valid ComfyUI model look like it escaped the folder.
                relative = path.relative_to(folder).as_posix()
            except ValueError:
                continue
            files.append((path, relative))
        return files


    @classmethod
    def _local_model_matches(
        cls, folders: dict[str, list[Path]], category: str, model_name: Any
    ) -> list[Path]:
        relative = cls._normalise_model_reference(model_name, category)
        if not relative:
            return []
        matches: list[Path] = []
        target_relative = relative.casefold()
        target_name = Path(relative).name.casefold()
        for folder in folders.get(category, []):
            files = cls._local_model_files(folder)
            exact_matches = [
                path for path, path_relative in files
                if path_relative.casefold() == target_relative
            ]
            if exact_matches:
                for path in exact_matches:
                    if path not in matches:
                        matches.append(path)
                continue
            # Workflow JSON may differ from the on-disk spelling by case, or
            # contain only a basename while ComfyUI registered the file below a
            # subdirectory.  Match only inside the same category folder.
            for path, _path_relative in files:
                if path.name.casefold() == target_name and path not in matches:
                    matches.append(path)
        return matches


    @classmethod
    def _local_model_candidates(cls, folders: dict[str, list[Path]], category: str) -> list[str]:
        candidates: list[str] = []
        seen: set[str] = set()
        for folder in folders.get(category, []):
            for resolved, relative in cls._local_model_files(folder):
                value = f"{category}/{relative}"
                if value not in seen:
                    seen.add(value)
                    candidates.append(value)
        return candidates
