"""Configurable RunningHub workflow search adapter."""

from __future__ import annotations

import html
import json
import re
import uuid
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .region_router import RegionDecision, RegionRouter


class RunningHubProvider:
    """Search a configured RunningHub domestic/international endpoint."""

    def __init__(self, settings: Any, *, router: RegionRouter | None = None):
        self.settings = settings
        self.router = router or RegionRouter(settings)

    def _data(self) -> dict[str, Any]:
        return self.settings.load() if hasattr(self.settings, "load") else dict(self.settings or {})

    def _request_json(self, url: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> Any:
        body = None
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "User-Agent": "Comfyui-omniscience-Workflow-Search",
            "Client": "WEB",
            "User-Language": "zh_CN",
            "X-RH-Lang": "zh",
            "X-RH-Anonymous-Id": uuid.uuid4().hex,
        }
        parsed = urlsplit(url)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"
            headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/page-workflow"
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=20) as response:  # noqa: S310 - endpoint is user-configured
                content = response.read(2_000_000)
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise ValueError(f"RunningHub 搜索请求失败：{exc}") from exc
        try:
            return json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return content.decode("utf-8", errors="replace")

    def search(self, query: str, *, limit: int = 10, filters: dict[str, Any] | None = None, region: str | None = None) -> tuple[list[dict[str, Any]], RegionDecision]:
        base_url, decision = self.router.endpoint("runninghub", region=region)
        parsed_base = urlsplit(base_url)
        base_url = urlunsplit((parsed_base.scheme, parsed_base.netloc, parsed_base.path.rstrip("/"), "", ""))
        data = self._data()
        path = str(data.get("runninghub_search_path") or "/api/search/workflow")
        path = path.format(query=str(query).strip())
        url = f"{base_url}{path if path.startswith('/') else '/' + path}"
        params = {"size": max(1, min(50, int(limit))), "current": 1, "search": str(query).strip(), "tags": [], "sort": ""}
        if filters:
            params["tags"] = filters.get("tags") or []
        payload = self._request_json(url, method="POST", payload=params)
        raw = self._candidate_items(payload)
        if not isinstance(raw, list):
            raise ValueError("RunningHub 搜索结果缺少候选列表。")
        candidates = self._normalize(raw, query=query, region=decision.selected_region)
        candidates.sort(key=lambda item: (-float(item.get("score", 0)), str(item.get("title", ""))))
        return candidates[: params["size"]], decision

    @staticmethod
    def _unwrap_workflow(payload: Any) -> Any:
        if isinstance(payload, str):
            try:
                return RunningHubProvider._unwrap_workflow(json.loads(payload))
            except json.JSONDecodeError:
                return payload
        if isinstance(payload, dict):
            if isinstance(payload.get("prompt"), dict) or isinstance(payload.get("nodes"), list):
                return payload
            preferred = ("workflowJson", "workflowJSON", "workflowData", "workflowContent", "workflow", "prompt", "data", "result")
            for key in preferred:
                if key in payload:
                    value = RunningHubProvider._unwrap_workflow(payload[key])
                    if isinstance(value, (dict, list)):
                        return value
            for value in payload.values():
                value = RunningHubProvider._unwrap_workflow(value)
                if isinstance(value, (dict, list)) and (isinstance(value, list) or "prompt" in value or "nodes" in value):
                    return value
        return payload

    def get_workflow(self, candidate: dict[str, Any]) -> Any:
        base_url, _decision = self.router.endpoint("runninghub")
        parsed_base = urlsplit(base_url)
        base_url = urlunsplit((parsed_base.scheme, parsed_base.netloc, parsed_base.path.rstrip("/"), "", ""))
        data = self._data()
        path_template = str(data.get("runninghub_workflow_path") or "/api/workflow/export")
        path = path_template.format(id=str(candidate.get("id") or ""))
        url = f"{base_url}{path if path.startswith('/') else '/' + path}"
        payload = self._request_json(url, method="POST", payload={"workflowId": str(candidate.get("id") or "")})
        return self._unwrap_workflow(payload)

    @staticmethod
    def _candidate_items(payload: Any) -> list[Any] | None:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("results", "items", "list", "data", "records", "workflows", "workflowList", "rows"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
                nested = RunningHubProvider._candidate_items(value)
                if nested is not None:
                    return nested
            for value in payload.values():
                nested = RunningHubProvider._candidate_items(value)
                if nested is not None:
                    return nested
        if isinstance(payload, str):
            links = []
            for href, title in re.findall(r'href=["\']([^"\']*(?:workflow|page-workflow)[^"\']*)["\'][^>]*>([^<]{2,120})<', payload, re.I):
                links.append({"id": href, "title": html.unescape(re.sub(r"\\s+", " ", title)).strip(), "url": html.unescape(href)})
            return links or None
        return None

    @staticmethod
    def _normalize(items: list[Any], *, query: str, region: str) -> list[dict[str, Any]]:
        terms = {term.casefold() for term in re.findall(r"[\w\u3400-\u9fff]+", query) if len(term) > 1}
        result: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            candidate_id = str(item.get("id") or item.get("workflow_id") or item.get("slug") or "").strip()
            title = str(item.get("title") or item.get("name") or "未命名工作流").strip()
            if not candidate_id:
                candidate_id = f"candidate-{index + 1}"
            haystack = " ".join(str(item.get(key) or "") for key in ("title", "name", "description", "tags")).casefold()
            matches = sum(1 for term in terms if term in haystack)
            popularity = float(item.get("score") or item.get("rating") or item.get("downloads") or 0)
            score = matches * 10 + min(popularity / 1000, 5)
            result.append(
                {
                    "id": candidate_id,
                    "title": title,
                    "description": str(item.get("description") or ""),
                    "author": str(item.get("author") or item.get("owner") or ""),
                    "preview_url": str(item.get("preview_url") or item.get("thumbnail") or ""),
                    "source_url": str(item.get("source_url") or item.get("url") or ""),
                    "tags": item.get("tags") if isinstance(item.get("tags"), list) else [],
                    "node_sources": item.get("node_sources") if isinstance(item.get("node_sources"), list) else item.get("custom_nodes") if isinstance(item.get("custom_nodes"), list) else [],
                    "model_sources": item.get("model_sources") if isinstance(item.get("model_sources"), list) else item.get("models") if isinstance(item.get("models"), list) else [],
                    "resource_requirements": item.get("resource_requirements") if isinstance(item.get("resource_requirements"), dict) else {},
                    "execution_mode": str(item.get("execution_mode") or item.get("inference_mode") or "local").strip().lower(),
                    "remote_endpoint": str(item.get("remote_endpoint") or item.get("api_endpoint") or "").strip(),
                    "score": score,
                    "region": region,
                    "raw": item,
                }
            )
        return result
