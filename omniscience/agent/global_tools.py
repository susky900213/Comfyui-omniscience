"""Tool surface for the task-independent global conversation.

Global chat has no task, so it works against a private scratch workspace
(``paths.global_workspace()``) with read-only file tools, plus the browsing
subset once the user enables network access for the conversation.
"""

from __future__ import annotations

from typing import Any

from .. import paths
from ..tools.defs import global_chat_definitions
from ..workspace import Workspace

GLOBAL_NETWORK_DENIED = "全局对话未开启网络访问。请点击输入区下方的“网络访问”按钮开启后再调用浏览器工具。"
GLOBAL_BROWSER_SESSION = "global"


class GlobalChatToolMixin:
    def chat_workspace(self) -> Workspace:
        root = paths.global_workspace()
        root.mkdir(parents=True, exist_ok=True)
        return Workspace(root)

    def chat_network_allowed(self) -> bool:
        return bool(self.settings.load().get("global_network_enabled"))

    def chat_tools(
        self,
        install_requested: bool,
        node_install_requested: bool = False,
        workflow_requested: bool = False,
        workflow_install_requested: bool = False,
    ) -> list[dict[str, Any]]:
        return global_chat_definitions(
            install_requested=install_requested,
            node_install_requested=node_install_requested,
            workflow_requested=workflow_requested,
            workflow_install_requested=workflow_install_requested,
            network_enabled=self.chat_network_allowed(),
        )

    def uploaded_workflow_sources(self) -> list[str]:
        """Return uploaded JSON workflow paths visible to global chat."""
        uploads_root = self.chat_workspace().root / "uploads"
        uploads_root.mkdir(parents=True, exist_ok=True)
        return [
            str(entry.get("path") or "")
            for entry in self.chat_workspace().list_files("uploads", recursive=False)
            if entry.get("type") == "file" and str(entry.get("name") or "").casefold().endswith(".json")
        ]

    def default_uploaded_workflow_source(self) -> str:
        """Choose the newest uploaded workflow when the user says @workflow."""
        workspace = self.chat_workspace()
        candidates = []
        for source in self.uploaded_workflow_sources():
            try:
                path = workspace.resolve(source)
                candidates.append((path.stat().st_mtime_ns, source))
            except OSError:
                continue
        return max(candidates, default=(0, ""))[1]

    def chat_prompt_note(self) -> str:
        workspace = self.chat_workspace()
        network = (
            "已开启网络访问：可以调用 browser_navigate、browser_content、browser_screenshot 查阅公开网页。"
            if self.chat_network_allowed()
            else "未开启网络访问：浏览器工具会被拒绝。需要联网时提醒用户点击输入区下方的“网络访问”按钮。"
        )
        return (
            "\n\n## 全局工作区\n"
            f"用户上传的文件存放在 {workspace.root} 的 uploads/ 目录，可用 list_files、read_file、search_files 查阅；"
            "消息中以 @ 标注的文件内容会随消息一起提供。\n"
            f"## 网络访问\n{network}"
        )

    async def execute_chat_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        workspace = self.chat_workspace()
        if name == "list_files":
            return workspace.list_files(arguments.get("path", "."), bool(arguments.get("recursive", False)))
        if name == "read_file":
            return {"path": arguments.get("path"), "content": workspace.read_file(arguments["path"])}
        if name == "search_files":
            return workspace.search_files(arguments["query"], arguments.get("path", "."))
        if not name.startswith("browser_"):
            raise ValueError(f"全局对话不支持此工具：{name}")
        if not self.chat_network_allowed():
            return {"error": GLOBAL_NETWORK_DENIED}
        if name == "browser_navigate":
            return await self.browser.navigate(GLOBAL_BROWSER_SESSION, str(arguments["url"]))
        if name == "browser_content":
            return await self.browser.content(GLOBAL_BROWSER_SESSION, str(arguments.get("selector") or ""))
        if name == "browser_screenshot":
            destination = workspace.resolve(str(arguments.get("path") or f"screenshots/{GLOBAL_BROWSER_SESSION}.png"))
            return await self.browser.screenshot(GLOBAL_BROWSER_SESSION, destination)
        raise ValueError(f"全局对话不支持此浏览器工具：{name}")
