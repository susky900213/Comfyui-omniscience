"""SQLite-backed task, message, event, approval, and checkpoint storage.

Persistent state is canonical in this project (DESIGN.md section 10): the
in-memory runtime is only a projection that can be rebuilt from these tables.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from .util import json_dumps as _json
from .util import now as _now


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
                CREATE TABLE IF NOT EXISTS node_scans (
                    path TEXT PRIMARY KEY,
                    report TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflow_jobs (
                    id TEXT PRIMARY KEY,
                    query TEXT NOT NULL,
                    state TEXT NOT NULL,
                    region TEXT NOT NULL DEFAULT '{}',
                    selected_candidate_id TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS workflow_candidates (
                    job_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    rank INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (job_id, candidate_id),
                    FOREIGN KEY (job_id) REFERENCES workflow_jobs(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS workflow_jobs_updated ON workflow_jobs(updated_at DESC);
                CREATE INDEX IF NOT EXISTS workflow_candidates_rank ON workflow_candidates(job_id, rank);
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

    def create_workflow_job(self, query: str, *, region: dict[str, Any] | None = None) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        timestamp = _now()
        with self._lock, self._connection() as connection:
            connection.execute(
                "INSERT INTO workflow_jobs(id,query,state,region,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (job_id, str(query), "searching", _json(region or {}), timestamp, timestamp),
            )
        return self.get_workflow_job(job_id) or {}

    def get_workflow_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            row = connection.execute("SELECT * FROM workflow_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for key in ("region", "metadata"):
            try:
                value = json.loads(result.get(key) or "{}")
                result[key] = value if isinstance(value, dict) else {}
            except json.JSONDecodeError:
                result[key] = {}
        return result

    def update_workflow_job(self, job_id: str, **values: Any) -> dict[str, Any]:
        allowed = {"state", "region", "selected_candidate_id", "error", "metadata"}
        updates: list[str] = []
        parameters: list[Any] = []
        for key, value in values.items():
            if key not in allowed:
                continue
            if key in {"region", "metadata"}:
                value = _json(value if isinstance(value, dict) else {})
            updates.append(f"{key} = ?")
            parameters.append(value)
        if updates:
            updates.append("updated_at = ?")
            parameters.extend([_now(), job_id])
            with self._lock, self._connection() as connection:
                connection.execute(f"UPDATE workflow_jobs SET {', '.join(updates)} WHERE id = ?", parameters)
        return self.get_workflow_job(job_id) or {}

    def save_workflow_candidates(self, job_id: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        timestamp = _now()
        with self._lock, self._connection() as connection:
            connection.execute("DELETE FROM workflow_candidates WHERE job_id = ?", (job_id,))
            for rank, candidate in enumerate(candidates):
                candidate_id = str(candidate.get("id") or "").strip()
                if not candidate_id:
                    continue
                connection.execute(
                    "INSERT INTO workflow_candidates(job_id,candidate_id,payload,rank,created_at) VALUES(?,?,?,?,?)",
                    (job_id, candidate_id, _json(candidate), rank, timestamp),
                )
        return self.workflow_candidates(job_id)

    def workflow_candidates(self, job_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM workflow_candidates WHERE job_id = ? ORDER BY rank", (job_id,)
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                value = json.loads(row["payload"])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                result.append(value)
        return result

    def workflow_candidate(self, job_id: str, candidate_id: str) -> dict[str, Any] | None:
        return next((item for item in self.workflow_candidates(job_id) if item.get("id") == candidate_id), None)

    def latest_selected_workflow_job(self) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM workflow_jobs WHERE selected_candidate_id != '' ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        for key in ("region", "metadata"):
            try:
                value = json.loads(result.get(key) or "{}")
                result[key] = value if isinstance(value, dict) else {}
            except json.JSONDecodeError:
                result[key] = {}
        return result


    def delete_task(self, task_id: str) -> bool:
        """Delete one task and its dependent history, never its project files."""
        with self._lock, self._connection() as connection:
            exists = connection.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if exists is None:
                return False
            for table in ("messages", "events", "approvals", "checkpoints", "current_workflows"):
                connection.execute(f"DELETE FROM {table} WHERE task_id = ?", (task_id,))
            connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return True

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

    def save_node_scan(self, path: str, report: dict[str, Any]) -> dict[str, Any]:
        """Cache one node project's security report, keyed by its resolved path.

        Adoption is a one-time decision per project, so the report is kept for
        the history list: badges must not cost another scan (or model call) on
        every panel open.
        """
        if not isinstance(report, dict):
            raise ValueError("Node scan report must be a JSON object.")
        key = str(path)
        timestamp = _now()
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO node_scans(path,report,updated_at) VALUES(?,?,?)
                ON CONFLICT(path) DO UPDATE SET report = excluded.report, updated_at = excluded.updated_at
                """,
                (key, _json(report), timestamp),
            )
        return {"path": key, "report": report, "updated_at": timestamp}

    def node_scan(self, path: str) -> dict[str, Any] | None:
        with self._lock, self._connection() as connection:
            row = connection.execute("SELECT report,updated_at FROM node_scans WHERE path = ?", (str(path),)).fetchone()
        if row is None:
            return None
        try:
            report = json.loads(row["report"])
        except json.JSONDecodeError:
            return None
        return {"report": report if isinstance(report, dict) else {}, "updated_at": row["updated_at"]}

    def node_scans(self) -> dict[str, dict[str, Any]]:
        """Every cached report by path, so one list request stays a single query."""
        with self._lock, self._connection() as connection:
            rows = connection.execute("SELECT path,report,updated_at FROM node_scans").fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                report = json.loads(row["report"])
            except json.JSONDecodeError:
                continue
            if isinstance(report, dict):
                result[row["path"]] = {"report": report, "updated_at": row["updated_at"]}
        return result

    def clear_node_scans(self) -> int:
        with self._lock, self._connection() as connection:
            deleted = connection.execute("DELETE FROM node_scans").rowcount
        return int(deleted or 0)

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

    def last_event_seq(self, task_id: str) -> int:
        """Highest persisted event sequence for a task, or -1 when none exist."""
        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(seq), -1) FROM events WHERE task_id = ?", (task_id,)
            ).fetchone()
        return int(row[0]) if row else -1

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
