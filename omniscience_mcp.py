"""Small MCP client for Comfyui-omniscience.

The client deliberately implements the protocol boundary instead of importing
an optional MCP SDK.  That keeps the custom node installable with ComfyUI's
normal dependencies while supporting the standard stdio transport and the
common Streamable HTTP request/response form.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = "2025-06-18"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@dataclass
class MCPServer:
    name: str
    config: dict[str, Any]

    @property
    def disabled(self) -> bool:
        return bool(self.config.get("disabled", False))

    @property
    def auto_approve(self) -> set[str]:
        values = self.config.get("autoApprove", self.config.get("auto_approve", []))
        return {str(value) for value in values} if isinstance(values, list) else set()


class MCPProtocolError(RuntimeError):
    pass


class MCPManager:
    def __init__(self, path: Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._stdio_tools: dict[str, dict[str, Any]] = {}

    def load(self) -> dict[str, MCPServer]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            raw = {}
        if "mcpServers" in raw and isinstance(raw["mcpServers"], dict):
            raw = raw["mcpServers"]
        if not isinstance(raw, dict):
            return {}
        return {str(name): MCPServer(str(name), value) for name, value in raw.items() if isinstance(value, dict)}

    def public(self) -> list[dict[str, Any]]:
        result = []
        for server in self.load().values():
            config = server.config
            result.append(
                {
                    "name": server.name,
                    "transport": "stdio" if config.get("command") else config.get("type", "streamableHttp"),
                    "url": config.get("url", ""),
                    "command": config.get("command", ""),
                    "args": config.get("args", []),
                    "disabled": server.disabled,
                    "autoApprove": sorted(server.auto_approve),
                }
            )
        return result

    def update(self, values: dict[str, Any]) -> list[dict[str, Any]]:
        servers = values.get("mcpServers", values)
        if not isinstance(servers, dict):
            raise ValueError("MCP configuration must be an object keyed by server name.")
        cleaned: dict[str, dict[str, Any]] = {}
        for name, config in servers.items():
            if not isinstance(config, dict) or not str(name).strip():
                raise ValueError("Each MCP server must have a name and object configuration.")
            value = dict(config)
            if value.get("command"):
                value["command"] = str(value["command"])
                value["args"] = [str(argument) for argument in value.get("args", [])]
            elif value.get("url"):
                value["url"] = str(value["url"])
            else:
                raise ValueError(f"MCP server '{name}' needs command or url.")
            cleaned[str(name)] = value
        with self._lock:
            self.path.write_text(_json({"mcpServers": cleaned}), encoding="utf-8")
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        self._stdio_tools.clear()
        return self.public()

    @staticmethod
    def _request_id() -> int:
        return int(uuid.uuid4().int % 2_000_000_000)

    @classmethod
    def _message(cls, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": cls._request_id(), "method": method, "params": params or {}}

    @staticmethod
    def _validate_response(response: dict[str, Any], method: str) -> Any:
        if "error" in response:
            error = response.get("error") or {}
            raise MCPProtocolError(f"MCP {method} failed ({error.get('code')}): {error.get('message', error)}")
        if "result" not in response:
            raise MCPProtocolError(f"MCP {method} returned no result.")
        return response["result"]

    async def _stdio_request(self, server: MCPServer, requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
        command = str(server.config.get("command") or "").strip()
        args = [str(value) for value in server.config.get("args", [])]
        if not command:
            raise MCPProtocolError(f"MCP server '{server.name}' has no stdio command.")
        environment = os.environ.copy()
        environment.update({str(key): str(value) for key, value in (server.config.get("env") or {}).items()})
        try:
            process = await asyncio.create_subprocess_exec(
                command,
                *args,
                cwd=server.config.get("cwd") or None,
                env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:
            raise MCPProtocolError(f"Unable to start MCP server '{server.name}': {exc}") from exc
        responses: list[dict[str, Any]] = []
        try:
            assert process.stdin is not None and process.stdout is not None
            for message in requests:
                process.stdin.write((_json(message) + "\n").encode("utf-8"))
                await process.stdin.drain()
                expected = message.get("id")
                if expected is None:
                    # JSON-RPC notifications, including
                    # notifications/initialized, have no response.
                    continue
                while True:
                    line = await asyncio.wait_for(process.stdout.readline(), timeout=60)
                    if not line:
                        error = (await process.stderr.read()).decode("utf-8", errors="replace")[-2_000:]
                        raise MCPProtocolError(f"MCP server '{server.name}' closed stdout. {error}")
                    try:
                        response = json.loads(line.decode("utf-8"))
                    except json.JSONDecodeError:
                        continue
                    if response.get("id") == expected:
                        responses.append(response)
                        break
            return responses
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()

    @staticmethod
    def _http_request(server: MCPServer, messages: list[dict[str, Any]], session_id: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
        url = str(server.config.get("url") or "")
        if not url:
            raise MCPProtocolError(f"MCP server '{server.name}' has no HTTP URL.")
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        headers.update({str(key): str(value) for key, value in (server.config.get("headers") or {}).items()})
        headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        responses: list[dict[str, Any]] = []
        current_session = session_id
        for message in messages:
            request = urllib.request.Request(url, data=_json(message).encode("utf-8"), headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - configured by the user
                    current_session = response.headers.get("Mcp-Session-Id", current_session)
                    body = response.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:2_000]
                raise MCPProtocolError(f"MCP HTTP {exc.code}: {detail}") from exc
            except (urllib.error.URLError, OSError) as exc:
                raise MCPProtocolError(f"MCP HTTP request failed: {exc}") from exc
            if message.get("id") is None:
                continue
            for line in body.splitlines():
                if line.startswith("data:"):
                    line = line[5:].strip()
                if not line or line.startswith(":"):
                    continue
                try:
                    candidate = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict) and candidate.get("id") == message.get("id"):
                    responses.append(candidate)
                    break
        return responses, current_session

    async def _request(self, server: MCPServer, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if server.config.get("command"):
            return await self._stdio_request(server, messages)
        return (await asyncio.to_thread(self._http_request, server, messages))[0]

    async def _session(self, server: MCPServer, method: str, params: dict[str, Any] | None = None) -> Any:
        initialize = self._message(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "Comfyui-omniscience", "version": "0.2.0"},
            },
        )
        request = self._message(method, params)
        responses = await self._request(server, [initialize, {"jsonrpc": "2.0", "method": "notifications/initialized"}, request])
        if not responses:
            raise MCPProtocolError(f"MCP server '{server.name}' did not answer {method}.")
        return self._validate_response(responses[-1], method)

    async def list_tools(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        self._stdio_tools.clear()
        for server in self.load().values():
            if server.disabled:
                continue
            try:
                payload = await self._session(server, "tools/list")
            except MCPProtocolError as exc:
                result.append({"server": server.name, "error": str(exc), "tools": []})
                continue
            tools = payload.get("tools", []) if isinstance(payload, dict) else []
            public_tools = []
            for tool in tools:
                if not isinstance(tool, dict) or not tool.get("name"):
                    continue
                name = f"mcp__{server.name}__{tool['name']}"
                self._stdio_tools[name] = {"server": server.name, "tool": str(tool["name"])}
                public_tools.append({"name": name, "original_name": tool["name"], "description": tool.get("description", ""), "inputSchema": tool.get("inputSchema", {"type": "object"}), "autoApprove": tool["name"] in server.auto_approve})
            result.append({"server": server.name, "tools": public_tools})
        return result

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in self._stdio_tools:
            await self.list_tools()
        mapping = self._stdio_tools.get(name)
        if not mapping:
            raise ValueError(f"Unknown MCP tool: {name}")
        server = self.load().get(mapping["server"])
        if not server or server.disabled:
            raise MCPProtocolError(f"MCP server '{mapping['server']}' is disabled or missing.")
        payload = await self._session(server, "tools/call", {"name": mapping["tool"], "arguments": arguments})
        if isinstance(payload, dict) and payload.get("isError"):
            raise MCPProtocolError(_json(payload.get("content", payload)))
        return payload

    def is_auto_approved(self, name: str) -> bool:
        mapping = self._stdio_tools.get(name)
        if not mapping:
            return False
        server = self.load().get(mapping["server"])
        return bool(server and mapping["tool"] in server.auto_approve)
