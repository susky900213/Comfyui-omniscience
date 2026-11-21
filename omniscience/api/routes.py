"""aiohttp route registration for the ComfyUI panel (DESIGN.md section 8).

Task messages are accepted with ``202`` and executed by the turn
controller; the client observes progress through the events endpoint.
Everything else keeps the historical request/response shapes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from .. import node_review, node_security, skill_review, skill_security, uploads
from ..agent.node_install import NodeSecurityBlockedError
from ..region_router import RegionRouter
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

logger = logging.getLogger("Comfyui-omniscience")

HEALTH_VERSION = "0.3.0"
FEATURES = ["tasks", "global_chat", "cancellation", "checkpoints", "mcp", "browser", "live_workflow", "upload", "network_switch", "global_workspace", "skill_security", "custom_node_projects", "node_security", "node_install", "workflow_node_search"]


def _composer_workspace(request) -> Any:
    """Workspace backing @ mentions: the global scratch one, a task's, or the default."""
    if request.query.get("scope") == "global":
        return deps.global_chat_agent().chat_workspace()
    task_id = request.query.get("task_id")
    return deps.task_agent().workspace(task_id) if task_id else deps.legacy_agent().workspace()


def _response(payload: Any, status: int = 200):
    from aiohttp import web

    return web.json_response(payload, status=status)


async def _body(request) -> dict[str, Any]:
    try:
        value = await request.json()
    except (TypeError, ValueError) as exc:
        raise ValueError("Request body must be valid JSON.") from exc
    if not isinstance(value, dict):
        raise ValueError("Request body must be a JSON object.")
    return value


def register_routes(prompt_server: Any) -> None:
    """Register routes once when ComfyUI imports this custom node."""

    from aiohttp import web

    routes = prompt_server.routes

    @routes.get("/cline/health")
    async def cline_health(request):
        return _response({"name": PRODUCT_NAME, "status": "ok", "version": HEALTH_VERSION, "features": FEATURES})

    @routes.get("/cline/tasks")
    async def cline_tasks(request):
        try:
            return _response({"tasks": deps.task_agent().store.list_tasks(int(request.query.get("limit", "100")))})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.post("/cline/global-chat/workflows/search")
    async def cline_search_workflows(request):
        try:
            values = await _body(request)
            return _response(
                await deps.global_chat_agent().search_runninghub_workflows(
                    values.get("query", ""), limit=values.get("limit", 10), filters=values.get("filters")
                )
            )
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.post("/cline/global-chat/workflows/select")
    async def cline_select_workflow(request):
        try:
            values = await _body(request)
            return _response(
                deps.global_chat_agent().select_workflow(values.get("job_id"), values.get("candidate_id"))
            )
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.post("/cline/global-chat/workflows/inspect")
    async def cline_inspect_workflow(request):
        try:
            values = await _body(request)
            return _response(
                await deps.global_chat_agent().inspect_selected_workflow(
                    values.get("job_id"), loaded_nodes=values.get("loaded_nodes")
                )
            )
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.get("/cline/global-chat/workflows/{job_id}/dependencies")
    async def cline_workflow_dependencies(request):
        try:
            return _response(deps.global_chat_agent().dependency_plan(request.match_info["job_id"]))
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.post("/cline/global-chat/workflows/{job_id}/dependencies/nodes/search")
    async def cline_search_workflow_node_sources(request):
        try:
            agent = deps.global_chat_agent()
            job = agent.workflow_job(request.match_info["job_id"])
            if not job.get("selected_candidate_id"):
                raise ValueError("请先在界面中选择一个工作流，再搜索它的缺失节点。")
            return _response(await agent.search_workflow_node_sources(request.match_info["job_id"]))
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.post("/cline/global-chat/workflows/{job_id}/dependencies/nodes/source")
    async def cline_select_workflow_node_source(request):
        try:
            values = await _body(request)
            return _response(
                deps.global_chat_agent().select_workflow_node_source(
                    request.match_info["job_id"], values.get("class_type"), values.get("source")
                )
            )
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.post("/cline/global-chat/workflows/{job_id}/dependencies/models/search")
    async def cline_search_workflow_model_sources(request):
        try:
            return _response(await deps.global_chat_agent().search_workflow_model_sources(request.match_info["job_id"]))
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.get("/cline/global-chat/workflows/{job_id}/dependencies/models/local")
    async def cline_list_local_workflow_models(request):
        try:
            return _response(deps.global_chat_agent().local_model_inventory(request.match_info["job_id"]))
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.post("/cline/global-chat/workflows/{job_id}/dependencies/models/source")
    async def cline_select_workflow_model_source(request):
        try:
            values = await _body(request)
            return _response(
                deps.global_chat_agent().select_workflow_model_source(
                    request.match_info["job_id"], values.get("name"), values.get("source")
                )
            )
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.post("/cline/global-chat/workflows/{job_id}/dependencies/models/local")
    async def cline_select_workflow_local_model(request):
        try:
            values = await _body(request)
            return _response(
                deps.global_chat_agent().select_workflow_local_model(
                    request.match_info["job_id"], values.get("name"), values.get("path")
                )
            )
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.post("/cline/global-chat/workflows/{job_id}/dependencies/nodes/confirm")
    async def cline_confirm_workflow_nodes(request):
        try:
            values = await _body(request)
            return _response(
                await deps.global_chat_agent().confirm_node_installation(
                    request.match_info["job_id"],
                    confirmed=bool(values.get("confirmed")),
                    acknowledge_security_for=str(values.get("acknowledge_security_for") or ""),
                )
            )
        except NodeSecurityBlockedError as exc:
            return _response({"error": str(exc), "security": exc.report}, status=409)
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.post("/cline/global-chat/workflows/{job_id}/dependencies/models/confirm")
    async def cline_confirm_workflow_models(request):
        try:
            values = await _body(request)
            return _response(
                await deps.global_chat_agent().confirm_model_download(
                    request.match_info["job_id"],
                    confirmed=bool(values.get("confirmed")),
                    model_names=values.get("model_names"),
                    wait_for_completion=False,
                )
            )
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.get("/cline/global-chat/workflows/{job_id}/dependencies/models/status")
    async def cline_workflow_model_download_status(request):
        try:
            return _response(
                deps.global_chat_agent().model_download_status(
                    request.match_info["job_id"], request.query.get("name", "")
                )
            )
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.post("/cline/global-chat/workflows/{job_id}/dependencies/models/cancel")
    async def cline_cancel_workflow_model_download(request):
        try:
            values = await _body(request)
            return _response(
                deps.global_chat_agent().cancel_model_download(
                    request.match_info["job_id"], values.get("name", "")
                )
            )
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.post("/cline/global-chat/workflows/{job_id}/dependencies/confirm")
    async def cline_confirm_workflow_dependencies(request):
        try:
            values = await _body(request)
            return _response(
                await deps.global_chat_agent().confirm_dependency_plan(
                    request.match_info["job_id"], confirmed=bool(values.get("confirmed"))
                )
            )
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.post("/cline/global-chat/workflows/{job_id}/develop")
    async def cline_create_workflow_development_task(request):
        try:
            values = await _body(request)
            return _response(
                await deps.global_chat_agent().create_development_task(
                    request.match_info["job_id"], confirmed=bool(values.get("confirmed"))
                )
            )
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.post("/cline/global-chat/workflows/{job_id}/execute")
    async def cline_execute_workflow(request):
        def execution_diagnostic(job: dict[str, Any] | None, error: BaseException, code: str) -> dict[str, Any]:
            persisted = (job or {}).get("metadata", {}).get("execution_diagnostic")
            if isinstance(persisted, dict) and persisted:
                return persisted
            return static_diagnostic(
                code=getattr(error, "code", None) or code,
                message=str(error),
                details=getattr(error, "details", None),
                retryable=bool(getattr(error, "retryable", False)),
                prompt_id=str(getattr(error, "prompt_id", "") or ""),
            )

        try:
            values = await _body(request)
            return _response(
                await deps.global_chat_agent().execute_workflow(
                    request.match_info["job_id"],
                    confirmed=bool(values.get("confirmed")),
                    timeout=float(values.get("timeout", 1800)),
                )
            )
        except WorkflowExecutionError as exc:
            agent = deps.global_chat_agent()
            job = agent.workflow_job(request.match_info["job_id"])
            diagnostic = execution_diagnostic(job, exc, "execution_error")
            return _response({"error": str(exc), "workflow": job, "diagnostic": diagnostic}, status=400)
        except (ValueError, OSError) as exc:
            agent = deps.global_chat_agent()
            try:
                job = agent.workflow_job(request.match_info["job_id"])
            except (ValueError, OSError):
                job = None
            if job is None:
                return _response(
                    {
                        "error": str(exc),
                        "diagnostic": execution_diagnostic(None, exc, "preflight_error"),
                    },
                    status=400,
                )
            diagnostic = execution_diagnostic(job, exc, "preflight_error")
            return _response({"error": str(exc), "workflow": job, "diagnostic": diagnostic}, status=400)

    @routes.get("/cline/global-chat/workflows/{job_id}")
    async def cline_workflow_job(request):
        try:
            return _response(deps.global_chat_agent().workflow_job(request.match_info["job_id"]))
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=404)

    @routes.get("/cline/global-chat")
    async def cline_global_chat(request):
        try:
            agent = deps.global_chat_agent()
            return _response(
                {
                    "messages": agent.store.global_messages(),
                    "network_enabled": agent.chat_network_allowed(),
                }
            )
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.post("/cline/global-chat/messages")
    async def cline_global_chat_message(request):
        try:
            values = await _body(request)
            return _response(
                await deps.global_chat_agent().chat(
                    values.get("content", ""), values.get("skill_ids", []), str(values.get("context") or "")
                )
            )
        except (ValueError, RuntimeError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.delete("/cline/global-chat/history")
    async def cline_clear_global_chat(request):
        try:
            deps.global_chat_agent().store.clear_global_messages()
            return _response({"messages": []})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

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
            for project in projects[:MAX_NODE_BATCH_SCANS]:
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

    @routes.get("/cline/skills")
    async def cline_skills(request):
        return _response({"skills": deps.task_agent().skills.list()})

    async def _skill_gate(values: dict[str, Any], acknowledged: bool, *, source: str = ""):
        """Review with the configured engine and return ``(report, error_response)``."""
        report = await skill_review.review(values, settings=deps.task_agent().settings, source=source)
        if report["verdict"] == "blocked" and not acknowledged:
            return report, _response(
                {"error": "安全检测阻止保存：" + skill_security.summarize(report), "security": report},
                status=409,
            )
        if acknowledged:
            report["acknowledged"] = True
        return report, None

    @routes.post("/cline/skills/scan")
    async def cline_scan_skill(request):
        try:
            values = await _body(request)
            report = await skill_review.review(values, settings=deps.task_agent().settings)
            return _response({"security": report})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.post("/cline/skills")
    async def cline_create_skill(request):
        try:
            values = await _body(request)
            report, blocked = await _skill_gate(values, bool(values.pop("acknowledge_security", False)))
            if blocked is not None:
                return blocked
            return _response({"skill": deps.task_agent().skills.create(values, security=report)})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.patch("/cline/skills/{skill_id}")
    async def cline_update_skill(request):
        try:
            skill_id = request.match_info["skill_id"]
            values = await _body(request)
            store = deps.task_agent().skills
            if any(key in values for key in ("name", "description", "content", "prompt")):
                current = store.get(skill_id)
                if current is None:
                    return _response({"error": "Skill 不存在。"}, status=404)
                report, blocked = await _skill_gate(
                    {**current, **values},
                    bool(values.pop("acknowledge_security", False)),
                    source=str(current.get("security", {}).get("source") or ""),
                )
                if blocked is not None:
                    return blocked
                return _response({"skill": store.update(skill_id, values, security=report)})
            return _response({"skill": store.update(skill_id, values)})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.post("/cline/skills/{skill_id}/rescan")
    async def cline_rescan_skill(request):
        try:
            agent = deps.task_agent()
            store = agent.skills
            skill_id = request.match_info["skill_id"]
            skill = store.get(skill_id)
            if skill is None:
                return _response({"error": "Skill 不存在。"}, status=404)
            report = await skill_review.review(
                skill,
                settings=agent.settings,
                source=str(skill.get("security", {}).get("source") or ""),
            )
            return _response({"skill": store.rescan(skill_id, report)})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.post("/cline/skills/rescan")
    async def cline_rescan_skills(request):
        try:
            agent = deps.task_agent()
            store = agent.skills
            reports = []
            for skill in store.list():
                report = await skill_review.review(
                    skill,
                    settings=agent.settings,
                    source=str(skill.get("security", {}).get("source") or ""),
                )
                reports.append({"id": skill["id"], "security": report})
            return _response({"skills": store.rescan_all(reports)})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.delete("/cline/skills/{skill_id}")
    async def cline_delete_skill(request):
        try:
            return _response({"skill": deps.task_agent().remove_skill(request.match_info["skill_id"])})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=404)

    @routes.post("/cline/skills/import")
    async def cline_import_skills(request):
        try:
            values = await _body(request)
            imported = deps.task_agent().skills.import_items(
                values.get("skills", values),
                acknowledged=bool(values.get("acknowledge_security")),
            )
            return _response({"skills": deps.task_agent().skills.list(), "imported": imported})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.get("/cline/skills/export")
    async def cline_export_skills(request):
        return _response({"version": 1, "skills": deps.task_agent().skills.export_items()})

    @routes.get("/cline/mcp/tools")
    async def cline_mcp_tools(request):
        try:
            return _response({"groups": await deps.task_agent().mcp.list_tools()})
        except (ValueError, RuntimeError, OSError, MCPProtocolError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.get("/cline/settings")
    async def cline_settings(request):
        return _response(deps.legacy_agent().settings.public())

    @routes.post("/cline/settings")
    async def cline_update_settings(request):
        try:
            values = await _body(request)
            return _response(deps.legacy_agent().settings.update(values))
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.get("/cline/files")
    async def cline_files(request):
        try:
            path = request.query.get("path", ".")
            recursive = request.query.get("recursive", "false").lower() == "true"
            workspace = _composer_workspace(request)
            return _response(
                {
                    "workspace": str(workspace.root),
                    "path": path,
                    "entries": workspace.list_files(path, recursive),
                }
            )
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.get("/cline/file")
    async def cline_file(request):
        try:
            path = request.query.get("path", "")
            workspace = _composer_workspace(request)
            return _response({"path": path, "content": workspace.read_file(path)})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.post("/cline/chat")
    async def cline_chat(request):
        try:
            values = await _body(request)
            result = await deps.legacy_agent().chat(values.get("messages", []), str(values.get("context") or ""))
            return _response(result)
        except (ValueError, RuntimeError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.post("/cline/confirm")
    async def cline_confirm(request):
        try:
            values = await _body(request)
            action_id = str(values.get("id") or "")
            if not action_id:
                raise ValueError("Confirmation id is required.")
            result = await deps.legacy_agent().confirm(action_id, bool(values.get("approved")))
            return _response(result)
        except (ValueError, RuntimeError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)

    @routes.post("/cline/approvals/{approval_id}")
    async def cline_task_confirm(request):
        try:
            values = await _body(request)
            result = await deps.task_agent().confirm(request.match_info["approval_id"], bool(values.get("approved")))
            return _response(result)
        except (ValueError, RuntimeError, OSError, MCPProtocolError, BrowserUnavailable) as exc:
            return _response({"error": str(exc)}, status=400)
