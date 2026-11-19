"""Safe unpacking of node packages (DESIGN.md section 11.3).

A GitHub tarball or a user archive is untrusted input, so entries are inspected
one by one and only regular files and directories are written, always inside the
staging directory. Absolute paths, ``..`` traversal, symlinks, device files,
archive-set permissions and oversized payloads are all rejected. Nothing here
imports or executes the packaged code.
"""

from __future__ import annotations

import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from .constants import (
    MAX_NODE_PACKAGE_FILE_BYTES,
    MAX_NODE_PACKAGE_FILES,
    MAX_NODE_PACKAGE_UNCOMPRESSED_BYTES,
)

ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")
# Resource forks and VCS metadata are never part of an installed node.
IGNORED_ENTRIES = {"__macosx", ".ds_store", ".git", ".svn", ".hg"}
CHUNK_BYTES = 65536


class ArchiveError(ValueError):
    """Raised when an archive is malformed, oversized, or tries to escape staging."""


def looks_like_archive(name: Any) -> bool:
    return str(name or "").lower().endswith(ARCHIVE_SUFFIXES)


def _relative_path(name: Any) -> PurePosixPath | None:
    """Validate one entry name; ``None`` means the entry is skipped as noise."""
    value = str(name or "").replace("\\", "/")
    if "\x00" in value:
        raise ArchiveError("压缩包中存在非法文件名。")
    parts = [part for part in value.split("/") if part and part != "."]
    if not parts:
        return None
    if value.startswith("/") or ":" in parts[0] or ".." in parts:
        raise ArchiveError(f"压缩包中的路径尝试逃出安装目录：{value}")
    if any(part.lower() in IGNORED_ENTRIES for part in parts):
        return None
    return PurePosixPath(*parts)


class _Budget:
    """Cumulative file-count and byte caps across the whole archive."""

    def __init__(self) -> None:
        self.files = 0
        self.bytes = 0

    def begin_file(self, declared_size: int) -> None:
        self.files += 1
        if self.files > MAX_NODE_PACKAGE_FILES:
            raise ArchiveError(f"压缩包文件数超过 {MAX_NODE_PACKAGE_FILES} 个上限。")
        if int(declared_size or 0) > MAX_NODE_PACKAGE_FILE_BYTES:
            raise ArchiveError(f"压缩包内单个文件超过 {MAX_NODE_PACKAGE_FILE_BYTES} 字节上限。")

    def consume(self, size: int) -> None:
        self.bytes += size
        if self.bytes > MAX_NODE_PACKAGE_UNCOMPRESSED_BYTES:
            raise ArchiveError(f"压缩包解压后总大小超过 {MAX_NODE_PACKAGE_UNCOMPRESSED_BYTES} 字节上限。")


def _target(root: Path, relative: PurePosixPath) -> Path:
    target = root.joinpath(*relative.parts)
    if target.is_symlink():
        raise ArchiveError(f"拒绝写入已存在的符号链接：{relative}")
    return target


def _write(source: Any, target: Path, budget: _Budget) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as handle:
        while chunk := source.read(CHUNK_BYTES):
            budget.consume(len(chunk))
            handle.write(chunk)


def _iter_zip(package: zipfile.ZipFile) -> Iterator[tuple[PurePosixPath | None, bool, Any]]:
    for info in package.infolist():
        relative = _relative_path(info.filename)
        if relative is None:
            continue
        mode = info.external_attr >> 16
        is_directory = info.filename.endswith("/") or stat.S_ISDIR(mode)
        # Many writers store only permission bits, so an empty file-type field is
        # normal; anything that is not a regular file or directory is refused.
        if stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise ArchiveError(f"压缩包包含非常规文件（可能是符号链接）：{relative}")
        yield relative, is_directory, info


def _extract_zip(path: Path, root: Path, budget: _Budget) -> None:
    with zipfile.ZipFile(path) as package:
        for relative, is_directory, info in _iter_zip(package):
            target = _target(root, relative)
            if is_directory:
                target.mkdir(parents=True, exist_ok=True)
                continue
            budget.begin_file(getattr(info, "file_size", 0))
            with package.open(info) as source:
                _write(source, target, budget)


def _iter_tar(package: tarfile.TarFile) -> Iterator[tuple[PurePosixPath | None, bool, Any]]:
    for member in package:
        relative = _relative_path(member.name)
        if not member.isfile() and not member.isdir():
            # Symlinks, hardlinks, devices and fifos are refused outright: a node
            # project has no reason to ship them, and each one is an escape hatch.
            raise ArchiveError(f"节点压缩包包含非常规文件：{member.name}")
        if relative is None:
            continue
        yield relative, member.isdir(), member


def _extract_tar(path: Path, root: Path, budget: _Budget) -> None:
    with tarfile.open(path, "r:*") as package:
        for relative, is_directory, member in _iter_tar(package):
            target = _target(root, relative)
            if is_directory:
                target.mkdir(parents=True, exist_ok=True)
                continue
            budget.begin_file(getattr(member, "size", 0))
            source = package.extractfile(member)
            if source is None:
                continue
            with source:
                _write(source, target, budget)


def extract(archive: Path | str, destination: Path | str) -> dict[str, Any]:
    """Unpack *archive* into *destination* within the safety caps."""
    path = Path(archive)
    root = Path(destination)
    if not path.is_file():
        raise ArchiveError(f"压缩包不存在：{path}")
    root.mkdir(parents=True, exist_ok=True)
    budget = _Budget()
    try:
        if zipfile.is_zipfile(path):
            _extract_zip(path, root, budget)
        elif tarfile.is_tarfile(path):
            _extract_tar(path, root, budget)
        else:
            raise ArchiveError("不支持的压缩包格式，请使用 .zip、.tar、.tar.gz 或 .tar.xz。")
    except (ArchiveError, ValueError):
        raise
    except (zipfile.BadZipFile, tarfile.TarError, OSError) as exc:
        raise ArchiveError(f"压缩包无法解包：{exc}") from exc
    return {"files": budget.files, "bytes": budget.bytes}
