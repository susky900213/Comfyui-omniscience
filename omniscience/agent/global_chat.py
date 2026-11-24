"""Task-independent global conversation runtime (DESIGN.md section 6).

The global conversation has no task id, workspace, task metadata, or task
status. Its only tool installs text-only Skill packages from public GitHub
repositories; package scripts and other executable files are never run.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from .. import paths
from ..constants import MAX_GLOBAL_CHAT_STEPS, MAX_MESSAGE_CHARS
from ..context import ContextManager
from ..host import browser_manager
from ..prompts import GLOBAL_CHAT_SYSTEM_PROMPT, skill_prompt
from ..settings import SettingsStore
from ..skills import SkillStore
from ..storage import TaskStore
from ..util import json_text
from .github_skill import GitHubSkillInstaller
from .global_tools import GlobalChatToolMixin
from .legacy import ClineAgent
from .node_install import NodeInstallerMixin
from ..workflow_manager import WorkflowManagerMixin

logger = logging.getLogger("Comfyui-omniscience")


class GlobalChatAgent(GlobalChatToolMixin, GitHubSkillInstaller, NodeInstallerMixin, WorkflowManagerMixin):
    def __init__(self, settings: SettingsStore):
        self.settings = settings
        self.store = TaskStore(paths.TASK_DATABASE)
        self.skills = SkillStore(paths.SKILL_DATABASE)
        self.model = ClineAgent(settings)
        self.browser = browser_manager(self.settings.load)
        self._lock = asyncio.Lock()
        self._model_download_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._model_download_locks: dict[str, asyncio.Lock] = {}
        self._model_download_progress: dict[tuple[str, str], dict[str, Any]] = {}
        self._model_download_cancel_events: dict[tuple[str, str], Any] = {}
        self._node_install_tasks: dict[str, asyncio.Task] = {}

    @staticmethod
    def _region_from_text(text: str) -> str | None:
        if re.search(r"国内|中国站|国内站", str(text or "")):
            return "domestic"
        if re.search(r"国外|国际站|海外", str(text or "")):
            return "international"
        return None

    def _workflow_region(self, text: str) -> str | None:
        current = self._region_from_text(text)
        if current:
            return current
        for message in reversed(self.store.global_messages()):
            if message.get("role") != "user":
                continue
            previous = self._region_from_text(str(message.get("content") or ""))
            if previous:
                return previous
        return None

    async def clear_global_history(self) -> None:
        """Clear the global transcript and all state used to restore its cards.

        Model downloads are cooperative and may already have created partial
        files.  Signal and cancel their in-process tasks so they no longer
        update a workflow job that is about to be removed; this does not remove
        downloaded files or installed nodes.
        """
        async with self._lock:
            for cancel_event in self._model_download_cancel_events.values():
                cancel_event.set()
            for task in self._model_download_tasks.values():
                if not task.done():
                    task.cancel()
            for task in self._node_install_tasks.values():
                if not task.done():
                    task.cancel()
            self._model_download_tasks.clear()
            self._node_install_tasks.clear()
            self._model_download_locks.clear()
            self._model_download_progress.clear()
            self._model_download_cancel_events.clear()
            self.store.clear_global_messages()

    @staticmethod
    def _tool_catalog(tools: list[dict[str, Any]]) -> str:
        lines = ["\n\n## 本轮可用工具说明", "只调用与用户请求直接相关的工具；工具返回结果是真实执行结果，不要凭空声称成功。"]
        for tool in tools:
            function = tool.get("function") or {}
            name = str(function.get("name") or "unknown")
            description = str(function.get("description") or "未提供说明").strip()
            parameters = function.get("parameters") or {}
            properties = parameters.get("properties") if isinstance(parameters, dict) else {}
            arguments = ", ".join(str(key) for key in properties or {}) or "无参数"
            lines.append(f"- {name}（参数：{arguments}）：{description}")
        return "\n".join(lines)

    @staticmethod
    def _model_messages(messages: list[dict[str, Any]], context: str = "") -> list[dict[str, Any]]:
        prepared = [
            {key: value for key, value in message.items() if key not in {"seq", "created_at", "skill_ids"}}
            for message in messages
            if message.get("role") in {"user", "assistant", "tool"}
        ]
        if context:
            for index in range(len(prepared) - 1, -1, -1):
                if prepared[index].get("role") != "user":
                    continue
                prepared[index] = {**prepared[index], "content": f"{prepared[index].get('content') or ''}\n\n{context}"}
                break
        return prepared

    def _validated_skill_ids(self, skill_ids: Any) -> list[str]:
        if not isinstance(skill_ids, list):
            raise ValueError("skill_ids 必须是数组。")
        available = {skill["id"]: skill for skill in self.skills.list()}
        selected: list[str] = []
        for value in skill_ids:
            skill_id = str(value or "").strip()
            if not skill_id or skill_id in selected:
                continue
            skill = available.get(skill_id)
            if skill is None:
                raise ValueError("选择的 Skill 不存在。")
            if not skill.get("enabled"):
                raise ValueError("不能选择已停用的 Skill。")
            selected.append(skill_id)
        return selected

    def selected_skills(self, skill_ids: Any) -> list[dict[str, Any]]:
        available = {skill["id"]: skill for skill in self.skills.list()}
        return [available[skill_id] for skill_id in self._validated_skill_ids(skill_ids) if skill_id in available]

    async def chat(
        self,
        content: str,
        skill_ids: Any = None,
        context: str = "",
        workflow_source: str = "",
    ) -> dict[str, Any]:
        text = str(content or "").strip()
        if not text:
            raise ValueError("请输入对话内容。")
        if len(text) > MAX_MESSAGE_CHARS:
            raise ValueError("单条对话消息过大。")
        extra_context = str(context or "").strip()[: 200_000]
        selected_skill_ids = self._validated_skill_ids([] if skill_ids is None else skill_ids)
        async with self._lock:
            self.store.append_global_message({"role": "user", "content": text, "skill_ids": selected_skill_ids})
            system_prompt = (
                GLOBAL_CHAT_SYSTEM_PROMPT + self.chat_prompt_note() + skill_prompt(self.selected_skills(selected_skill_ids))
            )
            node_install_requested = self._explicit_node_install_request(text)
            workflow_install_requested = self.workflow_install_requested(text)
            effective_workflow_source = str(workflow_source or "").strip().replace("\\", "/")
            if effective_workflow_source:
                workflow_install_requested = True
            if workflow_install_requested and not effective_workflow_source:
                uploaded_match = re.search(r"(?:^|\s)@?(uploads/[^\s@]+\.json)(?=$|\s)", text, re.IGNORECASE)
                effective_workflow_source = uploaded_match.group(1).replace("\\", "/") if uploaded_match else ""
            if workflow_install_requested and not effective_workflow_source:
                effective_workflow_source = self.default_uploaded_workflow_source()
            workflow_requested = self.workflow_search_requested(text) and not workflow_install_requested
            # A node request must not also offer the Skill installer: the same
            # "install <github link>" phrasing matches both, and the wrong tool
            # would write to the other library.
            install_requested = self._explicit_install_request(text) and not node_install_requested
            tools = self.chat_tools(
                install_requested,
                node_install_requested,
                workflow_requested,
                workflow_install_requested,
            )
            system_prompt += self._tool_catalog(tools)
            tool_names = [tool.get("function", {}).get("name") for tool in tools]
            search_injected = "search_runninghub_workflows" in tool_names
            logger.info(
                "[global-chat-tools] workflow_requested=%s search_runninghub_workflows_injected=%s tools=%s",
                workflow_requested,
                search_injected,
                tool_names,
            )
            print(
                f"[omniscience] global-chat tools: workflow_requested={workflow_requested} "
                f"workflow_install_requested={workflow_install_requested} "
                f"search_runninghub_workflows_injected={search_injected} tools={tool_names}",
                flush=True,
            )
            # "required" only forces the very first call; once a tool result
            # is in the transcript the model must answer with text, otherwise
            # providers keep re-invoking the tool until the step limit.
            force_tool = install_requested or node_install_requested or workflow_requested or workflow_install_requested
            for _ in range(MAX_GLOBAL_CHAT_STEPS):
                model_messages = self._model_messages(self.store.global_messages(), extra_context)
                reserved = ContextManager.estimate_tokens(system_prompt) + ContextManager.estimate_tokens(
                    json_text(tools)
                )
                model_messages, _ = self.model.context.fallback(model_messages, reserved)
                response = await self.model._call_model(
                    model_messages,
                    tools=tools,
                    tool_choice="required" if force_tool else None,
                    system_prompt=system_prompt,
                )
                choice = (response.get("choices") or [{}])[0]
                assistant = choice.get("message") or {}
                calls = assistant.get("tool_calls") or []
                if not calls and force_tool and workflow_requested:
                    try:
                        selected_job = self.store.latest_selected_workflow_job()
                        inspect_request = bool(re.search(r"获取|详情|文件|依赖|下载|选定|已选择|拉取", text))
                        if selected_job and inspect_request:
                            result = await self.inspect_latest_selected_workflow()
                        else:
                            result = await self.search_runninghub_workflows(
                                text, region=self._workflow_region(text)
                            )
                    except Exception as exc:
                        result = {"error": str(exc)}
                    self.store.append_global_message({
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": "workflow-search-fallback",
                            "type": "function",
                            "function": {"name": "search_runninghub_workflows", "arguments": "{}"},
                        }],
                    })
                    self.store.append_global_message(
                        {"role": "tool", "tool_call_id": "workflow-search-fallback", "content": json_text(result)}
                    )
                    force_tool = False
                    continue
                if not calls and force_tool and workflow_install_requested:
                    try:
                        result = self.install_uploaded_workflow(effective_workflow_source)
                    except Exception as exc:
                        result = {"error": str(exc)}
                    self.store.append_global_message({
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": "workflow-install-fallback",
                            "type": "function",
                            "function": {"name": "install_uploaded_workflow", "arguments": "{}"},
                        }],
                    })
                    self.store.append_global_message(
                        {"role": "tool", "tool_call_id": "workflow-install-fallback", "content": json_text(result)}
                    )
                    force_tool = False
                    continue
                if not calls:
                    assistant_message = {
                        "role": "assistant",
                        "content": str(assistant.get("content") or "模型没有返回文本。"),
                    }
                    self.store.append_global_message(assistant_message)
                    return {"status": "ok", "message": assistant_message, "messages": self.store.global_messages()}

                self.store.append_global_message(
                    {"role": "assistant", "content": assistant.get("content"), "tool_calls": calls}
                )
                for index, call in enumerate(calls):
                    call_id = str(call.get("id") or f"global-skill-{index}")
                    try:
                        tool_name, arguments = self.model._parse_tool_call(call)
                        if tool_name == "install_github_skill":
                            if not install_requested:
                                raise ValueError("只有在用户明确请求安装 Skill 时才允许调用此工具。")
                            source = arguments.get("github_url")
                            if not self._install_source_allowed(text, source):
                                raise ValueError("工具参数必须对应用户明确提供的公开 GitHub Skill 来源。")
                            result = await self.install_github_skill(source)
                        elif tool_name == "install_comfyui_node":
                            if not node_install_requested:
                                raise ValueError("只有在用户明确请求安装节点时才允许调用此工具。")
                            source = arguments.get("source")
                            if not self._install_node_source_allowed(text, source):
                                raise ValueError("工具参数必须对应用户明确给出的 GitHub/GitCode 地址或已上传的节点压缩包。")
                            result = await self.install_comfyui_node(source)
                        elif tool_name == "search_runninghub_workflows":
                            if not workflow_requested:
                                raise ValueError("只有在用户明确请求查找工作流时才允许调用此工具。")
                            result = await self.search_runninghub_workflows(
                                arguments.get("query") or text,
                                limit=arguments.get("limit", 10),
                                filters=arguments.get("filters"),
                                region=self._workflow_region(text),
                            )
                        elif tool_name == "inspect_runninghub_workflow":
                            if not workflow_requested:
                                raise ValueError("只有在工作流搜索上下文中才允许检查候选工作流。")
                            job_id = str(arguments.get("job_id") or "").strip()
                            candidate_id = str(arguments.get("candidate_id") or "").strip()
                            job = self.store.get_workflow_job(job_id)
                            selected = self.store.workflow_candidate(job_id, candidate_id)
                            if selected is None or not job or job.get("selected_candidate_id") != candidate_id:
                                raise ValueError("候选工作流不存在或尚未被用户选择。")
                            result = await self.inspect_selected_workflow(job_id)
                        elif tool_name == "install_uploaded_workflow":
                            if not workflow_install_requested:
                                raise ValueError("只有在用户明确请求安装工作流时才允许调用此工具。")
                            source = str(arguments.get("source") or effective_workflow_source).strip().replace("\\", "/")
                            if not effective_workflow_source or source != effective_workflow_source:
                                raise ValueError("工具参数必须对应本轮待安装的上传工作流文件。")
                            result = self.install_uploaded_workflow(source)
                        else:
                            result = await self.execute_chat_tool(tool_name, arguments)
                    except Exception as exc:
                        logger.warning("Global chat tool failed: %s", exc)
                        result = {"error": str(exc)}
                    self.store.append_global_message(
                        {"role": "tool", "tool_call_id": call_id, "content": json_text(result)}
                    )
                force_tool = False
            raise RuntimeError("全局对话的工具调用超过步骤限制。")
