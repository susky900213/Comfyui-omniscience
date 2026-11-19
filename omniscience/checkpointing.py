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
