"""Project folder provisioning for tasks (DESIGN.md section 4, tasks table).

Task creation owns three filesystem artifacts: the project directory, the
ComfyUI ``custom_nodes`` symlink, and the starter ``workflow.json``. Each is
created or repaired through the helpers here so failures can roll back only
what the current call actually created.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from ..constants import MAX_CUSTOM_NODE_PROJECTS, WORKFLOW_FILENAME
from ..util import (
    ProjectFolderExistsError,
    create_filesystem_link,
    default_workspace,
    is_filesystem_link,
    project_folder_name,
    remove_filesystem_link,
    write_workflow_if_missing,
)
from ..workspace import Workspace

CUSTOM_NODES_SOURCE = "comfyui_custom_nodes"


class ProjectProvisioningMixin:
    def _comfyui_link_path(self, project: Path, folder_name: str) -> Path | None:
        """Return the custom_nodes link path for a project, if configured."""
        comfyui_nodes = self.settings.comfyui_node_directory()
        if comfyui_nodes is None:
            return None
        link = comfyui_nodes / project_folder_name(folder_name)
        # A workspace configured directly as custom_nodes needs no self-link.
        if link == project.resolve():
            return None
        return link

    def _ensure_comfyui_link(self, project: Path | str, folder_name: str) -> tuple[str, bool]:
        """Ensure a project is available from ComfyUI's custom_nodes directory.

        The boolean reports whether this call created the link.  Keeping it
        separate lets task creation roll back only a link that it owns, while
        completion can safely repair a link that was removed after creation.
        """
        project_path = Path(project).expanduser().resolve()
        link = self._comfyui_link_path(project_path, folder_name)
        if link is None:
            return "", False

        if is_filesystem_link(link) and link.resolve(strict=False) == project_path:
            return str(link), False
        if link.exists() or is_filesystem_link(link):
            raise ProjectFolderExistsError(f"ComfyUI custom_nodes 中已存在：{link.name}。请换一个文件夹名称。")
        try:
            create_filesystem_link(link, project_path, target_is_directory=True)
        except (FileExistsError, OSError) as exc:
            # Another process may have created the correct link between the
            # collision check and link creation. Never replace its target.
            if is_filesystem_link(link) and link.resolve(strict=False) == project_path:
                return str(link), False
            if link.exists() or is_filesystem_link(link):
                raise ProjectFolderExistsError(
                    f"ComfyUI custom_nodes 中已存在：{link.name}。请换一个文件夹名称。"
                ) from exc
            raise
        return str(link), True

    def _ensure_task_comfyui_link(self, task_id: str) -> str:
        """Repair the ComfyUI link when a task reaches a successful finish."""
        task = self.task(task_id)
        metadata = dict(task.get("metadata") or {})
        folder_name = metadata.get("project_folder") or Path(str(task["workspace"])).name
        link, _ = self._ensure_comfyui_link(task["workspace"], folder_name)
        if link and metadata.get("comfyui_link") != link:
            metadata["comfyui_link"] = link
            self.store.update_task(task_id, metadata=metadata)
        return link

    def _ensure_task_workflow(self, task_id: str) -> tuple[str, bool]:
        """Create the starter workflow if a task project does not have one."""
        task = self.task(task_id)
        metadata = dict(task.get("metadata") or {})
        folder_name = metadata.get("project_folder") or Path(str(task["workspace"])).name
        if metadata.get("project_source") == CUSTOM_NODES_SOURCE:
            # An adopted project is somebody else's repository. Report the
            # workflow it already has; never write files into it.
            existing = Path(task["workspace"]) / WORKFLOW_FILENAME
            workflow_value = str(existing) if existing.is_file() else ""
            if metadata.get("workflow") != workflow_value:
                metadata["workflow"] = workflow_value
                self.store.update_task(task_id, metadata=metadata)
            return workflow_value, False
        workflow, created = write_workflow_if_missing(Path(task["workspace"]), folder_name)
        workflow_value = str(workflow)
        if metadata.get("workflow") != workflow_value:
            metadata["workflow"] = workflow_value
            self.store.update_task(task_id, metadata=metadata)
        return workflow_value, created

    def create_task(self, title: str = "新任务", mode: str = "act", folder: str | None = None) -> dict[str, Any]:
        root = self.workspace().root
        # Direct Python callers from older integrations did not provide a
        # folder. Keep them working with a unique folder, while the HTTP API
        # requires the user-facing folder field explicitly.
        folder_name = project_folder_name(folder) if folder is not None else f"task-{uuid.uuid4().hex[:12]}"
        project = root / folder_name
        link = self._comfyui_link_path(project, folder_name)
        if link is not None and (link.exists() or is_filesystem_link(link)):
            if is_filesystem_link(link):
                raise ProjectFolderExistsError(f"ComfyUI custom_nodes 中已存在：{folder_name}。请换一个文件夹名称。")
            raise ProjectFolderExistsError(
                f"ComfyUI custom_nodes 中已有项目 {folder_name}。请在任务历史里点击它接管，或换一个文件夹名称。"
            )
        try:
            project.mkdir()
        except FileExistsError as exc:
            raise ProjectFolderExistsError(f"项目文件夹已存在：{folder_name}。请换一个文件夹名称。") from exc
        link_created = False
        link_value = ""
        workflow_created = False
        try:
            link_value, link_created = self._ensure_comfyui_link(project, folder_name)
            workflow, workflow_created = write_workflow_if_missing(project, folder_name)
            task = self.store.create_task(
                str(project.resolve()),
                title or folder_name,
                mode,
                {
                    "project_folder": folder_name,
                    "workspace_root": str(root),
                    "comfyui_link": link_value,
                    "workflow": str(workflow),
                },
            )
        except Exception:
            if link_created:
                try:
                    remove_filesystem_link(link_value)
                except OSError:
                    pass
            # Do not leave an orphan directory if persistence fails before the
            # task becomes visible in history.
            try:
                if workflow_created:
                    (project / WORKFLOW_FILENAME).unlink()
                project.rmdir()
            except OSError:
                pass
            raise
        self._event(task["id"], "task_created", task)
        return task

    def _registered_projects(self) -> dict[Path, dict[str, Any]]:
        """Map every task's project directory to its task row."""
        registered: dict[Path, dict[str, Any]] = {}
        for task in self.store.list_tasks(None):
            try:
                registered[Path(str(task.get("workspace") or "")).expanduser().resolve()] = task
            except OSError:
                continue
        return registered

    def custom_node_projects(self) -> list[dict[str, Any]]:
        """List ComfyUI ``custom_nodes`` folders that no task covers yet.

        Symlinks are skipped on purpose: they are the links created for
        workspace projects, whose tasks are already in the history.
        """
        nodes = self.settings.comfyui_node_directory()
        if nodes is None:
            return []
        registered = self._registered_projects()
        cached = self.store.node_scans()
        found: list[dict[str, Any]] = []
        try:
            entries = sorted(nodes.iterdir(), key=lambda item: item.name.lower())
        except OSError:
            return []
        for entry in entries:
            try:
                if entry.name.startswith(".") or entry.name == "__pycache__" or not entry.is_dir():
                    continue
                if is_filesystem_link(entry):
                    continue
                path = entry.resolve()
                if path in registered:
                    continue
                scan = cached.get(str(path)) or {}
                found.append(
                    {
                        "folder": entry.name,
                        "path": str(path),
                        "modified_at": entry.stat().st_mtime,
                        "security": scan.get("report"),
                        "scan_updated_at": scan.get("updated_at"),
                    }
                )
            except OSError:
                continue
            if len(found) >= MAX_CUSTOM_NODE_PROJECTS:
                break
        found.sort(key=lambda item: item["modified_at"], reverse=True)
        return found

    def custom_node_project_path(self, folder: str) -> Path:
        """Resolve one ``custom_nodes`` folder for scanning or adoption.

        Symlinks are refused: they point back at a workspace project whose task
        already exists, and following them would let a folder name escape into
        someone else's repository.
        """
        name = project_folder_name(folder)
        nodes = self.settings.comfyui_node_directory()
        if nodes is None:
            raise ValueError("请先在设置里填写 ComfyUI 目录，再接管 custom_nodes 中的项目。")
        project = nodes / name
        if is_filesystem_link(project):
            raise ValueError(f"{name} 是指向工作区的软链接或目录 Junction，对应任务已经在历史里。")
        if not project.is_dir():
            raise ValueError(f"ComfyUI custom_nodes 中没有项目文件夹：{name}")
        return project.resolve()

    def adopt_project(self, folder: str, *, security: dict[str, Any] | None = None) -> dict[str, Any]:
        """Register an existing ``custom_nodes`` folder as a task, without touching its files."""
        name = project_folder_name(folder)
        nodes = self.settings.comfyui_node_directory()
        path = self.custom_node_project_path(folder)
        existing = self._registered_projects().get(path)
        if existing is not None:
            return existing
        workflow = path / WORKFLOW_FILENAME
        metadata: dict[str, Any] = {
            "project_folder": name,
            "workspace_root": str(nodes),
            "comfyui_link": "",
            "workflow": str(workflow) if workflow.is_file() else "",
            "project_source": CUSTOM_NODES_SOURCE,
        }
        if isinstance(security, dict):
            metadata["security"] = security
        task = self.store.create_task(str(path), name, "act", metadata)
        self._event(task["id"], "task_created", task)
        return task

    def task_project_path(self, task_id: str) -> Path:
        """The project directory behind a task, for scanning an adopted node."""
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError("任务不存在。")
        path = Path(str(task.get("workspace") or "")).expanduser().resolve()
        if not path.is_dir():
            raise ValueError(f"任务的项目目录不存在：{path}")
        return path

    def record_task_security(self, task_id: str, report: dict[str, Any]) -> dict[str, Any]:
        """Keep the node report on the task so its badge survives a reload."""
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError("任务不存在。")
        metadata = dict(task.get("metadata") or {})
        metadata["security"] = report
        updated = self.store.update_task(task_id, metadata=metadata)
        self._event(
            task_id,
            "task_security_scanned",
            {
                "verdict": report.get("verdict"),
                "severity": report.get("severity"),
                "engine": report.get("engine"),
            },
        )
        return updated
