"""Command execution with a cancellable process group (DESIGN 5.4).

Each task's running command is registered under its task id so cancellation
can SIGTERM the whole process group.  Oversized output is middle-truncated:
the head and tail survive because failures usually live at the end.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
from typing import Any, Callable

from ..constants import MAX_COMMAND_CHARS, MAX_COMMAND_OUTPUT
from ..util import truncate_middle
from . import guards


class CommandRunner:
    def __init__(self, is_cancelled: Callable[[str], bool] | None = None):
        self.processes: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()
        self._is_cancelled = is_cancelled or (lambda task_id: False)

    @staticmethod
    def validate(command: str, timeout: Any) -> int:
        if not command or len(command) > MAX_COMMAND_CHARS:
            raise ValueError(f"Command must be between 1 and {MAX_COMMAND_CHARS} characters.")
        try:
            return min(120, max(1, int(timeout)))
        except (TypeError, ValueError):
            return 120

    @staticmethod
    def shell_executable() -> str:
        """Return the native command interpreter used by the current OS."""

        if os.name == "nt":
            return os.environ.get("COMSPEC", "cmd.exe")
        return os.environ.get("SHELL", "/bin/sh")

    @staticmethod
    def creation_flags() -> int:
        """Create a Windows process group so timeout/cancel can kill children."""

        if os.name == "nt":
            return int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        return 0

    @staticmethod
    def terminate_process_tree(process: Any) -> None:
        """Terminate *process* and its descendants using the native mechanism."""

        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                )
            except OSError:
                # A minimal Windows environment may not expose taskkill on PATH;
                # still terminate the command interpreter itself in that case.
                try:
                    process.terminate()
                except OSError:
                    pass
            return
        try:
            os.killpg(os.getpgid(process.pid), 15)
        except (OSError, ProcessLookupError):
            try:
                process.terminate()
            except OSError:
                pass

    def terminate(self, task_id: str) -> None:
        with self._lock:
            process = self.processes.get(task_id)
        if process is None or process.poll() is not None:
            return
        self.terminate_process_tree(process)

    def run_sync(self, task_id: str, command: str, timeout: Any, cwd: Any) -> dict[str, Any]:
        timeout_seconds = self.validate(command, timeout)
        guard_message = guards.guard_error(command, "act")
        if guard_message:
            raise ValueError(guard_message)
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            shell=True,  # noqa: S602 - command execution is bounded by local policy
            executable=self.shell_executable(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=(os.name != "nt"),
            creationflags=self.creation_flags(),
        )
        with self._lock:
            self.processes[task_id] = process
        if self._is_cancelled(task_id):
            self.terminate(task_id)
        try:
            try:
                stdout, stderr = process.communicate(timeout=timeout_seconds)
                return {"exit_code": process.returncode, "output": truncate_middle(stdout + stderr, MAX_COMMAND_OUTPUT)}
            except subprocess.TimeoutExpired as exc:
                self.terminate(task_id)
                stdout, stderr = process.communicate()
                partial_stdout = exc.stdout or stdout or ""
                partial_stderr = exc.stderr or stderr or ""
                if isinstance(partial_stdout, bytes):
                    partial_stdout = partial_stdout.decode(errors="replace")
                if isinstance(partial_stderr, bytes):
                    partial_stderr = partial_stderr.decode(errors="replace")
                return {
                    "exit_code": None,
                    "timed_out": True,
                    "output": truncate_middle(partial_stdout + partial_stderr, MAX_COMMAND_OUTPUT),
                }
        finally:
            with self._lock:
                if self.processes.get(task_id) is process:
                    self.processes.pop(task_id, None)

    async def run(self, task_id: str, command: str, timeout: Any, cwd: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self.run_sync, task_id, command, timeout, cwd)
