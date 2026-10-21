"""雅虎页跳转/水合等待。

上限 5 分钟，目标已到达立刻返回。只点看得到的节点，避免 DOM 里有、实际点不到时空等 30 秒判失败。
"""
import asyncio
import re

from .constants import WAIT_CLICK_MS, WAIT_HANG_MS


class WaitMixin:
    async def click_visible(self, locator, timeout_ms=WAIT_CLICK_MS, force=False):
        try:
            n = await locator.count()
        except Exception:
            return False
        for i in range(min(n, 8)):
            loc = locator.nth(i)
            try:
                if not await loc.is_visible():
                    continue
                await loc.click(timeout=timeout_ms, force=force)
                return True
            except Exception:
                try:
                    await loc.click(timeout=timeout_ms, force=True)
                    return True
                except Exception:
                    continue
        return False

    async def wait_until(self, predicate, timeout_ms=WAIT_HANG_MS, interval=0.4):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_ms / 1000.0
        while loop.time() < deadline:
            try:
                if await predicate():
                    return True
            except Exception:
                pass
            await asyncio.sleep(interval)
        try:
            return bool(await predicate())
        except Exception:
            return False

    async def wait_url(self, page, pattern, timeout_ms=WAIT_HANG_MS):
        rx = pattern if hasattr(pattern, "search") else re.compile(pattern)
        try:
            await page.wait_for_url(rx, timeout=timeout_ms)
            return True
        except Exception:
            return bool(rx.search(page.url or ""))

    async def wait_visible(self, locator, timeout_ms=WAIT_HANG_MS):
        try:
            await locator.first.wait_for(state="visible", timeout=timeout_ms)
            return True
        except Exception:
            try:
                return await locator.first.is_visible()
            except Exception:
                return False
