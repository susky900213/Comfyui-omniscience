"""Region-aware endpoint selection for external workflow and model services."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

REGIONS = {"domestic", "international"}
MODES = {"auto", *REGIONS}
DEFAULT_ENDPOINTS = {
    "runninghub_domestic_base_url": "https://www.runninghub.cn",
    "runninghub_international_base_url": "https://www.runninghub.ai",
    "huggingface_domestic_base_url": "https://hf-mirror.com",
    "huggingface_international_base_url": "https://huggingface.co",
    "public_ip_url": "https://ipapi.co/json/",
    "ip_geolocation_url": "https://ipapi.co/{ip}/json/",
    "github_proxy_base_url": "https://github.tbap.top",
}


@dataclass(frozen=True)
class RegionDecision:
    mode: str
    detected_region: str | None
    selected_region: str
    confidence: float | None
    detected_by: str
    failover_used: bool = False


class RegionRouter:
    """Choose a configured service endpoint from the machine's public exit region."""

    def __init__(
        self,
        settings: Any,
        *,
        fetch_public_ip: Callable[[], str] | None = None,
        geolocate: Callable[[str], dict[str, Any]] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings
        self.fetch_public_ip = fetch_public_ip or configured_public_ip_fetcher(settings)
        self.geolocate = geolocate or configured_geolocator(settings)
        self.clock = clock
        self._cached: tuple[float, dict[str, Any]] | None = None

    def _data(self) -> dict[str, Any]:
        raw = self.settings.load() if hasattr(self.settings, "load") else dict(self.settings or {})
        merged = {**DEFAULT_ENDPOINTS, **raw}
        merged["runninghub_domestic_base_url"] = DEFAULT_ENDPOINTS["runninghub_domestic_base_url"]
        merged["runninghub_international_base_url"] = DEFAULT_ENDPOINTS["runninghub_international_base_url"]
        merged["huggingface_domestic_base_url"] = str(
            raw.get("huggingface_domestic_base_url") or DEFAULT_ENDPOINTS["huggingface_domestic_base_url"]
        )
        merged["huggingface_international_base_url"] = str(
            raw.get("huggingface_international_base_url")
            or DEFAULT_ENDPOINTS["huggingface_international_base_url"]
        )
        return merged

    def _mode(self) -> str:
        value = str(self._data().get("network_region") or "domestic").strip().lower()
        return value if value in MODES else "auto"

    def detect(self, *, force: bool = False) -> dict[str, Any]:
        data = self._data()
        now = self.clock()
        cache_minutes = max(1, min(1440, int(data.get("ip_geolocation_cache_minutes") or 60)))
        if not force and self._cached and now - self._cached[0] < cache_minutes * 60:
            return dict(self._cached[1])
        try:
            public_ip = str(self.fetch_public_ip() or "").strip()
            if not public_ip:
                raise ValueError("未获得本机公网 IP。")
            result = dict(self.geolocate(public_ip) or {})
            region = str(result.get("region") or "").strip().lower()
            country_code = str(result.get("country_code") or result.get("countryCode") or "").strip().upper()
            if region not in REGIONS and country_code:
                region = "domestic" if country_code == "CN" else "international"
            if region not in REGIONS:
                raise ValueError("IP 地理服务没有返回 domestic/international。")
            detected = {
                "region": region,
                "confidence": float(result["confidence"]) if result.get("confidence") is not None else None,
                "detected_by": str(result.get("detected_by") or "configured_ip_geo_provider"),
                "checked_at": now,
            }
        except Exception as exc:
            detected = {
                "region": None,
                "confidence": None,
                "detected_by": "unavailable",
                "checked_at": now,
                "error": str(exc),
            }
        self._cached = (now, detected)
        return dict(detected)

    def decide(self, *, force: bool = False, region: str | None = None) -> RegionDecision:
        override = str(region or "").strip().lower()
        if override in REGIONS:
            return RegionDecision("manual_request", None, override, None, "user_message")
        mode = self._mode()
        if mode in REGIONS:
            return RegionDecision(mode, None, mode, None, "manual")
        detected = self.detect(force=force)
        region = detected.get("region")
        confidence = detected.get("confidence")
        if region not in REGIONS or confidence is not None and confidence < 0.5:
            raise ValueError("无法可靠判断本机公网 IP 所在地区，请在设置中手动选择国内或国外站点。")
        return RegionDecision("auto", region, region, confidence, str(detected.get("detected_by") or "auto"))

    def endpoint(self, service: str, *, force: bool = False, region: str | None = None) -> tuple[str, RegionDecision]:
        decision = self.decide(force=force, region=region)
        key = f"{service}_{decision.selected_region}_base_url"
        value = str(self._data().get(key) or "").strip().rstrip("/")
        if not value:
            raise ValueError(f"未配置 {service} 的{decision.selected_region}站点地址：{key}。请先在设置中填写国内/国外站点地址。")
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"{key} 不是有效的 HTTP(S) 地址。")
        return value, decision

    def github_url(self, url: str, *, force: bool = False, region: str | None = None) -> tuple[str, RegionDecision]:
        """Route a validated GitHub URL through the domestic proxy when needed.

        The proxy accepts the original HTTPS GitHub URL as a path component.  Do
        not route arbitrary URLs here: callers must provide a GitHub API or
        archive URL, and the resulting proxy URL is constrained to the configured
        proxy host.
        """
        value = str(url or "").strip()
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or host not in {
            "api.github.com",
            "codeload.github.com",
            "github.com",
            "raw.githubusercontent.com",
        } or parsed.port not in {None, 443}:
            raise ValueError("GitHub 请求地址无效。")
        decision = self.decide(force=force, region=region)
        if decision.selected_region == "international":
            return value, decision
        proxy = str(self._data().get("github_proxy_base_url") or DEFAULT_ENDPOINTS["github_proxy_base_url"]).strip().rstrip("/")
        proxy_parsed = urlparse(proxy)
        if proxy_parsed.scheme != "https" or not proxy_parsed.hostname or proxy_parsed.username or proxy_parsed.password or proxy_parsed.port not in {None, 443}:
            raise ValueError("GitHub 国内代理地址必须是无认证的 HTTPS 地址。")
        return f"{proxy}/{value}", decision

    @staticmethod
    def _fetch_public_ip() -> str:
        raise RuntimeError("未配置公网 IP 获取器。请在部署环境中注入受信任的 IP 服务。")

    @staticmethod
    def _geolocate(_public_ip: str) -> dict[str, Any]:
        raise RuntimeError("未配置 IP 地理服务。")

    async def endpoint_async(self, service: str, *, force: bool = False) -> tuple[str, RegionDecision]:
        return await asyncio.to_thread(self.endpoint, service, force=force)


def configured_public_ip_fetcher(settings: Any) -> Callable[[], str]:
    data = settings.load() if hasattr(settings, "load") else dict(settings or {})
    endpoint = str(data.get("public_ip_url") or DEFAULT_ENDPOINTS["public_ip_url"]).strip()
    if not endpoint:
        return RegionRouter._fetch_public_ip

    def fetch() -> str:
        request = Request(endpoint, headers={"Accept": "text/plain, application/json"})
        try:
            with urlopen(request, timeout=10) as response:  # noqa: S310 - endpoint is user-configured
                value = response.read(4096).decode("utf-8").strip()
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise ValueError(f"公网 IP 服务请求失败：{exc}") from exc
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                value = str(parsed.get("ip") or parsed.get("query") or "").strip()
        except json.JSONDecodeError:
            pass
        if not value:
            raise ValueError("公网 IP 服务返回为空。")
        return value

    return fetch


def configured_geolocator(settings: Any) -> Callable[[str], dict[str, Any]]:
    """Build a JSON geolocator from a user-configured endpoint; no URL is hard-coded."""
    data = settings.load() if hasattr(settings, "load") else dict(settings or {})
    endpoint = str(data.get("ip_geolocation_url") or DEFAULT_ENDPOINTS["ip_geolocation_url"]).strip()
    if not endpoint:
        return RegionRouter._geolocate

    def lookup(public_ip: str) -> dict[str, Any]:
        request = Request(endpoint.format(ip=public_ip), headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=10) as response:  # noqa: S310 - configured by the user
                payload = json.loads(response.read(200_000).decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
            raise ValueError(f"IP 地理服务请求失败：{exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError("IP 地理服务返回格式无效。")
        return payload

    return lookup
