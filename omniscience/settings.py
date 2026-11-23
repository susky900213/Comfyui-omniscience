"""Local settings file; API keys are never returned by GET settings."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from . import access_tokens, paths, providers, skill_security
from .constants import (
    DEFAULT_COMPACTION_THRESHOLD_PERCENT,
    DEFAULT_MAX_CONTEXT_TOKENS,
)
from .util import (
    compaction_threshold_percent,
    default_workspace,
    max_context_tokens,
)
from .workspace import Workspace


DEFAULT_RUNNINGHUB_DOMESTIC = "https://www.runninghub.cn"
DEFAULT_RUNNINGHUB_INTERNATIONAL = "https://www.runninghub.ai"
DEFAULT_HUGGINGFACE_DOMESTIC = "https://hf-mirror.com"
DEFAULT_HUGGINGFACE_INTERNATIONAL = "https://huggingface.co"
DEFAULT_PUBLIC_IP_URL = "https://ipapi.co/json/"
DEFAULT_IP_GEOLOCATION_URL = "https://ipapi.co/{ip}/json/"


class SettingsStore:
    """Small local settings file; API keys are never returned by GET settings."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path).expanduser() if path is not None else Path(paths.CONFIG_PATH)
        self.legacy_path = Path(paths.LEGACY_CONFIG_PATH) if self.path == Path(paths.CONFIG_PATH) else None
        self._lock = threading.Lock()

    def load(self) -> dict[str, Any]:
        paths_to_try = [self.path]
        if self.legacy_path and self.legacy_path != self.path:
            paths_to_try.append(self.legacy_path)
        for path in paths_to_try:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except (FileNotFoundError, OSError, json.JSONDecodeError):
                continue
        return {}

    def public(self) -> dict[str, Any]:
        data = self.load()
        workspace = data.get("workspace") or str(default_workspace())
        workspace_path = Path(workspace).expanduser()
        workspace_path.mkdir(parents=True, exist_ok=True)
        workspace = str(workspace_path.resolve())
        comfyui_value = data["comfyui_directory"] if "comfyui_directory" in data else paths.DEFAULT_COMFYUI_DIRECTORY
        comfyui_path = Path(str(comfyui_value).strip()).expanduser() if str(comfyui_value or "").strip() else None
        comfyui = str(comfyui_path.resolve()) if comfyui_path and comfyui_path.is_dir() else str(comfyui_value or "").strip()
        api_base_url = data.get("api_base_url") or paths.DEFAULT_API_BASE_URL
        custom = data.get("custom_providers")
        provider = str(data.get("provider") or "").strip()
        known = {entry["id"] for entry in providers.public_providers(custom)}
        if provider not in known:
            provider = providers.infer(api_base_url)
        return {
            "provider": provider,
            "providers": providers.public_providers(custom),
            "api_base_url": api_base_url,
            "browser_cdp_url": data.get("browser_cdp_url") or "auto",
            "global_network_enabled": bool(data.get("global_network_enabled")),
            "network_region": str(data.get("network_region") or "domestic"),
            "runninghub_domestic_base_url": DEFAULT_RUNNINGHUB_DOMESTIC,
            "runninghub_international_base_url": DEFAULT_RUNNINGHUB_INTERNATIONAL,
            "runninghub_search_path": str(data.get("runninghub_search_path") or "/api/search/workflow"),
            "runninghub_workflow_path": str(data.get("runninghub_workflow_path") or "/api/workflow/export"),
            "huggingface_domestic_base_url": str(data.get("huggingface_domestic_base_url") or DEFAULT_HUGGINGFACE_DOMESTIC),
            "huggingface_international_base_url": str(
                data.get("huggingface_international_base_url") or DEFAULT_HUGGINGFACE_INTERNATIONAL
            ),
            "ip_geolocation_url": str(data.get("ip_geolocation_url") or DEFAULT_IP_GEOLOCATION_URL),
            "public_ip_url": str(data.get("public_ip_url") or DEFAULT_PUBLIC_IP_URL),
            "skill_security_mode": skill_security.resolve_review_mode(data),
            "skill_security_model": str(data.get("skill_security_model") or "").strip(),
            "model": data.get("model") or paths.DEFAULT_MODEL,
            "max_context_tokens": max_context_tokens(data.get("max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS)),
            "compaction_threshold_percent": compaction_threshold_percent(
                data.get("compaction_threshold_percent", DEFAULT_COMPACTION_THRESHOLD_PERCENT)
            ),
            "workspace": workspace,
            "comfyui_directory": comfyui,
            "comfyui_server_url": str(data.get("comfyui_server_url") or "http://127.0.0.1:8188"),
            "api_key_configured": bool(data.get("api_key") or paths.env("OMNISCIENCE_API_KEY", "CLINE_API_KEY")),
            "api_key_from_environment": bool(paths.env("OMNISCIENCE_API_KEY", "CLINE_API_KEY")),
            "gitcode_token_configured": bool(self.gitcode_token()),
            "gitcode_token_from_environment": bool(paths.env("GITCODE_TOKEN", "CLINE_GITCODE_TOKEN")),
        }

    def update(self, values: dict[str, Any]) -> dict[str, Any]:
        current = self.load()
        for key in (
            "api_base_url",
            "model",
            "workspace",
            "comfyui_directory",
            "comfyui_server_url",
            "browser_cdp_url",
            "network_region",
            "runninghub_search_path",
            "runninghub_workflow_path",
            "huggingface_domestic_base_url",
            "huggingface_international_base_url",
            "ip_geolocation_url",
            "public_ip_url",
        ):
            if key in values and values[key] is not None:
                current[key] = str(values[key]).strip()
        if "global_network_enabled" in values and values["global_network_enabled"] is not None:
            current["global_network_enabled"] = bool(values["global_network_enabled"])
        if "network_region" in values and values["network_region"] is not None:
            region = str(values["network_region"]).strip().lower()
            if region not in {"auto", "domestic", "international"}:
                raise ValueError("网络区域只能是 auto、domestic 或 international。")
            current["network_region"] = region
        if "skill_security_mode" in values and values["skill_security_mode"] is not None:
            mode = str(values["skill_security_mode"]).strip().lower()
            if mode not in skill_security.REVIEW_MODES:
                raise ValueError(f"Skill 安全检测方式只能是 {'、'.join(skill_security.REVIEW_MODES)}。")
            current["skill_security_mode"] = mode
        if "skill_security_model" in values:
            review_model = str(values["skill_security_model"] or "").strip()
            if review_model:
                current["skill_security_model"] = review_model
            else:
                current.pop("skill_security_model", None)
        if "custom_providers" in values and values["custom_providers"] is not None:
            current["custom_providers"] = providers.normalize_custom_providers(values["custom_providers"])
        if str(values.get("provider") or "").strip():
            provider_id = str(values["provider"]).strip()
            provider = providers.find(provider_id, current.get("custom_providers"))
            if provider is None:
                raise ValueError(f"未知 Provider：{provider_id}")
            current["provider"] = provider_id
            # Provider presets only fill defaults; explicit values in this
            # same request (or a provider without a default) never get clobbered.
            if provider["api_base_url"] and not str(values.get("api_base_url") or "").strip():
                current["api_base_url"] = provider["api_base_url"]
            if provider["models"] and not str(values.get("model") or "").strip() and str(current.get("model") or "") not in provider["models"]:
                current["model"] = provider["models"][0]
        if "max_context_tokens" in values and values["max_context_tokens"] is not None:
            current["max_context_tokens"] = max_context_tokens(values["max_context_tokens"])
        if "compaction_threshold_percent" in values and values["compaction_threshold_percent"] is not None:
            current["compaction_threshold_percent"] = compaction_threshold_percent(values["compaction_threshold_percent"])
        if "api_key" in values:
            key = str(values.get("api_key") or "").strip()
            if key:
                current["api_key"] = key
            # An empty field means "leave the stored key unchanged". This
            # prevents opening Settings and clicking Save from deleting it.
        if "gitcode_token" in values:
            token = str(values.get("gitcode_token") or "").strip()
            if token:
                current["gitcode_token"] = token
            # Environment-provided tokens and stored tokens are preserved when
            # the password field is left blank.
        workspace = current.get("workspace") or str(default_workspace())
        workspace_path = Path(workspace).expanduser()
        workspace_path.mkdir(parents=True, exist_ok=True)
        Workspace(workspace_path)
        current["workspace"] = str(workspace_path.resolve())
        comfyui_value = current.get("comfyui_directory")
        if comfyui_value:
            comfyui_path = Path(str(comfyui_value)).expanduser()
            if not comfyui_path.is_dir():
                raise ValueError(f"ComfyUI 目录不存在或不是文件夹：{comfyui_path}")
            current["comfyui_directory"] = str(comfyui_path.resolve())
        current.setdefault("api_base_url", paths.DEFAULT_API_BASE_URL)
        current.setdefault("model", paths.DEFAULT_MODEL)
        current["max_context_tokens"] = max_context_tokens(current.get("max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS))
        current["compaction_threshold_percent"] = compaction_threshold_percent(
            current.get("compaction_threshold_percent", DEFAULT_COMPACTION_THRESHOLD_PERCENT)
        )
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        return self.public()

    def api_key(self) -> str:
        return paths.env("OMNISCIENCE_API_KEY", "CLINE_API_KEY") or str(self.load().get("api_key") or "")

    def gitcode_token(self) -> str:
        return access_tokens.resolve("gitcode", self.load().get("gitcode_token"))

    def comfyui_node_directory(self) -> Path | None:
        data = self.load()
        value = data["comfyui_directory"] if "comfyui_directory" in data else paths.DEFAULT_COMFYUI_DIRECTORY
        if not str(value or "").strip():
            return None
        comfyui_path = Path(str(value)).expanduser().resolve()
        if not comfyui_path.is_dir():
            raise ValueError(f"ComfyUI 目录不存在或不是文件夹：{comfyui_path}")
        node_directory = comfyui_path / "custom_nodes"
        node_directory.mkdir(parents=True, exist_ok=True)
        if not node_directory.is_dir():
            raise ValueError(f"ComfyUI custom_nodes 不是文件夹：{node_directory}")
        return node_directory.resolve()

    def comfyui_workflow_directory(self) -> Path | None:
        """Return ComfyUI's default user's saved-workflow directory."""
        data = self.load()
        value = data["comfyui_directory"] if "comfyui_directory" in data else paths.DEFAULT_COMFYUI_DIRECTORY
        if not str(value or "").strip():
            return None
        comfyui_path = Path(str(value)).expanduser().resolve()
        if not comfyui_path.is_dir():
            raise ValueError(f"ComfyUI 目录不存在或不是文件夹：{comfyui_path}")
        workflow_directory = comfyui_path / "user" / "default" / "workflows"
        workflow_directory.mkdir(parents=True, exist_ok=True)
        if not workflow_directory.is_dir():
            raise ValueError(f"ComfyUI 工作流目录不是文件夹：{workflow_directory}")
        return workflow_directory.resolve()
