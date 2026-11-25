"""Shadow-Git checkpoints with a filesystem snapshot fallback."""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any

from .util import is_directory_junction, is_filesystem_link, remove_filesystem_link


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
                "GIT_AUTHOR_NAME": "Comfyui-omniscience",
                "GIT_AUTHOR_EMAIL": "comfyui-omniscience@localhost",
                "GIT_COMMITTER_NAME": "Comfyui-omniscience",
                "GIT_COMMITTER_EMAIL": "comfyui-omniscience@localhost",
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

        def ignore_snapshot_entries(source: str, names: list[str]) -> set[str]:
            ignored = {name for name in names if name in {".git", "__pycache__", ".omniscience-checkpoints"}}
            for name in names:
                candidate = Path(source) / name
                if is_directory_junction(candidate):
                    # Junctions are not reliably represented as symlinks by
                    # shutil on Windows. Skipping them prevents copytree from
                    # traversing into a linked directory outside the project.
                    ignored.add(name)
            return ignored

        shutil.copytree(
            root,
            destination,
            symlinks=True,
            ignore=ignore_snapshot_entries,
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
                if path.is_file() or is_filesystem_link(path):
                    if is_filesystem_link(path):
                        remove_filesystem_link(path)
                    else:
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
        def walk_tree(start: Path) -> list[Path]:
            found: list[Path] = []
            try:
                entries = list(start.iterdir())
            except OSError:
                return found
            for entry in entries:
                found.append(entry)
                if entry.is_dir() and not is_filesystem_link(entry):
                    found.extend(walk_tree(entry))
            return found

        snapshot_files = {path.relative_to(snapshot) for path in walk_tree(snapshot)}
        root_entries = walk_tree(root)
        # Remove every existing link before copytree. A Junction may occupy a
        # path that is a normal directory in the snapshot; leaving it in place
        # would make dirs_exist_ok follow the Junction and write outside root.
        for path in root_entries:
            if is_filesystem_link(path):
                remove_filesystem_link(path)
        for path in sorted(walk_tree(root), key=lambda value: len(value.parts), reverse=True):
            if ".git" in path.relative_to(root).parts:
                continue
            if path.relative_to(root) not in snapshot_files:
                if path.is_dir() and not is_filesystem_link(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    if is_filesystem_link(path):
                        remove_filesystem_link(path)
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
