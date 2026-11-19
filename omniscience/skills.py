"""SQLite-backed reusable prompt skills (kept outside the task database)."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from . import skill_security
from .util import now as _now


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
                    security TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS skills_updated ON skills(updated_at DESC);
                """
            )
            columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(skills)")}
            if "security" not in columns:
                connection.execute("ALTER TABLE skills ADD COLUMN security TEXT NOT NULL DEFAULT '{}'")

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
        security = value.get("security")
        if isinstance(security, str):
            try:
                security = json.loads(security or "{}")
            except json.JSONDecodeError:
                security = {}
        value["security"] = security if isinstance(security, dict) else {}
        return value

    @staticmethod
    def _security_report(values: dict[str, Any], *, source: str = "") -> dict[str, Any]:
        """Scan the prompt text that is about to be stored."""
        return skill_security.scan_values(values, source=source)

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

    def create(
        self,
        values: dict[str, Any],
        *,
        source: str = "",
        security: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = self._validated_values(values)
        report = security or self._security_report(normalized, source=source)
        skill_id = uuid.uuid4().hex
        timestamp = _now()
        try:
            with self._lock, self._connection() as connection:
                connection.execute(
                    "INSERT INTO skills(id,name,description,content,enabled,security,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        skill_id,
                        normalized["name"],
                        normalized["description"],
                        normalized["content"],
                        int(normalized["enabled"]),
                        json.dumps(report, ensure_ascii=False),
                        timestamp,
                        timestamp,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"Skill 名称已存在：{normalized['name']}") from exc
        return self.get(skill_id) or {}

    def update(
        self,
        skill_id: str,
        values: dict[str, Any],
        *,
        source: str = "",
        security: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        skill_id = str(skill_id)
        current = self.get(skill_id)
        if current is None:
            raise ValueError("Skill 不存在。")
        normalized = self._validated_values(values, partial=True)
        if not normalized:
            return current
        if any(key in normalized for key in ("name", "description", "content")):
            if security is None:
                merged = {**current, **normalized}
                security = self._security_report(
                    merged,
                    source=source or str(current.get("security", {}).get("source") or ""),
                )
            normalized["security"] = json.dumps(security, ensure_ascii=False)
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

    def rescan(self, skill_id: str, security: dict[str, Any] | None = None) -> dict[str, Any]:
        """Re-run the detector against a stored skill (used after rule updates)."""
        skill = self.get(skill_id)
        if skill is None:
            raise ValueError("Skill 不存在。")
        acknowledged = bool(skill.get("security", {}).get("acknowledged"))
        report = security or skill_security.scan_values(
            skill,
            source=str(skill.get("security", {}).get("source") or ""),
        )
        if acknowledged and report.get("verdict") == "blocked":
            report["acknowledged"] = True
        with self._lock, self._connection() as connection:
            connection.execute(
                "UPDATE skills SET security = ? WHERE id = ?",
                (json.dumps(report, ensure_ascii=False), str(skill_id)),
            )
        return self.get(skill_id) or {}

    def rescan_all(self, reports: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """Re-run the detector across the whole library after a rule update."""
        if reports is None:
            return [self.rescan(skill["id"]) for skill in self.list()]
        by_id = {str(report.get("id")): report.get("security") for report in reports}
        return [self.rescan(skill["id"], by_id.get(skill["id"])) for skill in self.list()]

    def delete(self, skill_id: str) -> dict[str, Any]:
        skill = self.get(skill_id)
        if skill is None:
            raise ValueError("Skill 不存在。")
        with self._lock, self._connection() as connection:
            connection.execute("DELETE FROM skills WHERE id = ?", (str(skill_id),))
        return skill

    def import_items(self, items: Any, *, acknowledged: bool = False) -> list[dict[str, Any]]:
        if isinstance(items, dict):
            items = items.get("skills")
        if not isinstance(items, list) or len(items) > 500:
            raise ValueError("导入内容必须是最多 500 个 Skill 的数组。")
        reports: list[dict[str, Any]] = []
        blocked: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("每个 Skill 必须是 JSON 对象。")
            report = skill_security.scan_values(item)
            if acknowledged and report["verdict"] == "blocked":
                report["acknowledged"] = True
            reports.append(report)
            if report["verdict"] == "blocked" and not acknowledged:
                blocked.append(str(item.get("name") or "未命名"))
        if blocked:
            raise ValueError(
                "安全检测阻止导入：" + "、".join(blocked[:5]) + "。请确认来源后勾选“仍然导入”。"
            )
        imported: list[dict[str, Any]] = []
        for item, report in zip(items, reports):
            normalized = self._validated_values(item)
            with self._lock, self._connection() as connection:
                existing = connection.execute(
                    "SELECT id FROM skills WHERE name = ? COLLATE NOCASE", (normalized["name"],)
                ).fetchone()
            if existing is None:
                imported.append(self.create(normalized, security=report))
            else:
                imported.append(self.update(existing["id"], normalized, security=report))
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
