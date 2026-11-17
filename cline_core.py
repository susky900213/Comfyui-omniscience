"""Persistent task, event, context, and checkpoint primitives for ComfyUI-Cline.

The original Cline applications keep these concerns outside the UI.  This
module provides the same boundary for the ComfyUI adapter: task state is
durable, the browser only renders events, and a task can resume after a
ComfyUI restart.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable


def _now() -> float:
    return time.time()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class TaskStore:
    """SQLite-backed task state shared by the HTTP handlers and agent loop."""

    def __init__(self, path: Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    workspace TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'act',
                    status TEXT NOT NULL DEFAULT 'idle',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    task_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (task_id, seq),
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS global_messages (
                    seq INTEGER PRIMARY KEY,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    task_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (task_id, seq),
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    arguments TEXT NOT NULL,
                    call_payload TEXT NOT NULL,
                    call_index INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at REAL NOT NULL,
                    resolved_at REAL,
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    label TEXT NOT NULL,
                    reference TEXT NOT NULL,
                    backend TEXT NOT NULL,
                    message_seq INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS current_workflows (
                    task_id TEXT PRIMARY KEY,
                    workflow TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'comfyui_canvas',
                    updated_at REAL NOT NULL,
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS events_task_seq ON events(task_id, seq);
                CREATE INDEX IF NOT EXISTS messages_task_seq ON messages(task_id, seq);
                CREATE INDEX IF NOT EXISTS global_messages_seq ON global_messages(seq);
                CREATE INDEX IF NOT EXISTS tasks_updated ON tasks(updated_at DESC);
                """
            )

    @staticmethod
    def _task_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        try:
            metadata = json.loads(value.get("metadata") or "{}")
            value["metadata"] = metadata if isinstance(metadata, dict) else {}
        except json.JSONDecodeError:
            value["metadata"] = {}
        # A task's workspace is the project directory created for it.  Expose
        # a stable folder name as well so the history UI does not have to
        # guess it from the task title.
        value["folder"] = value["metadata"].get("project_folder") or Path(str(value.get("workspace") or "")).name
        value["project_path"] = str(value.get("workspace") or "")
        return value

    def create_task(
        self,
        workspace: str,
        title: str = "新任务",
        mode: str = "act",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        task_id = uuid.uuid4().hex
        timestamp = _now()
        mode = mode if mode in {"plan", "act"} else "act"
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT INTO tasks(id,title,workspace,mode,status,metadata,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (task_id, title.strip() or "新任务", str(workspace), mode, "idle", _json(metadata or {}), timestamp, timestamp),
            )
        return self.get_task(task_id) or {}

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._task_row(row)

    def list_tasks(self, limit: int | None = 100) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            if limit is None:
                rows = connection.execute("SELECT * FROM tasks ORDER BY updated_at DESC").fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?", (max(1, min(500, int(limit))),)
                ).fetchall()
        return [self._task_row(row) for row in rows if row is not None]

    def update_task(self, task_id: str, **values: Any) -> dict[str, Any]:
        allowed = {"title", "workspace", "mode", "status", "metadata"}
        updates: list[str] = []
        parameters: list[Any] = []
        for key, value in values.items():
            if key not in allowed:
                continue
            if key == "mode" and value not in {"plan", "act"}:
                raise ValueError("Task mode must be 'plan' or 'act'.")
            if key == "metadata":
                value = _json(value if isinstance(value, dict) else {})
            updates.append(f"{key} = ?")
            parameters.append(value)
        if updates:
            updates.append("updated_at = ?")
            parameters.extend([_now(), task_id])
            with self._lock, self._connection() as connection:
                connection.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", parameters)
        return self.get_task(task_id) or {}

    def start_task(self, task_id: str) -> dict[str, Any]:
        """Transition a task to running.

        A user cancellation stops the current run; it does not terminate the
        task.  Always allowing this transition also lets tasks created by an
        older version, which may still have ``cancelled`` persisted, resume in
        the same project folder.
        """
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE tasks SET status = 'running', updated_at = ? WHERE id = ?",
                (_now(), task_id),
            )
        return self.get_task(task_id) or {}

    def append_message(self, task_id: str, message: dict[str, Any]) -> dict[str, Any]:
        payload = dict(message)
        role = str(payload.get("role") or "user")
        timestamp = _now()
        with self._lock, self._connection() as connection:
            row = connection.execute("SELECT COALESCE(MAX(seq), -1) + 1 FROM messages WHERE task_id = ?", (task_id,)).fetchone()
            seq = int(row[0]) if row else 0
            connection.execute(
                "INSERT INTO messages(task_id,seq,role,payload,created_at) VALUES(?,?,?,?,?)",
                (task_id, seq, role, _json(payload), timestamp),
            )
            connection.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (timestamp, task_id))
        return {"seq": seq, "created_at": timestamp, **payload}

    def messages(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT seq, role, payload, created_at FROM messages WHERE task_id = ? ORDER BY seq", (task_id,)
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except json.JSONDecodeError:
                payload = {"role": row["role"], "content": row["payload"]}
            payload["seq"] = row["seq"]
            payload["created_at"] = row["created_at"]
            result.append(payload)
        return result

    def append_global_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """Persist a message for the task-independent global conversation."""
        payload = dict(message)
        role = str(payload.get("role") or "user")
        timestamp = _now()
        with self._lock, self._connection() as connection:
            row = connection.execute("SELECT COALESCE(MAX(seq), -1) + 1 FROM global_messages").fetchone()
            seq = int(row[0]) if row else 0
            connection.execute(
                "INSERT INTO global_messages(seq,role,payload,created_at) VALUES(?,?,?,?)",
                (seq, role, _json(payload), timestamp),
            )
        return {"seq": seq, "created_at": timestamp, **payload}

    def global_messages(self) -> list[dict[str, Any]]:
        """Return the independent global conversation transcript."""
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT seq, role, payload, created_at FROM global_messages ORDER BY seq"
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except json.JSONDecodeError:
                payload = {"role": row["role"], "content": row["payload"]}
            payload["seq"] = row["seq"]
            payload["created_at"] = row["created_at"]
            result.append(payload)
        return result

    def clear_global_messages(self) -> None:
        """Delete only the global transcript; task history is untouched."""
        with self._lock, self._connection() as connection:
            connection.execute("DELETE FROM global_messages")

    def set_current_workflow(self, task_id: str, workflow: dict[str, Any], source: str = "comfyui_canvas") -> dict[str, Any]:
        """Persist the latest workflow serialized by the active ComfyUI canvas.

        This cache is deliberately separate from the project's ``workflow.json``.
        A live canvas may contain unsaved user changes and must never overwrite a
        user-authored file just because the Agent inspected it.
        """
        if not isinstance(workflow, dict):
            raise ValueError("Current workflow must be a JSON object.")
        source_value = str(source or "comfyui_canvas").strip()[:100] or "comfyui_canvas"
        timestamp = _now()
        try:
            payload = json.dumps(workflow, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("Current workflow must contain only valid JSON values.") from exc
        with self._lock, self._connection() as connection:
            task = connection.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                raise ValueError("Task does not exist.")
            connection.execute(
                """
                INSERT INTO current_workflows(task_id,workflow,source,updated_at)
                VALUES(?,?,?,?)
                ON CONFLICT(task_id) DO UPDATE SET
                    workflow = excluded.workflow,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (task_id, payload, source_value, timestamp),
            )
            connection.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (timestamp, task_id))
        return self.current_workflow(task_id) or {}

    def current_workflow(self, task_id: str) -> dict[str, Any] | None:
        """Return the latest live canvas workflow, if the frontend has synced one."""
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT workflow,source,updated_at FROM current_workflows WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            workflow = json.loads(row["workflow"])
        except json.JSONDecodeError:
            return None
        if not isinstance(workflow, dict):
            return None
        return {
            "workflow": workflow,
            "source": row["source"],
            "updated_at": row["updated_at"],
            "bytes": len(row["workflow"].encode("utf-8")),
        }

    def truncate_messages(self, task_id: str, seq: int) -> None:
        with self._lock, self._connection() as connection:
            connection.execute("DELETE FROM messages WHERE task_id = ? AND seq > ?", (task_id, int(seq)))
            connection.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (_now(), task_id))

    def clear_history(self, task_id: str) -> dict[str, Any]:
        """Delete conversation records while retaining the task and project.

        Checkpoints are deliberately retained: they represent filesystem
        recovery points, not conversation turns.  The caller is responsible
        for refusing this operation while an agent is actively running.
        """
        timestamp = _now()
        with self._lock, self._connection() as connection:
            row = connection.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise ValueError("Task does not exist.")
            connection.execute("DELETE FROM approvals WHERE task_id = ?", (task_id,))
            connection.execute("DELETE FROM events WHERE task_id = ?", (task_id,))
            connection.execute("DELETE FROM messages WHERE task_id = ?", (task_id,))
            connection.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (timestamp, task_id))
        return self.get_task(task_id) or {}

    def append_event(self, task_id: str, kind: str, payload: Any) -> dict[str, Any]:
        timestamp = _now()
        with self._lock, self._connection() as connection:
            row = connection.execute("SELECT COALESCE(MAX(seq), -1) + 1 FROM events WHERE task_id = ?", (task_id,)).fetchone()
            seq = int(row[0]) if row else 0
            connection.execute(
                "INSERT INTO events(task_id,seq,kind,payload,created_at) VALUES(?,?,?,?,?)",
                (task_id, seq, kind, _json(payload), timestamp),
            )
            connection.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (timestamp, task_id))
        return {"seq": seq, "kind": kind, "payload": payload, "created_at": timestamp}

    def events(self, task_id: str, after: int = -1, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT seq,kind,payload,created_at FROM events WHERE task_id = ? AND seq > ? ORDER BY seq LIMIT ?",
                (task_id, int(after), max(1, min(1000, int(limit)))),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except json.JSONDecodeError:
                payload = row["payload"]
            result.append({"seq": row["seq"], "kind": row["kind"], "payload": payload, "created_at": row["created_at"]})
        return result

    def create_approval(self, task_id: str, tool_name: str, arguments: dict[str, Any], call_payload: dict[str, Any], call_index: int) -> dict[str, Any]:
        approval_id = uuid.uuid4().hex
        timestamp = _now()
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT INTO approvals(id,task_id,tool_name,arguments,call_payload,call_index,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (approval_id, task_id, tool_name, _json(arguments), _json(call_payload), int(call_index), "pending", timestamp),
            )
        return self.approval(approval_id) or {}

    def approval(self, approval_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            row = connection.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        for key in ("arguments", "call_payload"):
            try:
                value[key] = json.loads(value[key])
            except json.JSONDecodeError:
                value[key] = {}
        return value

    def resolve_approval(self, approval_id: str, approved: bool) -> dict[str, Any]:
        status = "approved" if approved else "rejected"
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE approvals SET status = ?, resolved_at = ? WHERE id = ? AND status = 'pending'",
                (status, _now(), approval_id),
            )
        return self.approval(approval_id) or {}

    def pending_approval(self, task_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM approvals WHERE task_id = ? AND status = 'pending' ORDER BY created_at LIMIT 1", (task_id,)
            ).fetchone()
        if row is None:
            return None
        return self.approval(row["id"])

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        """Stop the current run and return the task to the idle state.

        The runtime may still be unwinding an in-flight model or tool call when
        this method returns.  The runtime keeps a separate in-memory
        cancellation signal for that in-flight operation; the durable task
        remains resumable so the next user message stays in the same node.
        """
        timestamp = _now()
        with self._lock, self._connection() as connection:
            row = connection.execute("SELECT id FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise ValueError("Task does not exist.")
            connection.execute(
                "UPDATE tasks SET status = 'idle', updated_at = ? WHERE id = ?",
                (timestamp, task_id),
            )
            connection.execute(
                "UPDATE approvals SET status = 'rejected', resolved_at = ? WHERE task_id = ? AND status = 'pending'",
                (timestamp, task_id),
            )
        return self.get_task(task_id) or {}

    def add_checkpoint(self, task_id: str, label: str, reference: str, backend: str, message_seq: int, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        checkpoint_id = uuid.uuid4().hex
        timestamp = _now()
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT INTO checkpoints(id,task_id,label,reference,backend,message_seq,created_at,metadata) VALUES(?,?,?,?,?,?,?,?)",
                (checkpoint_id, task_id, label, reference, backend, int(message_seq), timestamp, _json(metadata or {})),
            )
        return self.checkpoint(checkpoint_id) or {}

    def checkpoint(self, checkpoint_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            row = connection.execute("SELECT * FROM checkpoints WHERE id = ?", (checkpoint_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        try:
            value["metadata"] = json.loads(value["metadata"] or "{}")
        except json.JSONDecodeError:
            value["metadata"] = {}
        return value

    def checkpoints(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            rows = connection.execute("SELECT id FROM checkpoints WHERE task_id = ? ORDER BY created_at", (task_id,)).fetchall()
        return [value for row in rows if (value := self.checkpoint(row["id"])) is not None]


class SkillStore:
    """SQLite-backed reusable prompt skills.

    Skills are deliberately kept in their own database.  They are user-authored
    prompt instructions rather than task history, so deleting a task must not
    delete a skill and exporting skills must not include conversation data.
    """

    MAX_NAME_CHARS = 128
    MAX_DESCRIPTION_CHARS = 1_000
    MAX_CONTENT_BYTES = 200_000

    def __init__(self, path: Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS skills (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    description TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS skills_updated ON skills(updated_at DESC);
                """
            )

    @classmethod
    def _validated_values(cls, values: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if not partial or "name" in values:
            name = str(values.get("name") or "").strip()
            if not name or len(name) > cls.MAX_NAME_CHARS or any(ord(char) < 32 for char in name):
                raise ValueError(f"Skill 名称不能为空，且不能超过 {cls.MAX_NAME_CHARS} 个字符。")
            result["name"] = name
        if not partial or "description" in values:
            description = str(values.get("description") or "").strip()
            if len(description) > cls.MAX_DESCRIPTION_CHARS:
                raise ValueError(f"Skill 描述不能超过 {cls.MAX_DESCRIPTION_CHARS} 个字符。")
            result["description"] = description
        if not partial or "content" in values or "prompt" in values:
            content = values.get("content", values.get("prompt", ""))
            content = str(content or "").strip()
            if not content:
                raise ValueError("Skill 提示词内容不能为空。")
            if len(content.encode("utf-8")) > cls.MAX_CONTENT_BYTES:
                raise ValueError(f"Skill 提示词不能超过 {cls.MAX_CONTENT_BYTES} 字节。")
            result["content"] = content
        if "enabled" in values:
            result["enabled"] = bool(values["enabled"])
        elif not partial:
            result["enabled"] = True
        return result

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        value = dict(row)
        value["enabled"] = bool(value.get("enabled"))
        return value

    def get(self, skill_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            row = connection.execute("SELECT * FROM skills WHERE id = ?", (str(skill_id),)).fetchone()
        return self._row(row)

    def list(self, include_disabled: bool = True) -> list[dict[str, Any]]:
        query = "SELECT * FROM skills"
        parameters: tuple[Any, ...] = ()
        if not include_disabled:
            query += " WHERE enabled = 1"
        query += " ORDER BY name COLLATE NOCASE"
        with self._lock, self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [value for row in rows if (value := self._row(row)) is not None]

    def create(self, values: dict[str, Any]) -> dict[str, Any]:
        normalized = self._validated_values(values)
        skill_id = uuid.uuid4().hex
        timestamp = _now()
        try:
            with self._lock, self._connection() as connection:
                connection.execute(
                    "INSERT INTO skills(id,name,description,content,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        skill_id,
                        normalized["name"],
                        normalized["description"],
                        normalized["content"],
                        int(normalized["enabled"]),
                        timestamp,
                        timestamp,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"Skill 名称已存在：{normalized['name']}") from exc
        return self.get(skill_id) or {}

    def update(self, skill_id: str, values: dict[str, Any]) -> dict[str, Any]:
        skill_id = str(skill_id)
        current = self.get(skill_id)
        if current is None:
            raise ValueError("Skill 不存在。")
        normalized = self._validated_values(values, partial=True)
        if not normalized:
            return current
        updates = [f"{key} = ?" for key in normalized]
        parameters = [int(value) if key == "enabled" else value for key, value in normalized.items()]
        parameters.extend([_now(), skill_id])
        try:
            with self._lock, self._connection() as connection:
                connection.execute(
                    f"UPDATE skills SET {', '.join(updates)}, updated_at = ? WHERE id = ?",
                    parameters,
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"Skill 名称已存在：{normalized.get('name', current['name'])}") from exc
        return self.get(skill_id) or {}

    def delete(self, skill_id: str) -> dict[str, Any]:
        skill = self.get(skill_id)
        if skill is None:
            raise ValueError("Skill 不存在。")
        with self._lock, self._connection() as connection:
            connection.execute("DELETE FROM skills WHERE id = ?", (str(skill_id),))
        return skill

    def import_items(self, items: Any) -> list[dict[str, Any]]:
        if isinstance(items, dict):
            items = items.get("skills")
        if not isinstance(items, list) or len(items) > 500:
            raise ValueError("导入内容必须是最多 500 个 Skill 的数组。")
        imported: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("每个 Skill 必须是 JSON 对象。")
            normalized = self._validated_values(item)
            with self._lock, self._connection() as connection:
                existing = connection.execute(
                    "SELECT id FROM skills WHERE name = ? COLLATE NOCASE", (normalized["name"],)
                ).fetchone()
            if existing is None:
                imported.append(self.create(normalized))
            else:
                imported.append(self.update(existing["id"], normalized))
        return imported

    def export_items(self) -> list[dict[str, Any]]:
        return [
            {
                "name": skill["name"],
                "description": skill["description"],
                "content": skill["content"],
                "enabled": skill["enabled"],
            }
            for skill in self.list()
        ]


class ContextManager:
    """Estimate and compact model context without requiring a tokenizer package.

    OpenAI-compatible providers do not expose a common context-limit discovery
    endpoint.  The estimator is intentionally conservative and can be replaced
    by a provider-specific tokenizer later without changing the compaction
    boundary.
    """

    COMPACTION_THRESHOLD = 0.8
    _TOKEN_PATTERN = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9_]+|[^\sA-Za-z0-9_\u4e00-\u9fff]", re.UNICODE)

    def __init__(
        self,
        max_tokens: int = 32_000,
        keep_messages: int = 14,
        max_chars: int | None = None,
        compaction_threshold: float = COMPACTION_THRESHOLD,
    ):
        # max_chars is retained for callers of the original ContextManager API.
        # A rough four-character/token conversion keeps old small-budget tests
        # useful while the production boundary is expressed in tokens.
        self.max_chars = max_chars
        if max_chars is not None:
            max_tokens = max(1, int(max_chars / 4))
        self.max_tokens = max(1, int(max_tokens))
        self.keep_messages = keep_messages
        self.compaction_threshold = min(1.0, max(0.01, float(compaction_threshold)))

    @classmethod
    def estimate_tokens(cls, value: Any) -> int:
        """Return a deterministic, dependency-free token estimate.

        CJK characters and punctuation are counted individually; runs of Latin
        letters/digits are counted as one token per four characters. JSON/tool
        payloads therefore receive a deliberately conservative estimate.
        """
        text = str(value or "")
        if not text:
            return 0
        total = 0
        for part in cls._TOKEN_PATTERN.findall(text):
            if len(part) == 1 and not part.isalnum() and not part == "_":
                total += 1
            elif re.fullmatch(r"[\u4e00-\u9fff]", part):
                total += 1
            else:
                total += max(1, (len(part) + 3) // 4)
        return total

    @classmethod
    def estimate_messages_tokens(cls, messages: list[dict[str, Any]]) -> int:
        # The small per-message overhead accounts for role and tool-call fields.
        total = 0
        for message in messages:
            total += cls.estimate_tokens(message.get("content")) + 4
            if message.get("tool_calls"):
                total += cls.estimate_tokens(_json(message["tool_calls"]))
            if message.get("tool_call_id"):
                total += cls.estimate_tokens(message["tool_call_id"])
        return total

    @classmethod
    def estimate_prompt_tokens(cls, messages: list[dict[str, Any]], reserved_tokens: int = 0) -> int:
        """Estimate the complete input prompt, including non-message fields."""
        return cls.estimate_messages_tokens(messages) + max(0, int(reserved_tokens))

    def needs_compaction(self, messages: list[dict[str, Any]], reserved_tokens: int = 0) -> bool:
        return bool(messages) and self.estimate_prompt_tokens(messages, reserved_tokens) > self.max_tokens * self.compaction_threshold

    def _split_messages(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        if not messages:
            return [], [], []
        # Preserve the first system-like/user instruction and the latest turns.
        # The current protocol normally puts system instructions outside this
        # list, but keeping the first message also protects older integrations.
        head = [dict(messages[0])] if messages else []
        tail_start = max(1, len(messages) - self.keep_messages)
        # Never leave a tool result without the assistant tool call that
        # introduced it. This may retain one or more extra recent messages.
        while tail_start > 1 and messages[tail_start].get("role") == "tool":
            tail_start -= 1
        return head, [dict(message) for message in messages[tail_start:]], [dict(message) for message in messages[1:tail_start]]

    def fallback(self, messages: list[dict[str, Any]], reserved_tokens: int = 0) -> tuple[list[dict[str, Any]], bool]:
        """Compact locally when the optional model summary request fails."""
        if not self.needs_compaction(messages, reserved_tokens):
            return messages, False
        head, tail, omitted = self._split_messages(messages)
        if not omitted:
            # There is no older turn to summarize (for example, one very large
            # user message). Keep the transcript shape intact and trim the
            # message in place instead of duplicating it with an empty summary.
            compacted = [dict(message) for message in messages]
            available_tokens = max(1, self.max_tokens - max(0, int(reserved_tokens)))
            over_hard_limit = self.estimate_messages_tokens(compacted) > available_tokens or (
                self.max_chars is not None
                and sum(len(str(message.get("content") or "")) for message in compacted) > self.max_chars
            )
            if not over_hard_limit:
                return messages, False
            changed = False
            while self.estimate_messages_tokens(compacted) > available_tokens or (
                self.max_chars is not None
                and sum(len(str(message.get("content") or "")) for message in compacted) > self.max_chars
            ):
                largest = max(range(len(compacted)), key=lambda index: len(str(compacted[index].get("content") or "")))
                content = str(compacted[largest].get("content") or "")
                if not content:
                    break
                excess = self.estimate_messages_tokens(compacted) - available_tokens
                target_length = max(1, len(content) - max(1, excess * 4))
                compacted[largest]["content"] = content[: max(1, target_length - 1)] + "…"
                changed = True
            return compacted, changed
        summary_parts: list[str] = []
        for message in omitted:
            role = message.get("role", "unknown")
            content = str(message.get("content") or "").replace("\n", " ")
            if len(content) > 240:
                content = content[:240] + "…"
            if content:
                summary_parts.append(f"{role}: {content}")
        summary = {
            "role": "user",
            "content": "[Earlier context compacted by Cline]\n" + "\n".join(summary_parts[-80:]),
        }
        compacted = head + [summary] + tail
        # A small budget can be smaller than the preserved recent messages.
        # Trim content rather than returning an over-budget request. Keep the
        # marker itself so the model knows why earlier turns are absent.
        available_tokens = max(1, self.max_tokens - max(0, int(reserved_tokens)))
        while self.estimate_messages_tokens(compacted) > available_tokens or (
            self.max_chars is not None
            and sum(len(str(message.get("content") or "")) for message in compacted) > self.max_chars
        ):
            excess = self.estimate_messages_tokens(compacted) - available_tokens
            changed = False
            for index, message in enumerate(compacted):
                content = str(message.get("content") or "")
                minimum = len("[Earlier context compacted by Cline]") if index == 1 else 0
                removable = max(0, len(content) - minimum)
                if removable:
                    target_length = len(content) - min(removable, max(1, excess * 4))
                    if target_length and target_length < len(content):
                        message["content"] = content[: target_length - 1] + "…"
                    else:
                        message["content"] = content[:target_length]
                    changed = True
                    break
            if not changed:
                break
        return compacted, True

    @staticmethod
    def summary_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Build a safe, tool-free prompt for a summarization model call."""
        result: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            if role not in {"user", "assistant", "tool"}:
                continue
            content = str(message.get("content") or "")
            if message.get("tool_calls"):
                content += "\nTool calls: " + _json(message["tool_calls"])
            result.append({"role": role, "content": content})
        return result

    def messages_to_summarize(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return only the old turns that will be replaced by a summary."""
        _, _, omitted = self._split_messages(messages)
        return self.summary_messages(omitted)

    def bounded_summary_messages(self, messages: list[dict[str, Any]], ratio: float = 0.65) -> list[dict[str, Any]]:
        """Keep the summary request itself below the configured context budget."""
        result = self.messages_to_summarize(messages)
        budget = max(128, int(self.max_tokens * max(0.2, min(0.8, ratio))))
        while result and self.estimate_messages_tokens(result) > budget:
            # Prefer retaining the most recent omitted turns; they are usually
            # the ones needed to connect the summary to the preserved tail.
            result.pop(0)
        return result

    def with_summary(
        self,
        messages: list[dict[str, Any]],
        summary_text: str,
        reserved_tokens: int = 0,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Replace omitted history with a model-generated summary."""
        if not self.needs_compaction(messages, reserved_tokens) or not str(summary_text).strip():
            return messages, False
        head, tail, omitted = self._split_messages(messages)
        if not omitted:
            # A single oversized message has no replaceable history. Use the
            # same safe local trimming path as a failed summary request rather
            # than inserting a duplicate summary beside the original message.
            return self.fallback(messages, reserved_tokens)
        summary = {
            "role": "user",
            "content": "[Earlier context summarized by Cline]\n" + str(summary_text).strip(),
        }
        compacted = head + [summary] + tail
        available_tokens = max(1, self.max_tokens - max(0, int(reserved_tokens)))
        if self.estimate_messages_tokens(compacted) > available_tokens:
            # Keep the successful summary but enforce the configured hard
            # budget. fallback() would summarize the original messages again.
            while self.estimate_messages_tokens(compacted) > available_tokens:
                largest = max(range(len(compacted)), key=lambda index: len(str(compacted[index].get("content") or "")))
                content = str(compacted[largest].get("content") or "")
                minimum = len("[Earlier context summarized by Cline]") if largest == 1 else 0
                if len(content) <= minimum:
                    break
                compacted[largest]["content"] = content[: max(minimum, len(content) - 256)] + "…"
        return compacted, True

    def prepare(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
        """Compatibility method: use the deterministic local fallback only."""
        return self.fallback(messages)


class CheckpointManager:
    """Shadow-Git checkpoints with a filesystem snapshot fallback."""

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir).expanduser()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _repo(self, task_id: str) -> Path:
        return self.state_dir / "checkpoints" / f"{task_id}.git"

    @staticmethod
    def _git_path() -> str | None:
        return shutil.which("git")

    def _git(self, root: Path, repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        git = self._git_path()
        if not git:
            raise RuntimeError("git is not available")
        env = os.environ.copy()
        env.update(
            {
                "GIT_AUTHOR_NAME": "ComfyUI-Cline",
                "GIT_AUTHOR_EMAIL": "comfyui-cline@localhost",
                "GIT_COMMITTER_NAME": "ComfyUI-Cline",
                "GIT_COMMITTER_EMAIL": "comfyui-cline@localhost",
            }
        )
        return subprocess.run(
            [git, f"--git-dir={repo}", f"--work-tree={root}", *args],
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=check,
        )

    def _git_checkpoint(self, task_id: str, root: Path, label: str) -> tuple[str, str]:
        repo = self._repo(task_id)
        repo.parent.mkdir(parents=True, exist_ok=True)
        if not repo.exists():
            self._git(root, repo, "init", "--bare")
        self._git(root, repo, "add", "-A", "--", ".")
        self._git(root, repo, "commit", "--allow-empty", "-m", label)
        reference = self._git(root, repo, "rev-parse", "HEAD").stdout.strip()
        return "git", reference

    def _snapshot_checkpoint(self, task_id: str, root: Path) -> tuple[str, str]:
        snapshot_root = self.state_dir / "snapshots" / task_id
        snapshot_root.mkdir(parents=True, exist_ok=True)
        reference = uuid.uuid4().hex
        destination = snapshot_root / reference
        shutil.copytree(
            root,
            destination,
            symlinks=True,
            ignore=shutil.ignore_patterns(".git", "__pycache__", ".cline-checkpoints"),
        )
        return "snapshot", reference

    def create(self, task_id: str, root: Path, label: str) -> tuple[str, str, dict[str, Any]]:
        with self._lock:
            try:
                backend, reference = self._git_checkpoint(task_id, root, label)
                return backend, reference, {"label": label}
            except (OSError, RuntimeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                backend, reference = self._snapshot_checkpoint(task_id, root)
                return backend, reference, {"label": label, "fallback_reason": str(exc)[:500]}

    def _safe_extract(self, archive: tarfile.TarFile, root: Path) -> None:
        root_resolved = root.resolve()
        for member in archive.getmembers():
            destination = (root / member.name).resolve()
            try:
                destination.relative_to(root_resolved)
            except ValueError as exc:
                raise ValueError("Checkpoint archive contains an unsafe path.") from exc
            archive.extract(member, root)

    def _restore_git(self, root: Path, repo: Path, reference: str) -> None:
        tracked = self._git(root, repo, "ls-files", "-z").stdout.split("\0")
        target = self._git(root, repo, "ls-tree", "-r", "--name-only", reference).stdout.splitlines()
        target_set = set(target)
        for relative in tracked:
            if relative and relative not in target_set:
                path = root / relative
                if path.is_file() or path.is_symlink():
                    path.unlink(missing_ok=True)
        with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as temporary:
            archive_path = Path(temporary.name)
        try:
            self._git(root, repo, "archive", "-o", str(archive_path), reference)
            with tarfile.open(archive_path) as archive:
                self._safe_extract(archive, root)
        finally:
            archive_path.unlink(missing_ok=True)

    def _restore_snapshot(self, root: Path, snapshot: Path) -> None:
        snapshot_files = {path.relative_to(snapshot) for path in snapshot.rglob("*")}
        for path in sorted(root.rglob("*"), key=lambda value: len(value.parts), reverse=True):
            if ".git" in path.relative_to(root).parts:
                continue
            if path.relative_to(root) not in snapshot_files:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
        shutil.copytree(snapshot, root, symlinks=True, dirs_exist_ok=True)

    def restore(self, task_id: str, root: Path, backend: str, reference: str) -> None:
        with self._lock:
            if backend == "git":
                self._restore_git(root, self._repo(task_id), reference)
            elif backend == "snapshot":
                self._restore_snapshot(root, self.state_dir / "snapshots" / task_id / reference)
            else:
                raise ValueError(f"Unsupported checkpoint backend: {backend}")

    def compare(self, task_id: str, root: Path, backend: str, reference: str) -> str:
        if backend == "git":
            return self._git(root, self._repo(task_id), "diff", "--no-ext-diff", "--unified=3", reference, "--", ".", check=False).stdout[-100_000:]
        return "Filesystem snapshot comparison is available through Restore; git is required for a textual diff."