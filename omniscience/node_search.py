"""Search public GitHub and Gitee repositories for workflow node dependencies.

Search results are suggestions only. A user must select a repository, and the
node installer still stages and scans its contents before writing to
``ComfyUI/custom_nodes``.
"""

from __future__ import annotations

import re
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .agent.github_skill import GitHubSkillInstaller
from .agent.node_install import NodeInstallerMixin

MAX_RESULTS = 8
MAX_RESPONSE_BYTES = 1_000_000


def _class_name(value: Any, platform: str) -> str:
    raw_name = str(value or "")
    if len(raw_name) > 160 or re.search(r"[\x00-\x1f\x7f]", raw_name):
        raise ValueError(f"节点类名无效，无法安全搜索 {platform}。")
    name = raw_name.strip()
    if not name or not re.fullmatch(r"[\w .-]{1,160}", name):
        raise ValueError(f"节点类名无效，无法安全搜索 {platform}。")
    return name


def _result(item: Any, *, platform: str) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    full_name = str(item.get("full_name") or item.get("path_with_namespace") or "").strip()
    html_url = str(item.get("html_url") or item.get("web_url") or "").strip()
    try:
        host, owner, repository, prefix, ref = NodeInstallerMixin._node_repository_source(html_url)
    except (TypeError, ValueError):
        return None
    if host != platform or prefix or ref:
        return None
    parsed = urlparse(html_url)
    if parsed.path.strip("/").count("/") != 1:
        return None
    canonical = f"https://{platform}/{owner}/{repository}"
    if full_name.casefold() != f"{owner}/{repository}".casefold():
        return None
    try:
        stars = int(item.get("stargazers_count") or item.get("stars_count") or 0)
    except (TypeError, ValueError):
        stars = 0
    return {
        "full_name": full_name,
        "source": canonical,
        "url": canonical,
        "provider": "github" if platform == "github.com" else "gitee",
        "description": str(item.get("description") or "")[:500],
        "stars": stars,
        "updated_at": str(item.get("updated_at") or ""),
        "language": str(item.get("language") or ""),
    }


class GitHubNodeSearcher:
    """Query GitHub's repository search API for a single ComfyUI node class."""

    @staticmethod
    def search(class_type: Any, *, limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
        name = _class_name(class_type, "GitHub")
        count = max(1, min(MAX_RESULTS, int(limit)))
        query = urlencode({"q": f'"{name}" ComfyUI', "sort": "stars", "order": "desc", "per_page": count})
        url = f"https://api.github.com/search/repositories?{query}"
        try:
            payload = GitHubSkillInstaller._github_request(url, max_bytes=MAX_RESPONSE_BYTES)
        except Exception as exc:
            raise ValueError(f"GitHub 节点仓库搜索失败（{name}）：{exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise ValueError("GitHub 节点搜索返回格式无效。")

        results: list[dict[str, Any]] = []
        for item in payload["items"]:
            result = _result(item, platform="github.com")
            if result:
                results.append(result)
            if len(results) >= count:
                break
        return results


class _GiteeRedirectHandler(HTTPRedirectHandler):
    """Only follow HTTPS redirects that remain on Gitee."""

    def redirect_request(self, request, response, code, message, headers, new_url):
        parsed = urlparse(new_url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in {"gitee.com", "www.gitee.com"} or parsed.port not in {None, 443}:
            raise ValueError("Gitee 搜索尝试跳转到不受支持的地址，已阻止。")
        return super().redirect_request(request, response, code, message, headers, new_url)


class GiteeNodeSearcher:
    """Search public Gitee repositories for a single ComfyUI node class."""

    @staticmethod
    def _request(url: str) -> Any:
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "Comfyui-omniscience-Node-Search"})
        try:
            with build_opener(_GiteeRedirectHandler()).open(request, timeout=20) as response:
                if (urlparse(response.geturl()).hostname or "").lower() not in {"gitee.com", "www.gitee.com"}:
                    raise ValueError("Gitee 搜索被重定向到不受支持的主机，已拒绝请求。")
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise ValueError(f"Gitee 节点仓库搜索请求失败：{exc}") from exc
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("Gitee 搜索返回的数据超过安全大小限制。")
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Gitee 节点搜索返回了无法识别的数据。") from exc

    @classmethod
    def search(cls, class_type: Any, *, limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
        name = _class_name(class_type, "Gitee")
        count = max(1, min(MAX_RESULTS, int(limit)))
        query = urlencode({"q": f'"{name}" ComfyUI', "sort": "stars_count", "order": "desc", "per_page": count})
        url = f"https://gitee.com/api/v5/search/repositories?{query}"
        try:
            payload = cls._request(url)
        except Exception as exc:
            raise ValueError(f"Gitee 节点仓库搜索失败（{name}）：{exc}") from exc
        items = payload if isinstance(payload, list) else payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise ValueError("Gitee 节点搜索返回格式无效。")
        results = []
        for item in items:
            result = _result(item, platform="gitee.com")
            if result:
                results.append(result)
            if len(results) >= count:
                break
        return results