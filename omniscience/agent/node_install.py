"""Install a ComfyUI custom node project from GitHub or an archive (DESIGN 11.3).

A package -- public repository tarball or user-uploaded archive -- is staged in a
private temporary directory and scanned with the same node security engine that
gates adoption, so nothing reaches ``ComfyUI/custom_nodes`` before a report
exists. Only files are moved into place: ``requirements.txt`` is never installed,
``setup.py`` and bundled scripts are never run, and ``scripts_executed`` is
always ``False``.

A non-clean report refuses the install by default. ``acknowledged`` can only be
set by the panel after the user confirms it; the model that asked for the install
cannot authorise its own bypass. Installing also does not adopt the project as a task --
the node shows up as a 待接管 card with its badge already cached, and the user
decides whether to work on it.
"""

from __future__ import annotations

import asyncio
import ast
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .. import archives, node_review, node_security, paths, skill_review, skill_security
from ..constants import MAX_NODE_PACKAGE_BYTES
from ..util import ProjectFolderExistsError, project_folder_name
from ..workspace import Workspace

# GitHub hands the tarball to us through its own blob host, so redirects are
# followed only between these HTTPS hosts.
TARBALL_HOSTS = {"api.github.com", "codeload.github.com", "objects.githubusercontent.com", "github.com"}
GITEE_HOSTS = {"gitee.com", "www.gitee.com"}
DOWNLOAD_TIMEOUT_SECONDS = 60
MAX_REPO_INFO_BYTES = 1_000_000
VERDICT_LABELS = {"clean": "未发现风险", "review": "需要复核", "blocked": "已被阻止"}
# Offering the install tool has to be the user's own request, with a source in it.
NODE_REQUEST_PATTERN = re.compile(r"节点|custom[\s_-]*nodes?|comfyui[\s_-]*node", re.IGNORECASE)
NODE_SOURCE_PATTERN = re.compile(
    r"https?://|\.[a-z0-9_.-]*?(?:zip|tar\.gz|tar\.bz2|tar\.xz|tgz|tbz2|txz|tar)\b|[\w.-]+/[\w.-]+",
    re.IGNORECASE,
)
INSTALL_NOTE = (
    "只安装了仓库文件：依赖不会自动安装，自带脚本也没有执行。"
    "如需第三方包请自行 pip 安装，然后重启 ComfyUI 才会加载节点。"
)


class NodeSecurityBlockedError(ValueError):
    """Raised when the security scan refuses a node package."""

    def __init__(self, folder: str, report: dict[str, Any]):
        headline = "安全检测发现需复核或阻止的风险" if report.get("verdict") == "review" else "安全检测阻止安装"
        super().__init__(
            f"{headline}节点“{folder}”：{skill_security.summarize(report)} "
            f"{skill_security.findings_text(report, limit=3)} "
            "风险节点不会通过工作流依赖自动安装。请在节点管理区单独查看报告，必要时人工复核。"
        )
        self.folder = folder
        self.report = report


class _TarballRedirectHandler(HTTPRedirectHandler):
    """Follow node-package redirects only between GitHub-owned HTTPS hosts."""

    def redirect_request(self, request, response, code, message, headers, new_url):
        parsed = urlparse(new_url)
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").lower() not in TARBALL_HOSTS
            or parsed.port not in {None, 443}
        ):
            raise ValueError("GitHub 下载尝试跳转到不受支持的地址，已阻止安装。")
        return super().redirect_request(request, response, code, message, headers, new_url)


class _GiteeRedirectHandler(HTTPRedirectHandler):
    """Follow Gitee archive redirects only to Gitee HTTPS hosts."""

    def redirect_request(self, request, response, code, message, headers, new_url):
        parsed = urlparse(new_url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in GITEE_HOSTS or parsed.port not in {None, 443}:
            raise ValueError("Gitee 下载尝试跳转到不受支持的地址，已阻止安装。")
        return super().redirect_request(request, response, code, message, headers, new_url)


def archive_stem(name: Any) -> str:
    """The folder name an archive implies, without its (possibly compound) suffix."""
    value = str(name or "")
    lowered = value.lower()
    for suffix in sorted(archives.ARCHIVE_SUFFIXES, key=len, reverse=True):
        if lowered.endswith(suffix) and len(value) > len(suffix):
            return value[: len(value) - len(suffix)]
    return Path(value).stem


class NodeInstallerMixin:
    """Provides ``install_comfyui_node`` to the global conversation and the panel."""

    async def install_comfyui_node(
        self,
        source: Any,
        *,
        acknowledged: bool = False,
        pin_commit: bool = False,
        expected_class_type: str = "",
    ) -> dict[str, Any]:
        """Install a node project from a public GitHub/Gitee link or uploaded archive."""
        value = str(source or "").strip()
        if not value:
            raise ValueError("请提供 GitHub/Gitee 仓库地址或节点压缩包。")
        if archives.looks_like_archive(value):
            if pin_commit:
                raise ValueError("工作流节点必须使用可固定版本的 GitHub/Gitee 仓库来源，不能使用可变压缩包链接。")
            return await self.install_workspace_archive(value, acknowledged=acknowledged)
        host, owner, repository, prefix, ref = self._node_repository_source(value)
        resolved_ref = ref or await asyncio.to_thread(self._repository_default_branch, host, owner, repository)
        if pin_commit and not re.fullmatch(r"[0-9a-fA-F]{40}", resolved_ref):
            raise ValueError("节点安装必须提供已固定的 40 位 Git commit 哈希。")
        package = await asyncio.to_thread(self._repository_archive, host, owner, repository, resolved_ref)
        source_url = f"https://{host}/{owner}/{repository}/tree/{resolved_ref}"
        if prefix:
            source_url = f"{source_url}/{prefix}"
        return await self._install_package(
            package,
            archive_name=f"{repository}.zip" if host in GITEE_HOSTS else f"{repository}.tar.gz",
            folder=prefix.split("/")[-1] if prefix else repository,
            source=source_url,
            subdirectory=prefix,
            acknowledged=acknowledged,
            expected_class_type=expected_class_type,
        )

    async def install_workspace_archive(self, relative_path: Any, *, acknowledged: bool = False) -> dict[str, Any]:
        """Install a node archive the user uploaded into the global workspace."""
        workspace = Workspace(paths.global_workspace())
        path = workspace.resolve(str(relative_path or ""))
        if not path.is_file():
            raise ValueError(f"没有找到上传的压缩包：{relative_path}")
        if not archives.looks_like_archive(path.name):
            raise ValueError("节点安装包必须是 .zip 或 .tar.gz 压缩包。")
        if path.stat().st_size > MAX_NODE_PACKAGE_BYTES:
            raise ValueError(f"节点压缩包超过 {MAX_NODE_PACKAGE_BYTES} 字节上限。")
        package = await asyncio.to_thread(path.read_bytes)
        return await self._install_package(
            package,
            archive_name=path.name,
            folder=archive_stem(path.name),
            source=f"archive:{path.name}",
            acknowledged=acknowledged,
        )

    async def install_archive_bytes(
        self,
        *,
        filename: Any,
        package: Any,
        acknowledged: bool = False,
    ) -> dict[str, Any]:
        """Install a node archive the panel submitted directly as base64."""
        name = str(filename or "")
        if not archives.looks_like_archive(name):
            raise ValueError("节点压缩包文件名无效，需要 .zip 或 .tar.gz 等压缩包后缀。")
        data = bytes(package or b"")
        if len(data) > MAX_NODE_PACKAGE_BYTES:
            raise ValueError(f"节点压缩包超过 {MAX_NODE_PACKAGE_BYTES} 字节上限。")
        return await self._install_package(
            data,
            archive_name=name,
            folder=archive_stem(name),
            source=f"archive:{name}",
            acknowledged=acknowledged,
        )

    async def _install_package(
        self,
        package: bytes,
        *,
        archive_name: str,
        folder: str,
        source: str,
        subdirectory: str = "",
        acknowledged: bool = False,
        expected_class_type: str = "",
    ) -> dict[str, Any]:
        if not package:
            raise ValueError("节点压缩包内容为空。")
        if len(package) > MAX_NODE_PACKAGE_BYTES:
            raise ValueError(f"节点压缩包超过 {MAX_NODE_PACKAGE_BYTES} 字节上限。")
        nodes = self.settings.comfyui_node_directory()
        if nodes is None:
            raise ValueError("请先在设置里填写 ComfyUI 目录，才能安装节点。")
        staging = Path(tempfile.mkdtemp(prefix="omniscience-node-"))
        try:
            archive_path = staging / Path(archive_name).name
            unpacked = staging / "package"
            await asyncio.to_thread(archive_path.write_bytes, package)
            stats = await asyncio.to_thread(archives.extract, archive_path, unpacked)
            root = await asyncio.to_thread(
                self._locate_node_package, unpacked, subdirectory=subdirectory, folder=folder
            )
            if expected_class_type and not await asyncio.to_thread(
                self._exports_node_class, root, expected_class_type
            ):
                raise ValueError(
                    f"仓库 {source} 中未能静态确认导出工作流所需节点“{expected_class_type}”，已拒绝安装。"
                )
            target = nodes / root.name
            # Scan before anything lands in custom_nodes, and re-check the name
            # right before the move: the review step can take a model round-trip.
            report = await self._review_package(root, source=source, acknowledged=acknowledged)
            placed = await asyncio.to_thread(self._place_package, root, target, report)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return {
            "status": "installed",
            "folder": target.name,
            "path": str(target),
            "source": source,
            "files_installed": placed,
            "package_bytes": len(package),
            "uncompressed_bytes": stats["bytes"],
            "security": {
                "verdict": report.get("verdict"),
                "severity": report.get("severity"),
                "engine": report.get("engine"),
                "mode": report.get("mode"),
                "summary": VERDICT_LABELS.get(str(report.get("verdict")), report.get("verdict")),
                "findings": (report.get("findings") or [])[:6],
                "domains": (report.get("domains") or [])[:12],
                "acknowledged": bool(report.get("acknowledged")),
            },
            "dependencies": (report.get("dependencies") or [])[:12],
            "requires_restart": True,
            "adopted": False,
            "scripts_executed": False,
            "note": INSTALL_NOTE,
        }

    async def _review_package(self, root: Path, *, source: str, acknowledged: bool) -> dict[str, Any]:
        static = await asyncio.to_thread(node_security.scan_project, root, source=source)
        mode = node_review.review_mode(self.settings)
        if mode == "static":
            report = static
        else:
            reviewed = await node_review.review(static, settings=self.settings)
            if mode == "hybrid" or reviewed.get("fallback"):
                report = reviewed
            else:
                # AI-only mode must not erase static findings; the stricter
                # verdict wins, even if the model overlooks a static match.
                report = skill_review.merge(static, reviewed, ai_engine=node_review.AI_ENGINE)
        report["source"] = source
        if report.get("verdict") != "clean":
            if not acknowledged:
                raise NodeSecurityBlockedError(root.name, report)
            report["acknowledged"] = True
        return report

    def _place_package(self, root: Path, target: Path, report: dict[str, Any]) -> int:
        if target.exists() or target.is_symlink():
            raise ProjectFolderExistsError(
                f"ComfyUI custom_nodes 中已存在：{target.name}。已拒绝覆盖现有项目，请改名后再安装。"
            )
        try:
            shutil.move(str(root), str(target))
        except (OSError, shutil.Error) as exc:
            raise ValueError(f"节点安装失败，未写入 custom_nodes：{exc}") from exc
        self.store.save_node_scan(str(target.resolve()), report)
        return sum(1 for item in target.rglob("*") if item.is_file())

    @staticmethod
    def _locate_node_package(unpacked: Path, *, subdirectory: str = "", folder: str = "") -> Path:
        """Find the installable node directory inside a freshly unpacked package."""
        if not unpacked.is_dir():
            raise ValueError("压缩包解包后为空。")
        entries = [item for item in sorted(unpacked.iterdir()) if not item.is_symlink()]
        root = unpacked
        if subdirectory:
            relative = PurePosixPath(subdirectory)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("节点文件夹路径无效。")
            root = root.joinpath(*relative.parts)
            if not root.is_dir():
                raise ValueError(f"仓库中没有找到文件夹：{subdirectory}")
        else:
            directories = [item for item in entries if item.is_dir()]
            files = [item for item in entries if item.is_file()]
            # GitHub tarballs and most node archives wrap everything in one folder.
            if len(directories) == 1 and not files:
                root = directories[0]
        if not (root / "__init__.py").is_file():
            raise ValueError(
                "该包顶层没有 __init__.py，不是可安装的 ComfyUI 自定义节点项目；请指向真正的节点文件夹或节点压缩包。"
            )
        name = project_folder_name(folder or root.name)
        # ComfyUI imports the folder as a Python package, so a space in the name
        # would install something that cannot load.
        if any(character.isspace() for character in name):
            raise ValueError("节点文件夹名称不能包含空格，请改用 GitHub 链接或重命名后再上传。")
        if name != root.name:
            # The install location is named after the source, not the wrapper folder.
            renamed = root.parent / name
            root.rename(renamed)
            root = renamed
        return root

    @staticmethod
    def _exports_node_class(root: Path, class_type: str) -> bool:
        """Statically verify a GitHub package maps the workflow's exact node name."""
        expected = str(class_type or "").strip()
        if not expected or len(expected) > 160:
            return False
        scanned_bytes = 0
        scanned_files = 0
        for path in sorted(root.rglob("*.py")):
            if path.is_symlink() or not path.is_file():
                continue
            scanned_files += 1
            if scanned_files > 400:
                return False
            try:
                if path.stat().st_size > 200_000:
                    continue
                scanned_bytes += path.stat().st_size
                if scanned_bytes > 2_000_000:
                    return False
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, SyntaxError):
                continue
            for node in tree.body:
                value = None
                if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "NODE_CLASS_MAPPINGS"
                    for target in node.targets
                ):
                    value = node.value
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "NODE_CLASS_MAPPINGS":
                    value = node.value
                if isinstance(value, ast.Dict) and any(
                    isinstance(key, ast.Constant) and key.value == expected for key in value.keys
                ):
                    return True
        return False

    @staticmethod
    def _explicit_node_install_request(content: Any) -> bool:
        """Whether the user asked, in their own words, to install a node project."""
        request = str(content or "").casefold()
        install_requested = bool(
            re.search(r"安装|装一下|装个|下载|添加|导入|\b(?:install|download|add|import)\b", request)
        )
        node_requested = bool(NODE_REQUEST_PATTERN.search(request))
        negated = bool(
            re.search(
                r"(?:不要|无需|不需要|别|禁止|不能|无法|没法).{0,24}(?:安装|下载|添加|导入)|"
                r"(?:don't|dont|do not|no need to|cannot|can't|unable to).{0,32}(?:install|download|add|import)",
                request,
            )
        )
        return bool(install_requested and node_requested and not negated and NODE_SOURCE_PATTERN.search(request))

    @classmethod
    def _requested_node_source(cls, content: Any) -> str | None:
        """The node source the user actually named: repo URL, archive, then ``owner/repo``."""
        text = str(content or "")
        url = re.search(r"https://(?:github\.com|gitee\.com)/[A-Za-z0-9._~:/?#\[\]!$&'()*+,;=%@-]+", text, re.IGNORECASE)
        if url:
            source = url.group(0).rstrip(".,;:!?)\"]")
            try:
                cls._node_repository_source(source)
            except (TypeError, ValueError):
                return None
            return source
        archive = re.search(
            r"(?:^|[\s@\"])([^\s@\"]+?\.(?:zip|tar\.gz|tar\.bz2|tar\.xz|tgz|tbz2|txz|tar))(?=$|[\s\",])",
            text,
            re.IGNORECASE,
        )
        if archive:
            return archive.group(1).lstrip("@")
        shorthand = re.search(
            r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?![A-Za-z0-9_.-])",
            text,
        )
        if not shorthand:
            return None
        return f"https://github.com/{shorthand.group(1)}/{shorthand.group(2)}"

    @classmethod
    def _install_node_source_allowed(cls, content: Any, source: Any) -> bool:
        """The tool argument must be the source the user gave, never one the model chose."""
        if not cls._explicit_node_install_request(content):
            return False
        explicit = cls._requested_node_source(content)
        if not explicit:
            return False
        proposed = str(source or "").strip()
        if archives.looks_like_archive(explicit) or archives.looks_like_archive(proposed):
            return explicit.strip() == proposed
        try:
            explicit_source = cls._node_repository_source(explicit)
            proposed_source = cls._node_repository_source(proposed)
        except (TypeError, ValueError):
            return False
        explicit_host, explicit_owner, explicit_repo, explicit_prefix, explicit_ref = explicit_source
        proposed_host, proposed_owner, proposed_repo, proposed_prefix, proposed_ref = proposed_source
        return (
            explicit_host == proposed_host
            and explicit_owner.casefold() == proposed_owner.casefold()
            and explicit_repo.casefold() == proposed_repo.casefold()
            and (not explicit_prefix or explicit_prefix == proposed_prefix)
            and (explicit_ref is None or explicit_ref == proposed_ref)
        )

    @staticmethod
    def _github_node_source(source: Any) -> tuple[str, str, str, str | None]:
        """Validate a public GitHub repository or ``tree`` folder link."""
        host, owner, repository, prefix, ref = NodeInstallerMixin._node_repository_source(source)
        if host != "github.com":
            raise ValueError("请提供公开 GitHub HTTPS 仓库或节点文件夹链接。")
        return owner, repository, prefix, ref

    @staticmethod
    def _node_repository_source(source: Any) -> tuple[str, str, str, str, str | None]:
        """Validate a public GitHub/Gitee repository or ``tree`` folder link."""
        value = str(source or "").strip()
        parsed = urlparse(value)
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or host not in {"github.com", "gitee.com"}
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
            or len(parts) < 2
        ):
            raise ValueError("请提供公开 GitHub/Gitee HTTPS 仓库或节点文件夹链接。")
        owner, repository = parts[0], parts[1].removesuffix(".git")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repository):
            raise ValueError("GitHub/Gitee 仓库地址无效。")
        ref: str | None = None
        prefix = ""
        if len(parts) > 2:
            if parts[2] != "tree" or len(parts) < 4:
                raise ValueError("链接应指向 GitHub/Gitee 仓库或仓库内的节点文件夹（tree 链接）。")
            ref = parts[3]
            prefix = "/".join(parts[4:]).strip("/")
        if any(part in {".", ".."} for part in prefix.split("/") if part):
            raise ValueError("节点文件夹路径无效。")
        return host, owner, repository, prefix, ref

    @classmethod
    def _repository_default_branch(cls, host: str, owner: str, repository: str) -> str:
        if host == "github.com":
            return cls._github_default_branch(owner, repository)
        try:
            info = cls._gitee_json(f"https://gitee.com/api/v5/repos/{owner}/{repository}")
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            raise ValueError(f"无法读取公开 Gitee 仓库：{exc}") from exc
        ref = str((info or {}).get("default_branch") or "").strip() if isinstance(info, dict) else ""
        if not ref:
            raise ValueError("Gitee 仓库没有可用的默认分支。")
        return ref

    @classmethod
    def _repository_commit(cls, host: str, owner: str, repository: str, ref: str) -> str:
        if host == "github.com":
            return cls._github_commit(owner, repository, ref)
        try:
            info = cls._gitee_json(
                f"https://gitee.com/api/v5/repos/{owner}/{repository}/commits/{quote(ref, safe='')}"
            )
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            raise ValueError(f"无法固定 Gitee 节点版本：{exc}") from exc
        sha = str((info or {}).get("sha") or "").strip() if isinstance(info, dict) else ""
        if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
            raise ValueError("Gitee 没有返回有效提交哈希，已拒绝安装可变版本。")
        return sha

    @staticmethod
    def _gitee_json(url: str) -> Any:
        from ..node_search import GiteeNodeSearcher

        return GiteeNodeSearcher._request(url)

    @classmethod
    def _repository_archive(cls, host: str, owner: str, repository: str, ref: str) -> bytes:
        if host == "github.com":
            return cls._github_tarball(owner, repository, ref)
        url = f"https://gitee.com/{quote(owner, safe='')}/{quote(repository, safe='')}/repository/archive/{quote(ref, safe='')}.zip"
        request = Request(url, headers={"Accept": "application/zip", "User-Agent": "Comfyui-omniscience-Node-Installer"})
        try:
            with build_opener(_GiteeRedirectHandler()).open(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:  # noqa: S310 - host-validated Gitee URL
                final_host = (urlparse(response.geturl()).hostname or "").lower()
                if final_host not in GITEE_HOSTS:
                    raise ValueError("Gitee 下载被重定向到不受支持的主机，已拒绝安装。")
                package = response.read(MAX_NODE_PACKAGE_BYTES + 1)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise ValueError(f"无法下载 Gitee 节点仓库：{exc}") from exc
        if len(package) > MAX_NODE_PACKAGE_BYTES:
            raise ValueError(f"Gitee 节点压缩包超过 {MAX_NODE_PACKAGE_BYTES} 字节上限。")
        return package

    @staticmethod
    def _github_json(url: str) -> Any:
        from .github_skill import GitHubSkillInstaller

        return GitHubSkillInstaller._github_request(url, max_bytes=MAX_REPO_INFO_BYTES)

    @classmethod
    def _github_default_branch(cls, owner: str, repository: str) -> str:
        try:
            info = cls._github_json(f"https://api.github.com/repos/{owner}/{repository}")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise ValueError(f"无法读取公开 GitHub 仓库：{exc}") from exc
        ref = str((info or {}).get("default_branch") or "").strip() if isinstance(info, dict) else ""
        if not ref:
            raise ValueError("GitHub 仓库没有可用的默认分支。")
        return ref

    @classmethod
    def _github_commit(cls, owner: str, repository: str, ref: str) -> str:
        """Resolve a branch/tag to an immutable commit before a gated install."""
        try:
            info = cls._github_json(
                f"https://api.github.com/repos/{owner}/{repository}/commits/{quote(ref, safe='')}"
            )
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            raise ValueError(f"无法固定 GitHub 节点版本：{exc}") from exc
        sha = str((info or {}).get("sha") or "").strip() if isinstance(info, dict) else ""
        if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
            raise ValueError("GitHub 没有返回有效提交哈希，已拒绝安装可变版本。")
        return sha

    @classmethod
    def _node_tarball(cls, owner: str, repository: str, ref: str) -> bytes:
        """Try GitHub first, then the same owner/repo on Gitee for slow/blocked GitHub links."""
        try:
            return cls._github_tarball(owner, repository, ref)
        except (ValueError, HTTPError, URLError, TimeoutError, OSError) as github_error:
            url = f"https://gitee.com/{quote(owner, safe='')}/{quote(repository, safe='')}/repository/archive/{quote(ref, safe='')}.zip"
            request = Request(
                url,
                headers={
                    "Accept": "application/zip",
                    "User-Agent": "Comfyui-omniscience-Node-Installer",
                },
            )
            try:
                with build_opener().open(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:  # noqa: S310 - fixed Gitee host
                    final_host = (urlparse(response.geturl()).hostname or "").lower()
                    if final_host not in {"gitee.com", "www.gitee.com"}:
                        raise ValueError("Gitee 下载被重定向到不受支持的主机，已拒绝安装。")
                    package = response.read(MAX_NODE_PACKAGE_BYTES + 1)
            except (HTTPError, URLError, TimeoutError, OSError) as gitee_error:
                raise ValueError(f"GitHub 和 Gitee 节点仓库均无法下载：GitHub={github_error}；Gitee={gitee_error}") from gitee_error
            if len(package) > MAX_NODE_PACKAGE_BYTES:
                raise ValueError(f"Gitee 节点压缩包超过 {MAX_NODE_PACKAGE_BYTES} 字节上限。")
            return package

    @classmethod
    def _github_tarball(cls, owner: str, repository: str, ref: str) -> bytes:
        url = f"https://codeload.github.com/{owner}/{repository}/tar.gz/{quote(ref, safe='')}"
        request = Request(
            url,
            headers={
                "Accept": "application/gzip",
                "User-Agent": "Comfyui-omniscience-Node-Installer",
            },
        )
        opener = build_opener(_TarballRedirectHandler())
        try:
            with opener.open(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:  # noqa: S310 - host-validated GitHub URL
                final_host = (urlparse(response.geturl()).hostname or "").lower()
                if final_host not in TARBALL_HOSTS:
                    raise ValueError("GitHub 下载被重定向到不受支持的主机，已拒绝安装。")
                package = response.read(MAX_NODE_PACKAGE_BYTES + 1)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise ValueError(f"无法下载 GitHub 节点仓库：{exc}") from exc
        if len(package) > MAX_NODE_PACKAGE_BYTES:
            raise ValueError(f"节点压缩包超过 {MAX_NODE_PACKAGE_BYTES} 字节上限。")
        return package
