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


class WorkflowNodeCandidatesMixin:
    """WorkflowNodeCandidatesMixin implementation."""

    @staticmethod
    def _node_source_candidates(value: Any) -> list[dict[str, Any]]:
        """Normalize the persisted node-source cache without losing candidates.

        Older jobs stored one Manager mapping per class type.  Newer jobs may
        store a list directly or a primary mapping with a ``candidates`` list.
        Accept all three forms so an existing workflow job can still be resumed
        after the source-attribution changes.
        """
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if not isinstance(value, dict):
            return []
        nested = value.get("candidates")
        if isinstance(nested, list):
            candidates = [item for item in nested if isinstance(item, dict)]
            primary = {key: item for key, item in value.items() if key != "candidates"}
            if primary.get("manager_id") or primary.get("repository") or primary.get("source"):
                candidates.insert(0, primary)
            return candidates
        return [value]


    @staticmethod
    def _node_candidate_key(candidate: dict[str, Any]) -> tuple[str, str, str, str, str, str, str]:
        metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}

        def value(*keys: str) -> str:
            for key in keys:
                raw = candidate.get(key)
                if raw not in (None, "", [], {}):
                    return str(raw).strip()
                raw = metadata.get(key)
                if raw not in (None, "", [], {}):
                    return str(raw).strip()
            return ""

        return (
            value("manager_id").casefold(),
            value("install_id", "id").casefold(),
            value("repository", "source", "reference", "repo", "url").rstrip("/").casefold(),
            value("aux_id").casefold(),
            value("cnr_id").casefold(),
            value("version", "selected_version", "latest_version").casefold(),
            value("install_type", "type").casefold(),
        )


    @classmethod
    def _merge_node_source_candidates(cls, *values: Any) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str, str, str, str]] = set()
        for value in values:
            for candidate in cls._node_source_candidates(value):
                key = cls._node_candidate_key(candidate)
                if key in seen:
                    continue
                seen.add(key)
                result.append(dict(candidate))
        return result


    @staticmethod
    def _node_candidate_matches_aux_id(candidate: dict[str, Any], aux_id: Any) -> bool:
        expected = str(aux_id or "").strip().casefold().rstrip("/")
        if not expected:
            return False
        values = {
            str(candidate.get(key) or "").strip().casefold().rstrip("/")
            for key in ("aux_id", "repository", "source", "reference", "manager_id", "install_id", "cnr_id")
        }
        metadata = candidate.get("metadata")
        if isinstance(metadata, dict):
            values.update(
                str(metadata.get(key) or "").strip().casefold().rstrip("/")
                for key in ("aux_id", "repository", "repo", "reference", "url", "source", "install_id", "cnr_id")
            )
        return expected in {value for value in values if value}


    @staticmethod
    def _node_candidate_source(candidate: dict[str, Any]) -> str:
        metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
        for key in ("source", "reference", "repository", "repo", "url", "github_url"):
            value = candidate.get(key) or metadata.get(key)
            if value:
                return str(value).strip()
        return ""


    @staticmethod
    def _node_candidate_value(candidate: dict[str, Any], field: str) -> str:
        value = candidate.get(field)
        if value in (None, "", [], {}):
            metadata = candidate.get("metadata")
            if isinstance(metadata, dict):
                value = metadata.get(field)
                if value in (None, "", [], {}):
                    aliases = {
                        "version": ("selected_version", "latest_version"),
                        "install_type": ("type",),
                    }
                    for alias in aliases.get(field, ()):
                        value = metadata.get(alias)
                        if value not in (None, "", [], {}):
                            break
        return str(value or "").strip()


    @classmethod
    def _annotated_node_candidates(
        cls, candidates: list[dict[str, Any]], aux_id: Any
    ) -> list[dict[str, Any]]:
        """Attach the evidence used to consider a candidate for one occurrence."""
        expected = str(aux_id or "").strip()
        return [
            {
                **candidate,
                "matches_aux_id": bool(expected and cls._node_candidate_matches_aux_id(candidate, expected)),
                "aux_id_evidence": expected,
            }
            for candidate in candidates
        ]


    @classmethod
    def _resolve_node_instance(
        cls,
        instance: dict[str, Any],
        candidates: list[dict[str, Any]],
        selected: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Resolve one occurrence without turning a class-name match into ownership."""
        aux_id = str(instance.get("aux_id") or "").strip()
        matching = cls._instance_node_candidates(candidates, aux_id)
        # Keep rejected alternatives visible on the instance.  In particular,
        # an aux_id mismatch is useful evidence that a candidate exists but
        # cannot safely be assigned to this occurrence.
        annotated = cls._annotated_node_candidates(candidates, aux_id)
        selected_candidate = selected.get("candidate") if isinstance(selected, dict) else None
        if not isinstance(selected_candidate, dict):
            selected_candidate = matching[0] if len(matching) == 1 else None
        source = str((selected or {}).get("source") or "").strip() if isinstance(selected, dict) else ""
        manager_id = str((selected or {}).get("manager_id") or "").strip() if isinstance(selected, dict) else ""
        if selected:
            if not manager_id and isinstance(selected_candidate, dict):
                manager_id = str(selected_candidate.get("manager_id") or "").strip()
            if not source and isinstance(selected_candidate, dict):
                source = cls._node_candidate_source(selected_candidate)
            if (
                aux_id
                and isinstance(selected_candidate, dict)
                and not cls._node_candidate_matches_aux_id(selected_candidate, aux_id)
            ):
                return {
                    **instance,
                    "status": "unresolved",
                    "source": "",
                    "manager_id": "",
                    "source_type": "unresolved",
                    "source_selected": False,
                    "candidate": dict(selected_candidate),
                    "candidates": annotated,
                    "candidate_count": len(annotated),
                    "source_evidence": {
                        "kind": "selected_source_aux_id_mismatch",
                        "aux_id": aux_id,
                        "aux_id_matched": False,
                    },
                }
            if isinstance(selected_candidate, dict) and selected_candidate:
                selected_key = cls._node_candidate_key(selected_candidate)
                candidate_keys = {cls._node_candidate_key(item) for item in candidates}
                if candidate_keys and selected_key not in candidate_keys:
                    return {
                        **instance,
                        "status": "unresolved",
                        "source": "",
                        "manager_id": "",
                        "source_type": "unresolved",
                        "source_selected": False,
                        "candidate": dict(selected_candidate),
                        "candidates": annotated,
                        "candidate_count": len(annotated),
                        "source_evidence": {
                            "kind": "selected_source_not_in_search_results",
                            "aux_id": aux_id,
                            "aux_id_matched": False,
                        },
                    }
            provider = str(selected.get("provider") or selected.get("source_type") or "user_selected").strip()
            return {
                **instance,
                "status": "selected",
                "source": source,
                "manager_id": manager_id,
                "source_type": provider,
                "source_selected": bool(source or manager_id),
                "candidate": dict(selected_candidate) if isinstance(selected_candidate, dict) else None,
                "candidates": annotated,
                "candidate_count": len(annotated),
                "source_evidence": {
                    "kind": "user_selection",
                    "aux_id": aux_id,
                    "aux_id_matched": bool(
                        isinstance(selected_candidate, dict)
                        and aux_id
                        and cls._node_candidate_matches_aux_id(selected_candidate, aux_id)
                    ),
                },
            }

        identities = {
            cls._node_plugin_identity(candidate)
            for candidate in matching
            if cls._node_plugin_identity(candidate)
        }
        if len(identities) == 1 and matching:
            candidate = matching[0]
            source = cls._node_candidate_source(candidate)
            manager_id = str(candidate.get("manager_id") or "").strip()
            return {
                **instance,
                "status": "resolved",
                "source": source,
                "manager_id": manager_id,
                "source_type": "comfyui_manager" if manager_id else "workflow_source" if source else "unresolved",
                "source_selected": bool(source or manager_id),
                "candidate": dict(candidate),
                "candidates": annotated,
                "candidate_count": len(annotated),
                "source_evidence": {
                    "kind": "aux_id" if aux_id else "exact_class_type",
                    "aux_id": aux_id,
                    "aux_id_matched": bool(aux_id and cls._node_candidate_matches_aux_id(candidate, aux_id)),
                    "plugin_id": next(iter(identities)),
                },
            }
        if len(identities) > 1:
            status = "ambiguous"
            reason = "multiple_plugins"
        elif aux_id and candidates:
            status = "unresolved"
            reason = "aux_id_unmatched"
        else:
            status = "unresolved"
            reason = "no_candidate"
        return {
            **instance,
            "status": status,
            "source": "",
            "manager_id": "",
            "source_type": "unresolved",
            "source_selected": False,
            "candidate": None,
            "candidates": annotated,
            "candidate_count": len(annotated),
            "source_evidence": {"kind": reason, "aux_id": aux_id, "aux_id_matched": False},
        }


    @classmethod
    def _instance_node_candidates(
        cls, candidates: list[dict[str, Any]], aux_id: Any
    ) -> list[dict[str, Any]]:
        """Return only candidates supported by this node instance.

        An explicit ``aux_id`` is positive ownership evidence.  If none of the
        retained repositories matches it, returning an empty list is deliberate:
        a class-name match must not silently override the source metadata.
        Without an aux id all candidates remain available for ambiguity review.
        """
        expected = str(aux_id or "").strip()
        if not expected:
            return list(candidates)
        return [item for item in candidates if cls._node_candidate_matches_aux_id(item, expected)]


    @classmethod
    def _node_plugin_identity(cls, item: dict[str, Any]) -> str:
        source = str(item.get("source") or item.get("repository") or "").strip().rstrip("/")
        if source:
            return f"source:{source.casefold()}"
        aux_id = str(item.get("aux_id") or "").strip()
        if aux_id:
            return f"aux:{aux_id.casefold()}"
        manager_id = str(item.get("manager_id") or "").strip()
        return f"manager:{manager_id}" if manager_id else ""


    @staticmethod
    def _diagnostic_dependency_plan(metadata: dict[str, Any]) -> dict[str, Any]:
        """Rebuild only user-selected dependency actions after preparation.

        Preparation removes the transient dependency plan once a workflow is
        ready. Keep diagnosis useful by deriving a minimal plan from persisted
        selections, never from a guessed URL or a raw client request.
        """
        existing = metadata.get("dependency_plan")
        if isinstance(existing, dict):
            return existing
        inspection = metadata.get("inspection") or {}
        node_sources = metadata.get("node_source_selections") or {}
        prepared_nodes = metadata.get("prepared_node_results") or {}
        nodes = [
            {
                "class_type": str(class_type),
                "source": (selection or {}).get("source") if isinstance(selection, dict) else "",
                "manager_id": (selection or {}).get("manager_id") if isinstance(selection, dict) else "",
                "source_type": (selection or {}).get("provider") if isinstance(selection, dict) else "",
                "manager": (selection or {}).get("manager") if isinstance(selection, dict) else None,
            }
            for class_type in (
                inspection.get("installable_missing_nodes")
                or inspection.get("missing_nodes")
                or []
            )
            for selection in [node_sources.get(str(class_type))]
            if str(class_type) not in prepared_nodes
        ]
        model_sources = metadata.get("model_source_selections") or {}
        prepared_models = {
            str(item.get("name"))
            for item in metadata.get("prepared_models") or []
            if isinstance(item, dict)
        }
        models = [
            {
                "name": str(item.get("name")),
                "status": item.get("local_status") or "source_required",
                "source": model_sources.get(str(item.get("name"))),
            }
            for item in inspection.get("models") or []
            if isinstance(item, dict)
            and str(item.get("name")) not in prepared_models
            and str(item.get("local_status") or "") not in {"installed", "selected_local"}
            and item.get("loader_status") != "recognized"
            and isinstance(model_sources.get(str(item.get("name"))), dict)
        ]
        return {"nodes": nodes, "models": models}
