"""雅虎页跳转/水合等待。

上限 5 分钟，目标已到达立刻返回。只点看得到的节点，避免 DOM 里有、实际点不到时空等 30 秒判失败。
"""
import asyncio
import re
from urllib.parse import urlparse

from .constants import WAIT_CLICK_MS, WAIT_HANG_MS


def yahoo_host_path(url):
    try:
        parsed = urlparse(url or "")
        return (parsed.netloc or "").lower(), parsed.path or ""
    except Exception:
        return "", ""


def is_yahoo_login_or_reauth_url(url):
    """login / 钱包二次验证。不要用整段 URL 子串判断，.done 里会带上结算地址。"""
    host, _path = yahoo_host_path(url)
    if host == "login.yahoo.co.jp" or host.endswith(".login.yahoo.co.jp"):
        return True
    if "wallet.yahoo.co.jp" in host:
        return True
    return False


def is_auction_checkout_host_url(url):
    if is_yahoo_login_or_reauth_url(url):
        return False
    host, path = yahoo_host_path(url)
    if host == "contact.auctions.yahoo.co.jp" or host.endswith(".contact.auctions.yahoo.co.jp"):
        return True
    if "buy.auctions.yahoo.co.jp" in host and "/order/" in path:
        return True
    return False


def is_purchase_complete_url(url):
    if is_yahoo_login_or_reauth_url(url):
        return False
    _host, path = yahoo_host_path(url)
    return (
        "/buyer/payment/complete" in path
        or path.endswith("/payment/complete")
        or "/order/thank-you" in path
    )


class WaitMixin:
    async def has_visible(self, locator, limit=8):
        try:
            n = await locator.count()
        except Exception:
            return False
        for i in range(min(n, limit)):
            try:
                if await locator.nth(i).is_visible():
                    return True
            except Exception:
                continue
        return False

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

    def _is_yahoo_login_or_reauth_url(self, url):
        return is_yahoo_login_or_reauth_url(url)

    def _is_auction_checkout_host_url(self, url):
        return is_auction_checkout_host_url(url)

    def _is_purchase_complete_url(self, url):
        return is_purchase_complete_url(url)

    async def wait_out_yahoo_login_or_reauth(self, page, why=""):
        """钱包/登录二次验证：等人在浏览器登完，直到回到结算站。"""
        if not is_yahoo_login_or_reauth_url(page.url if page else ""):
            return True
        hint = f"（{why}）" if why else ""
        await self.log(
            f"检测到 Yahoo 登录/钱包验证页{hint}，请在该窗口完成登录。最长等 5 分钟。 当前: {page.url}",
            "WARNING",
        )
        try:
            await self.trigger_sound("attention")
        except Exception:
            pass

        async def back():
            return is_auction_checkout_host_url(page.url or "")

        ok = await self.wait_until(back, timeout_ms=WAIT_HANG_MS)
        if ok:
            await self.log(f"已离开登录/验证页: {page.url}")
            return True
        await self.log(f"仍停在登录/验证页: {page.url}", "ERROR")
        return False
