"""Workflow discovery orchestration for the global conversation."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from threading import Event
from pathlib import Path
from typing import Any

from .workflow_parser import inspect_workflow
from .workflow_provider import RunningHubProvider
from .workflow_executor import ComfyUIWorkflowExecutor
from .model_downloader import CATEGORIES, ModelDownloader
from .model_downloader import ModelDownloadCancelled
from .model_source import ModelSourceSearcher
from . import paths
from .resource_assessor import assess, local_snapshot, rank_resource
from .node_search import GiteeNodeSearcher, GitHubNodeSearcher

MAX_WORKFLOW_NODE_SEARCHES = 5
MODEL_SUFFIXES = {".safetensors", ".sft", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx", ".pkl"}

# The names used by workflow fields are not always the names registered by
# ComfyUI.  For example, the built-in CLIP loader uses the ``text_encoders``
# folder while the UI historically exposed it as ``clip``.  Keep the mapping
# in one place so inspection, dependency planning, and local selection agree.
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


class WorkflowManagerMixin:
    """Persisted RunningHub search and candidate selection for global chat."""

    def workflow_search_requested(self, content: Any) -> bool:
        text = str(content or "").casefold()
        workflow = bool(re.search(r"工作流|workflow|comfyui[\s_-]*workflow", text))
        if not workflow:
            return False
        # A named workflow request such as “Moss 工作流” should expose the
        # search tool even when the user did not use a verb like “搜索”.
        return not bool(re.search(r"不要|无需|不需要|别|禁止|don't|do not", text))

    @staticmethod
    def simplify_workflow_query(query: str) -> str:
        value = str(query or "").strip()
        value = re.sub(r"(?:我想要|我需要|帮我|请帮我|请搜索|搜索|查找|推荐|安装(?:下|一个|这个)?)", " ", value, flags=re.I)
        value = re.sub(r"(?:的)\s*", " ", value)
        value = re.sub(r"(?:工作流|workflow)\b", " ", value, flags=re.I)
        value = re.sub(r"\s+", " ", value).strip(" ，,。.!！")
        return value or str(query or "").strip()

    @staticmethod
    def requested_region(content: Any) -> str | None:
        text = str(content or "")
        if re.search(r"国内|中国站|国内站", text):
            return "domestic"
        if re.search(r"国外|国际站|海外", text):
            return "international"
        return None

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
        updated = self.store.update_workflow_job(
            job_key,
            state="selected",
            selected_candidate_id=candidate_key,
            metadata={"candidate": candidate},
        )
        return {"status": "selected", "job": updated, "candidate": candidate}

    async def inspect_selected_workflow(self, job_id: Any, *, loaded_nodes: list[str] | None = None) -> dict[str, Any]:
        job = self.store.get_workflow_job(str(job_id or "").strip())
        if job is None:
            raise ValueError("工作流搜索任务不存在。")
        candidate = self.store.workflow_candidate(job["id"], job.get("selected_candidate_id"))
        if candidate is None:
            raise ValueError("请先选择一个工作流候选。")
        provider = RunningHubProvider(self.settings)
        if loaded_nodes is None:
            loaded_nodes_error = ""
            try:
                loaded_nodes = await asyncio.to_thread(ComfyUIWorkflowExecutor(self.settings).loaded_nodes)
            except Exception as exc:
                loaded_nodes = None
                loaded_nodes_error = str(exc)
        else:
            loaded_nodes_error = ""
        workflow = await asyncio.to_thread(provider.get_workflow, candidate)
        report = inspect_workflow(workflow, loaded_nodes=loaded_nodes, loaded_nodes_error=loaded_nodes_error)
        settings_data = self.settings.load() if hasattr(self.settings, "load") else dict(self.settings or {})
        comfyui_root = str(settings_data.get("comfyui_directory") or "").strip()
        folders, folder_source, folder_warning = self._model_folders(comfyui_root)
        for model in report.get("models", []):
            category = self._model_category(model, workflow)
            local_paths = [str(path) for path in self._local_model_matches(folders, category, model.get("name"))]
            model["local_paths"] = local_paths
            model["local_status"] = "installed" if local_paths else "missing"
            model["download_status"] = "ready" if local_paths else "source_required"
            model["category"] = category
        report["folder_paths_source"] = folder_source
        report["folder_paths"] = {
            category: [str(path) for path in paths]
            for category, paths in folders.items()
            if paths
        }
        if folder_warning:
            report["folder_paths_warning"] = folder_warning
        metadata = dict(job.get("metadata") or {})
        metadata["inspection"] = report
        metadata["workflow"] = workflow
        updated = self.store.update_workflow_job(job["id"], state="checking_dependencies", metadata=metadata)
        return {"status": "checked", "job": updated, "candidate": candidate, "inspection": report, "workflow": workflow}

    async def inspect_latest_selected_workflow(self) -> dict[str, Any]:
        job = self.store.latest_selected_workflow_job()
        if job is None:
            raise ValueError("当前没有用户已选择的工作流候选。")
        return await self.inspect_selected_workflow(job["id"])

    def dependency_plan(self, job_id: Any) -> dict[str, Any]:
        job = self.workflow_job(job_id)
        inspection = (job.get("metadata") or {}).get("inspection") or {}
        missing_nodes = inspection.get("missing_nodes") or []
        unverified_nodes = inspection.get("unverified_nodes") or []
        prepared_models = (job.get("metadata") or {}).get("prepared_models") or []
        prepared_names = {
            str(item.get("name") or "")
            for item in prepared_models
            if isinstance(item, dict) and item.get("selection_type") != "local"
        }
        local_model_selections = (job.get("metadata") or {}).get("local_model_selections") or {}
        models = [
            model for model in (inspection.get("models") or [])
            if model.get("local_status") != "installed" and str(model.get("name") or "") not in prepared_names
        ]
        candidate = (job.get("metadata") or {}).get("candidate") or {}
        node_sources = candidate.get("node_sources") or []
        job_metadata = dict(job.get("metadata") or {})
        selected_node_sources = job_metadata.get("node_source_selections") or {}
        node_search_results = job_metadata.get("node_search_results") or {}
        settings_data = self.settings.load() if hasattr(self.settings, "load") else dict(self.settings or {})
        comfyui_root = str(settings_data.get("comfyui_directory") or "").strip()
        models_root = Path(comfyui_root).expanduser().resolve() / "models" if comfyui_root else Path("/__missing_comfyui_models__")
        nodes = []
        from .agent.node_install import NodeInstallerMixin

        for name in missing_nodes:
            source = next(
                (
                    item.get("source") or item.get("url") or item.get("github_url")
                    for item in node_sources
                    if isinstance(item, dict)
                    and (name == item.get("class_type") or name in (item.get("class_types") or []))
                ),
                None,
            )
            selected = selected_node_sources.get(name)
            selected_commit = ""
            if isinstance(selected, dict):
                source = selected.get("source")
                selected_commit = str(selected.get("commit") or "")
            try:
                host, owner, repository, prefix, _ref = NodeInstallerMixin._node_repository_source(source)
                if prefix:
                    raise ValueError("节点来源必须指向仓库根目录。")
                source = f"https://{host}/{owner}/{repository}"
            except (TypeError, ValueError):
                source = None
            if source and selected_commit and re.fullmatch(r"[0-9a-fA-F]{40}", selected_commit):
                source = f"{source}/tree/{selected_commit}"
            candidates = node_search_results.get(name, [])
            nodes.append(
                {
                    "class_type": name,
                    "status": "missing",
                    "source": source,
                    "source_candidates": candidates if isinstance(candidates, list) else [],
                    "source_selected": bool(source),
                    "download_allowed": bool(source),
                }
            )
        folders, folder_source, folder_warning = self._model_folders(comfyui_root)
        planned_models = []
        model_source_selections = job_metadata.get("model_source_selections") or {}
        model_search_results = job_metadata.get("model_search_results") or {}
        for model in models:
            model_name = str(model.get("name") or "")
            basename = Path(model_name.replace("\\", "/")).name
            source = model_source_selections.get(model_name)
            if isinstance(source, dict):
                source = dict(source)
                source.setdefault("filename", basename)
                source.setdefault("category", self._model_category(model, job_metadata.get("workflow")))
                repo_id = str(source.get("repo_id") or "")
                source_filename = str(source.get("filename") or "").replace("\\", "/")
                digest = str(source.get("sha256") or "").lower()
                if (
                    not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo_id)
                    or Path(source_filename).name != basename
                    or ".." in Path(source_filename).parts
                    or digest and not re.fullmatch(r"[0-9a-f]{64}", digest)
                ):
                    source = None
            category = self._model_category(model, job_metadata.get("workflow"))
            local_matches = self._local_model_matches(folders, category, model.get("name"))
            local_candidates = self._local_model_candidates(folders, category)
            local_selection = local_model_selections.get(model_name)
            if isinstance(local_selection, dict):
                selected_path = str(local_selection.get("workflow_path") or "").replace("\\", "/")
                selected_category = str(local_selection.get("category") or "")
                selected_file = self._selected_local_file(folders, selected_category, selected_path)
                if selected_category != category or selected_file is None:
                    local_selection = None
            planned = {**model, "status": "selected_local" if local_selection else "installed" if local_matches else "ready" if source else "source_required", "source": source, "source_candidates": model_search_results.get(model_name, []), "local_paths": [str(item) for item in local_matches], "local_candidates": local_candidates, "local_selection": local_selection, "category": category, "download_allowed": bool(source)}
            planned_models.append(planned)
        plan = {
            "job_id": job["id"],
            "state": job.get("state"),
            "nodes": nodes,
            "unverified_nodes": [
                {"class_type": name, "status": "unverified", "download_allowed": False}
                for name in unverified_nodes
            ],
            "models": planned_models,
            "missing_models": [item for item in planned_models if item.get("status") not in {"installed", "selected_local"}],
            "model_download_ready": bool(planned_models) and all(item.get("source") or item.get("status") in {"installed", "selected_local"} for item in planned_models),
            "restart_required": bool(missing_nodes),
            "requires_user_confirmation": bool(missing_nodes or models),
            "folder_paths_source": folder_source,
            "folder_paths": {
                category: [str(path) for path in paths]
                for category, paths in folders.items()
                if paths
            },
            "folder_paths_warning": folder_warning,
            "message": "节点仓库候选仅供参考；必须由你选择来源并确认安装。安全检测通过前不会写入 custom_nodes。",
        }
        metadata = dict(job.get("metadata") or {})
        metadata["dependency_plan"] = plan
        self.store.update_workflow_job(job["id"], state="selected", metadata=metadata)
        return plan

    @staticmethod
    def _model_category(model: dict[str, Any], workflow: dict[str, Any] | None = None) -> str:
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

    async def search_workflow_model_sources(self, job_id: Any) -> dict[str, Any]:
        """Inspect local files first, then search exact filenames via web search and Hugging Face."""
        job = self.workflow_job(job_id)
        metadata = dict(job.get("metadata") or {})
        plan = metadata.get("dependency_plan") or self.dependency_plan(job_id)
        metadata = dict((self.store.get_workflow_job(job["id"]) or job).get("metadata") or {})
        selections = metadata.get("model_source_selections") or {}
        prior_results = dict(metadata.get("model_search_results") or {})
        results: dict[str, list[dict[str, Any]]] = {}
        local_results: dict[str, list[str]] = {}
        warnings: dict[str, str] = {}
        for model in plan.get("models", []):
            name = str(model.get("name") or "")
            if model.get("source") or model.get("status") in {"installed", "selected_local"} or name in selections:
                continue
            local_candidates = [str(path) for path in model.get("local_candidates") or [] if str(path).strip()]
            if local_candidates:
                local_results[name] = local_candidates
                prior_results[name] = []
                results[name] = []
                continue
            try:
                candidates = await asyncio.to_thread(ModelSourceSearcher(self.settings).search, Path(name.replace("\\", "/")).name)
            except ValueError as exc:
                candidates = []
                warnings[name] = str(exc)
            prior_results[name] = candidates
            results[name] = candidates
        metadata["model_search_results"] = prior_results
        self.store.update_workflow_job(job["id"], metadata=metadata)
        message = "已先检查 ComfyUI 本地模型；仅对没有本地候选的文件先搜索 Bing/Google 中的 Hugging Face 第一页结果，再回退 Hugging Face API。选择远程来源后仍需单独确认下载。"
        if warnings:
            message += " 部分远程搜索受限或失败，不影响查看和选择本地模型。"
        return {
            "job_id": job["id"], "results": results, "local_results": local_results,
            "warnings": warnings, "message": message,
        }

    def select_workflow_model_source(self, job_id: Any, model_name: Any, source: Any) -> dict[str, Any]:
        name = str(model_name or "").strip()
        job = self.workflow_job(job_id)
        metadata = dict(job.get("metadata") or {})
        inspection = metadata.get("inspection") or {}
        models = inspection.get("models") or []
        match = next((model for model in models if str(model.get("name") or "") == name), None)
        if match is None or match.get("local_status") == "installed":
            raise ValueError("该工作流没有此缺失模型，不能选择下载来源。")
        if not name or name in {".", ".."}:
            raise ValueError("模型名称无效。")
        candidates = (metadata.get("model_search_results") or {}).get(name, [])
        repo_id = str((source or {}).get("repo_id") or "") if isinstance(source, dict) else ""
        filename = str((source or {}).get("filename") or "") if isinstance(source, dict) else ""
        selected = next((item for item in candidates if item.get("repo_id") == repo_id and item.get("filename") == filename), None)
        if selected is None:
            raise ValueError("模型来源不在本次 Hugging Face 搜索结果中，请重新搜索并从列表中选择。")
        normalized_filename = filename.replace("\\", "/")
        if Path(normalized_filename).name != Path(name.replace("\\", "/")).name or ".." in Path(normalized_filename).parts:
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
        if model is None or model.get("local_status") == "installed":
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
        source = workflow.get("prompt") if isinstance(workflow.get("prompt"), dict) else workflow.get("nodes")
        if isinstance(source, dict):
            entries = [(str(node_id), node) for node_id, node in source.items() if isinstance(node, dict)]
        elif isinstance(source, list):
            entries = [(str(node.get("id") or index), node) for index, node in enumerate(source) if isinstance(node, dict)]
        else:
            return False
        target_nodes = {str(node_id) for node_id in (model.get("nodes") or [])}
        field = str(model.get("field") or "")
        old_value = str(model.get("name") or "")
        changed = False
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
                except ValueError:
                    pass
            else:
                values = node.get("inputs") if isinstance(node.get("inputs"), dict) else None
                if values is not None and str(values.get(field) or "") == old_value:
                    values[field] = selected_path
                    changed = True
        return changed

    async def search_workflow_node_sources(self, job_id: Any) -> dict[str, Any]:
        """Search GitHub first, using Gitee only when the GitHub search fails."""
        job = self.workflow_job(job_id)
        plan = (job.get("metadata") or {}).get("dependency_plan") or self.dependency_plan(job_id)
        metadata = dict(job.get("metadata") or {})
        prior_results = dict(metadata.get("node_search_results") or {})
        unresolved_items = [
            item
            for item in plan.get("nodes", [])
            if not item.get("source")
        ]
        unsearched = [
            item for item in unresolved_items
            if str(item.get("class_type") or "") not in prior_results
        ]
        # Search new classes first so empty results cannot starve later nodes.
        # Once all classes have been queried, another click retries empty results.
        retry_items = [
            item for item in unresolved_items
            if not prior_results.get(str(item.get("class_type") or ""))
        ]
        if unsearched:
            search_items = unsearched
        elif retry_items:
            cursor = int(metadata.get("node_search_retry_cursor") or 0) % len(retry_items)
            rotated = retry_items[cursor:] + retry_items[:cursor]
            search_items = rotated[:MAX_WORKFLOW_NODE_SEARCHES]
            metadata["node_search_retry_cursor"] = (cursor + len(search_items)) % len(retry_items)
        else:
            search_items = []
        results: dict[str, list[dict[str, Any]]] = {
            str(item.get("class_type")): prior_results[str(item.get("class_type"))]
            for item in unresolved_items
            if str(item.get("class_type") or "") in prior_results
        }
        searched = search_items[:MAX_WORKFLOW_NODE_SEARCHES]
        for item in searched:
            class_type = str(item.get("class_type") or "")
            try:
                candidates = await asyncio.to_thread(GitHubNodeSearcher.search, class_type)
            except ValueError as exc:
                try:
                    candidates = await asyncio.to_thread(GiteeNodeSearcher.search, class_type)
                except ValueError as gitee_error:
                    raise ValueError(f"GitHub 搜索失败：{exc}；Gitee 搜索失败：{gitee_error}") from gitee_error
            results[class_type] = candidates
        previous = prior_results
        previous.update({str(item.get("class_type")): results[str(item.get("class_type"))] for item in searched})
        metadata["node_search_results"] = previous
        metadata.pop("dependency_plan", None)
        self.store.update_workflow_job(job["id"], metadata=metadata)
        deferred = max(len(unsearched) - len(searched), 0)
        return {
            "job_id": job["id"],
            "results": results,
            "deferred": deferred,
            "message": "优先搜索 GitHub；仅当 GitHub 搜索失败时回退到 Gitee。GitHub 正常返回空结果时不会回退；安装前仍会运行安全扫描。"
            + (f" 为控制请求量，另有 {deferred} 个节点可再次点击搜索。" if deferred else ""),
        }

    def select_workflow_node_source(self, job_id: Any, class_type: Any, source: Any) -> dict[str, Any]:
        """Persist only an exact Gitee/GitHub repository URL returned by this job's search."""
        name = str(class_type or "").strip()
        value = str(source or "").strip().rstrip("/")
        job = self.workflow_job(job_id)
        metadata = dict(job.get("metadata") or {})
        inspection = metadata.get("inspection") or {}
        if name not in (inspection.get("missing_nodes") or []):
            raise ValueError("该工作流中没有此缺失节点，不能选择安装来源。")
        if name in (job.get("metadata") or {}).get("prepared_node_results", {}):
            raise ValueError("该节点已准备完成，不能再次更换来源。")
        candidates = (metadata.get("node_search_results") or {}).get(name, [])
        if not any(str(item.get("source") or "").rstrip("/") == value for item in candidates):
            raise ValueError("节点来源不在本次 Gitee/GitHub 搜索结果中，请重新搜索并从列表中选择。")
        from .agent.node_install import NodeInstallerMixin

        host, owner, repository, prefix, _ref = NodeInstallerMixin._node_repository_source(value)
        if prefix:
            raise ValueError("节点仓库来源必须指向仓库根目录。")
        canonical = f"https://{host}/{owner}/{repository}"
        selections = dict(metadata.get("node_source_selections") or {})
        selections[name] = {"source": canonical, "selected_at": time.time()}
        metadata["node_source_selections"] = selections
        results = dict(metadata.get("node_search_results") or {})
        results.pop(name, None)
        metadata["node_search_results"] = results
        metadata.pop("dependency_plan", None)
        updated = self.store.update_workflow_job(job["id"], metadata=metadata)
        return {"status": "source_selected", "job_id": job["id"], "class_type": name, "source": canonical, "job": updated}

    async def confirm_node_installation(
        self,
        job_id: Any,
        *,
        confirmed: bool = False,
        acknowledge_security_for: str = "",
    ) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("必须由用户明确确认后才能安装缺少的节点。")
        job = self.workflow_job(job_id)
        plan = (job.get("metadata") or {}).get("dependency_plan") or self.dependency_plan(job_id)
        if not plan.get("nodes"):
            return {"status": "nodes_prepared", "job_id": job["id"], "nodes": [], "models_pending": bool(plan.get("models"))}
        unresolved = [item.get("class_type") for item in plan.get("nodes", []) if not item.get("source")]
        if unresolved:
            raise ValueError("以下缺失节点尚未选择 Gitee/GitHub 来源：" + "、".join(str(item) for item in unresolved[:8]))
        metadata = dict(job.get("metadata") or {})
        prepared = dict(metadata.get("prepared_node_results") or {})
        installed = []
        for item in plan.get("nodes", []):
            if not item.get("source"):
                raise ValueError(f"节点 {item.get('class_type')} 没有可信安装来源。")
            class_type = str(item.get("class_type") or "")
            existing = prepared.get(class_type)
            if existing:
                installed.append(existing)
                continue
            selected_sources = dict((self.store.get_workflow_job(job["id"]) or {}).get("metadata", {}).get("node_source_selections") or {})
            selected = dict(selected_sources.get(class_type) or {})
            try:
                host, owner, repository, _prefix, ref = self._node_repository_source(item["source"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"节点 {class_type} 的 Gitee/GitHub 来源无效：{exc}") from exc
            commit = str(selected.get("commit") or (ref if re.fullmatch(r"[0-9a-fA-F]{40}", str(ref or "")) else ""))
            if not commit:
                resolved_ref = str(ref or await asyncio.to_thread(self._repository_default_branch, host, owner, repository))
                commit = await asyncio.to_thread(self._repository_commit, host, owner, repository, resolved_ref)
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
                result = await self.install_comfyui_node(
                    item["source"],
                    pin_commit=True,
                    acknowledged=class_type == acknowledge_security_for,
                    expected_class_type=class_type,
                )
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
        metadata = dict(self.store.get_workflow_job(job["id"]) or job).get("metadata") or {}
        metadata["prepared_nodes"] = installed
        self.store.update_workflow_job(job["id"], state="selected", metadata=metadata)
        return {"status": "nodes_prepared", "job_id": job["id"], "nodes": installed, "models_pending": bool(plan.get("models"))}


    async def confirm_model_download(
        self, job_id: Any, *, confirmed: bool = False, model_names: list[str] | None = None,
        wait_for_completion: bool = True, cancel_event: Event | None = None,
    ) -> dict[str, Any]:
        """Download selected dependencies; optionally queue without holding an HTTP request open."""
        if not confirmed:
            raise ValueError("必须由用户明确确认后才能下载模型。")
        job = self.workflow_job(job_id)
        if not wait_for_completion:
            if not isinstance(model_names, list):
                raise ValueError("请在模型列表中明确选择要下载的文件。")
            requested = {str(name).strip() for name in model_names if str(name).strip()}
            if len(requested) != 1:
                raise ValueError("一次只能选择并下载一个模型文件。")
            requested_name = next(iter(requested))
            task_key = (str(job["id"]), requested_name)
            task = getattr(self, "_model_download_tasks", {}).get(task_key)
            if task is not None and not task.done():
                return {"status": "downloading", "job_id": job["id"], "name": requested_name}
            if job.get("state") == "preparing_models":
                raise ValueError("当前工作流已有模型下载任务正在运行。")
            metadata = dict(job.get("metadata") or {})
            prepared = next(
                (item for item in metadata.get("prepared_models") or [] if isinstance(item, dict) and item.get("name") == requested_name),
                None,
            )
            if prepared is not None:
                return {"status": "completed", "job_id": job["id"], "name": requested_name, "model": prepared}
        plan = (job.get("metadata") or {}).get("dependency_plan") or self.dependency_plan(job_id)
        settings_data = self.settings.load() if hasattr(self.settings, "load") else dict(self.settings or {})
        comfyui_root = str(settings_data.get("comfyui_directory") or "").strip()
        if not comfyui_root:
            raise ValueError("请先配置 ComfyUI 目录，才能放置模型。")
        models_root = Path(comfyui_root).expanduser().resolve() / "models"
        model_folders, _folder_source, _folder_warning = self._model_folders(comfyui_root)
        cache_root = Path(paths.STATE_DIRECTORY).expanduser().resolve() / "workflow-models"
        if not isinstance(model_names, list):
            raise ValueError("请在模型列表中明确选择要下载的文件。")
        requested_names = {str(name).strip() for name in (model_names or []) if str(name).strip()}
        if len(requested_names) != 1:
            raise ValueError("一次只能选择并下载一个模型文件。")
        selected_models = [
            item for item in plan.get("models", [])
            if item.get("status") not in {"installed", "selected_local"} and (not requested_names or item.get("name") in requested_names)
        ]
        if requested_names != {str(item.get("name") or "") for item in selected_models}:
            raise ValueError("所选模型不属于当前工作流的缺失模型。")
        if not selected_models:
            raise ValueError("当前没有待下载的缺失模型。")
        for item in selected_models:
            if not isinstance(item.get("source"), dict):
                raise ValueError(f"模型 {item.get('name')} 没有可信下载来源，请先搜索并选择 Hugging Face 文件。")
            source_filename = str(item["source"].get("filename") or item.get("name") or "").replace("\\", "/")
            if Path(source_filename).name != Path(str(item.get("name") or "").replace("\\", "/")).name or ".." in Path(source_filename).parts:
                raise ValueError(f"模型 {item.get('name')} 的下载来源文件名不匹配。")
        if not wait_for_completion:
            model_name = str(selected_models[0].get("name") or "")
            tasks = getattr(self, "_model_download_tasks", None)
            if tasks is None:
                tasks = {}
                self._model_download_tasks = tasks
            progress_by_key = getattr(self, "_model_download_progress", None)
            if progress_by_key is None:
                progress_by_key = {}
                self._model_download_progress = progress_by_key
            locks = getattr(self, "_model_download_locks", None)
            if locks is None:
                locks = {}
                self._model_download_locks = locks
            lock = locks.setdefault(str(job["id"]), asyncio.Lock())
            key = (str(job["id"]), model_name)
            cancel_events = getattr(self, "_model_download_cancel_events", None)
            if cancel_events is None:
                cancel_events = {}
                self._model_download_cancel_events = cancel_events
            cancel_events[key] = Event()
            progress_by_key[key] = {"downloaded_bytes": 0, "total_bytes": None}
            self.store.update_workflow_job(job["id"], state="selected", error="")
            task = asyncio.create_task(
                self._run_background_model_download(lock, job["id"], model_name, cancel_events[key])
            )
            tasks[key] = task
            task.add_done_callback(lambda completed, task_key=key: self._discard_model_download_task(task_key, completed))
            return {"status": "downloading", "job_id": job["id"], "name": model_name}
        self.store.update_workflow_job(job["id"], state="preparing_models")
        installed = []
        try:
            for item in selected_models:
                source = item.get("source")
                downloader = ModelDownloader(self.settings)

                def report_progress(
                    downloaded_bytes: int,
                    total_bytes: int | None,
                    model_key: tuple[str, str] = (str(job["id"]), str(item.get("name") or "")),
                ) -> None:
                    self._model_download_progress[model_key] = {
                        "downloaded_bytes": downloaded_bytes,
                        "total_bytes": total_bytes,
                    }

                if cancel_event is not None and cancel_event.is_set():
                    raise ModelDownloadCancelled("模型下载已取消；已保留部分文件，可重试并从断点继续。")
                downloaded = await asyncio.to_thread(
                    downloader.download,
                    repo_id=source.get("repo_id"), filename=source.get("filename") or item.get("name"),
                    category=source.get("category") or self._model_category(item), cache_root=cache_root,
                    expected_sha256=source.get("sha256") or "",
                    source_url=source.get("source_url") or "",
                    on_progress=report_progress,
                    cancel_event=cancel_event,
                )
                if cancel_event is not None and cancel_event.is_set():
                    raise ModelDownloadCancelled("模型下载已取消；已保留部分文件，可重试并从断点继续。")
                category = str(downloaded["category"] or "")
                registered_folders = model_folders.get(category) or []
                target_root = registered_folders[0] if registered_folders else None
                linked = await asyncio.to_thread(
                    downloader.link_to_comfyui,
                    downloaded["path"],
                    category=category,
                    models_root=models_root,
                    target_root=target_root,
                )
                installed.append({"name": item.get("name"), "download": downloaded, "link": linked})
        except ModelDownloadCancelled:
            latest = self.store.get_workflow_job(job["id"]) or job
            self.store.update_workflow_job(job["id"], state="selected", error="模型下载已取消，可重试并从断点继续。", metadata=latest.get("metadata") or {})
            raise
        except Exception:
            latest = self.store.get_workflow_job(job["id"]) or job
            self.store.update_workflow_job(job["id"], state="selected", error="模型下载失败，请检查来源后重试。", metadata=latest.get("metadata") or {})
            raise
        latest = self.store.get_workflow_job(job["id"]) or job
        metadata = dict(latest.get("metadata") or {})
        prepared = list(metadata.get("prepared_models") or [])
        prepared_by_name = {str(item.get("name") or ""): item for item in prepared if isinstance(item, dict)}
        prepared_by_name.update({str(item.get("name") or ""): item for item in installed})
        metadata["prepared_models"] = list(prepared_by_name.values())
        metadata.pop("dependency_plan", None)
        remaining = [item for item in plan.get("models", []) if item.get("status") not in {"installed", "selected_local"} and item.get("name") not in prepared_by_name]
        state = "ready" if not remaining else "selected"
        self.store.update_workflow_job(job["id"], state=state, error="", metadata=metadata)
        return {"status": "models_prepared", "job_id": job["id"], "models": installed, "remaining_models": [item.get("name") for item in remaining], "restart_required": bool(metadata.get("prepared_nodes"))}

    async def _run_background_model_download(
        self, lock: asyncio.Lock, job_id: Any, model_name: str, cancel_event: Event
    ) -> dict[str, Any]:
        async with lock:
            try:
                if cancel_event.is_set():
                    raise ModelDownloadCancelled("模型下载已取消；已保留部分文件，可重试并从断点继续。")
                return await self.confirm_model_download(
                    job_id, confirmed=True, model_names=[model_name], cancel_event=cancel_event
                )
            except ModelDownloadCancelled:
                raise
            except Exception as exc:
                job = self.store.get_workflow_job(str(job_id)) or {}
                self.store.update_workflow_job(
                    str(job_id), state="selected", error=f"模型下载失败：{exc}", metadata=job.get("metadata") or {}
                )
                raise

    def cancel_model_download(self, job_id: Any, model_name: Any) -> dict[str, Any]:
        """Request cooperative cancellation while keeping the partial file resumable."""
        name = str(model_name or "").strip()
        if not name:
            raise ValueError("请指定要取消的模型文件。")
        job = self.workflow_job(job_id)
        key = (str(job["id"]), name)
        task = getattr(self, "_model_download_tasks", {}).get(key)
        if task is None or task.done():
            status = self.model_download_status(job["id"], name)
            if status["status"] == "completed":
                raise ValueError("模型已下载完成，无需取消。")
            raise ValueError("该模型当前没有正在运行的下载任务。")
        cancel_events = getattr(self, "_model_download_cancel_events", {})
        cancel_event = cancel_events.get(key)
        if cancel_event is None:
            raise ValueError("下载任务不支持取消，请重新启动下载后再试。")
        cancel_event.set()
        return {"status": "cancelling", "job_id": job["id"], "name": name}

    def _discard_model_download_task(self, key: tuple[str, str], task: asyncio.Task) -> None:
        """Drop finished tasks without consuming their result or hiding its error status."""
        try:
            error = task.exception()
        except asyncio.CancelledError:
            error = "模型下载任务已取消。"
        if self._model_download_tasks.get(key) is task:
            self._model_download_tasks.pop(key, None)
        if error is not None:
            logging.getLogger("Comfyui-omniscience").warning("Background model download failed: %s", error)

    def model_download_status(self, job_id: Any, model_name: Any) -> dict[str, Any]:
        """Return persisted progress/completion for a selected model download."""
        name = str(model_name or "").strip()
        job = self.workflow_job(job_id)
        metadata = dict(job.get("metadata") or {})
        prepared = next(
            (item for item in metadata.get("prepared_models") or [] if isinstance(item, dict) and item.get("name") == name),
            None,
        )
        if prepared is not None:
            return {"status": "completed", "job_id": job["id"], "name": name, "model": prepared}
        task = getattr(self, "_model_download_tasks", {}).get((str(job["id"]), name))
        cancel_event = getattr(self, "_model_download_cancel_events", {}).get((str(job["id"]), name))
        if cancel_event is not None and cancel_event.is_set():
            status = "cancelled" if task is None or task.done() else "cancelling"
            progress = getattr(self, "_model_download_progress", {}).get((str(job["id"]), name), {})
            return {"status": status, "job_id": job["id"], "name": name, **progress}
        if "模型下载已取消" in str(job.get("error") or ""):
            return {"status": "cancelled", "job_id": job["id"], "name": name, "error": job["error"]}
        if task is not None and task.done():
            try:
                error = task.exception()
            except asyncio.CancelledError:
                error = "模型下载任务已取消。"
            if error is not None:
                return {"status": "failed", "job_id": job["id"], "name": name, "error": str(error)}
        if task is not None and not task.done():
            progress = getattr(self, "_model_download_progress", {}).get((str(job["id"]), name), {})
            return {"status": "downloading", "job_id": job["id"], "name": name, **progress}
        if job.get("state") == "preparing_models":
            return {
                "status": "failed", "job_id": job["id"], "name": name,
                "error": "下载服务中断，可重新点击下载并从已下载位置继续。",
            }
        if job.get("error"):
            return {"status": "failed", "job_id": job["id"], "name": name, "error": job["error"]}
        return {"status": "idle", "job_id": job["id"], "name": name}

    async def create_development_task(self, job_id: Any, *, confirmed: bool = False) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("需要用户明确确认后才能创建开发任务。")
        job = self.workflow_job(job_id)
        if job.get("state") not in {"not_found", "offering_task", "failed"}:
            raise ValueError("当前工作流搜索任务还不能转为开发任务。")
        from .api import deps

        agent = deps.task_agent()
        folder = f"workflow-dev-{job['id'][:10]}"
        metadata = {
            "workflow_job_id": job["id"],
            "workflow_query": job.get("query", ""),
            "workflow_search_state": job.get("state"),
            "workflow_search_error": job.get("error", ""),
            "workflow_candidates": job.get("candidates", []),
        }
        task = agent.create_task(f"开发工作流：{job.get('query', '')[:80]}", "act", folder)
        task = agent.store.update_task(task["id"], metadata={**(task.get("metadata") or {}), **metadata})
        agent.store.append_message(
            task["id"],
            {
                "role": "user",
                "content": (
                    "工作流搜索未找到可直接使用的结果。请根据以下需求自行设计并开发：\n"
                    f"{job.get('query', '')}\n\n搜索状态：{job.get('state')}\n"
                    f"失败原因：{job.get('error') or '没有达到匹配阈值。'}"
                ),
            },
        )
        self.store.update_workflow_job(job["id"], state="creating_task", metadata={"task_id": task["id"]})
        return {"status": "task_created", "task_id": task["id"], "folder": task.get("folder"), "redirect": "task"}

    async def execute_workflow(self, job_id: Any, *, confirmed: bool = False, timeout: float = 1800.0) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("必须由用户明确确认后才能执行工作流。")
        job = self.workflow_job(job_id)
        if job.get("state") not in {"ready", "selected", "checking_dependencies"}:
            raise ValueError(f"当前工作流状态不能执行：{job.get('state')}")
        metadata = dict(job.get("metadata") or {})
        candidate = metadata.get("candidate") or {}
        execution_mode = str(candidate.get("execution_mode") or "local").lower()
        if execution_mode in {"remote", "cloud", "api", "hosted"} or candidate.get("remote_endpoint"):
            raise ValueError("该工作流需要非本地模型或云端推理，请使用候选卡片显示的远程服务，不会在本机 ComfyUI 上执行。")
        resource_assessment = metadata.get("candidate", {}).get("resource_assessment") or {}
        if resource_assessment.get("feasible") is False:
            raise ValueError("当前机器资源不足，不能执行该工作流：" + "；".join(resource_assessment.get("reasons") or []))
        workflow = metadata.get("workflow")
        if not isinstance(workflow, dict):
            raise ValueError("工作流内容尚未下载或解析。")
        prompt = workflow.get("prompt") if isinstance(workflow.get("prompt"), dict) else None
        if not prompt:
            raise ValueError("当前工作流不是可自动执行的 ComfyUI API 格式，请先转换为 API 工作流。")
        self.store.update_workflow_job(job["id"], state="executing")
        try:
            result = await asyncio.to_thread(ComfyUIWorkflowExecutor(self.settings).execute, prompt, timeout=timeout)
        except Exception as exc:
            self.store.update_workflow_job(job["id"], state="failed", error=str(exc), metadata={"execution_error": str(exc)})
            raise
        metadata["execution"] = result
        metadata["outputs"] = result.get("outputs") or {}
        self.store.update_workflow_job(job["id"], state="ready", metadata=metadata, error="")
        return {"status": "completed", "job_id": job["id"], "prompt_id": result["prompt_id"], "outputs": result["outputs"], "restart_required": False}

    def workflow_job(self, job_id: Any) -> dict[str, Any]:
        job_key = str(job_id or "").strip()
        job = self.store.get_workflow_job(job_key)
        if job is None:
            raise ValueError("工作流搜索任务不存在。")
        job["candidates"] = self.store.workflow_candidates(job_key)
        return job
