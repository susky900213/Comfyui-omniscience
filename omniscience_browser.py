"""Optional Playwright browser adapter.

Browser automation is intentionally optional: ComfyUI installations that do
not install Playwright still load normally and receive a useful dependency
error when a browser tool is requested.

When a CDP endpoint is configured (or ``auto`` detects a browser started with
``--remote-debugging-port``), the manager attaches to that existing user
browser and reuses its real profile (cookies, logins).  Otherwise it falls
back to a private headless Chromium it owns.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable


class BrowserUnavailable(RuntimeError):
    pass


DEFAULT_CDP_URL = "http://127.0.0.1:9222"


class BrowserManager:
    def __init__(self, config: Callable[[], dict[str, Any]] | None = None):
        self.config = config or (lambda: {})
        self._playwright = None
        self._browser = None
        self._user_browser = False
        self._pages: dict[str, Any] = {}
        # One lock per task keeps browser work concurrent across tasks while
        # steps within a task stay serialized; _init_lock guards startup.
        self._task_locks: dict[str, asyncio.Lock] = {}
        self._init_lock = asyncio.Lock()
        self._shutdown_lock = asyncio.Lock()

    def _lock_for(self, task_id: str) -> asyncio.Lock:
        lock = self._task_locks.get(task_id)
        if lock is None:
            lock = self._task_locks[task_id] = asyncio.Lock()
        return lock

    def _cdp_setting(self) -> str:
        return str(self.config().get("browser_cdp_url") or "").strip()

    async def _start_playwright(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BrowserUnavailable(
                "Browser automation is not installed. Install 'playwright' and run 'playwright install chromium'."
            ) from exc
        return await async_playwright().start()

    async def _connect_user_browser(self, playwright, cdp_url: str):
        try:
            return await playwright.chromium.connect_over_cdp(cdp_url, timeout=3000)
        except Exception as exc:
            raise BrowserUnavailable(
                f"无法连接已打开的浏览器（{cdp_url}）：{exc}。请用 --remote-debugging-port 启动 Chrome/Edge，或改用内置浏览器。"
            ) from exc

    async def _ensure(self) -> None:
        if self._browser is not None and not self._user_browser:
            return
        if self._browser is not None and self._browser.is_connected():
            return
        self._browser = None
        self._user_browser = False
        async with self._init_lock:
            if self._browser is not None and (not self._user_browser or self._browser.is_connected()):
                return
            if self._playwright is None:
                self._playwright = await self._start_playwright()
            setting = self._cdp_setting()
            explicit = bool(setting) and setting.lower() != "auto"
            if setting.lower() == "auto" or explicit:
                url = DEFAULT_CDP_URL if setting.lower() == "auto" else setting
                try:
                    browser = await self._connect_user_browser(self._playwright, url)
                except BrowserUnavailable:
                    if explicit:
                        raise
                    # auto mode without a debuggable browser: use headless.
                else:
                    self._browser = browser
                    self._user_browser = True
                    return
            try:
                self._browser = await self._playwright.chromium.launch(headless=True)
            except Exception:
                await self._playwright.stop()
                self._playwright = None
                raise

    async def _page(self, task_id: str) -> Any:
        await self._ensure()
        page = self._pages.get(task_id)
        if page is None or page.is_closed():
            if self._user_browser:
                # Attach to the real user profile so cookies and logins work.
                context = self._browser.contexts[0] if self._browser.contexts else await self._browser.new_context()
                page = await context.new_page()
            else:
                page = await self._browser.new_page()
            self._pages[task_id] = page
        return page

    async def navigate(self, task_id: str, url: str) -> dict[str, Any]:
        if not url.startswith(("http://", "https://")):
            raise ValueError("Browser navigation only supports http:// and https:// URLs.")
        async with self._lock_for(task_id):
            page = await self._page(task_id)
            response = await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            return {"url": page.url, "status": response.status if response else None, "title": await page.title()}

    async def content(self, task_id: str, selector: str = "") -> dict[str, Any]:
        async with self._lock_for(task_id):
            page = await self._page(task_id)
            if selector:
                text = await page.locator(selector).inner_text(timeout=10_000)
            else:
                text = await page.locator("body").inner_text(timeout=10_000)
            return {"url": page.url, "title": await page.title(), "text": text[:100_000]}

    async def screenshot(self, task_id: str, destination: Path) -> dict[str, Any]:
        async with self._lock_for(task_id):
            page = await self._page(task_id)
            destination.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(destination), full_page=True)
            return {"path": str(destination), "url": page.url}

    async def click(self, task_id: str, selector: str) -> dict[str, Any]:
        async with self._lock_for(task_id):
            page = await self._page(task_id)
            await page.locator(selector).click(timeout=10_000)
            return {"url": page.url, "selector": selector}

    async def fill(self, task_id: str, selector: str, value: str) -> dict[str, Any]:
        async with self._lock_for(task_id):
            page = await self._page(task_id)
            await page.locator(selector).fill(value, timeout=10_000)
            return {"url": page.url, "selector": selector}

    async def close(self, task_id: str) -> None:
        async with self._lock_for(task_id):
            page = self._pages.pop(task_id, None)
            self._task_locks.pop(task_id, None)
            if page is not None and not page.is_closed():
                await page.close()

    async def shutdown(self) -> None:
        async with self._shutdown_lock:
            for page in list(self._pages.values()):
                if not page.is_closed():
                    await page.close()
            self._pages.clear()
            self._task_locks.clear()
            # Never close the user's own browser; only the private instance we launched.
            if self._browser is not None and not self._user_browser:
                await self._browser.close()
            if self._playwright is not None:
                await self._playwright.stop()
            self._browser = None
            self._user_browser = False
            self._playwright = None
