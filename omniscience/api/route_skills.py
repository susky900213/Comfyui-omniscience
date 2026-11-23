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

def register_skills_routes(routes: Any) -> None:
    from aiohttp import web

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
