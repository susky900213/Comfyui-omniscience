"""OpenAI-compatible chat-completions transport (aiohttp, non-streaming).

A cancelled agent task cancels the provider request itself, instead of
leaving a blocking network thread alive until timeout -- the reason aiohttp
is used here rather than urllib in a worker thread.
"""

from __future__ import annotations

import asyncio
from typing import Any

from . import paths
from .constants import MODEL_TEMPERATURE, MODEL_TIMEOUT_SECONDS
from .util import json_text


async def complete(
    *,
    model: str,
    api_base_url: str,
    api_key: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | None = None,
    temperature: float = MODEL_TEMPERATURE,
    max_tokens: int = 4096,
) -> dict[str, Any]:
    model = str(model or "").strip()
    if not model:
        raise ValueError("No model configured. Open Comfyui-omniscience settings and enter a model name.")
    base_url = str(api_base_url or paths.DEFAULT_API_BASE_URL).rstrip("/")
    endpoint = base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice or "auto"
    try:
        from aiohttp import ClientError, ClientSession, ClientTimeout

        timeout = ClientTimeout(total=MODEL_TIMEOUT_SECONDS)
        async with ClientSession(timeout=timeout) as session:
            async with session.post(endpoint, json=payload, headers=headers) as response:
                if response.status >= 400:
                    detail = (await response.text())[:2_000]
                    raise RuntimeError(f"模型 API 返回 HTTP {response.status}: {detail}")
                body = await response.json(content_type=None)
    except asyncio.TimeoutError as exc:
        raise RuntimeError("模型 API 请求超时。") from exc
    except ClientError as exc:
        raise RuntimeError(f"无法连接模型 API: {exc}") from exc
    if not isinstance(body, dict) or not body.get("choices"):
        raise RuntimeError(f"模型 API 返回了无法识别的响应: {json_text(body, 2_000)}")
    return body


def extract_content(response: dict[str, Any]) -> str:
    choices = response.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return str(message.get("content") or "")
