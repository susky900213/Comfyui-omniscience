"""Discover Hugging Face files from a search engine and Hub metadata."""

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .region_router import RegionRouter

MAX_MODEL_SOURCE_RESULTS = 8
REPO_ID = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
HUGGINGFACE_HOSTS = {"huggingface.co", "www.huggingface.co"}
SEARCH_ENGINE_USER_AGENT = "Comfyui-omniscience-Model-Search/1.0"
DEBUG_BING_ENV = "OMNISCIENCE_DEBUG_BING"
logger = logging.getLogger("Comfyui-omniscience")


def normalise_model_filename(value: Any) -> str:
    """Return a comparison form for model basenames from workflow and Hub data."""
    basename = Path(str(value or "").replace("\\", "/")).name.strip()
    if not basename:
        return ""
    path = Path(basename)
    stem = re.sub(r"[-_\s]+", "_", path.stem.casefold())
    return f"{stem}{path.suffix.casefold()}"


def model_filenames_match(actual: Any, expected: Any) -> bool:
    """Compare safe model basenames while allowing common separator variants."""
    actual_name = Path(str(actual or "").replace("\\", "/")).name.strip()
    expected_name = Path(str(expected or "").replace("\\", "/")).name.strip()
    return bool(
        actual_name
        and expected_name
        and normalise_model_filename(actual_name) == normalise_model_filename(expected_name)
    )


class _SearchResultParser(HTMLParser):
    """Collect anchor text without depending on a search engine's CSS classes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._anchor: dict[str, str] | None = None
        self._text: list[str] = []
        self._after_anchor = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a" or self._anchor is not None:
            return
        attributes = dict(attrs)
        href = str(attributes.get("href") or "").strip()
        if href:
            self._anchor = {"href": href}
            self._text = []
            self._after_anchor = False

    def handle_data(self, data: str) -> None:
        if self._anchor is not None:
            self._text.append(data)
        elif self._after_anchor and self.results:
            snippet = f"{self.results[-1].get('snippet', '')} {data}"
            self.results[-1]["snippet"] = re.sub(r"\s+", " ", snippet).strip()[:1000]

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._anchor is None:
            return
        result = dict(self._anchor)
        result["title"] = re.sub(r"\s+", " ", " ".join(self._text)).strip()
        result["snippet"] = ""
        self.results.append(result)
        self._anchor = None
        self._text = []
        self._after_anchor = True


class ModelSourceSearcher:
    """Search for exact Hugging Face files, preferring the regional web search."""

    def __init__(self, settings: Any):
        self.settings = settings
        self.router = RegionRouter(settings)
        self._last_region = ""

    @staticmethod
    def _request_json(url: str) -> Any:
        request = Request(url, headers={"Accept": "application/json", "User-Agent": SEARCH_ENGINE_USER_AGENT})
        try:
            with urlopen(request, timeout=20) as response:  # noqa: S310 - URL is built from the configured Hub endpoint
                import json

                return json.loads(response.read(2_000_000).decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            raise ValueError(f"Hugging Face 模型来源搜索失败：{exc}") from exc

    @staticmethod
    def _request_html(url: str) -> str:
        request = Request(url, headers={"Accept": "text/html", "User-Agent": SEARCH_ENGINE_USER_AGENT})
        try:
            with urlopen(request, timeout=12) as response:  # noqa: S310 - URL is selected from a fixed search engine list
                payload = response.read(4_000_000)
                status = getattr(response, "status", None) or response.getcode()
                content_type = response.headers.get("Content-Type", "")
            html = payload.decode("utf-8", errors="replace")
            logger.warning(
                "[model-search] search response url=%s status=%s content_type=%s bytes=%d",
                url,
                status,
                content_type,
                len(payload),
            )
            if os.environ.get(DEBUG_BING_ENV, "").lower() in {"1", "true", "yes", "on", "raw"} and "bing.com" in (
                urlsplit(url).hostname or ""
            ):
                logger.warning("[model-search] Bing raw HTML prefix:\n%s", html[:6000])
            return html
        except (HTTPError, URLError, TimeoutError, OSError, UnicodeError) as exc:
            raise ValueError(f"搜索引擎模型来源搜索失败：{exc}") from exc

    @staticmethod
    def _search_engine_url(region: str, basename: str, *, limit: int, huggingface_only: bool = False) -> str:
        # The unrestricted query is useful for providers that do not use
        # Hugging Face, but Bing can return unrelated local-language results
        # for a model filename.  The caller retries with this focused query
        # when no validated file URL was found.
        query = basename
        count = max(1, min(10, int(limit)))
        if region == "domestic":
            # ``first=1`` deliberately keeps this request on Bing's first page.
            return "https://cn.bing.com/search?" + urlencode({"q": query, "count": count, "first": 1})
        # ``start=0`` deliberately keeps this request on Google's first page.
        return "https://www.google.com/search?" + urlencode({"q": query, "num": count, "start": 0})

    @staticmethod
    def _bing_search_url(region: str, basename: str, *, limit: int = MAX_MODEL_SOURCE_RESULTS) -> str:
        query = basename
        count = max(1, min(10, int(limit)))
        host = "cn.bing.com" if region == "domestic" else "www.bing.com"
        return "https://" + host + "/search?" + urlencode({"q": query, "count": count, "first": 1})

    def bing_search_url(self, filename: str, *, limit: int = MAX_MODEL_SOURCE_RESULTS) -> str:
        """Return a user-facing Bing query for a model filename."""
        basename = Path(str(filename or "").replace("\\", "/")).name.strip()
        if not basename:
            raise ValueError("模型文件名不能为空。")
        region = self._last_region or "domestic"
        return self._bing_search_url(region, basename, limit=limit)

    def manual_source(self, value: str, filename: str) -> dict[str, Any]:
        """Validate a user-provided Hugging Face file page and normalize it."""
        basename = Path(str(filename or "").replace("\\", "/")).name.strip()
        if not basename:
            raise ValueError("模型文件名不能为空。")
        endpoint, decision = self.router.endpoint("huggingface")
        region = str(getattr(decision, "selected_region", "") or "")
        source = self._source_from_huggingface_url(
            value,
            basename,
            endpoint=endpoint,
            region=region,
            source_type="huggingface_manual",
        )
        if source is None:
            raise ValueError(
                "手动地址无效。请填写 Hugging Face 文件地址，格式为 "
                "https://huggingface.co/<用户名>/<仓库>/blob/<分支>/<模型文件相对路径>，"
                "且文件名必须与工作流依赖一致。"
            )
        return source

    @staticmethod
    def _unwrap_search_url(value: str) -> str:
        """Resolve Google/Bing redirect links without trusting their hosts.

        Search engines may put the real result behind a relative redirect,
        Google ``/url?q=...`` link, or Bing ``/ck/a?...&u=a1...`` link.  The
        caller still performs the Hugging Face host and path allow-list check
        after this method returns.
        """
        raw = str(value or "").strip()
        if not raw:
            return ""
        candidates = [raw]
        relative = not urlsplit(raw).scheme
        if relative:
            candidates.extend((urljoin("https://www.google.com", raw), urljoin("https://www.bing.com", raw)))

        for candidate in candidates:
            for _ in range(2):
                parsed = urlsplit(candidate)
                hostname = (parsed.hostname or "").lower()
                query = parse_qs(parsed.query)
                if hostname in {"www.google.com", "google.com"} and parsed.path == "/url":
                    target = str((query.get("q") or query.get("url") or [""])[0]).strip()
                elif hostname in {"www.bing.com", "bing.com", "cn.bing.com"} and parsed.path in {"/ck/a", "/aclick"}:
                    target = ""
                    for key in ("u", "url", "r"):
                        values = query.get(key) or []
                        if not values:
                            continue
                        target = str(values[0]).strip()
                        if key == "u" and target.startswith("a1"):
                            try:
                                payload = target[2:]
                                padding = "=" * (-len(payload) % 4)
                                decoded = base64.urlsafe_b64decode(payload + padding).decode("utf-8")
                                if decoded.startswith(("http://", "https://")):
                                    target = decoded
                            except (binascii.Error, ValueError, UnicodeDecodeError):
                                pass
                        break
                else:
                    break
                if not target or target == candidate:
                    break
                candidate = unquote(target)
            parsed = urlsplit(candidate)
            # A relative redirect can be tried against both search engines;
            # do not return an unresolved Google wrapper before trying Bing.
            if relative and parsed.hostname in {"www.google.com", "google.com", "www.bing.com", "bing.com", "cn.bing.com"} and parsed.path in {"/url", "/ck/a", "/aclick"}:
                continue
            if parsed.scheme in {"http", "https"}:
                return candidate
        return raw

    @staticmethod
    def _normalised_base(endpoint: str) -> str:
        parsed = urlsplit(str(endpoint or "").strip())
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))

    @classmethod
    def _normalised_file_url(cls, endpoint: str, repo_id: str, filename: str, revision: str = "main") -> str:
        return (
            f"{cls._normalised_base(endpoint)}/{quote(repo_id, safe='/')}/resolve/"
            f"{quote(revision or 'main', safe='')}/{quote(filename.replace(chr(92), '/'), safe='/')}?download=true"
        )

    @classmethod
    def _source_from_huggingface_url(
        cls,
        value: str,
        basename: str,
        *,
        endpoint: str,
        region: str,
        source_type: str,
        title: str = "",
        snippet: str = "",
    ) -> dict[str, Any] | None:
        """Turn only a public HF file URL into the downloader's safe source shape."""
        parsed = urlsplit(cls._unwrap_search_url(value))
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in HUGGINGFACE_HOSTS:
            return None
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        # ``tree`` is a directory browser page, not a downloadable file.  Do
        # not turn it into a source merely because its last path component
        # happens to match the requested basename.
        if len(parts) < 5 or parts[2] not in {"blob", "resolve"}:
            return None
        repo_id = f"{parts[0]}/{parts[1]}"
        revision = parts[3]
        filename = "/".join(parts[4:])
        if not REPO_ID.fullmatch(repo_id) or not revision or not filename:
            return None
        if not model_filenames_match(Path(filename).name, basename) or ".." in Path(filename).parts:
            return None
        return {
            "repo_id": repo_id,
            "filename": filename,
            "sha256": "",
            "source_type": source_type,
            "source_url": cls._normalised_file_url(endpoint, repo_id, filename, revision),
            "region": region,
            "revision": revision,
            "title": title,
            "snippet": snippet,
        }

    def _search_web(self, basename: str, *, endpoint: str, region: str, limit: int) -> list[dict[str, Any]]:
        last_error: ValueError | None = None
        for huggingface_only in (False, True):
            url = self._search_engine_url(region, basename, limit=limit, huggingface_only=huggingface_only)
            try:
                html = self._request_html(url)
            except ValueError as exc:
                last_error = exc
                logger.warning("[model-search] search request failed url=%s error=%s", url, exc)
                continue

            parser = _SearchResultParser()
            parser.feed(html)
            logger.warning(
                "[model-search] parsed search page query=%r focused=%s anchors=%d",
                basename,
                huggingface_only,
                len(parser.results),
            )
            debug_bing = os.environ.get(DEBUG_BING_ENV, "").lower() in {"1", "true", "yes", "on", "raw"}
            if debug_bing and "bing.com" in (urlsplit(url).hostname or ""):
                lower_html = html.lower()
                lower_basename = basename.lower()
                positions: list[int] = []
                cursor = 0
                while len(positions) < 10:
                    position = lower_html.find(lower_basename, cursor)
                    if position < 0:
                        break
                    positions.append(position)
                    cursor = position + len(lower_basename)
                logger.warning(
                    "[model-search] Bing filename occurrences=%d positions=%s",
                    len(positions),
                    positions,
                )
                for position in positions:
                    logger.warning(
                        "[model-search] Bing HTML around filename[%d]:\n%s",
                        position,
                        html[max(0, position - 500) : position + len(basename) + 1000],
                    )
            if debug_bing:
                for index, result in enumerate(parser.results[:30], 1):
                    logger.warning(
                        "[model-search] candidate[%d] href=%r title=%r snippet=%r",
                        index,
                        result.get("href", ""),
                        result.get("title", ""),
                        result.get("snippet", ""),
                    )

            found: list[dict[str, Any]] = []
            seen: set[tuple[str, str]] = set()
            for result in parser.results:
                source = self._source_from_huggingface_url(
                    result.get("href", ""),
                    basename,
                    endpoint=endpoint,
                    region=region,
                    source_type="huggingface_search",
                    title=result.get("title", ""),
                    snippet=result.get("snippet", ""),
                )
                if source is None:
                    continue
                key = (str(source["repo_id"]), str(source["filename"]))
                if key in seen:
                    continue
                seen.add(key)
                found.append(source)
                if len(found) >= max(1, min(MAX_MODEL_SOURCE_RESULTS, int(limit))):
                    break
            logger.warning(
                "[model-search] validated Hugging Face results focused=%s count=%d",
                huggingface_only,
                len(found),
            )
            if found:
                return found

        if last_error is not None:
            raise last_error
        return []

    def _search_api(
        self,
        basename: str,
        *,
        endpoint: str,
        region: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        base = self._normalised_base(endpoint)
        query = urlencode({"search": Path(basename).stem, "limit": max(1, min(MAX_MODEL_SOURCE_RESULTS * 2, int(limit) * 2))})
        results = self._request_json(f"{base}/api/models?{query}")
        if not isinstance(results, list):
            raise ValueError("Hugging Face 模型搜索返回格式无效。")

        found: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in results:
            if not isinstance(item, dict):
                continue
            repo_id = str(item.get("id") or item.get("modelId") or "").strip()
            if not REPO_ID.fullmatch(repo_id):
                continue
            details = item
            siblings = details.get("siblings")
            if not isinstance(siblings, list):
                details = self._request_json(f"{base}/api/models/{quote(repo_id, safe='/')}")
                siblings = details.get("siblings") if isinstance(details, dict) else None
            if not isinstance(siblings, list):
                continue
            for sibling in siblings:
                if isinstance(sibling, str):
                    path, file_info = sibling, {}
                elif isinstance(sibling, dict):
                    path, file_info = str(sibling.get("rfilename") or ""), sibling
                else:
                    continue
                if not model_filenames_match(Path(path.replace("\\", "/")).name, basename):
                    continue
                key = (repo_id, path)
                if key in seen:
                    continue
                seen.add(key)
                lfs = file_info.get("lfs") if isinstance(file_info.get("lfs"), dict) else {}
                digest = str(lfs.get("sha256") or lfs.get("oid") or "").lower()
                if digest.startswith("sha256:"):
                    digest = digest.removeprefix("sha256:")
                found.append(
                    {
                        "repo_id": repo_id,
                        "filename": path,
                        "sha256": digest if re.fullmatch(r"[0-9a-f]{64}", digest) else "",
                        "source_type": "huggingface_api",
                        "source_url": self._normalised_file_url(endpoint, repo_id, path),
                        "region": region,
                        "revision": "main",
                        "title": "",
                        "snippet": "",
                    }
                )
                if len(found) >= max(1, min(MAX_MODEL_SOURCE_RESULTS, int(limit))):
                    return found
        return found

    def enrich_sha256(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Fill missing hashes from Hub metadata without changing candidate identity."""
        if not isinstance(candidates, list) or not candidates:
            return candidates if isinstance(candidates, list) else []
        endpoint, _decision = self.router.endpoint("huggingface")
        base = self._normalised_base(endpoint)
        details_by_repo: dict[str, Any] = {}
        enriched: list[dict[str, Any]] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            item = dict(candidate)
            digest = str(item.get("sha256") or "").lower()
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                repo_id = str(item.get("repo_id") or "")
                filename = str(item.get("filename") or "").replace("\\", "/")
                if REPO_ID.fullmatch(repo_id) and filename and ".." not in Path(filename).parts:
                    if repo_id not in details_by_repo:
                        try:
                            details_by_repo[repo_id] = self._request_json(
                                f"{base}/api/models/{quote(repo_id, safe='/')}"
                            )
                        except ValueError:
                            details_by_repo[repo_id] = None
                    details = details_by_repo.get(repo_id)
                    siblings = details.get("siblings") if isinstance(details, dict) else []
                    for sibling in siblings if isinstance(siblings, list) else []:
                        if not isinstance(sibling, dict) or str(sibling.get("rfilename") or "") != filename:
                            continue
                        lfs = sibling.get("lfs") if isinstance(sibling.get("lfs"), dict) else {}
                        digest = str(lfs.get("sha256") or lfs.get("oid") or "").lower()
                        if digest.startswith("sha256:"):
                            digest = digest.removeprefix("sha256:")
                        break
            item["sha256"] = digest if re.fullmatch(r"[0-9a-f]{64}", digest) else ""
            enriched.append(item)
        return enriched

    def search(self, filename: str, *, limit: int = MAX_MODEL_SOURCE_RESULTS) -> list[dict[str, Any]]:
        basename = Path(str(filename or "").replace("\\", "/")).name.strip()
        if not basename:
            raise ValueError("模型文件名不能为空。")
        endpoint, decision = self.router.endpoint("huggingface")
        region = str(getattr(decision, "selected_region", "") or "")
        self._last_region = region
        web_error = ""
        try:
            web_results = self._search_web(basename, endpoint=endpoint, region=region, limit=limit)
        except ValueError as exc:
            web_results = []
            web_error = str(exc)
        if web_results:
            return web_results
        try:
            return self._search_api(basename, endpoint=endpoint, region=region, limit=limit)
        except ValueError as exc:
            if web_error:
                raise ValueError(f"{web_error}；Hugging Face 回退搜索失败：{exc}") from exc
            raise