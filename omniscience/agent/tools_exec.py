"""Tool execution surface for the persistent task runtime (DESIGN 5.1-5.6).

Exposes the mode-filtered tool set, dispatches one tool call, runs shell
commands through the cancellable :class:`CommandRunner`, and records
checkpoints after state-changing tools.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ..checkpointing import CheckpointManager
from ..tools import guards
from ..tools.commands import CommandRunner
from ..tools.defs import definitions_for_mode
from ..workspace import Workspace

# Read-only tools never modify the project, so they skip checkpoint creation.
CHECKPOINT_EXEMPT_TOOLS = {"list_files", "read_file", "search_files", "browser_content"}

NETWORK_DENIED = "该任务未开启网络访问。请让用户点击输入区下方的“网络访问”按钮开启后再调用此工具。"


class TaskToolExecutionMixin:
    checkpoints: CheckpointManager

    def _network_allowed(self, task_id: str) -> bool:
        task = self.store.get_task(task_id) or {}
        return bool((task.get("metadata") or {}).get("network_enabled"))

    async def _tool_definitions(self, mode: str) -> list[dict[str, Any]]:
        return definitions_for_mode(mode, await self.mcp.list_tools())

    async def _execute_tool(self, task_id: str, name: str, arguments: dict[str, Any]) -> Any:
        if name == "get_current_workflow":
            current = self.store.current_workflow(task_id)
            if current is None:
                return {
                    "available": False,
                    "message": "当前 ComfyUI 画布尚未同步到 Omniscience。请用户在 ComfyUI 中打开 Omniscience 后重试，或点击“读取当前画布”。",
                }
            return {"available": True, **current}
        if name.startswith("browser_") and not self._network_allowed(task_id):
            return {"error": NETWORK_DENIED}
        workspace = self.workspace(task_id)
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
        if name == "editor":
            return {
                "edited": workspace.apply_editor_edit(
                    arguments["path"],
                    old_text=arguments.get("old_text"),
                    new_text=arguments.get("new_text"),
                    insert_line=arguments.get("insert_line"),
                )
            }
        if name == "run_command":
            command = str(arguments.get("command") or "")
            if guards.network_accessed(command) and not self._network_allowed(task_id):
                return {"error": NETWORK_DENIED}
            return await self._run_command(
                task_id, command, arguments.get("timeout", 120), workspace
            )
        if name == "browser_navigate":
            return await self.browser.navigate(task_id, str(arguments["url"]))
        if name == "browser_content":
            return await self.browser.content(task_id, str(arguments.get("selector") or ""))
        if name == "browser_screenshot":
            destination = workspace.resolve(str(arguments.get("path") or f".omniscience-browser/{task_id}.png"))
            return await self.browser.screenshot(task_id, destination)
        if name == "browser_click":
            return await self.browser.click(task_id, str(arguments["selector"]))
        if name == "browser_fill":
            return await self.browser.fill(task_id, str(arguments["selector"]), str(arguments["value"]))
        if name.startswith("mcp__"):
            return await self.mcp.call_tool(name, arguments)
        raise ValueError(f"Unknown assistant tool: {name}")

    @staticmethod
    def _validate_command(command: str, timeout: Any) -> int:
        """Compatibility validator kept for callers of the original endpoint."""
        if not command or len(command) > 2_000:
            raise ValueError("Command must be between 1 and 2,000 characters.")
        if "\x00" in command:
            raise ValueError("This command is blocked by the local safety policy.")
        try:
            return min(120, max(1, int(timeout)))
        except (TypeError, ValueError):
            return 120

    def _run_command_process(self, task_id: str, command: str, timeout: Any, workspace: Workspace) -> dict[str, Any]:
        self._check_cancelled(task_id)
        return self.commands.run_sync(task_id, command, timeout, workspace.root)

    async def _run_command(self, task_id: str, command: str, timeout: Any, workspace: Workspace) -> dict[str, Any]:
        return await asyncio.to_thread(self._run_command_process, task_id, command, timeout, workspace)

    async def _checkpoint_after_tool(self, task_id: str, tool_name: str) -> None:
        if tool_name in CHECKPOINT_EXEMPT_TOOLS:
            return
        task = self.task(task_id)
        backend, reference, metadata = self.checkpoints.create(task_id, Path(task["workspace"]), f"After {tool_name}")
        messages = self.store.messages(task_id)
        checkpoint = self.store.add_checkpoint(
            task_id,
            f"After {tool_name}",
            reference,
            backend,
            messages[-1].get("seq", 0) if messages else 0,
            metadata,
        )
        self._event(task_id, "checkpoint_created", checkpoint)

    async def restore_checkpoint(
        self,
        checkpoint_id: str,
        restore_task: bool = False,
        restore_files: bool = True,
    ) -> dict[str, Any]:
        checkpoint = self.store.checkpoint(checkpoint_id)
        if not checkpoint:
            raise ValueError("Checkpoint does not exist.")
        task = self.task(checkpoint["task_id"])
        if restore_files:
            self.checkpoints.restore(task["id"], Path(task["workspace"]), checkpoint["backend"], checkpoint["reference"])
        if restore_task:
            self.store.truncate_messages(task["id"], checkpoint["message_seq"])
        self._event(
            task["id"],
            "checkpoint_restored",
            {"checkpoint": checkpoint_id, "restore_files": restore_files, "restore_task": restore_task},
        )
        return {"status": "ok", "task": self.task(task["id"]), "checkpoint": checkpoint}

    def _requires_confirmation(self, mode: str, name: str) -> bool:
        """Return whether a tool should pause for user approval.

        Kept as a compatibility hook for integrations that call this method.
        The current runtime executes every tool exposed for the task mode
        directly; the policy layer and command guard are the safety boundary.
        """
        return False

    def _confirmation(self, action_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "run_command":
            description = f"在工作区执行命令：{arguments.get('command', '')}"
        elif name.startswith("browser_"):
            description = f"使用浏览器操作：{name}"
        elif name.startswith("mcp__"):
            description = f"调用 MCP 工具：{name}"
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
