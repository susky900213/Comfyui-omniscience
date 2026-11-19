"""Public GitHub Skill package installer (DESIGN.md section 6, global chat).

The installer only reads text files from public GitHub repositories through
the GitHub API/raw hosts. Package scripts and other executable files are
never run; the ``scripts_executed`` result flag is always ``False``.

A repository link installs every Skill package it publishes, so a link to the
repository root works the same way the folder link does.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .. import skill_review, skill_security
from ..constants import MAX_GLOBAL_SKILL_PACKAGE_BYTES, MAX_GLOBAL_SKILL_PACKAGE_FILES
from ..skills import SkillStore


class _GitHubRedirectHandler(HTTPRedirectHandler):
    """Allow installer redirects only between HTTPS GitHub API/raw hosts."""

    def redirect_request(self, request, response, code, message, headers, new_url):
        parsed = urlparse(new_url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in {
            "api.github.com",
            "raw.githubusercontent.com",
        } or parsed.port not in {None, 443}:
            raise ValueError("GitHub 下载尝试跳转到不受支持的地址，已阻止。")
        return super().redirect_request(request, response, code, message, headers, new_url)


class SkillAlreadyInstalledError(ValueError):
    """Raised per package when a Skill of that name already exists in the library."""

    def __init__(self, skill: dict[str, Any]):
        super().__init__(f"Skill“{skill['name']}”已存在，未重复安装。")
        self.skill = skill


class SkillSecurityBlockedError(ValueError):
    """Raised per package when the security scan refuses the Skill text."""

    def __init__(self, name: str, report: dict[str, Any]):
        super().__init__(
            f"安全检测阻止安装 Skill“{name}”：{skill_security.summarize(report)} "
            f"{skill_security.findings_text(report, limit=3)}"
        )
        self.name = name
        self.report = report


VERDICT_LABELS = {"clean": "未发现风险", "review": "需要复核", "blocked": "已被阻止"}


class GitHubSkillInstaller:
    """Mixin providing ``install_github_skill`` and its request gating."""

    # Repository folders that hold internal tooling rather than shippable Skills.
    IGNORED_SKILL_DIRECTORIES = {
        ".github",
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        "vendor",
    }
    ALLOWED_REFERENCE_DIRECTORIES = {"docs", "references", "templates", "examples"}
    MAX_SKILLS_PER_REPOSITORY = 10

    @staticmethod
    def _regular_blob(item: dict[str, Any]) -> bool:
        return item.get("type") == "blob" and item.get("mode") == "100644"

    @classmethod
    def _skill_candidates(cls, entries: list[dict[str, Any]], prefix: str) -> list[str]:
        """One SKILL.md per Skill folder name, preferring the shallowest copy."""
        paths: list[str] = []
        for item in entries:
            if not cls._regular_blob(item):
                continue
            path = str(item.get("path") or "")
            if path != "SKILL.md" and not path.endswith("/SKILL.md"):
                continue
            if any(part in cls.IGNORED_SKILL_DIRECTORIES for part in path.split("/")[:-1]):
                continue
            if prefix and not path.startswith(prefix.rstrip("/") + "/"):
                continue
            paths.append(path)
        best: dict[str, str] = {}
        for path in paths:
            folder = str(Path(path).parent)
            key = "" if folder == "." else folder.split("/")[-1]
            current = best.get(key)
            if current is None or len(path.split("/")) < len(current.split("/")):
                best[key] = path
        return sorted(best.values(), key=lambda value: (len(value.split("/")), value))

    @staticmethod
    def _github_request(
        url: str,
        accept: str = "application/vnd.github+json",
        max_bytes: int = MAX_GLOBAL_SKILL_PACKAGE_BYTES,
    ) -> Any:
        request = Request(
            url,
            headers={"Accept": accept, "User-Agent": "Comfyui-omniscience-Skill-Installer"},
        )
        opener = build_opener(_GitHubRedirectHandler())
        with opener.open(request, timeout=20) as response:  # noqa: S310 - URL is constructed from validated public GitHub URL
            final_host = (urlparse(response.geturl()).hostname or "").lower()
            if final_host not in {"api.github.com", "raw.githubusercontent.com"}:
                raise ValueError("GitHub 请求被重定向到不受支持的主机，已拒绝安装。")
            body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError("GitHub 返回的数据超过安全大小限制。")
        if "raw.githubusercontent.com" in url:
            return body.decode("utf-8")
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("GitHub 返回了无法识别的 Skill 数据。") from exc

    @staticmethod
    def _github_skill_source(source: Any) -> tuple[str, str, str, str | None]:
        value = str(source or "").strip()
        parsed = urlparse(value)
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").lower() != "github.com"
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
            or len(parts) < 2
        ):
            raise ValueError("请提供公开 GitHub HTTPS 仓库或 Skill 文件夹链接。")
        owner, repository = parts[0], parts[1].removesuffix(".git")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repository):
            raise ValueError("GitHub 仓库地址无效。")
        ref: str | None = None
        prefix = ""
        if len(parts) > 2:
            if parts[2] == "tree" and len(parts) >= 4:
                ref = parts[3]
                prefix = "/".join(parts[4:]).strip("/")
            elif parts[2] == "blob" and len(parts) >= 5:
                ref = parts[3]
                file_path = "/".join(parts[4:]).strip("/")
                if Path(file_path).name.lower() != "skill.md":
                    raise ValueError("GitHub 文件链接必须指向 SKILL.md。")
                prefix = str(Path(file_path).parent).replace("\\", "/")
                if prefix == ".":
                    prefix = ""
            else:
                raise ValueError("链接应指向 GitHub 仓库、tree 文件夹或 SKILL.md 文件。")
        if any(part in {".", ".."} for part in prefix.split("/") if part):
            raise ValueError("Skill 文件夹路径无效。")
        return owner, repository, prefix, ref

    @staticmethod
    def _skill_metadata(skill_markdown: str, folder_name: str) -> tuple[str, str, str]:
        text = str(skill_markdown).replace("\r\n", "\n")
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            raise ValueError("Skill 包缺少 SKILL.md YAML frontmatter。")
        try:
            end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
        except StopIteration as exc:
            raise ValueError("SKILL.md YAML frontmatter 未闭合。") from exc
        fields: dict[str, str] = {}
        for line in lines[1:end]:
            match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*):\s*(.*?)\s*$", line)
            if match:
                fields[match.group(1).lower()] = match.group(2).strip().strip("\"'")
        name = fields.get("name") or folder_name
        description = fields.get("description") or ""
        content = "\n".join(lines[end + 1 :]).strip()
        if not description:
            raise ValueError("SKILL.md frontmatter 必须包含 description。")
        if not content:
            raise ValueError("SKILL.md 没有 Skill 指令正文。")
        return name, description, content

    async def _package_entries_from_contents(
        self,
        api_root: str,
        request_json: Callable[[str], Any],
        ref: str,
        prefix: str,
    ) -> list[dict[str, Any]]:
        """Enumerate one package folder through the contents API for truncated trees."""
        budget = {"directories": 0}

        async def read_contents(directory: str) -> list[dict[str, Any]]:
            budget["directories"] += 1
            if budget["directories"] > MAX_GLOBAL_SKILL_PACKAGE_FILES:
                raise ValueError("Skill 包目录过于复杂，最多读取 40 个目录。")
            encoded = "/".join(quote(part, safe="") for part in directory.split("/") if part)
            endpoint = f"{api_root}/contents/{encoded}?ref={quote(ref, safe='')}"
            try:
                result = await asyncio.to_thread(request_json, endpoint)
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                raise ValueError(f"无法读取 GitHub Skill 文件夹 {directory}: {exc}") from exc
            if not isinstance(result, list):
                raise ValueError(f"GitHub 路径不是文件夹：{directory}")
            return result

        def blob_item(path: str, name: str, size: Any) -> dict[str, Any]:
            return {"path": path, "type": "blob", "mode": "100644", "size": int(size or 0)}

        entries: list[dict[str, Any]] = []
        package_dirs: list[str] = []
        root_listing = await read_contents(prefix)
        if any(
            item.get("type") == "file" and str(item.get("name", "")).lower() == "skill.md"
            for item in root_listing
        ):
            package_dirs.append(prefix)
        else:
            child_dirs = [item for item in root_listing if item.get("type") == "dir"]
            if len(child_dirs) > MAX_GLOBAL_SKILL_PACKAGE_FILES:
                raise ValueError("Skill 文件夹包含过多子目录，请直接链接到包含 SKILL.md 的目录。")
            for item in child_dirs:
                child = f"{prefix.rstrip('/')}/{item['name']}"
                if any(
                    entry.get("type") == "file" and str(entry.get("name", "")).lower() == "skill.md"
                    for entry in await read_contents(child)
                ):
                    package_dirs.append(child)
        if not package_dirs:
            raise ValueError("提供的 GitHub 文件夹中没有找到 SKILL.md。")

        for package_dir in package_dirs[: self.MAX_SKILLS_PER_REPOSITORY]:
            package_listing = await read_contents(package_dir)
            for item in package_listing:
                if item.get("type") == "file" and item.get("mode") in {None, "100644"}:
                    entries.append(blob_item(f"{package_dir}/{item['name']}", str(item.get("name")), item.get("size")))
            for item in package_listing:
                if item.get("type") == "dir" and str(item.get("name", "")).lower() in self.ALLOWED_REFERENCE_DIRECTORIES:
                    await self._collect_reference_tree(read_contents, blob_item, entries, f"{package_dir}/{item['name']}")
        return entries

    async def _collect_reference_tree(
        self,
        read_contents: Callable[[str], Any],
        blob_item: Callable[[str, str, Any], dict[str, Any]],
        entries: list[dict[str, Any]],
        directory: str,
        depth: int = 0,
    ) -> None:
        if depth > 5 or len(entries) > MAX_GLOBAL_SKILL_PACKAGE_FILES * self.MAX_SKILLS_PER_REPOSITORY:
            return
        for item in await read_contents(directory):
            name = str(item.get("name") or "")
            if item.get("type") == "file":
                if Path(name).suffix.lower() in {".md", ".txt"}:
                    entries.append(blob_item(f"{directory}/{name}", name, item.get("size")))
            elif item.get("type") == "dir":
                await self._collect_reference_tree(read_contents, blob_item, entries, f"{directory}/{name}", depth + 1)

    async def _scan_package(
        self,
        *,
        name: str,
        description: str,
        content: str,
        source: str,
        acknowledged: bool,
    ) -> dict[str, Any]:
        """Run the configured review engine before anything is written to the library."""
        report = await skill_review.review(
            {"name": name, "description": description, "content": content},
            settings=self.settings,
            source=source,
        )
        if report["verdict"] == "blocked":
            if not acknowledged:
                raise SkillSecurityBlockedError(name, report)
            report["acknowledged"] = True
        return report

    async def _install_package(
        self,
        *,
        owner: str,
        repository: str,
        ref: str,
        entries: list[dict[str, Any]],
        skill_path: str,
        acknowledged: bool = False,
    ) -> dict[str, Any]:
        package_dir = str(Path(skill_path).parent).replace("\\", "/")
        if package_dir == ".":
            package_dir = ""
        folder_name = package_dir.split("/")[-1] if package_dir else repository
        # The frontmatter name must match the folder, so the library can be
        # checked before any package file is downloaded.
        existing = next(
            (skill for skill in self.skills.list() if str(skill["name"]).casefold() == folder_name.casefold()),
            None,
        )
        if existing is not None:
            raise SkillAlreadyInstalledError(existing)
        package_files: list[tuple[str, str, int]] = []
        for item in entries:
            path = str(item.get("path") or "")
            if not self._regular_blob(item) or item.get("size", 0) > MAX_GLOBAL_SKILL_PACKAGE_BYTES:
                continue
            if path != skill_path and not path.startswith(package_dir.rstrip("/") + "/"):
                continue
            relative = path[len(package_dir.rstrip("/") + "/") :] if package_dir else path
            relative_parts = relative.split("/")
            if path != skill_path and (
                len(relative_parts) < 2
                or relative_parts[0].lower() not in self.ALLOWED_REFERENCE_DIRECTORIES
                or Path(path).suffix.lower() not in {".md", ".txt"}
            ):
                continue
            package_files.append((path, relative, int(item.get("size") or 0)))
        if len(package_files) > MAX_GLOBAL_SKILL_PACKAGE_FILES:
            raise ValueError("Skill 包文本文件超过数量限制，最多导入 40 个文件。")
        total_size = sum(size for _, _, size in package_files)
        if total_size > MAX_GLOBAL_SKILL_PACKAGE_BYTES:
            raise ValueError("Skill 包文本文件总大小超过 200 KB 限制。")

        async def fetch_file(path: str) -> str:
            encoded_path = "/".join(quote(part, safe="") for part in path.split("/"))
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repository}/{quote(ref, safe='')}/{encoded_path}"
            try:
                return await asyncio.to_thread(self._github_request, raw_url, "text/plain")
            except (HTTPError, URLError, TimeoutError, OSError, UnicodeDecodeError) as exc:
                raise ValueError(f"无法下载 Skill 文本文件 {path}: {exc}") from exc

        downloaded = await asyncio.gather(*(fetch_file(path) for path, _, _ in package_files))
        files = {relative: body for (_, relative, _), body in zip(package_files, downloaded)}
        name, description, instructions = self._skill_metadata(files["SKILL.md"], folder_name)
        if name != folder_name:
            raise ValueError("SKILL.md frontmatter 的 name 必须与 GitHub Skill 文件夹名称一致。")
        if len(name) > SkillStore.MAX_NAME_CHARS:
            raise ValueError(f"Skill 名称不能超过 {SkillStore.MAX_NAME_CHARS} 个字符。")
        references = [
            f"\n\n---\nReference file: {path}\n---\n{body.strip()}"
            for path, body in sorted(files.items())
            if path != "SKILL.md" and body.strip()
        ]
        content = instructions + "".join(references)
        if len(content.encode("utf-8")) > SkillStore.MAX_CONTENT_BYTES:
            raise ValueError("Skill 指令和参考文档超过 200 KB 限制。")
        package_source = f"https://github.com/{owner}/{repository}/tree/{quote(ref, safe='')}/{package_dir}".rstrip("/")
        # Scan before writing, so a malicious package never reaches the library.
        security = await self._scan_package(
            name=name,
            description=description,
            content=content,
            source=package_source,
            acknowledged=acknowledged,
        )
        skill = await asyncio.to_thread(
            self.skills.create,
            {"name": name, "description": description, "content": content, "enabled": True},
            source=package_source,
            security=security,
        )
        return {
            "skill": skill,
            "source": package_source,
            "files_imported": len(files),
        }

    async def install_github_skill(self, source: Any, *, acknowledged: bool = False) -> dict[str, Any]:
        owner, repository, requested_prefix, requested_ref = self._github_skill_source(source)
        api_root = f"https://api.github.com/repos/{owner}/{repository}"

        def request_json(url: str) -> Any:
            return self._github_request(url, max_bytes=10_000_000)

        try:
            if requested_ref is None:
                repo_info = await asyncio.to_thread(request_json, api_root)
                ref = str(repo_info.get("default_branch") or "").strip()
                if not ref:
                    raise ValueError("GitHub 仓库没有可用的默认分支。")
            else:
                ref = requested_ref
            tree_url = f"{api_root}/git/trees/{quote(ref, safe='')}?recursive=1"
            tree = await asyncio.to_thread(request_json, tree_url)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise ValueError(f"无法读取公开 GitHub Skill：{exc}") from exc
        entries = tree.get("tree") if isinstance(tree, dict) else None
        if not isinstance(entries, list):
            raise ValueError("GitHub 返回了无法识别的仓库目录。")
        if tree.get("truncated"):
            if not requested_prefix:
                raise ValueError("GitHub 仓库过大，无法安全自动查找 Skill；请提供具体 Skill 文件夹链接。")
            entries = await self._package_entries_from_contents(api_root, request_json, ref, requested_prefix)

        candidates = self._skill_candidates(entries, requested_prefix)
        if not candidates:
            raise ValueError("该 GitHub 仓库中没有找到可安装的 SKILL.md。")
        if len(candidates) > self.MAX_SKILLS_PER_REPOSITORY:
            raise ValueError(
                f"该仓库包含 {len(candidates)} 个 Skill，超过单次安装上限 {self.MAX_SKILLS_PER_REPOSITORY}；请提供具体 Skill 文件夹链接。"
            )

        installed: list[dict[str, Any]] = []
        already: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        for skill_path in candidates:
            try:
                installed.append(
                    await self._install_package(
                        owner=owner,
                        repository=repository,
                        ref=ref,
                        entries=entries,
                        skill_path=skill_path,
                        acknowledged=acknowledged,
                    )
                )
            except SkillAlreadyInstalledError as exc:
                already.append(exc.skill)
            except ValueError as exc:
                skipped.append({"path": skill_path, "reason": str(exc)})
        if installed:
            status, skills = "installed", [item["skill"] for item in installed]
        elif already:
            status, skills = "already_installed", already
        else:
            blocked = [item for item in skipped if "安全检测" in str(item["reason"])]
            reasons = "；".join(f"{item['path']}：{item['reason']}" for item in (blocked or skipped)[:3])
            suffix = " 若确认来源可信，可勾选“仍然安装”重试。" if blocked else ""
            raise ValueError(f"未能安装该仓库中的 Skill。{reasons}{suffix}")
        return {
            "status": status,
            "skill": skills[0],
            "skills": skills,
            "security": [
                {
                    "name": skill["name"],
                    "verdict": verdict,
                    "summary": VERDICT_LABELS.get(verdict, verdict),
                    "findings": (skill.get("security", {}).get("findings") or [])[:6],
                }
                for skill in skills
                for verdict in [str(skill.get("security", {}).get("verdict") or "clean")]
            ],
            "sources": [item["source"] for item in installed],
            "source": installed[0]["source"] if installed else f"https://github.com/{owner}/{repository}",
            "files_imported": sum(int(item["files_imported"]) for item in installed),
            "already_installed": [skill["name"] for skill in already],
            "skipped": skipped,
            "scripts_executed": False,
        }

    @staticmethod
    def _explicit_install_request(content: str) -> bool:
        text = str(content or "")
        request = text.casefold()
        install_requested = bool(re.search(
            r"安装|下载|添加|导入|\b(?:install|download|add|import)\b",
            request,
        ))
        negated = bool(re.search(
            r"(?:不要|无需|不需要|别|禁止|不能|无法|没法).{0,24}(?:安装|下载|添加|导入)|"
            r"(?:don't|dont|do not|no need to|cannot|can't|unable to).{0,32}(?:install|download|add|import)",
            request,
        ))
        plugin_requested = bool(re.search(r"插件|\bplugins?\b|\bsoftware\b|\bdependencies\b", request))
        source_present = bool(
            re.search(r"https://github\.com/[^\s<>\"']+", text, re.IGNORECASE)
            or re.search(r"(?<![A-Za-z0-9_.-])[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?![A-Za-z0-9_.-])", text)
        )
        return install_requested and source_present and not negated and not plugin_requested

    @staticmethod
    def _install_source_allowed(content: str, source: Any) -> bool:
        if not GitHubSkillInstaller._explicit_install_request(content):
            return False
        explicit_url = GitHubSkillInstaller._requested_skill_source(content)
        if not explicit_url:
            return False
        try:
            explicit_source = GitHubSkillInstaller._github_skill_source(explicit_url)
            proposed_source = GitHubSkillInstaller._github_skill_source(source)
        except (TypeError, ValueError):
            return False
        explicit_owner, explicit_repo, explicit_prefix, explicit_ref = explicit_source
        proposed_owner, proposed_repo, proposed_prefix, proposed_ref = proposed_source
        return (
            explicit_owner.casefold() == proposed_owner.casefold()
            and explicit_repo.casefold() == proposed_repo.casefold()
            and (not explicit_prefix or explicit_prefix == proposed_prefix)
            and (explicit_ref is None or explicit_ref == proposed_ref)
        )

    @classmethod
    def _requested_skill_source(cls, content: str) -> str | None:
        if not cls._explicit_install_request(content):
            return None
        match = re.search(r"https://github\.com/[^\s<>\"']+", str(content or ""), re.IGNORECASE)
        if match:
            source = match.group(0).rstrip(".,;:!?)\"]")
            try:
                cls._github_skill_source(source)
            except (TypeError, ValueError):
                return None
            return source
        shorthand = re.search(
            r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?![A-Za-z0-9_.-])",
            str(content or ""),
        )
        if not shorthand:
            return None
        return f"https://github.com/{shorthand.group(1)}/{shorthand.group(2)}"
