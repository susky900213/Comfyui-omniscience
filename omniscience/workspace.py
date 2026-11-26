"""Filesystem sandbox restricted to one resolved workspace directory.

DESIGN 5.3: the model only proposes edit intents; every write happens here.
Three edit entry points share the same guards: ``write_file`` (full file),
``apply_patch`` (contextual diff), and ``apply_editor_edit`` (precise
old_text/new_text replacement).  All writes are atomic and never follow a
path outside the workspace.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .constants import MAX_FILE_BYTES, MAX_LIST_ENTRIES, MAX_SEARCH_LINE_CHARS, MAX_SEARCH_RESULTS, MAX_WRITE_BYTES
from .util import safe_relative

IGNORED_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "__pycache__",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}


class Workspace:
    """Filesystem operations restricted to one resolved workspace directory."""

    def __init__(self, root: str | os.PathLike[str]):
        candidate = Path(root).expanduser().resolve()
        if not candidate.is_dir():
            raise ValueError(f"Workspace does not exist or is not a directory: {candidate}")
        self.root = candidate

    def resolve(self, relative_path: str) -> Path:
        relative = safe_relative(relative_path)
        resolved = (self.root / relative).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("Path must stay inside the workspace.") from exc
        return resolved

    def display_path(self, path: Path) -> str:
        return "." if path == self.root else path.relative_to(self.root).as_posix()

    def stays_inside(self, path: Path) -> bool:
        """Return whether a directory entry remains inside the workspace after resolving links."""

        try:
            path.resolve().relative_to(self.root)
            return True
        except (OSError, ValueError):
            return False

    def list_files(self, relative_path: str = ".", recursive: bool = False) -> list[dict[str, Any]]:
        directory = self.resolve(relative_path)
        if not directory.is_dir():
            raise ValueError(f"Not a directory: {relative_path}")
        entries: list[dict[str, Any]] = []
        iterator: Iterable[Path] = directory.rglob("*") if recursive else directory.iterdir()
        for item in sorted(iterator, key=lambda value: (not value.is_dir(), value.name.lower())):
            # A custom node is often installed as a symlink. Do not expose a
            # link whose target escapes the configured workspace: clicking it
            # would otherwise produce a confusing path-boundary error.
            if not self.stays_inside(item):
                continue
            if any(part in IGNORED_DIRECTORIES for part in item.relative_to(self.root).parts):
                continue
            try:
                is_dir = item.is_dir()
                entries.append(
                    {
                        "path": self.display_path(item),
                        "name": item.name,
                        "type": "directory" if is_dir else "file",
                        "size": None if is_dir else item.stat().st_size,
                    }
                )
            except OSError:
                continue
            if len(entries) >= MAX_LIST_ENTRIES:
                break
        return entries

    def read_file(self, relative_path: str) -> str:
        path = self.resolve(relative_path)
        if not path.is_file():
            raise ValueError(f"Not a file: {relative_path}")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(f"File is larger than the {MAX_FILE_BYTES} byte limit.")
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Only UTF-8 text files can be read by the assistant.") from exc

    def search_files(self, query: str, relative_path: str = ".") -> list[dict[str, Any]]:
        query = str(query or "")
        if not query:
            raise ValueError("Search query cannot be empty.")
        start = self.resolve(relative_path)
        candidates = [start] if start.is_file() else start.rglob("*")
        results: list[dict[str, Any]] = []
        for path in candidates:
            if len(results) >= MAX_SEARCH_RESULTS:
                break
            if not self.stays_inside(path):
                continue
            if not path.is_file() or any(part in IGNORED_DIRECTORIES for part in path.relative_to(self.root).parts):
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                if query.lower() in line.lower():
                    results.append(
                        {
                            "path": self.display_path(path),
                            "line": line_number,
                            "text": line[:MAX_SEARCH_LINE_CHARS],
                        }
                    )
                    if len(results) >= MAX_SEARCH_RESULTS:
                        break
        return results

    def write_file(self, relative_path: str, content: str) -> str:
        path = self.resolve(relative_path)
        encoded = str(content).encode("utf-8")
        if len(encoded) > MAX_WRITE_BYTES:
            raise ValueError(f"File is larger than the {MAX_WRITE_BYTES} byte limit.")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(path, str(content))
        return self.display_path(path)

    def apply_patch(self, relative_path: str, patch: str) -> str:
        path = self.resolve(relative_path)
        original = self.read_file(relative_path) if path.exists() else ""
        updated = apply_unified_patch(original, str(patch))
        return self._store(path, updated, "Patched file")

    def apply_editor_edit(
        self,
        relative_path: str,
        old_text: str | None = None,
        new_text: str | None = None,
        insert_line: int | None = None,
    ) -> str:
        """Precise edit entry point (DESIGN 5.3 borrow #1, Omniscience `editor`).

        Either replace a uniquely-occurring ``old_text`` with ``new_text``, or
        insert ``new_text`` at ``insert_line``.  A non-existent file is created
        with ``new_text``.  Ambiguous or missing matches are rejected without
        writing, so the model can re-read and retry deliberately.
        """
        path = self.resolve(relative_path)
        text = str(new_text or "")
        if path.exists() and insert_line is not None:
            lines = self.read_file(relative_path).splitlines(keepends=True)
            position = max(0, min(len(lines), int(insert_line)))
            insertion = text if text.endswith("\n") or not text else text + "\n"
            lines.insert(position, insertion)
            return self._store(path, "".join(lines), "Edited file")
        if path.exists() and old_text is not None and str(old_text):
            original = self.read_file(relative_path)
            occurrences = original.count(str(old_text))
            if occurrences == 0:
                raise ValueError("old_text was not found in the file; re-read it and match the exact content.")
            if occurrences > 1:
                raise ValueError("old_text matches multiple locations; add more surrounding context so the edit is unique.")
            return self._store(path, original.replace(str(old_text), text, 1), "Edited file")
        if not path.exists():
            return self._store(path, text, "Created file")
        raise ValueError("Provide old_text/new_text to edit or insert_line to insert.")

    def _store(self, path: Path, content: str, noun: str) -> str:
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_WRITE_BYTES:
            raise ValueError(f"{noun} is larger than the {MAX_WRITE_BYTES} byte limit.")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(path, content)
        return self.display_path(path)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as temporary:
            temporary.write(content)
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)


def apply_unified_patch(original: str, patch: str) -> str:
    """Apply a small unified diff, including the common Omniscience patch wrapper."""

    raw_lines = str(patch).replace("\r\n", "\n").splitlines(keepends=True)
    lines = [line for line in raw_lines if not line.startswith(("*** Begin Patch", "*** End Patch"))]
    lines = [line for line in lines if not line.startswith("*** Update File:")]
    hunk_indexes = [index for index, line in enumerate(lines) if line.startswith("@@")]
    if not hunk_indexes:
        raise ValueError("Patch must contain at least one unified-diff hunk starting with '@@'.")

    source = original.splitlines(keepends=True)
    output: list[str] = []
    source_position = 0
    for hunk_number, header_index in enumerate(hunk_indexes):
        header = lines[header_index]
        match = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", header)
        if not match:
            raise ValueError(f"Invalid unified diff hunk header: {header.rstrip()}")
        old_line = int(match.group(1))
        old_start = 0 if old_line == 0 else old_line - 1
        if old_start < source_position or old_start > len(source):
            raise ValueError("Patch context does not match the current file.")
        output.extend(source[source_position:old_start])
        end = hunk_indexes[hunk_number + 1] if hunk_number + 1 < len(hunk_indexes) else len(lines)
        for line in lines[header_index + 1 : end]:
            if line.startswith("\\ No newline at end of file"):
                continue
            if not line:
                raise ValueError("Malformed empty patch line.")
            marker, value = line[0], line[1:]
            if marker == " ":
                if source_position >= len(source) or source[source_position] != value:
                    raise ValueError("Patch context does not match the current file.")
                output.append(value)
                source_position += 1
            elif marker == "-":
                if source_position >= len(source) or source[source_position] != value:
                    raise ValueError("Patch removal does not match the current file.")
                source_position += 1
            elif marker == "+":
                output.append(value)
            else:
                raise ValueError(f"Unsupported patch line: {line.rstrip()}")
    output.extend(source[source_position:])
    return "".join(output)
