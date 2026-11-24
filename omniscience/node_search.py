"""Search public GitHub repositories for workflow node dependencies.

Search results are suggestions only. A user must select a repository, and the
node installer still stages and scans its contents before writing to
``ComfyUI/custom_nodes``.
"""

from __future__ import annotations

import contextlib
import os
import re
import json
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlparse, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen
from html.parser import HTMLParser

from .agent.github_skill import GitHubSkillInstaller
from .agent.node_install import NodeInstallerMixin
from .region_router import RegionRouter

MAX_RESULTS = 8
MAX_RESPONSE_BYTES = 1_000_000
GITHUB_API_BASE = "https://api.github.com"
GITHUB_PROXY_PREFIX = "https://github.tbap.top/"
GITHUB_PROXY_HOST = "github.tbap.top"
MANAGER_GITHUB_ENDPOINT = f"{GITHUB_PROXY_PREFIX.rstrip('/')}/https://github.com"
DOMESTIC_PIP_INDEX_URL = "https://mirrors.aliyun.com/pypi/simple/"
MANAGER_MAPPING_PATH = "/v2/customnode/getmappings?mode=local"
MANAGER_QUEUE_TASK_PATH = "/v2/manager/queue/task"
MANAGER_QUEUE_HISTORY_PATH = "/v2/manager/queue/history"
MANAGER_RESPONSE_BYTES = 20_000_000
MANAGER_POLL_SECONDS = 1.0
MANAGER_POLL_TIMEOUT_SECONDS = 1_800.0
# User-confirmed package ownership for the workflow under investigation.  These
# are package IDs, not repository guesses: Manager still supplies and validates
# the candidate metadata.  Other ambiguous node names remain unresolved.
CONFIRMED_NODE_PACKAGE_IDS = {
    "SigmasGraph": {"supernodes"},
    "Text": {"kaytool"},
}
_PIP_ENVIRONMENT_LOCK = threading.RLock()
SEARCH_ENGINE_USER_AGENT = "Comfyui-omniscience-Node-Search/1.0"
SEARCH_ENGINE_HOSTS = {"cn.bing.com", "www.bing.com", "www.google.com", "google.com"}


@contextlib.contextmanager
def _manager_pip_environment(decision: Any) -> Iterator[None]:
    """Apply the regional package index only while Manager installs a node.

    ComfyUI-Manager launches pip (or uv as its fallback) as a child process and
    inherits the ComfyUI process environment.  The environment is process-wide,
    so serialize these short-lived overrides and always restore the caller's
    original settings afterwards.
    """
    if decision is None or getattr(decision, "selected_region", "") != "domestic":
        yield
        return

    keys = ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "UV_INDEX_URL", "UV_EXTRA_INDEX_URL")
    with _PIP_ENVIRONMENT_LOCK:
        previous = {key: os.environ.get(key) for key in keys}
        try:
            os.environ["PIP_INDEX_URL"] = DOMESTIC_PIP_INDEX_URL
            os.environ.pop("PIP_EXTRA_INDEX_URL", None)
            # Current Manager prefers pip, but its fallback is uv.  Keep the
            # same domestic source in both cases.
            os.environ["UV_INDEX_URL"] = DOMESTIC_PIP_INDEX_URL
            os.environ.pop("UV_EXTRA_INDEX_URL", None)
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


@contextlib.contextmanager
def _manager_git_environment(decision: Any, proxy_base: str = "https://github.tbap.top") -> Iterator[None]:
    """Configure Manager's native GitHub endpoint for the queued install.

    Manager does not clone the URL from the queue payload verbatim. Its Git
    helper first normalizes the repository URL and then applies the endpoint
    stored in ``GITHUB_ENDPOINT``. The helper reads that environment variable
    at module import time, so update both the environment and the already
    loaded module while the queue worker is processing this task.
    """
    if decision is None or getattr(decision, "selected_region", "") != "domestic":
        yield
        return

    with _PIP_ENVIRONMENT_LOCK:
        previous_environment = os.environ.get("GITHUB_ENDPOINT")
        git_utils = sys.modules.get("comfyui_manager.common.git_utils")
        previous_module_endpoint = getattr(git_utils, "GITHUB_ENDPOINT", None) if git_utils is not None else None
        try:
            proxy_base = str(proxy_base or "https://github.tbap.top").strip().rstrip("/")
            proxy = urlparse(proxy_base)
            if proxy.scheme != "https" or not proxy.hostname or proxy.username or proxy.password or proxy.port not in {None, 443}:
                raise ValueError("GitHub 国内代理地址必须是无认证的 HTTPS 地址。")
            manager_endpoint = f"{proxy_base}/https://github.com"
            os.environ["GITHUB_ENDPOINT"] = manager_endpoint
            if git_utils is not None:
                git_utils.GITHUB_ENDPOINT = manager_endpoint
            yield
        finally:
            if previous_environment is None:
                os.environ.pop("GITHUB_ENDPOINT", None)
            else:
                os.environ["GITHUB_ENDPOINT"] = previous_environment
            if git_utils is not None:
                git_utils.GITHUB_ENDPOINT = previous_module_endpoint


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
        "provider": "github",
        "description": str(item.get("description") or "")[:500],
        "stars": stars,
        "updated_at": str(item.get("updated_at") or ""),
        "language": str(item.get("language") or ""),
    }


class _NodeSearchResultParser(HTMLParser):
    """Collect generic search-result anchors without depending on CSS classes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._href = ""
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a" or self._href:
            return
        href = str(dict(attrs).get("href") or "").strip()
        if href:
            self._href = href
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._href:
            return
        self.results.append({
            "href": self._href,
            "title": re.sub(r"\s+", " ", " ".join(self._text)).strip()[:500],
        })
        self._href = ""
        self._text = []


class _SearchEngineRedirectHandler(HTTPRedirectHandler):
    """Keep regional node searches on their selected search engine."""

    def redirect_request(self, request, response, code, message, headers, new_url):
        parsed = urlparse(new_url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in SEARCH_ENGINE_HOSTS or parsed.port not in {None, 443}:
            raise ValueError("节点搜索尝试跳转到不受支持的搜索站点，已阻止。")
        return super().redirect_request(request, response, code, message, headers, new_url)


def _search_result_target(href: Any, base_url: str) -> str:
    """Unwrap Bing/Google result links and return an absolute HTTPS URL."""
    value = urljoin(base_url, str(href or "").strip())
    parsed = urlsplit(value)
    if (parsed.hostname or "").lower() in SEARCH_ENGINE_HOSTS:
        query = parse_qs(parsed.query)
        value = unquote(str((query.get("q") or query.get("url") or [""])[0]))
    return value


def _github_repository_root_from_search_target(target: Any) -> str | None:
    """Turn a GitHub repository content result into its strict repository URL.

    Search engines commonly link to a README under ``blob`` rather than to the
    repository root.  That is useful evidence for discovering a repository, but
    it must never be retained as an install source.  Only ``blob`` and ``tree``
    links are eligible for this normalization; issue, pull-request, release,
    user, and arbitrary GitHub pages remain rejected.
    """
    value = str(target or "").strip()
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() != "github.com"
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
    ):
        return None
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if len(parts) == 2:
        root = value.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    elif len(parts) >= 4 and parts[2].lower() in {"blob", "tree"}:
        root = f"https://github.com/{parts[0]}/{parts[1]}"
    else:
        return None
    try:
        host, owner, repository, prefix, ref = NodeInstallerMixin._node_repository_source(root)
    except (TypeError, ValueError):
        return None
    if host != "github.com" or prefix or ref:
        return None
    return f"https://github.com/{owner}/{repository}"


class WebNodeSearcher:
    """Search the regional web engine for strict GitHub repository-root candidates."""

    @staticmethod
    def _search_url(region: str, class_type: str, limit: int) -> tuple[str, str]:
        query = f'"{class_type}"'
        count = max(1, min(MAX_RESULTS, int(limit)))
        if region == "domestic":
            engine = "bing"
            url = "https://cn.bing.com/search?" + urlencode({"q": query, "count": count, "first": 1})
        else:
            engine = "google"
            url = "https://www.google.com/search?" + urlencode({"q": query, "num": count, "start": 0})
        return engine, url

    @classmethod
    def search_url(cls, class_type: Any, *, region: str, limit: int = MAX_RESULTS) -> tuple[str, str]:
        name = _class_name(class_type, "区域 Web 搜索")
        selected_region = str(region or "").strip().lower()
        if selected_region not in {"domestic", "international"}:
            raise ValueError("区域 Web 搜索必须选择 domestic 或 international。")
        return cls._search_url(selected_region, name, limit)

    @staticmethod
    def _request_html(url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() not in SEARCH_ENGINE_HOSTS:
            raise ValueError("节点搜索地址无效。")
        request = Request(url, headers={"Accept": "text/html", "User-Agent": SEARCH_ENGINE_USER_AGENT})
        try:
            with build_opener(_SearchEngineRedirectHandler()).open(request, timeout=20) as response:
                body = response.read(4_000_000)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            raise ValueError("区域 Web 节点搜索请求失败。") from exc
        return body.decode("utf-8", errors="replace")

    @classmethod
    def search(cls, class_type: Any, *, region: str, limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
        name = _class_name(class_type, "区域 Web 搜索")
        selected_region = str(region or "").strip().lower()
        if selected_region not in {"domestic", "international"}:
            raise ValueError("区域 Web 搜索必须选择 domestic 或 international。")
        engine, url = cls._search_url(selected_region, name, limit)
        parser = _NodeSearchResultParser()
        parser.feed(cls._request_html(url))
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in parser.results:
            target = _search_result_target(item.get("href"), url)
            canonical = _github_repository_root_from_search_target(target)
            if not canonical:
                continue
            key = canonical.casefold()
            if key in seen:
                continue
            seen.add(key)
            owner, repository = canonical.removeprefix("https://github.com/").split("/", 1)
            results.append({
                "full_name": f"{owner}/{repository}",
                "source": canonical,
                "url": canonical,
                "repository": canonical,
                "provider": "web_search",
                "region": selected_region,
                "search_engine": engine,
                "search_query": url,
                "title": item.get("title") or f"{owner}/{repository}",
                "description": "",
                "verified": False,
                "match": "web_search_repository_root",
            })
            if len(results) >= 1:
                break
        return results


class GitHubNodeSearcher:
    """Query GitHub's repository search API for a single ComfyUI node class."""

    @staticmethod
    def search(class_type: Any, *, limit: int = MAX_RESULTS, token: str = "") -> list[dict[str, Any]]:
        # ``token`` remains accepted for compatibility, but GitHub search is
        # intentionally always anonymous.
        name = _class_name(class_type, "GitHub")
        count = max(1, min(MAX_RESULTS, int(limit)))
        query = urlencode({"q": f'"{name}" ComfyUI', "sort": "stars", "order": "desc", "per_page": count})
        url = f"{GITHUB_API_BASE}/search/repositories?{query}"
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


class _GitHubProxyRedirectHandler(HTTPRedirectHandler):
    """Only follow HTTPS redirects that remain on the configured proxy host."""

    def redirect_request(self, request, response, code, message, headers, new_url):
        parsed = urlparse(new_url)
        if parsed.scheme != "https" or (parsed.hostname or "").lower() != GITHUB_PROXY_HOST or parsed.port not in {None, 443}:
            raise ValueError("GitHub 国内代理搜索尝试跳转到不受支持的地址，已阻止。")
        return super().redirect_request(request, response, code, message, headers, new_url)


class GitHubProxyNodeSearcher:
    """Search GitHub repositories through the domestic GitHub API proxy."""

    @staticmethod
    def _request(url: str) -> Any:
        if not str(url).startswith(GITHUB_PROXY_PREFIX + GITHUB_API_BASE + "/"):
            raise ValueError("GitHub 国内代理搜索地址无效。")
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "Comfyui-omniscience-Node-Search"})
        try:
            with build_opener(_GitHubProxyRedirectHandler()).open(request, timeout=20) as response:
                if (urlparse(response.geturl()).hostname or "").lower() != GITHUB_PROXY_HOST:
                    raise ValueError("GitHub 国内代理搜索被重定向到不受支持的主机，已拒绝请求。")
                body = response.read(MAX_RESPONSE_BYTES + 1)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise ValueError("GitHub 国内代理节点仓库搜索请求失败。") from exc
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("GitHub 国内代理搜索返回的数据超过安全大小限制。")
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("GitHub 国内代理节点搜索返回了无法识别的数据。") from exc

    @classmethod
    def search(cls, class_type: Any, *, limit: int = MAX_RESULTS) -> list[dict[str, Any]]:
        name = _class_name(class_type, "GitHub 国内代理")
        count = max(1, min(MAX_RESULTS, int(limit)))
        query = urlencode({"q": f'"{name}" ComfyUI', "sort": "stars", "order": "desc", "per_page": count})
        api_url = f"{GITHUB_API_BASE}/search/repositories?{query}"
        url = f"{GITHUB_PROXY_PREFIX}{api_url}"
        try:
            payload = cls._request(url)
        except Exception as exc:
            raise ValueError(f"GitHub 国内代理节点仓库搜索失败（{name}）：{exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise ValueError("GitHub 国内代理节点搜索返回格式无效。")
        results = []
        for item in payload["items"]:
            result = _result(item, platform="github.com")
            if result:
                result["provider"] = "github_proxy"
                results.append(result)
            if len(results) >= count:
                break
        return results


class ComfyUIManagerClient:
    """Resolve and install nodes through the ComfyUI-Manager API.

    Manager is the authority for node-to-repository mappings and installation.
    This client deliberately talks only to the configured local ComfyUI server;
    it never turns a repository URL into an ad-hoc Manager installation request.
    """

    def __init__(self, settings: Any, *, request_timeout: float = 20.0):
        self.settings = settings
        self.request_timeout = request_timeout
        self._last_candidates: list[dict[str, Any]] = []
        self._last_resolution_error = ""
        self._mapping_cache: dict[str, dict[str, list[dict[str, Any]]]] = {}

    def _data(self) -> dict[str, Any]:
        return self.settings.load() if hasattr(self.settings, "load") else dict(self.settings or {})

    def _comfyui_directory(self) -> Path | None:
        value = str(self._data().get("comfyui_directory") or "").strip()
        if not value:
            return None
        directory = Path(value).expanduser()
        try:
            directory = directory.resolve()
        except OSError:
            return None
        return directory if directory.is_dir() else None

    def _manager_cache_files(self, suffix: str) -> list[Path]:
        """Return local Manager cache files without trusting arbitrary paths.

        The Manager HTTP mapping endpoint is not guaranteed to contain every
        extension mapping.  Its local ``extension-node-map`` cache is the
        authoritative fallback for an installed ComfyUI and is also what the
        Manager UI uses to identify nodes such as ``SigmasGraph``.
        """
        comfyui_directory = self._comfyui_directory()
        if comfyui_directory is None:
            return []
        cache_directory = comfyui_directory / "user" / "__manager" / "cache"
        if not cache_directory.is_dir():
            return []
        try:
            files = list(cache_directory.glob(f"*{suffix}"))
        except OSError:
            return []
        return sorted(files, key=lambda path: path.name, reverse=True)

    @staticmethod
    def _read_manager_cache(path: Path) -> Any:
        try:
            if path.stat().st_size > MANAGER_RESPONSE_BYTES:
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None

    def _manager_package_catalog(self) -> dict[str, dict[str, Any]]:
        """Index the local Manager package list by canonical repository URL."""
        catalog: dict[str, dict[str, Any]] = {}
        for path in self._manager_cache_files("_custom-node-list.json"):
            payload = self._read_manager_cache(path)
            entries = payload.get("custom_nodes") if isinstance(payload, dict) else payload
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                references = [entry.get("reference"), *(entry.get("files") or [])]
                for reference in references:
                    repository = self._repository_url({"repository": reference})
                    if repository:
                        catalog.setdefault(repository.casefold().rstrip("/"), entry)
        return catalog

    def _cached_extension_mappings(self) -> dict[str, list[dict[str, Any]]]:
        """Read Manager's local extension-node map into normalised candidates."""
        catalog = self._manager_package_catalog()
        result: dict[str, list[dict[str, Any]]] = {}
        for path in self._manager_cache_files("_extension-node-map.json"):
            payload = self._read_manager_cache(path)
            if not isinstance(payload, dict):
                continue
            for repository_key, raw in payload.items():
                names, metadata = self._mapping_nodes(raw)
                if not names:
                    continue
                repository = self._repository_url({**metadata, "repository": repository_key})
                if not repository:
                    continue
                package = catalog.get(repository.casefold().rstrip("/"), {})
                parsed = urlparse(repository)
                repository_name = parsed.path.strip("/").rsplit("/", 1)[-1] if parsed.path else ""
                manager_id = str(
                    package.get("id")
                    or metadata.get("id")
                    or repository_name.casefold()
                    or repository
                ).strip()
                title = str(
                    metadata.get("title_aux")
                    or metadata.get("title")
                    or package.get("title")
                    or repository_name
                    or repository
                ).strip()
                item = {
                    "manager_id": manager_id,
                    "install_id": manager_id,
                    "cnr_id": manager_id if "/" not in manager_id else "",
                    "aux_id": "",
                    "repository": repository,
                    "title": title,
                    "version": str(metadata.get("version") or "").strip(),
                    "install_type": str(package.get("install_type") or metadata.get("install_type") or "").strip(),
                    "reference": repository,
                    "pip": metadata.get("pip") or metadata.get("requirements") or "",
                    "metadata": {**metadata, "cache_file": str(path)},
                    "class_types": names,
                    "provider": "comfyui_manager",
                    "verified": True,
                    "mapping_source": "manager_extension_cache",
                }
                for name in names:
                    result.setdefault(name, []).append(
                        dict(item, match="exact", match_name=name, class_type=name)
                    )
        return result

    def cached_candidates(self, class_type: Any, *, aux_id: Any = "") -> list[dict[str, Any]]:
        """Return exact candidates from Manager's local extension mapping cache."""
        name = _class_name(class_type, "ComfyUI-Manager")
        candidates = list(self._cached_extension_mappings().get(name, []))
        expected = str(aux_id or "").strip()
        if expected:
            matching = [item for item in candidates if self._candidate_matches_aux_id(item, expected)]
            if matching:
                candidates = matching
        unique: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for item in candidates:
            key = (
                str(item.get("manager_id") or "").casefold(),
                str(item.get("repository") or "").rstrip("/").casefold(),
                str(item.get("class_type") or "").casefold(),
                str(item.get("version") or "").casefold(),
            )
            if key not in seen:
                seen.add(key)
                unique.append(item)
        return unique

    def preferred_candidates(self, class_type: Any, *, aux_id: Any = "") -> list[dict[str, Any]]:
        """Use live Manager mappings first and cache mappings only as fallback.

        The live ``local`` map is authoritative when several installed packages
        register the same class name.  The extension cache fills gaps in older
        Manager versions that omit a package such as ``SigmasGraph``.
        """
        if self._comfyui_directory() is None:
            candidates = self.cached_candidates(class_type, aux_id=aux_id)
        else:
            try:
                candidates = self.resolve_candidates(class_type, aux_id=aux_id)
            except ValueError:
                candidates = self.cached_candidates(class_type, aux_id=aux_id)
        preferred_ids = CONFIRMED_NODE_PACKAGE_IDS.get(str(class_type).strip(), set())
        if preferred_ids:
            preferred = [
                item
                for item in candidates
                if str(
                    item.get("manager_id")
                    or item.get("install_id")
                    or item.get("cnr_id")
                    or ""
                ).strip().casefold() in {value.casefold() for value in preferred_ids}
            ]
            if preferred:
                return preferred
        return candidates

    def _base_url(self) -> str:
        value = str(self._data().get("comfyui_server_url") or "http://127.0.0.1:8188").strip().rstrip("/")
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("ComfyUI 服务地址无效。")
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} and not self._data().get("allow_remote_comfyui"):
            raise ValueError("ComfyUI-Manager 默认只允许访问本机 ComfyUI 服务。")
        return value

    def _json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(f"{self._base_url()}{path}", data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.request_timeout) as response:  # noqa: S310 - validated local endpoint
                raw = response.read(MANAGER_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            detail = exc.read(1_000).decode("utf-8", errors="replace")
            raise ValueError(f"ComfyUI-Manager 请求失败：{method} {path}（HTTP {exc.code}）：{detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ValueError(f"无法连接 ComfyUI-Manager：{exc}") from exc
        if len(raw) > MANAGER_RESPONSE_BYTES:
            raise ValueError("ComfyUI-Manager 返回数据超过安全大小限制。")
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("ComfyUI-Manager 返回了无法解析的 JSON。") from exc

    @staticmethod
    def _candidate_field(candidate: dict[str, Any], *keys: str) -> Any:
        for key in keys:
            value = candidate.get(key)
            if value not in (None, "", [], {}):
                return value
        metadata = candidate.get("metadata")
        if isinstance(metadata, dict):
            for key in keys:
                value = metadata.get(key)
                if value not in (None, "", [], {}):
                    return value
        return ""

    @staticmethod
    def _mapping_nodes(value: Any) -> tuple[list[str], dict[str, Any]]:
        """Unpack the tuple/object variants returned by Manager mappings.

        ComfyUI-Manager has returned ``[node_names, metadata]`` for a long
        time, while cached and newer endpoints can expose the same information
        as an object.  Do not assume that every list-valued field contains
        strings: some custom-node-list entries use objects with ``name`` or
        ``class_type`` instead.
        """

        def names_from(value: Any) -> list[str]:
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, list):
                return []
            names: list[str] = []
            for item in value:
                if isinstance(item, dict):
                    item = item.get("class_type") or item.get("node_name") or item.get("name") or item.get("id")
                name = str(item or "").strip()
                if name and name not in names:
                    names.append(name)
            return names

        if isinstance(value, list) and value:
            # The Manager tuple is encoded as a JSON list.  A plain list of
            # names is also accepted for local/cache implementations.
            metadata = value[1] if len(value) > 1 and isinstance(value[1], dict) else {}
            if len(value) > 1 and isinstance(value[1], dict):
                return names_from(value[0]), metadata
            return names_from(value), {}
        if isinstance(value, dict):
            names: list[str] = []
            for key in ("node_names", "nodes", "class_types", "node_types", "aliases"):
                for name in names_from(value.get(key)):
                    if name not in names:
                        names.append(name)
            for key in ("node_name", "class_type"):
                name = str(value.get(key) or "").strip()
                if name and name not in names:
                    names.append(name)
            return names, value
        return [], {}

    @staticmethod
    def _repository_url(metadata: dict[str, Any]) -> str:
        for key in ("repository", "repo", "reference", "url", "github", "source", "github_url"):
            value = str(metadata.get(key) or "").strip().rstrip("/")
            if value.startswith(("https://github.com/", "http://github.com/", "https://gitcode.com/")):
                return value
        return ""

    @staticmethod
    def _metadata_value(metadata: dict[str, Any], *keys: str) -> str:
        for key in keys:
            value = str(metadata.get(key) or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _normalised_name(value: Any) -> str:
        return str(value or "").strip().casefold()

    def mappings(self, *, mode: str = "local") -> dict[str, list[dict[str, Any]]]:
        safe_mode = str(mode or "local").strip().lower()
        if safe_mode not in {"local", "remote", "cache", "nickname"}:
            raise ValueError("ComfyUI-Manager 映射模式无效。")
        cached = self._mapping_cache.get(safe_mode)
        if cached is not None:
            return cached
        payload = self._json("GET", MANAGER_MAPPING_PATH.replace("local", safe_mode, 1))
        if isinstance(payload, dict):
            for wrapper in ("mappings", "data", "custom_nodes", "node_mappings"):
                nested = payload.get(wrapper)
                if isinstance(nested, dict):
                    payload = nested
                    break
        if not isinstance(payload, dict):
            raise ValueError("ComfyUI-Manager 节点映射格式无效。")
        result: dict[str, list[dict[str, Any]]] = {}
        for pack_id, raw in payload.items():
            names, metadata = self._mapping_nodes(raw)
            pattern = str(metadata.get("nodename_pattern") or "").strip()
            if not names and not pattern:
                continue
            item = {
                "manager_id": str(pack_id).strip(),
                "install_id": self._metadata_value(metadata, "install_id", "manager_id", "id") or str(pack_id).strip(),
                "cnr_id": str(metadata.get("cnr_id") or "").strip(),
                "aux_id": str(metadata.get("aux_id") or "").strip(),
                "repository": self._repository_url(metadata),
                "title": str(metadata.get("title_aux") or metadata.get("title") or pack_id).strip(),
                "version": self._metadata_value(metadata, "selected_version", "version", "latest_version"),
                "install_type": self._metadata_value(metadata, "install_type", "type"),
                "reference": self._metadata_value(metadata, "reference", "repository", "repo"),
                "pip": metadata.get("pip") or metadata.get("requirements") or "",
                "metadata": metadata,
                "class_types": names,
                "provider": "comfyui_manager",
                "verified": True,
            }
            if not item["cnr_id"] and not item["aux_id"]:
                # The mapping key is the install ID after Manager's
                # map_to_unified_keys step. It may be a CNR id or an
                # author/repository auxiliary id, so retain it as manager_id
                # and choose the version below conservatively.
                item["cnr_id"] = str(pack_id).strip() if "/" not in str(pack_id) else ""
                item["aux_id"] = str(pack_id).strip() if "/" in str(pack_id) else ""
            for name in names:
                result.setdefault(name, []).append(
                    dict(item, match="exact", match_name=name, class_type=name)
                )
            if pattern:
                try:
                    re.compile(pattern)
                except re.error:
                    # A broken remote mapping must not make every node search
                    # fail.  It is retained in metadata for diagnostics but is
                    # never used as a match.
                    pattern = ""
                if pattern:
                    item["nodename_pattern"] = pattern
                    item["match"] = "pattern"
                    result.setdefault(f"__pattern__:{pack_id}", []).append(item)
        self._mapping_cache[safe_mode] = result
        return result

    @staticmethod
    def _candidate_matches_aux_id(candidate: dict[str, Any], aux_id: Any) -> bool:
        expected = str(aux_id or "").strip().casefold()
        if not expected:
            return False
        values = {
            str(candidate.get(key) or "").strip().casefold()
            for key in ("aux_id", "repository", "source", "manager_id")
        }
        metadata = candidate.get("metadata")
        if isinstance(metadata, dict):
            values.update(
                str(metadata.get(key) or "").strip().casefold()
                for key in ("aux_id", "repository", "repo", "url", "source")
            )
        return expected in {value.rstrip("/") for value in values if value}

    def resolve_candidates(self, class_type: Any, *, aux_id: Any = "") -> list[dict[str, Any]]:
        name = _class_name(class_type, "ComfyUI-Manager")
        self._last_resolution_error = ""
        cached_candidates = self.cached_candidates(name, aux_id=aux_id)
        try:
            mappings = self.mappings()
        except ValueError as exc:
            self._last_resolution_error = str(exc)
            mappings = {}
        normalised = self._normalised_name(name)
        candidates: list[dict[str, Any]] = []
        for key, items in mappings.items():
            for item in items:
                match_kind = ""
                match_name = ""
                if key.startswith("__pattern__:"):
                    pattern = str(item.get("nodename_pattern") or "")
                    try:
                        matched = bool(pattern and re.match(pattern, name))
                    except re.error:
                        matched = False
                    if matched:
                        match_kind = "pattern"
                else:
                    known_names = [key, *item.get("class_types", [])]
                    for known_name in known_names:
                        if self._normalised_name(known_name) == normalised:
                            match_kind = "exact" if self._normalised_name(known_name) == self._normalised_name(key) else "alias"
                            match_name = str(known_name).strip()
                            break
                if match_kind:
                    candidates.append(
                        dict(
                            item,
                            class_type=name,
                            match=match_kind,
                            match_name=match_name or item.get("match_name") or name,
                        )
                    )
        if not candidates:
            candidates = list(cached_candidates)
        # Do not discard ambiguous candidates.  A canvas aux_id is stronger
        # evidence than a repeated class name, so place matching repositories
        # first while retaining all alternatives for user review.
        expected = str(aux_id or "").strip()
        candidates.sort(
            key=lambda item: (
                0 if expected and self._candidate_matches_aux_id(item, expected) else 1,
                {"exact": 0, "alias": 1, "pattern": 2}.get(str(item.get("match") or ""), 3),
            )
        )
        unique: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str, str, str, str]] = set()
        for item in candidates:
            key = (
                str(item.get("manager_id") or "").casefold(),
                str(item.get("install_id") or "").casefold(),
                str(item.get("repository") or item.get("reference") or "").rstrip("/").casefold(),
                str(item.get("aux_id") or "").casefold(),
                str(item.get("cnr_id") or "").casefold(),
                str(item.get("version") or item.get("selected_version") or "").casefold(),
                str(item.get("install_type") or "").casefold(),
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        self._last_candidates = unique
        return unique

    def resolve(self, class_type: Any, *, aux_id: Any = "") -> dict[str, Any] | None:
        candidates = self.resolve_candidates(class_type, aux_id=aux_id)
        return candidates[0] if candidates else None

    def install(
        self,
        manager_id: str,
        *,
        version: str = "latest",
        source: str = "",
        candidate: dict[str, Any] | None = None,
        on_progress: Any = None,
    ) -> dict[str, Any]:
        """Install a node and select the regional package index for Manager pip."""
        try:
            decision = RegionRouter(self.settings).decide()
        except (ValueError, OSError):
            # Preserve the existing Git fallback behavior when auto detection is
            # unavailable.  International pip behavior is also left untouched.
            decision = None
        proxy_base = str(self._data().get("github_proxy_base_url") or "https://github.tbap.top")
        with _manager_pip_environment(decision), _manager_git_environment(decision, proxy_base):
            return self._install(
                manager_id,
                version=version,
                source=source,
                candidate=candidate,
                on_progress=on_progress,
                region_decision=decision,
            )

    @staticmethod
    def _history_task(payload: Any, ui_id: str) -> dict[str, Any] | None:
        """Normalize Manager's single-task and collection history responses.

        ``/v2/manager/queue/history`` returns a collection when queried without
        an id, but current Manager versions return the matching task directly
        under ``history`` when ``ui_id`` is supplied.  Treating that direct task
        as a collection makes a completed install look like it is still running
        forever.
        """
        if not isinstance(payload, dict):
            return None
        history = payload.get("history")
        if not isinstance(history, dict):
            return None
        task = history.get(ui_id)
        if isinstance(task, dict):
            return task
        if str(history.get("ui_id") or "") == ui_id:
            return history
        return None

    @staticmethod
    def _history_status(task: dict[str, Any]) -> tuple[bool, str, list[str]]:
        """Return terminal state, normalized result, and Manager messages."""
        status = task.get("status") if isinstance(task.get("status"), dict) else {}
        status_name = str(status.get("status_str") or "").strip().lower()
        if "." in status_name:
            status_name = status_name.rsplit(".", 1)[-1]
        result_name = str(task.get("result") or "").strip().lower()
        if "." in result_name:
            result_name = result_name.rsplit(".", 1)[-1]
        messages = [str(item) for item in status.get("messages") or [] if str(item).strip()]
        completed = bool(status.get("completed")) or result_name in {"success", "skip"}
        return completed, status_name or result_name, messages

    def _install(
        self,
        manager_id: str,
        *,
        version: str = "latest",
        source: str = "",
        candidate: dict[str, Any] | None = None,
        on_progress: Any = None,
        region_decision: Any = None,
    ) -> dict[str, Any]:
        candidate = candidate if isinstance(candidate, dict) else {}
        package_id = str(
            self._candidate_field(candidate, "install_id", "id")
            or manager_id
            or self._candidate_field(candidate, "manager_id", "cnr_id", "aux_id")
            or ""
        ).strip()
        if not package_id or len(package_id) > 300:
            raise ValueError("ComfyUI-Manager 节点包 ID 无效。")
        requested_package_id = package_id
        candidate_version = str(
            self._candidate_field(candidate, "selected_version", "version", "latest_version") or ""
        ).strip()
        install_type = str(self._candidate_field(candidate, "install_type", "type") or "").strip().casefold()
        repository = str(
            self._candidate_field(candidate, "repository", "reference", "source", "repo", "url")
            or source
            or ""
        ).strip()
        selected_version = str(version or "").strip()
        if not selected_version or (selected_version == "latest" and candidate_version):
            selected_version = candidate_version or "latest"
        # Manager uses ``nightly`` for Git repository installs.  ``unknown`` is
        # reserved for an already-installed, unregistered node and makes the
        # queue worker search for ``<id>@unknown`` in the catalog.  That turns a
        # valid repository candidate such as ``supernodes`` into:
        # ``Node 'supernodes@unknown' not found ...``.
        if install_type in {"git-clone", "git", "repository"} or "/" in package_id:
            selected_version = "nightly"
        # Manager's nightly manifest indexes an unregistered Git repository by
        # its repository basename (for example ``ComfyUI-SuperNodes``), while
        # the manifest entry may also expose a CNR-style id (``supernodes``).
        # The queue worker resolves ``params.id`` before it reads the repository
        # metadata, so sending the CNR id makes it fail with
        # ``Node 'supernodes@nightly' not found``.  Use the repository basename
        # for the install target and keep the original candidate id in the
        # returned metadata/progress events.
        if install_type in {"git-clone", "git", "repository"} and repository:
            repository_path = urlparse(repository).path.rstrip("/")
            repository_name = repository_path.rsplit("/", 1)[-1].removesuffix(".git").strip()
            if repository_name:
                package_id = repository_name
        # Keep the canonical GitHub URL here. Manager normalizes the repository
        # and applies the configured GITHUB_ENDPOINT immediately before cloning.
        manager_repository = repository
        ui_id = f"omniscience-node-{uuid.uuid4().hex}"
        client_id = f"omniscience-{uuid.uuid4().hex}"
        payload = {
            "ui_id": ui_id,
            "client_id": client_id,
            "kind": "install",
            "params": {
                "id": package_id,
                "version": selected_version,
                "selected_version": selected_version,
                "channel": "default",
                "mode": "local",
                "skip_post_install": False,
            },
        }
        params = payload["params"]
        if manager_repository:
            # ``repository`` is the field understood by current Manager queue
            # models.  Keep ``reference`` below for older Manager versions and
            # for the persisted request shape used by this project.
            params["repository"] = manager_repository
        for key in ("install_type", "reference", "pip"):
            value = self._candidate_field(candidate, key)
            if value not in (None, "", [], {}):
                if key == "reference" and install_type in {"git-clone", "git", "repository"}:
                    value = manager_repository
                params[key] = value
        self._json("POST", MANAGER_QUEUE_TASK_PATH, payload)
        if callable(on_progress):
            try:
                on_progress({"phase": "queued", "manager_task_id": ui_id, "manager_id": requested_package_id})
            except Exception:
                # Progress reporting is advisory and must never make a valid
                # Manager installation fail.
                pass
        # Newer Manager versions expose the queue worker explicitly. Older
        # versions simply return an empty response here, which is harmless.
        try:
            self._json("POST", "/v2/manager/queue/start")
        except ValueError:
            # The task is already queued; history polling below gives the
            # authoritative result and reports an actual start failure.
            pass
        deadline = time.monotonic() + MANAGER_POLL_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            history = self._json("GET", f"{MANAGER_QUEUE_HISTORY_PATH}?ui_id={quote(ui_id, safe='')}")
            task = self._history_task(history, ui_id)
            if isinstance(task, dict):
                completed, status_name, manager_messages = self._history_status(task)
                if callable(on_progress):
                    try:
                        on_progress(
                            {
                                "phase": "running" if not completed else "completed",
                                "manager_task_id": ui_id,
                                "manager_id": requested_package_id,
                                "manager_status": status_name or "running",
                                "manager_messages": manager_messages[-3:],
                            }
                        )
                    except Exception:
                        pass
                if completed:
                    if status_name not in {"success", "skip"}:
                        message = "；".join(manager_messages)
                        raise ValueError(f"ComfyUI-Manager 安装失败：{message or task.get('result') or '未知错误'}")
                    return {
                        "status": "manager_installed",
                        "manager_task_id": ui_id,
                        "manager_id": requested_package_id,
                        "source": source,
                        "manager_result": task,
                        "requires_restart": True,
                        "security": {"status": "manager_managed", "note": "安装由 ComfyUI-Manager 执行；请在节点加载后再次进行 /object_info 验证。"},
                    }
            time.sleep(MANAGER_POLL_SECONDS)
        raise ValueError("ComfyUI-Manager 安装任务超时，请在 Manager 面板查看任务状态。")