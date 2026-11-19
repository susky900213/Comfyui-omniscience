"""Persistent task runtime (DESIGN.md sections 4, 5 and 9).

``PersistentClineAgent`` is the application boundary for the ComfyUI panel:
all task input, assistant output, tool results, and checkpoints are
persisted. Tool calls execute directly through the policy layer; approval
records are retained only for old databases and API compatibility.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import Any

from .. import paths
from ..checkpointing import CheckpointManager
from ..constants import (
    DEFAULT_COMPACTION_THRESHOLD_PERCENT,
    DEFAULT_MAX_CONTEXT_TOKENS,
    MAX_AGENT_STEPS,
    MAX_CURRENT_WORKFLOW_BYTES,
    MAX_MESSAGE_CHARS,
)
from ..context import ContextManager
from ..host import browser_manager, mcp_manager
from ..model_client import complete
from ..prompts import (
    SYSTEM_PROMPT,
    COMPACTION_SYSTEM_PROMPT,
    build_system_prompt,
    project_rules,
    skill_prompt,
    wrap_user_input,
    workspace_metadata,
)
from ..settings import SettingsStore
from ..skills import SkillStore
from ..storage import TaskStore
from ..tools import policy, results
from ..tools.commands import CommandRunner
from ..util import (
    TaskCancelled,
    compaction_threshold_percent,
    default_workspace,
    json_text,
    max_context_tokens,
)
from ..workspace import Workspace
from .legacy import ClineAgent
from .node_install import NodeInstallerMixin
from .projects import ProjectProvisioningMixin
from .tools_exec import TaskToolExecutionMixin

logger = logging.getLogger("Comfyui-omniscience")


class PersistentClineAgent(ProjectProvisioningMixin, TaskToolExecutionMixin, NodeInstallerMixin):
    """Task-oriented runtime used by the ComfyUI panel."""

    def __init__(self, settings: SettingsStore):
        self.settings = settings
        self.store = TaskStore(paths.TASK_DATABASE)
        self.skills = SkillStore(paths.SKILL_DATABASE)
        self.checkpoints = CheckpointManager(paths.STATE_DIRECTORY)
        settings_data = self.settings.load()
        try:
            configured_max = max_context_tokens(
                settings_data.get("max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS)
            )
        except ValueError:
            configured_max = DEFAULT_MAX_CONTEXT_TOKENS
        try:
            configured_threshold = compaction_threshold_percent(
                settings_data.get("compaction_threshold_percent", DEFAULT_COMPACTION_THRESHOLD_PERCENT)
            )
        except ValueError:
            configured_threshold = DEFAULT_COMPACTION_THRESHOLD_PERCENT
        self.context = ContextManager(
            configured_max, compaction_threshold=configured_threshold / 100
        )
        self.mcp = mcp_manager(paths.MCP_SETTINGS_PATH)
        self.browser = browser_manager(self.settings.load)
        self.commands = CommandRunner(is_cancelled=lambda task_id: task_id in self._cancel_requested)
        self._lock = asyncio.Lock()
        self._task_locks: dict[str, asyncio.Lock] = {}
        self._running_tasks: dict[str, asyncio.Task[Any]] = {}
        # Cancellation interrupts the current async run without making the
        # persisted task terminal.  The marker is cleared after that run has
        # fully unwound, immediately before the task can accept the next turn.
        self._cancel_requested: set[str] = set()
        # A compaction is a model call. Keep its result for subsequent tool
        # steps and later user turns instead of summarizing the same prefix on
        # every request. The cache is intentionally process-local; the durable
        # transcript remains the source of truth after a restart.
        self._context_cache: dict[str, dict[str, Any]] = {}
        self._processes: dict[str, Any] = {}
        self._process_lock = threading.RLock()
        # Optional follow-up queue provided by the turn controller. It is
        # drained at every step boundary so messages sent while a turn runs
        # reach the model in the same turn when possible (DESIGN 7).
        self.followup_provider: Any = None
        # Cancel kills the whole process group through the shared registry.
        self.commands.processes = self._processes
        self.commands._lock = self._process_lock

    def workspace(self, task_id: str | None = None) -> Workspace:
        if task_id:
            task = self.task(task_id)
            return Workspace(task["workspace"])
        configured = self.settings.load().get("workspace") or str(default_workspace())
        return Workspace(configured)

    def task(self, task_id: str) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if task is None:
            raise ValueError("Task does not exist.")
        return task

    def delete_task(self, task_id: str) -> dict[str, Any]:
        """Remove task history and owned links without deleting project files."""
        task = self.task(task_id)
        if task_id in self._running_tasks or task.get("status") == "running":
            raise ValueError("任务正在运行，请先停止后再删除。")
        metadata = dict(task.get("metadata") or {})
        link_value = str(metadata.get("comfyui_link") or "")
        if link_value:
            link = Path(link_value).expanduser()
            workspace = Path(str(task.get("workspace") or "")).expanduser()
            try:
                owns_link = link.is_symlink() and link.resolve(strict=False) == workspace.resolve()
            except OSError:
                owns_link = False
            if owns_link:
                link.unlink()
        if not self.store.delete_task(task_id):
            raise ValueError("任务不存在。")
        self._context_cache.pop(task_id, None)
        self._task_locks.pop(task_id, None)
        self._cancel_requested.discard(task_id)
        return {"deleted": True, "task_id": task_id, "project_path": str(task.get("workspace") or "")}

    def _validated_skill_ids(self, skill_ids: Any) -> list[str]:
        """Validate message-selected IDs against the global enabled Skill library."""
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

    def selected_skills(self, task_id: str, skill_ids: Any = None) -> list[dict[str, Any]]:
        """Return globally managed, enabled Skills selected for one message."""
        self.task(task_id)
        if skill_ids is None:
            skill_ids = self._latest_message_skill_ids(task_id)
        available = {skill["id"]: skill for skill in self.skills.list()}
        if not isinstance(skill_ids, list):
            return []
        selected: list[dict[str, Any]] = []
        for value in skill_ids:
            skill_id = str(value or "").strip()
            skill = available.get(skill_id)
            if skill is None:
                continue
            if not skill.get("enabled"):
                continue
            selected.append(skill)
        return selected

    def _latest_message_skill_ids(self, task_id: str) -> list[str]:
        for message in reversed(self.store.messages(task_id)):
            if message.get("role") == "user":
                values = message.get("skill_ids", [])
                return values if isinstance(values, list) else []
        return []

    def update_task_skills(self, task_id: str, skill_ids: Any) -> dict[str, Any]:
        """Compatibility adapter: validate IDs without persisting them on a task."""
        self.task(task_id)
        self._validated_skill_ids(skill_ids)
        return self.task(task_id)

    def remove_skill(self, skill_id: str) -> dict[str, Any]:
        """Delete a Skill from the global library."""
        return self.skills.delete(skill_id)

    def system_prompt(self, task_id: str, mode: str | None = None, skill_ids: Any = None) -> str:
        """Assemble the per-request system prompt (DESIGN Appendix A.5)."""
        task = self.task(task_id)
        task_mode = mode or task["mode"]
        settings_data = self.settings.load()
        comfyui_directory = str(settings_data.get("comfyui_directory") or paths.DEFAULT_COMFYUI_DIRECTORY or "")
        workspace_path = Path(str(task["workspace"]))
        folder_name = (task.get("metadata") or {}).get("project_folder") or workspace_path.name
        network_enabled = bool((task.get("metadata") or {}).get("network_enabled"))
        network_note = (
            "\n\n## 网络访问\n当前任务已开启网络访问：可以调用 browser_* 工具，命令中的 curl/git clone 等联网操作会被允许。"
            if network_enabled
            else "\n\n## 网络访问\n当前任务未开启网络访问：browser_* 工具和联网命令（curl、wget、git clone 等）会被后端拒绝。"
            "需要联网时提醒用户点击输入区下方的“网络访问”按钮。"
        )
        return build_system_prompt(
            task_mode,
            cwd=str(workspace_path),
            project_name=str(folder_name),
            comfyui_directory=comfyui_directory,
            skills=self.selected_skills(task_id, skill_ids),
            rules_text=project_rules(workspace_path),
            metadata_text=workspace_metadata(
                {**task, "folder": folder_name, "mode": task_mode}, comfyui_directory
            ),
            preferred_language=str(settings_data.get("preferred_language") or ""),
        ) + network_note

    def update_current_workflow(self, task_id: str, workflow: Any, source: str = "comfyui_canvas") -> dict[str, Any]:
        """Store a validated snapshot of the workflow currently visible in ComfyUI."""
        self.task(task_id)
        if not isinstance(workflow, dict):
            raise ValueError("当前工作流必须是 JSON 对象。")
        try:
            serialized = json.dumps(workflow, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("当前工作流必须只包含有效的 JSON 值。") from exc
        if len(serialized.encode("utf-8")) > MAX_CURRENT_WORKFLOW_BYTES:
            raise ValueError(f"当前工作流过大，不能超过 {MAX_CURRENT_WORKFLOW_BYTES // 1_000_000} MB。")
        return self.store.set_current_workflow(task_id, workflow, source)

    def _event(self, task_id: str, kind: str, payload: Any) -> dict[str, Any]:
        return self.store.append_event(task_id, kind, payload)

    def _check_cancelled(self, task_id: str) -> None:
        if task_id in self._cancel_requested:
            raise TaskCancelled()

    def _cancelled_result(self, task_id: str) -> dict[str, Any]:
        return {
            "status": "cancelled",
            "message": {"role": "system", "content": "本次运行已被强制取消，可以继续提问。"},
            "task": self.task(task_id),
        }

    def _register_running_task(self, task_id: str) -> None:
        current = asyncio.current_task()
        if current is not None:
            self._running_tasks[task_id] = current

    def _unregister_running_task(self, task_id: str) -> None:
        current = asyncio.current_task()
        if self._running_tasks.get(task_id) is current:
            self._running_tasks.pop(task_id, None)

    def _lock_for(self, task_id: str) -> asyncio.Lock:
        # One turn per task runs at a time; different tasks no longer wait on
        # a single global lock (DESIGN 7: per-task sessions).
        lock = self._task_locks.get(task_id)
        if lock is None:
            lock = self._task_locks.setdefault(task_id, asyncio.Lock())
        return lock

    def _consume_followups(self, task_id: str) -> None:
        if self.followup_provider is None:
            return
        for followup in self.followup_provider(task_id) or []:
            message = {
                "role": "user",
                "content": str(followup.get("content") or "").strip(),
                "skill_ids": list(followup.get("skill_ids") or []),
            }
            if not message["content"]:
                continue
            if followup.get("context"):
                message["content"] += f"\n\n--- Additional workspace context ---\n{followup['context']}"
            self.store.append_message(task_id, message)
            self._event(task_id, "user_message", {"content": message["content"], "skill_ids": message["skill_ids"]})

    def _terminate_process(self, task_id: str) -> None:
        self.commands.terminate(task_id)

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        """Immediately stop the current agent operation without ending the task."""
        task = self.task(task_id)
        if task["status"] not in {"running", "waiting_approval"}:
            return task
        self._cancel_requested.add(task_id)
        cancelled = self.store.cancel_task(task_id)
        self._event(task_id, "task_cancelled", {"reason": "user", "message": "任务已被强制取消。"})
        self._terminate_process(task_id)
        running = self._running_tasks.get(task_id)
        current = asyncio.current_task()
        if running is not None and running is not current and not running.done():
            running.cancel()
        return cancelled

    def clear_history(self, task_id: str) -> dict[str, Any]:
        task = self.task(task_id)
        if task["status"] in {"running", "waiting_approval"} or task_id in self._running_tasks:
            raise ValueError("任务正在运行，请先停止任务后再清理历史对话。")
        cleared = self.store.clear_history(task_id)
        self._context_cache.pop(task_id, None)
        self.pending_cleanup(task_id)
        return cleared

    def pending_cleanup(self, task_id: str) -> None:
        """Remove in-memory approval state if a compatibility caller created it."""
        # Persistent approvals live in SQLite.  This hook also makes cleanup
        # explicit for future in-memory task extensions without exposing the
        # pending map as part of the HTTP API.
        return None

    async def chat(self, task_id: str, content: str, context: str = "", skill_ids: Any = None) -> dict[str, Any]:
        self.task(task_id)
        if not str(content).strip():
            raise ValueError("Message cannot be empty.")
        selected_skill_ids = self._validated_skill_ids(skill_ids if skill_ids is not None else [])
        # The cancel endpoint returns before the cancelled coroutine has
        # necessarily finished unwinding.  Wait for that same task to release
        # its slot, then let this new turn use the same task and workspace.
        current = asyncio.current_task()
        while (running := self._running_tasks.get(task_id)) is not None and running is not current:
            await asyncio.sleep(0)
        self._cancel_requested.discard(task_id)
        pending = self.store.pending_approval(task_id)
        if pending:
            # Tasks created by an older version may still contain one paused
            # approval. Resume that action automatically so the task does not
            # remain blocked after upgrading to direct execution.
            return await self.confirm(pending["id"], True)
        message = {"role": "user", "content": str(content).strip(), "skill_ids": selected_skill_ids}
        if context:
            message["content"] += f"\n\n--- Additional workspace context ---\n{context}"
        self.store.append_message(task_id, message)
        self._event(task_id, "user_message", {"content": message["content"], "skill_ids": selected_skill_ids})
        self._register_running_task(task_id)
        try:
            return await self._run_loop(task_id)
        except TaskCancelled:
            return self._cancelled_result(task_id)
        except asyncio.CancelledError:
            if task_id in self._cancel_requested:
                return self._cancelled_result(task_id)
            raise
        finally:
            self._unregister_running_task(task_id)
            self._cancel_requested.discard(task_id)

    async def confirm(self, approval_id: str, approved: bool) -> dict[str, Any]:
        approval = self.store.approval(approval_id)
        if not approval or approval["status"] != "pending":
            raise ValueError("This confirmation has expired or was already resolved.")
        task_id = approval["task_id"]
        self.task(task_id)
        self.store.resolve_approval(approval_id, approved)
        self._event(task_id, "approval_resolved", {"id": approval_id, "approved": bool(approved)})
        self._register_running_task(task_id)
        try:
            self._check_cancelled(task_id)
            if approved:
                try:
                    raw_result = await self._execute_tool(task_id, approval["tool_name"], approval["arguments"])
                except Exception as exc:  # Tool errors are part of the agent transcript.
                    raw_result = {"error": str(exc)}
            else:
                raw_result = "User denied this action. Do not retry it unless the user asks again."
            self._check_cancelled(task_id)
            result = results.normalize(approval["tool_name"], raw_result)
            call = approval["call_payload"]
            tool_message = {
                "role": "tool",
                "tool_call_id": call.get("id", f"omniscience-tool-{approval['call_index']}"),
                "content": json_text(result),
            }
            self.store.append_message(task_id, tool_message)
            self._event(task_id, "tool_result", {"tool": approval["tool_name"], "result": result})
            await self._checkpoint_after_tool(task_id, approval["tool_name"])
            messages = self.store.messages(task_id)
            calls: list[dict[str, Any]] = []
            for message in reversed(messages):
                if message.get("role") == "assistant" and message.get("tool_calls"):
                    calls = message["tool_calls"]
                    break
            next_result = await self._process_tool_calls(
                task_id, calls, int(approval["call_index"]) + 1, self.task(task_id)["mode"]
            )
            if next_result is not None:
                return next_result
            return await self._run_loop(task_id)
        except TaskCancelled:
            return self._cancelled_result(task_id)
        except asyncio.CancelledError:
            if task_id in self._cancel_requested:
                return self._cancelled_result(task_id)
            raise
        finally:
            self._unregister_running_task(task_id)
            self._cancel_requested.discard(task_id)

    @staticmethod
    def _messages_for_model(messages: list[dict[str, Any]], mode: str | None = None) -> list[dict[str, Any]]:
        clean: list[dict[str, Any]] = []
        for message in messages:
            value = {
                key: value
                for key, value in message.items()
                if key not in {"seq", "created_at", "skill_ids"}
            }
            if value.get("role") == "user" and mode is not None:
                # Mode stamping happens only at the model boundary; the
                # persisted transcript keeps the raw user content (A.2).
                value = {**value, "content": wrap_user_input(str(value.get("content") or ""), mode, False)}
            if value.get("role") in {"user", "assistant", "tool"}:
                clean.append(value)
        return clean

    async def _compact_messages(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        mode: str,
        task_id: str | None = None,
        skill_ids: Any = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        system_prompt = (
            self.system_prompt(task_id, mode, skill_ids)
            if task_id
            else SYSTEM_PROMPT + f"\nYou are currently in {mode.upper()} mode. In PLAN mode, do not modify files or run commands."
        )
        settings_data = self.settings.load()
        try:
            configured = max_context_tokens(
                settings_data.get("max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS)
            )
            threshold = compaction_threshold_percent(
                settings_data.get("compaction_threshold_percent", DEFAULT_COMPACTION_THRESHOLD_PERCENT)
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
        reserved = ContextManager.estimate_tokens(system_prompt) + ContextManager.estimate_tokens(json_text(tools))
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
            summary_system_prompt = COMPACTION_SYSTEM_PROMPT + (
                skill_prompt(self.selected_skills(task_id, skill_ids)) if task_id else ""
            )
            response = await self._call_model(
                summary_input,
                [],
                mode,
                system_prompt=summary_system_prompt,
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
            logger.warning("Context summary request failed; using local fallback: %s", exc)
            return self.context.fallback(messages, reserved)

    async def context_status(self, task_id: str, skill_ids: Any = None) -> dict[str, Any]:
        """Return the estimated prompt size using the current message Skills."""
        task = self.task(task_id)
        settings_data = self.settings.load()
        try:
            max_context = max_context_tokens(
                settings_data.get("max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS)
            )
        except ValueError:
            max_context = DEFAULT_MAX_CONTEXT_TOKENS
        try:
            threshold = compaction_threshold_percent(
                settings_data.get("compaction_threshold_percent", DEFAULT_COMPACTION_THRESHOLD_PERCENT)
            )
        except ValueError:
            threshold = DEFAULT_COMPACTION_THRESHOLD_PERCENT

        stored_messages = self.store.messages(task_id)
        model_messages = self._cached_context(task_id, stored_messages)
        if model_messages is None:
            model_messages = self._messages_for_model(stored_messages)
        tools = await self._tool_definitions(task["mode"])
        system_prompt = self.system_prompt(task_id, skill_ids=skill_ids)
        system_tokens = ContextManager.estimate_tokens(system_prompt)
        tools_tokens = ContextManager.estimate_tokens(json_text(tools))
        message_tokens = ContextManager.estimate_messages_tokens(model_messages)
        prompt_tokens = message_tokens + system_tokens + tools_tokens
        prompt_chars = len(system_prompt) + len(json_text(tools)) + sum(
            len(json_text(message)) for message in model_messages
        )
        prompt_percent = prompt_tokens / max_context * 100
        return {
            "task_id": task_id,
            "message_count": len(model_messages),
            "prompt_chars": prompt_chars,
            "prompt_tokens": prompt_tokens,
            "message_tokens": message_tokens,
            "system_tokens": system_tokens,
            "tools_tokens": tools_tokens,
            "max_context_tokens": max_context,
            "prompt_percent": round(prompt_percent, 1),
            "compaction_threshold_percent": threshold,
            "above_compaction_threshold": prompt_percent > threshold,
        }

    def _cached_context(
        self,
        task_id: str,
        stored_messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]] | None:
        cached = self._context_cache.get(task_id)
        if not cached:
            return None
        source_seq = int(cached.get("source_seq", -1))
        sequence = [int(message.get("seq", -1)) for message in stored_messages]
        if source_seq < 0 or source_seq not in sequence:
            self._context_cache.pop(task_id, None)
            return None
        source_index = sequence.index(source_seq)
        new_messages = self._messages_for_model(stored_messages[source_index + 1 :])
        return [dict(message) for message in cached.get("messages", [])] + new_messages

    def _remember_context(
        self,
        task_id: str,
        stored_messages: list[dict[str, Any]],
        model_messages: list[dict[str, Any]],
        compacted: bool,
    ) -> None:
        if not compacted or not stored_messages:
            return
        self._context_cache[task_id] = {
            "source_seq": int(stored_messages[-1].get("seq", -1)),
            "messages": [dict(message) for message in model_messages],
        }

    async def _run_loop(self, task_id: str) -> dict[str, Any]:
        async with self._lock_for(task_id):
            task = self.task(task_id)
            task = self.store.start_task(task_id)
            self._event(task_id, "task_started", {"mode": task["mode"]})
            try:
                for _ in range(MAX_AGENT_STEPS):
                    self._check_cancelled(task_id)
                    self._consume_followups(task_id)
                    stored_messages = self.store.messages(task_id)
                    model_messages = self._cached_context(task_id, stored_messages)
                    if model_messages is None:
                        model_messages = self._messages_for_model(stored_messages, task["mode"])
                    active_skill_ids = self._latest_message_skill_ids(task_id)
                    tools = await self._tool_definitions(task["mode"])
                    model_messages, compacted = await self._compact_messages(
                        model_messages, tools, task["mode"], task_id, active_skill_ids
                    )
                    if compacted:
                        self._event(task_id, "context_compacted", {"message_count": len(model_messages)})
                        self._remember_context(task_id, stored_messages, model_messages, compacted)
                    response = await self._call_model(
                        model_messages,
                        tools,
                        task["mode"],
                        system_prompt=self.system_prompt(task_id, skill_ids=active_skill_ids),
                    )
                    self._check_cancelled(task_id)
                    choice = (response.get("choices") or [{}])[0]
                    assistant = choice.get("message") or {}
                    calls = assistant.get("tool_calls") or []
                    assistant_message = {
                        "role": "assistant",
                        "content": assistant.get("content") or "",
                    }
                    if calls:
                        assistant_message["tool_calls"] = calls
                    self.store.append_message(task_id, assistant_message)
                    if assistant_message["content"]:
                        self._event(task_id, "assistant_text", {"text": assistant_message["content"]})
                    if not calls:
                        comfyui_link = self._ensure_task_comfyui_link(task_id)
                        workflow, workflow_created = self._ensure_task_workflow(task_id)
                        result = {
                            "status": "ok",
                            "message": {
                                "role": "assistant",
                                "content": assistant_message["content"] or "模型没有返回文本。",
                            },
                            "comfyui_link": comfyui_link,
                            "workflow": workflow,
                            "workflow_created": workflow_created,
                        }
                        self.store.update_task(task_id, status="idle")
                        self._event(task_id, "task_finished", result)
                        return result
                    pending = await self._process_tool_calls(task_id, calls, 0, task["mode"])
                    if pending is not None:
                        return pending
                raise RuntimeError("The assistant reached the tool-call step limit. Please continue the task.")
            except TaskCancelled:
                return self._cancelled_result(task_id)
            except Exception as exc:
                self.store.update_task(task_id, status="idle")
                self._event(task_id, "task_failed", {"error": str(exc)[:2_000]})
                raise
            finally:
                # Cancellation and other BaseException paths must not leave a
                # task permanently marked as running after ComfyUI resumes.
                current = self.task(task_id)
                if current["status"] == "running":
                    self.store.update_task(task_id, status="idle")

    async def _process_tool_calls(
        self,
        task_id: str,
        calls: list[dict[str, Any]],
        start_index: int,
        mode: str,
    ) -> dict[str, Any] | None:
        """Execute a model's calls in order without interactive approvals."""

        for index in range(start_index, len(calls)):
            self._check_cancelled(task_id)
            call = calls[index]
            name, arguments = ClineAgent._parse_tool_call(call)
            denial = policy.policy_error(mode, name, arguments)
            if denial is not None:
                tool_result = results.fail(name, denial)
            else:
                try:
                    tool_result = results.normalize(name, await self._execute_tool(task_id, name, arguments))
                except Exception as exc:
                    tool_result = results.fail(name, str(exc))
            self._check_cancelled(task_id)
            tool_message = {
                "role": "tool",
                "tool_call_id": call.get("id", f"omniscience-tool-{index}"),
                "content": json_text(tool_result),
            }
            self.store.append_message(task_id, tool_message)
            self._event(task_id, "tool_result", {"tool": name, "result": tool_result})
            await self._checkpoint_after_tool(task_id, name)
        return None

    async def _call_model(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        mode: str,
        system_prompt: str | None = None,
        max_tokens: int = 4096,
    ) -> dict[str, Any]:
        data = self.settings.load()
        prompt = system_prompt or (
            SYSTEM_PROMPT + f"\nYou are currently in {mode.upper()} mode. In PLAN mode, do not modify files or run commands."
        )
        return await complete(
            model=str(data.get("model") or paths.DEFAULT_MODEL),
            api_base_url=str(data.get("api_base_url") or paths.DEFAULT_API_BASE_URL),
            api_key=self.settings.api_key(),
            messages=[{"role": "system", "content": prompt}, *messages],
            tools=tools,
            max_tokens=max_tokens,
        )
