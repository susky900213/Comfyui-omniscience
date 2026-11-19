"""Task-independent global conversation runtime (DESIGN.md section 6).

The global conversation has no task id, workspace, task metadata, or task
status. Its only tool installs text-only Skill packages from public GitHub
repositories; package scripts and other executable files are never run.
"""

from __future__ import annotations

import asyncio
import logging
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

logger = logging.getLogger("Comfyui-omniscience")


class GlobalChatAgent(GlobalChatToolMixin, GitHubSkillInstaller, NodeInstallerMixin):
    def __init__(self, settings: SettingsStore):
        self.settings = settings
        self.store = TaskStore(paths.TASK_DATABASE)
        self.skills = SkillStore(paths.SKILL_DATABASE)
        self.model = ClineAgent(settings)
        self.browser = browser_manager(self.settings.load)
        self._lock = asyncio.Lock()

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

    async def chat(self, content: str, skill_ids: Any = None, context: str = "") -> dict[str, Any]:
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
            # A node request must not also offer the Skill installer: the same
            # "install <github link>" phrasing matches both, and the wrong tool
            # would write to the other library.
            install_requested = self._explicit_install_request(text) and not node_install_requested
            tools = self.chat_tools(install_requested, node_install_requested)
            # "required" only forces the very first call; once a tool result
            # is in the transcript the model must answer with text, otherwise
            # providers keep re-invoking the tool until the step limit.
            force_tool = install_requested or node_install_requested
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
                                raise ValueError("工具参数必须对应用户明确给出的 GitHub 地址或已上传的节点压缩包。")
                            result = await self.install_comfyui_node(source)
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
