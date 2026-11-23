"""Safe installation of user-uploaded ComfyUI workflow files."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Any

from .workflow_parser import inspect_workflow, load_workflow


class WorkflowInstallMixin:
    """Install validated workflow JSON without executing or overwriting it."""

    def _uploaded_workflow_path(self, source: Any):
        value = str(source or "").strip().replace("\\", "/")
        path = PurePosixPath(value)
        if path.is_absolute() or len(path.parts) != 2 or path.parts[0] != "uploads" or path.name.startswith("."):
            raise ValueError("工作流来源必须是全局工作区 uploads/ 下的 JSON 文件。")
        if path.suffix.casefold() != ".json":
            raise ValueError("目前只支持安装 JSON 格式的 ComfyUI 工作流。")
        workspace = self.chat_workspace()
        uploads_root = workspace.root / "uploads"
        if uploads_root.is_symlink() or not uploads_root.is_dir():
            raise ValueError("全局工作区 uploads/ 目录无效。")
        raw_path = workspace.root.joinpath(*path.parts)
        if raw_path.is_symlink() or not raw_path.is_file():
            raise ValueError("上传的工作流文件不存在或不是普通文件。")
        uploaded = raw_path.resolve()
        try:
            uploaded.relative_to(uploads_root.resolve())
        except ValueError as exc:
            raise ValueError("工作流文件必须位于 uploads/ 目录内。") from exc
        return uploaded

    def install_uploaded_workflow(self, source: Any) -> dict[str, Any]:
        """Validate and copy one uploaded workflow into ComfyUI's user library.

        This deliberately does not convert, execute, install nodes, or download
        models.  The original JSON is copied only after the existing read-only
        parser accepts it, and the destination is created with ``O_EXCL`` so a
        same-named workflow can never be silently replaced.
        """
        uploaded = self._uploaded_workflow_path(source)
        try:
            raw = uploaded.read_bytes()
        except OSError as exc:
            raise ValueError(f"无法读取上传的工作流：{exc}") from exc
        workflow = load_workflow(raw)
        inspection = inspect_workflow(workflow)
        settings_data = self.settings.load() if hasattr(self.settings, "load") else dict(self.settings or {})
        comfyui_root = str(settings_data.get("comfyui_directory") or "").strip()
        if not comfyui_root:
            raise ValueError("请先在设置里填写 ComfyUI 目录，才能安装工作流。")
        workflow_directory = self.settings.comfyui_workflow_directory()
        if workflow_directory is None:
            raise ValueError("请先在设置里填写 ComfyUI 目录，才能安装工作流。")
        comfyui_path = Path(comfyui_root).expanduser().resolve()
        try:
            workflow_directory.relative_to(comfyui_path)
        except ValueError as exc:
            raise ValueError("ComfyUI 工作流目录不能位于 ComfyUI 目录之外。") from exc
        destination = workflow_directory / uploaded.name
        if destination.exists() or destination.is_symlink():
            raise ValueError(f"ComfyUI 工作流目录中已存在：{destination.name}。请换一个文件名。")
        try:
            file_descriptor = os.open(
                str(destination),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(file_descriptor, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
        except FileExistsError as exc:
            raise ValueError(f"ComfyUI 工作流目录中已存在：{destination.name}。请换一个文件名。") from exc
        except OSError as exc:
            raise ValueError(f"无法写入 ComfyUI 工作流目录：{exc}") from exc
        workflow_job_id = ""
        candidate_id = ""
        candidate: dict[str, Any] | None = None
        if hasattr(self, "store"):
            job = self.store.create_workflow_job(f"上传工作流：{uploaded.name}", region={"mode": "local"})
            workflow_job_id = str(job.get("id") or "")
            if workflow_job_id:
                candidate_id = f"uploaded:{workflow_job_id}"
                candidate = {
                    "id": candidate_id,
                    "title": uploaded.stem or uploaded.name,
                    "description": "来自全局对话上传的本地工作流；可在此检查节点和模型依赖。",
                    "region": "local",
                    "execution_mode": "local",
                    "source_type": "uploaded",
                    "source": str(source).strip().replace("\\", "/"),
                    "resource_requirements": {},
                }
                self.store.save_workflow_candidates(workflow_job_id, [candidate])
                self.store.update_workflow_job(
                    workflow_job_id,
                    state="checking_dependencies",
                    selected_candidate_id=candidate_id,
                    metadata={
                        "candidate": candidate,
                        "workflow": workflow,
                        "inspection": inspection,
                        "uploaded_source": str(source).strip().replace("\\", "/"),
                        "destination": str(destination.resolve()),
                    },
                )
        return {
            "status": "installed",
            "source": str(source).strip().replace("\\", "/"),
            "destination": str(destination.resolve()),
            "format": inspection.get("format"),
            "node_count": inspection.get("node_count", 0),
            "class_types": inspection.get("class_types", []),
            "model_count": len(inspection.get("models") or []),
            "executed": False,
            "nodes_installed": False,
            "models_downloaded": False,
            "restart_required": False,
            "workflow_job_id": workflow_job_id,
            "candidate_id": candidate_id,
            "candidate": candidate,
        }
