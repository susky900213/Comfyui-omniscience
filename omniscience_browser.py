"""Optional Playwright browser adapter.

Browser automation is intentionally optional: ComfyUI installations that do
not install Playwright still load normally and receive a useful dependency
error when a browser tool is requested.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any


class BrowserUnavailable(RuntimeError):
    pass


class BrowserManager:
    def __init__(self):
        self._playwright = None
        self._browser = None
        self._pages: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def _ensure(self) -> None:
        if self._playwright is not None and self._browser is not None:
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BrowserUnavailable(
                "Browser automation is not installed. Install 'playwright' and run 'playwright install chromium'."
            ) from exc
        self._playwright = await async_playwright().start()
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
            page = await self._browser.new_page()
            self._pages[task_id] = page
        return page

    async def navigate(self, task_id: str, url: str) -> dict[str, Any]:
        if not url.startswith(("http://", "https://")):
            raise ValueError("Browser navigation only supports http:// and https:// URLs.")
        async with self._lock:
            page = await self._page(task_id)
            response = await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            return {"url": page.url, "status": response.status if response else None, "title": await page.title()}

    async def content(self, task_id: str, selector: str = "") -> dict[str, Any]:
        async with self._lock:
            page = await self._page(task_id)
            if selector:
                text = await page.locator(selector).inner_text(timeout=10_000)
            else:
                text = await page.locator("body").inner_text(timeout=10_000)
            return {"url": page.url, "title": await page.title(), "text": text[:100_000]}

    async def screenshot(self, task_id: str, destination: Path) -> dict[str, Any]:
        async with self._lock:
            page = await self._page(task_id)
            destination.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(destination), full_page=True)
            return {"path": str(destination), "url": page.url}

    async def click(self, task_id: str, selector: str) -> dict[str, Any]:
        async with self._lock:
            page = await self._page(task_id)
            await page.locator(selector).click(timeout=10_000)
            return {"url": page.url, "selector": selector}

    async def fill(self, task_id: str, selector: str, value: str) -> dict[str, Any]:
        async with self._lock:
            page = await self._page(task_id)
            await page.locator(selector).fill(value, timeout=10_000)
            return {"url": page.url, "selector": selector}

    async def close(self, task_id: str) -> None:
        async with self._lock:
            page = self._pages.pop(task_id, None)
            if page is not None and not page.is_closed():
                await page.close()

    async def shutdown(self) -> None:
        async with self._lock:
            for page in list(self._pages.values()):
                if not page.is_closed():
                    await page.close()
            self._pages.clear()
            if self._browser is not None:
                await self._browser.close()
            if self._playwright is not None:
                await self._playwright.stop()
            self._browser = None
            self._playwright = None
