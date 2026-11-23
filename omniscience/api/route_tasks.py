from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from .. import node_review, node_security, skill_review, skill_security, uploads
from ..agent.node_install import NodeSecurityBlockedError
from ..constants import (
    MAX_CURRENT_WORKFLOW_BYTES,
    MAX_NODE_BATCH_SCANS,
    MAX_NODE_PACKAGE_BYTES,
    PRODUCT_NAME,
    WORKFLOW_FILENAME,
)
from ..host import BrowserUnavailable, MCPProtocolError
from ..util import ProjectFolderExistsError
from ..workflow_diagnostics import static_diagnostic
from ..workflow_executor import WorkflowExecutionError
from . import deps
from . import routes as compatibility_routes
from .route_common import FEATURES, HEALTH_VERSION, _body, _composer_workspace, _response

def register_tasks_routes(routes: Any) -> None:
    from aiohttp import web

    @routes.post("/cline/tasks")
    async def cline_create_task(request):
        try:
            values = await _body(request)
            if "folder" not in values:
                raise ValueError("请输入项目文件夹名称。")
            return _response(
                deps.task_agent().create_task(
                    str(values.get("title") or values.get("folder") or "新任务"),
                    str(values.get("mode") or "act"),
                    str(values.get("folder") or ""),
                )
            )
        except ProjectFolderExistsError as exc:
            return _response({"error": str(exc), "code": "folder_exists"}, status=409)
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)
    @routes.get("/cline/custom-nodes")
    async def cline_custom_nodes(request):
        try:
            return _response({"projects": deps.task_agent().custom_node_projects()})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)
    @routes.post("/cline/custom-nodes/adopt")
    async def cline_adopt_custom_node(request):
        try:
            values = await _body(request)
            folder = str(values.get("folder") or "")
            report, blocked = await _node_gate(folder, bool(values.get("acknowledge_security")))
            if blocked is not None:
                return blocked
            return _response(
                {
                    "task": deps.task_agent().adopt_project(folder, security=report),
                    "security": report,
                }
            )
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)
    @routes.post("/cline/custom-nodes/install")
    async def cline_install_custom_node(request):
        """Install a node project from GitHub or an archive into ``custom_nodes``.

        The installer stages and scans outside ``custom_nodes``, so a ``blocked``
        report never leaves files behind; confirming it is possible only from here,
        and never through the global conversation's tool call.
        """
        try:
            values = await _body(request)
            agent = deps.task_agent()
            acknowledged = bool(values.get("acknowledge_security"))
            source = str(values.get("github_url") or values.get("source") or "").strip()
            if values.get("archive_base64"):
                name, package = uploads.decode_upload(
                    values.get("archive_name"),
                    values.get("archive_base64"),
                    max_bytes=MAX_NODE_PACKAGE_BYTES,
                )
                result = await agent.install_archive_bytes(filename=name, package=package, acknowledged=acknowledged)
            elif source:
                result = await agent.install_comfyui_node(source, acknowledged=acknowledged)
            else:
                raise ValueError("请提供 GitHub 仓库地址或上传节点压缩包。")
            return _response(result)
        except NodeSecurityBlockedError as exc:
            return _response({"error": str(exc), "security": exc.report}, status=409)
        except ProjectFolderExistsError as exc:
            return _response({"error": str(exc), "code": "folder_exists"}, status=409)
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)
    async def _node_scan(folder: str) -> dict[str, Any]:
        """Scan one custom_nodes project with the configured engine and cache it."""
        agent = deps.task_agent()
        path = agent.custom_node_project_path(folder)
        static = await asyncio.to_thread(node_security.scan_project, path, source=str(folder))
        report = await node_review.review(static, settings=agent.settings)
        agent.store.save_node_scan(str(path), report)
        return report
    async def _node_gate(folder: str, acknowledged: bool):
        """Review before adoption; ``blocked`` needs the user's explicit insistence.

        The report is computed first and nothing is written until it exists, and
        only the frontend can set ``acknowledged`` -- a model's own verdict can
        harden findings but never authorise its own bypass.
        """
        report = await _node_scan(folder)
        if report.get("verdict") == "blocked" and not acknowledged:
            return report, _response(
                {"error": "安全检测阻止接管：" + skill_security.summarize(report), "security": report},
                status=409,
            )
        if acknowledged:
            report["acknowledged"] = True
        return report, None
    @routes.post("/cline/custom-nodes/scan")
    async def cline_scan_custom_node(request):
        try:
            values = await _body(request)
            return _response({"security": await _node_scan(str(values.get("folder") or ""))})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)
    @routes.post("/cline/custom-nodes/scan-all")
    async def cline_scan_all_custom_nodes(request):
        """Batch scanning always uses the static engine: no model call, no cost."""
        try:
            agent = deps.task_agent()
            projects = agent.custom_node_projects()
            results: list[dict[str, Any]] = []
            for project in projects[:compatibility_routes.MAX_NODE_BATCH_SCANS]:
                path = Path(str(project.get("path") or ""))
                report = await asyncio.to_thread(node_security.scan_project, path, source=str(project.get("folder") or ""))
                report["mode"] = "static"
                agent.store.save_node_scan(str(path), report)
                results.append({"folder": project.get("folder"), "path": str(path), "security": report})
            return _response(
                {
                    "scanned": len(results),
                    "remaining": max(len(projects) - len(results), 0),
                    "mode": "static",
                    "results": results,
                }
            )
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)
    @routes.post("/cline/tasks/{task_id}/scan")
    async def cline_scan_task_project(request):
        """Re-scan an adopted task's project with the configured engine."""
        try:
            agent = deps.task_agent()
            task_id = request.match_info["task_id"]
            path = agent.task_project_path(task_id)
            static = await asyncio.to_thread(node_security.scan_project, path, source=path.name)
            report = await node_review.review(static, settings=agent.settings)
            agent.store.save_node_scan(str(path), report)
            return _response({"security": report, "task": agent.record_task_security(task_id, report)})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)
    @routes.delete("/cline/tasks/{task_id}")
    async def cline_delete_task(request):
        try:
            return _response(deps.task_agent().delete_task(request.match_info["task_id"]))
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=409 if "运行" in str(exc) else 404)
    @routes.get("/cline/tasks/{task_id}")
    async def cline_task(request):
        try:
            agent = deps.task_agent()
            task = agent.task(request.match_info["task_id"])
            return _response(
                {
                    "task": task,
                    "messages": agent.store.messages(task["id"]),
                    "pending_approval": agent.store.pending_approval(task["id"]),
                }
            )
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=404)
    @routes.get("/cline/tasks/{task_id}/workflow")
    async def cline_task_workflow(request):
        try:
            agent = deps.task_agent()
            task_id = request.match_info["task_id"]
            workflow_path, created = agent._ensure_task_workflow(task_id)
            task = agent.task(task_id)
            content = agent.workspace(task_id).read_file(WORKFLOW_FILENAME) if workflow_path else ""
            return _response({
                "path": workflow_path,
                "content": content,
                "created": created,
                "current": agent.store.current_workflow(task_id),
                "task": task,
            })
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=404)
    @routes.get("/cline/tasks/{task_id}/current-workflow")
    async def cline_current_workflow(request):
        try:
            agent = deps.task_agent()
            task_id = request.match_info["task_id"]
            agent.task(task_id)
            return _response({"current": agent.store.current_workflow(task_id), "task": agent.task(task_id)})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=404)
    @routes.get("/cline/tasks/{task_id}/context")
    async def cline_task_context(request):
        try:
            raw_skill_ids = request.query.get("skill_ids", "[]")
            skill_ids = json.loads(raw_skill_ids)
            return _response(await deps.task_agent().context_status(request.match_info["task_id"], skill_ids))
        except (ValueError, OSError, RuntimeError, MCPProtocolError) as exc:
            return _response({"error": str(exc)}, status=400)
    @routes.post("/cline/tasks/{task_id}/current-workflow")
    async def cline_update_current_workflow(request):
        try:
            if request.content_length and request.content_length > MAX_CURRENT_WORKFLOW_BYTES + 10_000:
                return _response({"error": f"当前工作流请求过大，不能超过 {MAX_CURRENT_WORKFLOW_BYTES // 1_000_000} MB。"}, status=413)
            values = await _body(request)
            workflow = values.get("workflow")
            source = str(values.get("source") or "comfyui_canvas")
            current = deps.task_agent().update_current_workflow(request.match_info["task_id"], workflow, source)
            return _response({"current": current, "task": deps.task_agent().task(request.match_info["task_id"])})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)
    @routes.patch("/cline/tasks/{task_id}")
    async def cline_update_task(request):
        try:
            values = await _body(request)
            return _response(deps.task_agent().store.update_task(request.match_info["task_id"], **values))
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)
    @routes.post("/cline/tasks/{task_id}/messages")
    async def cline_task_message(request):
        try:
            values = await _body(request)
            content = str(values.get("content") or "")
            skill_ids = values.get("skill_ids", [])
            controller = deps.controller()
            controller.validate(content, skill_ids)
            return _response(
                controller.submit(
                    request.match_info["task_id"],
                    content,
                    str(values.get("context") or ""),
                    skill_ids,
                ),
                status=202,
            )
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)
    @routes.post("/cline/tasks/{task_id}/upload")
    async def cline_task_upload(request):
        try:
            values = await _body(request)
            workspace = deps.task_agent().workspace(request.match_info["task_id"])
            return _response(uploads.store_upload(workspace, values.get("name"), values.get("content_base64")))
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)
    @routes.post("/cline/global/upload")
    async def cline_global_upload(request):
        try:
            values = await _body(request)
            workspace = deps.global_chat_agent().chat_workspace()
            return _response(uploads.store_upload(workspace, values.get("name"), values.get("content_base64")))
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)
    @routes.post("/cline/global/network")
    async def cline_global_network(request):
        try:
            values = await _body(request)
            settings = deps.global_chat_agent().settings
            enabled = bool(values.get("enabled"))
            settings.update({"global_network_enabled": enabled})
            return _response({"network_enabled": enabled})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)
    @routes.post("/cline/tasks/{task_id}/network")
    async def cline_task_network(request):
        try:
            values = await _body(request)
            task_id = request.match_info["task_id"]
            agent = deps.task_agent()
            task = agent.task(task_id)
            metadata = {**(task.get("metadata") or {}), "network_enabled": bool(values.get("enabled"))}
            return _response(agent.store.update_task(task_id, metadata=metadata))
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)
    @routes.post("/cline/tasks/{task_id}/cancel")
    async def cline_cancel_task(request):
        try:
            return _response(deps.controller().abort(request.match_info["task_id"]))
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=404)
    @routes.delete("/cline/tasks/{task_id}/history")
    async def cline_clear_task_history(request):
        try:
            task_id = request.match_info["task_id"]
            task = deps.task_agent().clear_history(task_id)
            return _response({"task": task, "messages": [], "cleared": True})
        except ValueError as exc:
            return _response({"error": str(exc)}, status=400)
        except OSError as exc:
            return _response({"error": str(exc)}, status=404)
    @routes.get("/cline/tasks/{task_id}/events")
    async def cline_task_events(request):
        try:
            task_id = request.match_info["task_id"]
            agent = deps.task_agent()
            agent.task(task_id)
            events = agent.store.events(task_id, int(request.query.get("after", "-1")))
            return _response(
                {
                    "events": events,
                    "running": deps.controller().is_running(task_id),
                    "queued": deps.controller().pending(task_id),
                }
            )
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=404)
    @routes.get("/cline/tasks/{task_id}/skills")
    async def cline_task_skills(request):
        """Compatibility response for clients that still request task Skills.

        Skills are global and selected per message now, so this route never
        exposes or persists a task-level selection.
        """
        try:
            agent = deps.task_agent()
            task_id = request.match_info["task_id"]
            task = agent.task(task_id)
            return _response({"task": task, "skills": agent.skills.list(), "selected_ids": [], "selected": []})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=404)
    @routes.post("/cline/tasks/{task_id}/skills")
    async def cline_update_task_skills(request):
        """Validate legacy task-Skill updates without persisting them."""
        try:
            values = await _body(request)
            agent = deps.task_agent()
            task_id = request.match_info["task_id"]
            task = agent.task(task_id)
            agent._validated_skill_ids(values.get("skill_ids", []))
            return _response({"task": task, "skills": agent.skills.list(), "selected_ids": [], "selected": []})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)
    @routes.post("/cline/tasks/{task_id}/mode")
    async def cline_task_mode(request):
        try:
            values = await _body(request)
            return _response(deps.task_agent().store.update_task(request.match_info["task_id"], mode=str(values.get("mode") or "act")))
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)
    @routes.get("/cline/tasks/{task_id}/checkpoints")
    async def cline_task_checkpoints(request):
        try:
            task_id = request.match_info["task_id"]
            agent = deps.task_agent()
            agent.task(task_id)
            return _response({"checkpoints": agent.store.checkpoints(task_id)})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=404)
    @routes.get("/cline/checkpoints/{checkpoint_id}/diff")
    async def cline_checkpoint_diff(request):
        try:
            agent = deps.task_agent()
            checkpoint = agent.store.checkpoint(request.match_info["checkpoint_id"])
            if not checkpoint:
                raise ValueError("Checkpoint does not exist.")
            task = agent.task(checkpoint["task_id"])
            diff = agent.checkpoints.compare(task["id"], Path(task["workspace"]), checkpoint["backend"], checkpoint["reference"])
            return _response({"diff": diff})
        except (ValueError, OSError, RuntimeError) as exc:
            return _response({"error": str(exc)}, status=400)
    @routes.post("/cline/checkpoints/{checkpoint_id}/restore")
    async def cline_checkpoint_restore(request):
        try:
            values = await _body(request)
            return _response(
                await deps.task_agent().restore_checkpoint(
                    request.match_info["checkpoint_id"],
                    bool(values.get("restore_task")),
                    bool(values.get("restore_files", True)),
                )
            )
        except (ValueError, OSError, RuntimeError) as exc:
            return _response({"error": str(exc)}, status=400)
    @routes.get("/cline/mcp")
    async def cline_mcp(request):
        return _response({"servers": deps.task_agent().mcp.public()})
    @routes.post("/cline/mcp")
    async def cline_update_mcp(request):
        try:
            values = await _body(request)
            return _response({"servers": deps.task_agent().mcp.update(values)})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)
