"""Legacy compatibility adapter for the original stateless chat endpoint.

``ClineAgent`` keeps the pre-task runtime working for direct API callers:
in-memory transcripts, no persistence, and the original tool set. The real
application boundary is :class:`omniscience.agent.runtime.PersistentClineAgent`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .. import paths
from ..constants import (
    DEFAULT_COMPACTION_THRESHOLD_PERCENT,
    DEFAULT_MAX_CONTEXT_TOKENS,
    MAX_AGENT_STEPS,
    MAX_COMMAND_OUTPUT,
    MAX_MESSAGE_CHARS,
)
from ..context import ContextManager
from ..model_client import complete
from ..prompts import COMPACTION_SYSTEM_PROMPT, SYSTEM_PROMPT
from ..settings import SettingsStore
from ..tools.defs import TOOL_DEFINITIONS
from ..tools.guards import BLOCKED_COMMANDS
from ..util import (
    compaction_threshold_percent,
    default_workspace,
    json_text,
    max_context_tokens,
)
from ..workspace import Workspace

logger = logging.getLogger("Comfyui-omniscience")


@dataclass
class PendingAction:
    messages: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    action_index: int
    name: str
    arguments: dict[str, Any]


class ClineAgent:
    def __init__(self, settings: SettingsStore):
        self.settings = settings
        self.pending: dict[str, PendingAction] = {}
        settings_data = self.settings.load()
        configured = settings_data.get("max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS)
        threshold = settings_data.get("compaction_threshold_percent", DEFAULT_COMPACTION_THRESHOLD_PERCENT)
        try:
            configured = max_context_tokens(configured)
        except ValueError:
            configured = DEFAULT_MAX_CONTEXT_TOKENS
        try:
            threshold = compaction_threshold_percent(threshold)
        except ValueError:
            threshold = DEFAULT_COMPACTION_THRESHOLD_PERCENT
        self.context = ContextManager(configured, compaction_threshold=threshold / 100)

    def workspace(self) -> Workspace:
        configured = self.settings.load().get("workspace") or str(default_workspace())
        return Workspace(configured)

    async def chat(self, messages: list[dict[str, Any]], context: str = "") -> dict[str, Any]:
        clean_messages = self._clean_messages(messages)
        if context:
            clean_messages.append(
                {
                    "role": "user",
                    "content": f"Additional workspace context:\n{context}",
                }
            )
        return await self._run_loop(clean_messages)

    async def confirm(self, action_id: str, approved: bool) -> dict[str, Any]:
        action = self.pending.pop(action_id, None)
        if action is None:
            raise ValueError("This confirmation has expired. Please send the request again.")
        if approved:
            result = self._execute_tool(action.name, action.arguments)
        else:
            result = "User denied this action. Do not retry it unless the user asks again."
        tool_call = action.tool_calls[action.action_index]
        action.messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.get("id", "omniscience-tool"),
                "content": json_text(result),
            }
        )
        return await self._run_loop(action.messages, action.tool_calls, action.action_index + 1)

    async def _run_loop(
        self,
        messages: list[dict[str, Any]],
        tool_calls: list[dict[str, Any]] | None = None,
        tool_index: int = 0,
    ) -> dict[str, Any]:
        for _ in range(MAX_AGENT_STEPS):
            if tool_calls is not None:
                for index in range(tool_index, len(tool_calls)):
                    name, arguments = self._parse_tool_call(tool_calls[index])
                    result = self._execute_tool(name, arguments)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_calls[index].get("id", f"omniscience-tool-{index}"),
                            "content": json_text(result),
                        }
                    )
                tool_calls = None
                tool_index = 0

            messages, _ = await self._compact_messages(messages, TOOL_DEFINITIONS)
            response = await self._call_model(messages, tools=TOOL_DEFINITIONS)
            choice = (response.get("choices") or [{}])[0]
            assistant = choice.get("message") or {}
            calls = assistant.get("tool_calls") or []
            if not calls:
                return {
                    "status": "ok",
                    "message": {
                        "role": "assistant",
                        "content": assistant.get("content") or "模型没有返回文本。",
                    },
                }
            messages.append(
                {
                    "role": "assistant",
                    "content": assistant.get("content"),
                    "tool_calls": calls,
                }
            )
            tool_calls = calls
            tool_index = 0
        raise RuntimeError("The assistant reached the tool-call step limit. Please continue the conversation.")

    async def _compact_messages(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], bool]:
        try:
            configured = max_context_tokens(
                self.settings.load().get("max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS)
            )
            threshold = compaction_threshold_percent(
                self.settings.load().get("compaction_threshold_percent", DEFAULT_COMPACTION_THRESHOLD_PERCENT)
            )
        except ValueError:
            configured = DEFAULT_MAX_CONTEXT_TOKENS
            threshold = DEFAULT_COMPACTION_THRESHOLD_PERCENT
        if self.context.max_tokens != configured or self.context.compaction_threshold != threshold / 100:
            self.context = ContextManager(
                configured,
                keep_messages=self.context.keep_messages,
                compaction_threshold=threshold / 100,
            )
        reserved = ContextManager.estimate_tokens(SYSTEM_PROMPT) + ContextManager.estimate_tokens(json_text(tools))
        if not self.context.needs_compaction(messages, reserved):
            return messages, False
        summary_input = self.context.bounded_summary_messages(messages)
        if not summary_input:
            return self.context.fallback(messages, reserved)
        summary_input.append(
            {
                "role": "user",
                "content": "Summarize the preceding conversation now. Preserve concrete task state and unresolved work.",
            }
        )
        try:
            response = await self._call_model(
                summary_input,
                tools=[],
                system_prompt=COMPACTION_SYSTEM_PROMPT,
                max_tokens=2048,
            )
            summary = ((response.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            if not str(summary).strip():
                raise RuntimeError("模型摘要响应为空。")
            compacted, changed = self.context.with_summary(messages, summary, reserved)
            if changed:
                return compacted, True
            return messages, False
        except Exception as exc:
            # The persisted/original transcript remains untouched. The local
            # fallback keeps the request usable while making the provider error
            # visible in the server log.
            logger.warning("Context summary request failed; using local fallback: %s", exc)
            return self.context.fallback(messages, reserved)

    async def _call_model(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        system_prompt: str = SYSTEM_PROMPT,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        data = self.settings.load()
        return await complete(
            model=str(data.get("model") or paths.DEFAULT_MODEL),
            api_base_url=str(data.get("api_base_url") or paths.DEFAULT_API_BASE_URL),
            api_key=self.settings.api_key(),
            messages=[{"role": "system", "content": system_prompt}, *messages],
            tools=tools,
            tool_choice=tool_choice,
            max_tokens=max_tokens,
        )

    @staticmethod
    def _urlopen_json(request: Request) -> dict[str, Any]:
        with urlopen(request, timeout=120) as response:  # noqa: S310 - configured local/provider URL
            return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _clean_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(messages, list) or not messages:
            raise ValueError("At least one user message is required.")
        cleaned: list[dict[str, Any]] = []
        for message in messages[-40:]:
            if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
                continue
            content = str(message.get("content") or "")
            # Do not reject a long transcript here: the model-input compactor
            # must see the old turns before deciding what can be summarized.
            # Keep the per-message safety bound for an accidentally huge input.
            if len(content) > MAX_MESSAGE_CHARS:
                raise ValueError("A single conversation message is too large.")
            cleaned.append({"role": message["role"], "content": content})
        if not cleaned or cleaned[-1]["role"] != "user":
            raise ValueError("The last conversation message must be from the user.")
        return cleaned

    @staticmethod
    def _parse_tool_call(tool_call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        function = tool_call.get("function") or {}
        name = str(function.get("name") or "")
        raw_arguments = function.get("arguments") or "{}"
        try:
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
        except json.JSONDecodeError as exc:
            raise ValueError(f"模型返回了无效的工具参数: {raw_arguments}") from exc
        if not name or not isinstance(arguments, dict):
            raise ValueError("模型返回了无效的工具调用。")
        return name, arguments

    @staticmethod
    def _requires_confirmation(name: str) -> bool:
        """Compatibility hook retained for callers of the original endpoint.

        Tool calls are intentionally executed directly. Safety is provided by
        the workspace boundary, blocked-command policy, and task cancellation,
        not by a confirmation prompt for every action.
        """
        return False

    def _confirmation(self, action_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "run_command":
            description = f"在工作区执行命令：{arguments.get('command', '')}"
        elif name == "apply_patch":
            description = f"修改文件：{arguments.get('path', '')}（应用补丁）"
        else:
            description = f"写入文件：{arguments.get('path', '')}"
        return {
            "id": action_id,
            "tool": name,
            "description": description,
            "arguments": {
                key: (value[:2_000] + "...[truncated]" if isinstance(value, str) and len(value) > 2_000 else value)
                for key, value in arguments.items()
            },
        }

    def _execute_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        workspace = self.workspace()
        if name == "list_files":
            return workspace.list_files(arguments.get("path", "."), bool(arguments.get("recursive", False)))
        if name == "read_file":
            return {"path": arguments.get("path"), "content": workspace.read_file(arguments["path"])}
        if name == "search_files":
            return workspace.search_files(arguments["query"], arguments.get("path", "."))
        if name == "write_file":
            return {"written": workspace.write_file(arguments["path"], arguments.get("content", ""))}
        if name == "apply_patch":
            return {"patched": workspace.apply_patch(arguments["path"], arguments["patch"])}
        if name == "run_command":
            return self._run_command(str(arguments.get("command") or ""), arguments.get("timeout", 120), workspace)
        raise ValueError(f"Unknown assistant tool: {name}")

    @staticmethod
    def _run_command(command: str, timeout: Any, workspace: Workspace) -> dict[str, Any]:
        if not command or len(command) > 2_000:
            raise ValueError("Command must be between 1 and 2,000 characters.")
        if "\x00" in command or BLOCKED_COMMANDS.search(command):
            raise ValueError("This command is blocked by the local safety policy.")
        try:
            timeout_seconds = min(120, max(1, int(timeout)))
        except (TypeError, ValueError):
            timeout_seconds = 120
        try:
            completed = subprocess.run(
                command,
                cwd=workspace.root,
                shell=True,  # noqa: S602 - command execution is bounded by local policy
                executable=os.environ.get("SHELL", "/bin/sh"),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            output = (completed.stdout + completed.stderr)[-MAX_COMMAND_OUTPUT:]
            return {"exit_code": completed.returncode, "output": output}
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode(errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode(errors="replace")
            return {"exit_code": None, "timed_out": True, "output": (stdout + stderr)[-MAX_COMMAND_OUTPUT:]}
