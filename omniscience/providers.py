"""Built-in model provider presets for the Omniscience-style settings picker.

Providers are OpenAI-compatible endpoint presets. Selecting one only fills
defaults (base URL and suggested models) into the flat ``api_base_url`` and
``model`` settings; explicitly saved values always win, so runtime code keeps
reading those two fields unchanged.
"""

from __future__ import annotations

from typing import Any

CUSTOM_PROVIDER_ID = "custom"

BUILTIN_PROVIDERS: list[dict[str, Any]] = [
    {"id": "openai", "label": "OpenAI", "api_base_url": "https://api.openai.com/v1", "models": ["gpt-5.1", "gpt-5.1-mini", "gpt-4.1-mini"], "key_required": True},
    {"id": "anthropic", "label": "Anthropic", "api_base_url": "https://api.anthropic.com/v1", "models": ["claude-sonnet-4-5", "claude-haiku-4-5"], "key_required": True},
    {"id": "openrouter", "label": "OpenRouter", "api_base_url": "https://openrouter.ai/api/v1", "models": [], "key_required": True},
    {"id": "deepseek", "label": "DeepSeek", "api_base_url": "https://api.deepseek.com/v1", "models": ["deepseek-chat", "deepseek-reasoner"], "key_required": True},
    {"id": "moonshot", "label": "Moonshot (Kimi)", "api_base_url": "https://api.moonshot.cn/v1", "models": ["kimi-latest"], "key_required": True},
    {"id": "zhipu", "label": "智谱 GLM", "api_base_url": "https://open.bigmodel.cn/api/paas/v4", "models": ["glm-4.6", "glm-4.5-air"], "key_required": True},
    {"id": "siliconflow", "label": "SiliconFlow", "api_base_url": "https://api.siliconflow.cn/v1", "models": [], "key_required": True},
    {"id": "ollama", "label": "Ollama（本地）", "api_base_url": "http://127.0.0.1:11434/v1", "models": ["qwen3", "llama3.2"], "key_required": False},
    {"id": "lmstudio", "label": "LM Studio（本地）", "api_base_url": "http://127.0.0.1:1234/v1", "models": [], "key_required": False},
    {"id": CUSTOM_PROVIDER_ID, "label": "自定义 OpenAI 兼容端点", "api_base_url": "", "models": [], "key_required": False},
]


def custom_id(name: str) -> str:
    return f"custom:{name}"


def normalize_custom_providers(values: Any) -> list[dict[str, Any]]:
    """Validate the user-managed provider list and assign stable ids."""
    if values in (None, ""):
        return []
    if not isinstance(values, list):
        raise ValueError("自定义 Provider 必须是列表。")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in values:
        if not isinstance(entry, dict):
            raise ValueError("每个自定义 Provider 必须是对象。")
        name = str(entry.get("name") or "").strip()
        base_url = str(entry.get("api_base_url") or "").strip()
        if not name or not base_url:
            raise ValueError("自定义 Provider 需要名称和 API 地址。")
        identifier = custom_id(name)
        if identifier in seen:
            raise ValueError(f"自定义 Provider 名称重复：{name}")
        seen.add(identifier)
        models = entry.get("models")
        models = [str(model).strip() for model in models if str(model).strip()] if isinstance(models, list) else []
        result.append({"id": identifier, "name": name, "label": name, "api_base_url": base_url, "models": models, "key_required": False})
    return result


def public_providers(custom_providers: Any) -> list[dict[str, Any]]:
    return [dict(provider) for provider in BUILTIN_PROVIDERS] + normalize_custom_providers(custom_providers)


def find(provider_id: str, custom_providers: Any) -> dict[str, Any] | None:
    wanted = str(provider_id or "").strip()
    for provider in public_providers(custom_providers):
        if provider["id"] == wanted:
            return provider
    return None


def infer(base_url: str) -> str:
    """Best-effort provider id for a stored base URL (legacy configs)."""
    wanted = str(base_url or "").strip().rstrip("/")
    if not wanted:
        return "openai"
    for provider in BUILTIN_PROVIDERS:
        default = str(provider["api_base_url"] or "").strip().rstrip("/")
        if default and default == wanted:
            return str(provider["id"])
    return CUSTOM_PROVIDER_ID
