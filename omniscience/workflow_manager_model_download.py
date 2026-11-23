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


class WorkflowModelDownloadMixin:
    """WorkflowModelDownloadMixin implementation."""

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
        plan = (job.get("metadata") or {}).get("dependency_plan")
        if not plan:
            plan = await asyncio.to_thread(self.dependency_plan, job_id)
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
            if self._model_directory_invalid(item) or item.get("status") == "missing_directory":
                raise ValueError(f"模型 {item.get('name')} 的 ComfyUI 模型目录无效，已禁止下载或安装。")
            if not isinstance(item.get("source"), dict):
                raise ValueError(f"模型 {item.get('name')} 没有可信下载来源，请先搜索并选择 Hugging Face 文件。")
            source_filename = str(item["source"].get("filename") or item.get("name") or "").replace("\\", "/")
            if not model_filenames_match(
                Path(source_filename).name,
                Path(str(item.get("name") or "").replace("\\", "/")).name,
            ) or ".." in Path(source_filename).parts:
                raise ValueError(f"模型 {item.get('name')} 的下载来源文件名不匹配。")
        model_name = str(selected_models[0].get("name") or "")
        if not wait_for_completion:
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
            previous_snapshot = dict(
                ((job.get("metadata") or {}).get("model_downloads") or {}).get(model_name) or {}
            )
            progress_by_key[key] = {
                "downloaded_bytes": int(previous_snapshot.get("downloaded_bytes") or 0),
                "total_bytes": previous_snapshot.get("total_bytes"),
            }
            self._save_model_download_snapshot(
                job["id"], model_name, status="downloading",
                downloaded_bytes=progress_by_key[key]["downloaded_bytes"],
                total_bytes=progress_by_key[key]["total_bytes"], error=""
            )
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
                    progress = {
                        "downloaded_bytes": downloaded_bytes,
                        "total_bytes": total_bytes,
                    }
                    self._model_download_progress[model_key] = progress
                    self._save_model_download_snapshot(
                        model_key[0], model_key[1], status="downloading", **progress
                    )

                if cancel_event is not None and cancel_event.is_set():
                    raise ModelDownloadCancelled("模型下载已取消；已保留部分文件，可重试并从断点继续。")
                downloaded = await asyncio.to_thread(
                    downloader.download,
                    repo_id=source.get("repo_id"), filename=source.get("filename") or item.get("name"),
                    category=str(item.get("category") or ""), cache_root=cache_root,
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
                    target_name=Path(str(item.get("name") or "").replace("\\", "/")).name,
                )
                installed.append({"name": item.get("name"), "download": downloaded, "link": linked})
        except ModelDownloadCancelled:
            latest = self.store.get_workflow_job(job["id"]) or job
            self._save_model_download_snapshot(
                job["id"], model_name, status="cancelled", error="模型下载已取消，可重试并从断点继续。"
            )
            latest = self.store.get_workflow_job(job["id"]) or latest
            self.store.update_workflow_job(job["id"], state="selected", error="模型下载已取消，可重试并从断点继续。", metadata=latest.get("metadata") or {})
            raise
        except ModelDownloadRetryableError as exc:
            latest = self.store.get_workflow_job(job["id"]) or job
            self._save_model_download_snapshot(job["id"], model_name, status="paused", error=str(exc))
            latest = self.store.get_workflow_job(job["id"]) or latest
            self.store.update_workflow_job(job["id"], state="selected", error="", metadata=latest.get("metadata") or {})
            raise
        except Exception as exc:
            latest = self.store.get_workflow_job(job["id"]) or job
            self._save_model_download_snapshot(
                job["id"], model_name, status="failed", error=f"模型下载失败：{exc}"
            )
            latest = self.store.get_workflow_job(job["id"]) or latest
            self.store.update_workflow_job(job["id"], state="selected", error="模型下载失败，请检查来源后重试。", metadata=latest.get("metadata") or {})
            raise
        latest = self.store.get_workflow_job(job["id"]) or job
        metadata = dict(latest.get("metadata") or {})
        prepared = list(metadata.get("prepared_models") or [])
        prepared_by_name = {str(item.get("name") or ""): item for item in prepared if isinstance(item, dict)}
        prepared_by_name.update({str(item.get("name") or ""): item for item in installed})
        metadata["prepared_models"] = list(prepared_by_name.values())
        self._save_model_download_snapshot(job["id"], model_name, status="completed", error="")
        latest = self.store.get_workflow_job(job["id"]) or latest
        metadata = dict(latest.get("metadata") or {})
        metadata["prepared_models"] = list(prepared_by_name.values())
        metadata.pop("dependency_plan", None)
        remaining = [item for item in plan.get("models", []) if item.get("status") not in {"installed", "selected_local"} and item.get("name") not in prepared_by_name]
        state = "ready" if not remaining else "selected"
        self.store.update_workflow_job(job["id"], state=state, error="", metadata=metadata)
        return {"status": "models_prepared", "job_id": job["id"], "models": installed, "remaining_models": [item.get("name") for item in remaining], "restart_required": bool(metadata.get("prepared_nodes"))}


    def _save_model_download_snapshot(
        self,
        job_id: Any,
        model_name: Any,
        *,
        status: str,
        downloaded_bytes: int | None = None,
        total_bytes: int | None = None,
        error: str | None = None,
    ) -> None:
        """Persist the resumable download projection alongside the workflow job."""
        job = self.store.get_workflow_job(str(job_id))
        if not job:
            return
        name = str(model_name or "").strip()
        if not name:
            return
        metadata = dict(job.get("metadata") or {})
        downloads = dict(metadata.get("model_downloads") or {})
        previous = dict(downloads.get(name) or {})
        snapshot = {
            **previous,
            "status": str(status),
            "downloaded_bytes": previous.get("downloaded_bytes", 0) if downloaded_bytes is None else int(downloaded_bytes),
            "total_bytes": previous.get("total_bytes") if total_bytes is None else int(total_bytes),
        }
        if error is not None:
            snapshot["error"] = str(error)
        downloads[name] = snapshot
        metadata["model_downloads"] = downloads
        self.store.update_workflow_job(str(job_id), metadata=metadata)


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
            except ModelDownloadRetryableError as exc:
                job = self.store.get_workflow_job(str(job_id)) or {}
                self._save_model_download_snapshot(str(job_id), model_name, status="paused", error=str(exc))
                job = self.store.get_workflow_job(str(job_id)) or job
                self.store.update_workflow_job(
                    str(job_id), state="selected", error="", metadata=job.get("metadata") or {}
                )
                raise
            except Exception as exc:
                job = self.store.get_workflow_job(str(job_id)) or {}
                self._save_model_download_snapshot(
                    str(job_id), model_name, status="failed", error=f"模型下载失败：{exc}"
                )
                job = self.store.get_workflow_job(str(job_id)) or job
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
        self._save_model_download_snapshot(
            job["id"], name, status="cancelling", error=""
        )
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
        snapshot = dict((metadata.get("model_downloads") or {}).get(name) or {})
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
            progress = {**snapshot, **getattr(self, "_model_download_progress", {}).get((str(job["id"]), name), {})}
            return {**progress, "status": status, "job_id": job["id"], "name": name}
        if "模型下载已取消" in str(job.get("error") or ""):
            return {
                **snapshot,
                "status": "cancelled",
                "job_id": job["id"],
                "name": name,
                "error": job["error"],
            }
        if task is not None and task.done():
            try:
                error = task.exception()
            except asyncio.CancelledError:
                error = "模型下载任务已取消。"
            if error is not None and str(snapshot.get("status") or "") != "paused":
                if self._is_resumable_download_error(snapshot.get("error") or error):
                    return {**snapshot, "status": "paused", "job_id": job["id"], "name": name}
                return {
                    **snapshot,
                    "status": "failed",
                    "job_id": job["id"],
                    "name": name,
                    "error": str(error),
                }
        if task is not None and not task.done():
            progress = {**snapshot, **getattr(self, "_model_download_progress", {}).get((str(job["id"]), name), {})}
            return {**progress, "status": "downloading", "job_id": job["id"], "name": name}
        if snapshot:
            status = str(snapshot.get("status") or "idle")
            if status == "failed" and self._is_resumable_download_error(snapshot.get("error")):
                status = "paused"
            if status in {"downloading", "cancelling"}:
                # The asyncio task is an in-process projection.  After a UI
                # reload or agent recreation the .part file is still valid,
                # but the old task is not; expose it as resumable instead of
                # showing a download that can never advance.
                status = "paused"
            return {**snapshot, "status": status, "job_id": job["id"], "name": name}
        if self._is_resumable_download_error(job.get("error")):
            return {
                "status": "paused",
                "job_id": job["id"],
                "name": name,
                "error": job["error"],
            }
        if job.get("state") == "preparing_models":
            return {
                "status": "failed", "job_id": job["id"], "name": name,
                "error": "下载服务中断，可重新点击下载并从已下载位置继续。",
            }
        if job.get("error"):
            return {
                **snapshot,
                "status": "failed",
                "job_id": job["id"],
                "name": name,
                "error": job["error"],
            }
        return {"status": "idle", "job_id": job["id"], "name": name}


    @staticmethod
    def _is_resumable_download_error(error: Any) -> bool:
        text = str(error or "").lower()
        return "timed out" in text or "timeout" in text or "超时" in text or "已保留可续传" in text
