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
from .route_common import FEATURES, HEALTH_VERSION, _body, _composer_workspace, _response

def register_workflows_routes(routes: Any) -> None:
    from aiohttp import web

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
            # dependency_plan performs blocking local HTTP/file operations while
            # consulting ComfyUI and ComfyUI-Manager.  Running it directly in
            # this aiohttp handler can deadlock when Manager is served by the
            # same ComfyUI event loop: the handler waits for Manager while the
            # loop is unable to process Manager's request.
            plan = await asyncio.to_thread(
                deps.global_chat_agent().dependency_plan,
                request.match_info["job_id"],
            )
            return _response(plan)
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
                    request.match_info["job_id"],
                    values.get("class_type"),
                    values.get("source"),
                    manager_id=values.get("manager_id"),
                    aux_id=values.get("aux_id"),
                    cnr_id=values.get("cnr_id"),
                    install_id=values.get("install_id"),
                    version=values.get("version"),
                    install_type=values.get("install_type"),
                    reference=values.get("reference"),
                    pip=values.get("pip"),
                    instance_id=values.get("instance_id"),
                    manual=bool(values.get("manual")),
                )
            )
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)
    @routes.post("/cline/global-chat/workflows/{job_id}/dependencies/models/search")
    async def cline_search_workflow_model_sources(request):
        try:
            values = await _body(request)
            return _response(
                await deps.global_chat_agent().search_workflow_model_sources(
                    request.match_info["job_id"], values.get("name")
                )
            )
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)
    @routes.get("/cline/global-chat/workflows/{job_id}/dependencies/models/local")
    async def cline_list_local_workflow_models(request):
        try:
            # The inventory may call ComfyUI's /internal/folder_paths endpoint
            # and scan the local model tree.  This handler itself runs on the
            # same aiohttp loop as that endpoint, so keep all of that blocking
            # work off the loop to avoid a self-request deadlock.
            agent = deps.global_chat_agent()
            inventory = await asyncio.to_thread(
                agent.local_model_inventory,
                request.match_info["job_id"],
            )
            return _response(inventory)
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
            agent = deps.global_chat_agent()
            selected = await asyncio.to_thread(
                agent.select_workflow_local_model,
                request.match_info["job_id"],
                values.get("name"),
                values.get("path"),
            )
            return _response(selected)
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)
    @routes.post("/cline/global-chat/workflows/{job_id}/dependencies/nodes/confirm")
    async def cline_confirm_workflow_nodes(request):
        try:
            values = await _body(request)
            return _response(
                await deps.global_chat_agent().start_node_installation(
                    request.match_info["job_id"],
                    confirmed=bool(values.get("confirmed")),
                    acknowledge_security_for=str(values.get("acknowledge_security_for") or ""),
                )
            )
        except NodeSecurityBlockedError as exc:
            return _response({"error": str(exc), "security": exc.report}, status=409)
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)
    @routes.get("/cline/global-chat/workflows/{job_id}/dependencies/nodes/status")
    async def cline_workflow_node_install_status(request):
        try:
            return _response(deps.global_chat_agent().node_install_status(request.match_info["job_id"]))
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
    @routes.post("/cline/global-chat/workflows/{job_id}/repair")
    async def cline_repair_workflow(request):
        """Execute one explicitly confirmed, server-side repair action."""
        try:
            values = await _body(request)
            return _response(
                await deps.global_chat_agent().repair_workflow(
                    request.match_info["job_id"],
                    action_id=values.get("action_id"),
                    confirmed=bool(values.get("confirmed")),
                    timeout=float(values.get("timeout", 1800)),
                )
            )
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)
    @routes.post("/cline/global-chat/workflows/{job_id}/repair-ai")
    async def cline_ai_repair_workflow(request):
        """Hand a persisted workflow failure to the confirmed ACT coding agent."""
        try:
            values = await _body(request)
            return _response(
                await deps.global_chat_agent().create_ai_repair_task(
                    request.match_info["job_id"], confirmed=bool(values.get("confirmed"))
                ),
                status=202,
            )
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)
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
                    values.get("content", ""),
                    values.get("skill_ids", []),
                    str(values.get("context") or ""),
                    str(values.get("workflow_source") or ""),
                )
            )
        except (ValueError, RuntimeError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)
    @routes.delete("/cline/global-chat/history")
    async def cline_clear_global_chat(request):
        try:
            await deps.global_chat_agent().clear_global_history()
            return _response({"messages": []})
        except (ValueError, OSError) as exc:
            return _response({"error": str(exc)}, status=400)
