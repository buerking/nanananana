"""雅虎页跳转/水合等待。

上限 5 分钟，目标已到达立刻返回。只点看得到的节点，避免 DOM 里有、实际点不到时空等 30 秒判失败。
"""
import asyncio
import re
from urllib.parse import parse_qs, unquote, urlparse

from .constants import WAIT_CLICK_MS, WAIT_HANG_MS

# 登录抽风页（会话还在，却画出 ID 表单）。不要拿「ログイン」单独判断，结算页也有再ログイン。
YAHOO_LOGIN_FORM_SEL = (
    "h1:has-text('ログイン'), "
    "input[name='handle'], "
    "input[placeholder*='メールアドレス'], "
    "input[placeholder*='携帯電話番号'], "
    "a:has-text('新規取得'), "
    "text=携帯電話番号/メールアドレス/ID, "
    "text=でログイン中です, "
    "text=キャプチャ, "
    "text=画像認証"
)


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


def yahoo_done_url_from(url):
    """取出 .done= / done= 回跳地址。login 与账号地址页都有；值本身可能已解码一次。"""
    try:
        query = parse_qs(urlparse(url or "").query)
        raw = (query.get(".done") or query.get("done") or [""])[0]
        if raw:
            return unquote(raw)
    except Exception:
        pass
    try:
        m = re.search(r"[?&]\.?done=([^&]+)", url or "")
        if m:
            return unquote(m.group(1))
    except Exception:
        pass
    return ""


def is_safe_yahoo_done_url(done):
    """只允许跳回雅虎拍卖站，避免 .done 被带到登录/钱包/外站。"""
    if not done:
        return False
    text = str(done).strip()
    if not text.lower().startswith("https://"):
        return False
    host, _path = yahoo_host_path(text)
    if not host:
        return False
    if host == "login.yahoo.co.jp" or host.endswith(".login.yahoo.co.jp"):
        return False
    if "wallet.yahoo.co.jp" in host:
        return False
    if host == "contact.auctions.yahoo.co.jp" or host.endswith(".contact.auctions.yahoo.co.jp"):
        return True
    if "buy.auctions.yahoo.co.jp" in host:
        return True
    if host == "auctions.yahoo.co.jp" or host.endswith(".auctions.yahoo.co.jp"):
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

    async def _yahoo_login_form_visible(self, page):
        """login.yahoo.co.jp 上的 ID 表单 / 已登录提示 / 验证码。"""
        if page is None:
            return False
        loc = page.locator(YAHOO_LOGIN_FORM_SEL)
        if await self.has_visible(loc):
            return True
        try:
            body = await page.locator("body").inner_text()
        except Exception:
            body = ""
        text = body or ""
        markers = (
            "新規取得",
            "携帯電話番号/メールアドレス/ID",
            "でログイン中です",
            "キャプチャ",
            "画像認証",
        )
        hits = sum(1 for m in markers if m in text)
        if "ログイン" in text and hits >= 1:
            return True
        return hits >= 2

    async def _try_skip_ghost_yahoo_login(self, page):
        """会话还在却画出登录表单：取出 .done= 直接打开回跳地址，不填账号、不点次へ。"""
        url = page.url if page else ""
        if not is_yahoo_login_or_reauth_url(url):
            return False
        host, _path = yahoo_host_path(url)
        if "wallet.yahoo.co.jp" in host:
            return False
        if not await self._yahoo_login_form_visible(page):
            return False
        done = yahoo_done_url_from(url)
        if not is_safe_yahoo_done_url(done):
            return False
        already = ""
        try:
            loc = page.locator("text=でログイン中です")
            if await self.has_visible(loc):
                already = (await loc.first.inner_text() or "").strip()
        except Exception:
            already = ""
        hint = f"（{already}）" if already else ""
        await self.log(f"登录页出现ログイン表单{hint}，会话多半还在。改打开 .done 回跳: {done}")
        try:
            await page.goto(done, wait_until="commit", timeout=WAIT_HANG_MS)
        except Exception as e:
            await self.log(f"打开 .done 超时: {e}", "WARNING")
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=WAIT_HANG_MS)
        except Exception:
            pass
        if is_yahoo_login_or_reauth_url(page.url or ""):
            await self.log(f"打开 .done 后仍在登录页: {page.url}", "WARNING")
            return False
        await self.log(f"已绕过登录抽风页: {page.url}")
        return True

    async def wait_out_yahoo_login_or_reauth(self, page, why=""):
        """钱包/登录二次验证：先尝试 .done 回跳；不行再等人在浏览器登完。"""
        if not is_yahoo_login_or_reauth_url(page.url if page else ""):
            return True
        if await self._try_skip_ghost_yahoo_login(page):
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
            url = page.url or ""
            if is_auction_checkout_host_url(url):
                return True
            return not is_yahoo_login_or_reauth_url(url)

        ok = await self.wait_until(back, timeout_ms=WAIT_HANG_MS)
        if ok:
            await self.log(f"已离开登录/验证页: {page.url}")
            return True
        await self.log(f"仍停在登录/验证页: {page.url}", "ERROR")
        return False
