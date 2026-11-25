"""Validated, resumable model downloads and ComfyUI model symlinks."""

from __future__ import annotations

import hashlib
import errno
import re
import shutil
import time
from threading import Event
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .region_router import RegionRouter
from .util import create_filesystem_link, is_filesystem_link, validate_windows_path_component

MAX_MODEL_BYTES = 50 * 1024 * 1024 * 1024
MAX_TRANSIENT_RETRIES = 2
CATEGORIES = {"checkpoints", "vae", "loras", "controlnet", "clip", "unet", "diffusion_models", "upscale_models", "embeddings"}


class ModelDownloadError(ValueError):
    pass


class ModelDownloadRetryableError(ModelDownloadError):
    """Raised when a transient network failure leaves a resumable partial file."""


class ModelDownloadCancelled(ModelDownloadError):
    """Raised when a caller requests cancellation of a resumable download."""


class ModelDownloader:
    def __init__(self, settings: Any, *, opener: Any = urlopen):
        self.settings = settings
        self.opener = opener
        self.router = RegionRouter(settings)

    def _data(self) -> dict[str, Any]:
        return self.settings.load() if hasattr(self.settings, "load") else dict(self.settings or {})

    def download(
        self,
        *,
        repo_id: str,
        filename: str,
        category: str,
        cache_root: Path,
        expected_sha256: str = "",
        source_url: str = "",
        on_progress: Any = None,
        cancel_event: Event | None = None,
    ) -> dict[str, Any]:
        self._raise_if_cancelled(cancel_event)
        repo_id = str(repo_id or "").strip()
        filename = str(filename or "").strip().replace("\\", "/")
        category = str(category or "").strip()
        if not repo_id or "/" not in repo_id or any(part in {"", ".", ".."} for part in repo_id.split("/")):
            raise ModelDownloadError("Hugging Face repo_id 无效。")
        try:
            for component in repo_id.split("/"):
                validate_windows_path_component(component, "Hugging Face 仓库名")
        except ValueError as exc:
            raise ModelDownloadError(str(exc)) from exc
        if not filename or filename.startswith("/") or ".." in Path(filename).parts:
            raise ModelDownloadError("模型文件名无效。")
        try:
            for component in filename.split("/"):
                validate_windows_path_component(component, "模型文件名")
        except ValueError as exc:
            raise ModelDownloadError(str(exc)) from exc
        if category not in CATEGORIES:
            raise ModelDownloadError(f"不支持的模型目录类别：{category}")
        endpoint, decision = self.router.endpoint("huggingface")
        cache_dir = (Path(cache_root).expanduser().resolve() / repo_id / filename).resolve()
        root = Path(cache_root).expanduser().resolve()
        cache_dir.relative_to(root)
        cache_dir.parent.mkdir(parents=True, exist_ok=True)
        if cache_dir.is_file():
            self._verify(cache_dir, expected_sha256)
        else:
            parsed = urlsplit(endpoint)
            base = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
            download_url = f"{base}/{quote(repo_id, safe='/')}/resolve/main/{quote(filename, safe='/')}?download=true"
            candidate_url = str(source_url or "").strip()
            if candidate_url:
                candidate = urlsplit(candidate_url)
                expected_path = f"/{quote(repo_id, safe='/')}/resolve/"
                if (
                    candidate.scheme in {"http", "https"}
                    and candidate.scheme == parsed.scheme
                    and candidate.netloc == parsed.netloc
                    and candidate.path.startswith(expected_path)
                    and Path(unquote(candidate.path.rsplit("/", 1)[-1])).name == Path(filename).name
                ):
                    download_url = candidate_url
            self._download_file(
                download_url, cache_dir, expected_sha256, on_progress=on_progress, cancel_event=cancel_event
            )
        self._raise_if_cancelled(cancel_event)
        return {"path": str(cache_dir), "repo_id": repo_id, "filename": filename, "category": category, "region": decision.selected_region}

    @staticmethod
    def _raise_if_cancelled(cancel_event: Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise ModelDownloadCancelled("模型下载已取消；已保留部分文件，可重试并从断点继续。")

    def _download_file(
        self, url: str, destination: Path, expected_sha256: str, *, on_progress: Any = None,
        cancel_event: Event | None = None,
    ) -> None:
        """Stream a file to a .part file, resuming with HTTP Range when possible."""
        partial = destination.with_name(destination.name + ".part")
        retried_without_range = False
        transient_retries = 0
        while True:
            self._raise_if_cancelled(cancel_event)
            offset = partial.stat().st_size if partial.is_file() else 0
            headers = {
                "Accept": "application/octet-stream",
                "User-Agent": "Comfyui-omniscience-Model-Downloader/1.0",
            }
            if offset:
                headers["Range"] = f"bytes={offset}-"
            request = Request(url, headers=headers)
            try:
                response = self.opener(request, timeout=60)
            except HTTPError as exc:
                if exc.code == 416 and offset and not retried_without_range:
                    partial.unlink(missing_ok=True)
                    retried_without_range = True
                    continue
                raise ModelDownloadError(f"模型文件下载请求失败：HTTP {exc.code} {exc.reason}") from exc
            except TimeoutError as exc:
                if transient_retries < MAX_TRANSIENT_RETRIES:
                    transient_retries += 1
                    time.sleep(transient_retries)
                    continue
                raise ModelDownloadRetryableError(
                    f"模型文件下载暂时中断（已保留可续传的部分文件）：{exc}"
                ) from exc
            except (URLError, OSError) as exc:
                raise ModelDownloadError(f"模型文件下载失败（已保留可续传的部分文件）：{exc}") from exc

            try:
                with response:
                    self._raise_if_cancelled(cancel_event)
                    status = getattr(response, "status", None) or response.getcode()
                    if status not in {200, 206}:
                        raise ModelDownloadError(f"模型文件下载失败：HTTP {status}")
                    append = status == 206
                    if append:
                        content_range = response.headers.get("Content-Range", "")
                        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", content_range.strip())
                        if not match or int(match.group(1)) != offset or int(match.group(2)) < offset:
                            raise ModelDownloadError("模型服务器返回了无效的 Content-Range，已中止续传。")
                        total = None if match.group(3) == "*" else int(match.group(3))
                        if total is not None and total > MAX_MODEL_BYTES:
                            partial.unlink(missing_ok=True)
                            raise ModelDownloadError("模型文件超过 50 GB 限制。")
                        expected_response_bytes = int(match.group(2)) - offset + 1
                        mode = "ab"
                    else:
                        offset = 0
                        total = None
                        expected_response_bytes = None
                        mode = "wb"

                    content_length = response.headers.get("Content-Length")
                    if content_length is not None:
                        expected_response_bytes = int(content_length)
                        if not append:
                            total = expected_response_bytes
                    if expected_response_bytes is not None and offset + expected_response_bytes > MAX_MODEL_BYTES:
                        partial.unlink(missing_ok=True)
                        raise ModelDownloadError("模型文件超过 50 GB 限制。")

                    received = 0
                    if callable(on_progress):
                        on_progress(offset, total)
                    with partial.open(mode) as output:
                        while True:
                            self._raise_if_cancelled(cancel_event)
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            received += len(chunk)
                            if offset + received > MAX_MODEL_BYTES:
                                partial.unlink(missing_ok=True)
                                raise ModelDownloadError("模型文件超过 50 GB 限制。")
                            output.write(chunk)
                            if callable(on_progress):
                                on_progress(offset + received, total)
                    if expected_response_bytes is not None and received != expected_response_bytes:
                        raise ModelDownloadError(
                            f"模型文件未完整下载（本次收到 {received} / {expected_response_bytes} 字节）；再次尝试可续传。"
                        )
                    if total is not None and partial.stat().st_size != total:
                        raise ModelDownloadError("模型文件未完整下载；再次尝试可续传。")
            except ModelDownloadError:
                raise
            except TimeoutError as exc:
                if transient_retries < MAX_TRANSIENT_RETRIES:
                    transient_retries += 1
                    time.sleep(transient_retries)
                    continue
                raise ModelDownloadRetryableError(
                    f"模型文件下载暂时中断（已保留可续传的部分文件）：{exc}"
                ) from exc
            except (HTTPError, URLError, OSError, ValueError) as exc:
                raise ModelDownloadError(f"模型文件下载失败（已保留可续传的部分文件）：{exc}") from exc
            break

        try:
            self._verify(partial, expected_sha256)
        except ModelDownloadError:
            partial.unlink(missing_ok=True)
            raise
        partial.replace(destination)

    def link_to_comfyui(
        self,
        model_path: Path | str,
        *,
        category: str,
        models_root: Path | str,
        target_root: Path | str | None = None,
        target_name: str | None = None,
    ) -> dict[str, Any]:
        """Link a downloaded model into a registered ComfyUI folder.

        ``target_root`` is the first folder registered for the category.  The
        legacy ``models/<category>`` layout remains the fallback when no
        registered folder is available.
        """
        if category not in CATEGORIES:
            raise ModelDownloadError(f"不支持的模型目录类别：{category}")
        source = Path(model_path).expanduser().resolve()
        if not source.is_file():
            raise ModelDownloadError(f"模型文件不存在：{source}")
        link_name = str(target_name or source.name).strip().replace("\\", "/")
        if not link_name or Path(link_name).name != link_name or ".." in Path(link_name).parts:
            raise ModelDownloadError("模型目标文件名无效。")
        try:
            validate_windows_path_component(link_name, "模型目标文件名")
        except ValueError as exc:
            raise ModelDownloadError(str(exc)) from exc
        destination_root = (
            Path(target_root).expanduser().resolve()
            if target_root is not None
            else (Path(models_root).expanduser().resolve() / category).resolve()
        )
        destination_root.mkdir(parents=True, exist_ok=True)
        target = destination_root / link_name
        if target.exists() or is_filesystem_link(target):
            if is_filesystem_link(target) and target.resolve(strict=False) == source:
                return {"path": str(target), "created": False}
            if target.is_file() and self._sha256(target) == self._sha256(source):
                return {"path": str(target), "created": False, "same_content": True}
            raise ModelDownloadError(f"模型目标已存在且内容不同，不覆盖：{target}")
        try:
            create_filesystem_link(target, source)
            return {"path": str(target), "created": True}
        except OSError as exc:
            # Windows hard links cannot cross volumes. A copied model is still
            # safer and more useful than failing an otherwise valid download;
            # preserve the result shape and tell callers that it is independent.
            if getattr(exc, "winerror", None) != 17 and getattr(exc, "errno", None) != errno.EXDEV:
                raise
            try:
                shutil.copy2(source, target)
            except OSError:
                target.unlink(missing_ok=True)
                raise
            return {"path": str(target), "created": True, "link_type": "copy"}

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def _verify(cls, path: Path, expected_sha256: str) -> None:
        if path.stat().st_size > MAX_MODEL_BYTES:
            raise ModelDownloadError("模型文件超过 50 GB 限制。")
        expected = str(expected_sha256 or "").strip().lower()
        if expected and cls._sha256(path) != expected:
            raise ModelDownloadError("模型 SHA-256 校验失败。")
