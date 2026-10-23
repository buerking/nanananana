"""雅虎页跳转/水合等待。

上限 5 分钟，目标已到达立刻返回。只点看得到的节点，避免 DOM 里有、实际点不到时空等 30 秒判失败。
"""
import asyncio
import re
from urllib.parse import parse_qs, unquote, urlparse

from .constants import ACCOUNT_EMAILS, WAIT_CLICK_MS, WAIT_HANG_MS

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


def yahoo_account_login_tokens():
    """ad / ym 登录页会显示完整邮箱，或「adxez09955でログイン中です」这种本地名。"""
    tokens = []
    seen = set()
    for emails in ACCOUNT_EMAILS.values():
        for email in emails or []:
            raw = str(email).strip()
            if not raw:
                continue
            low = raw.lower()
            if low not in seen:
                seen.add(low)
                tokens.append(low)
            local = low.split("@")[0].strip()
            if local and local not in seen:
                seen.add(local)
                tokens.append(local)
    return tokens


def yahoo_host_path(url):
    try:
        parsed = urlparse(url or "")
        return (parsed.netloc or "").lower(), parsed.path or ""
    except Exception:
        return "", ""


def is_yahoo_wallet_sub_add_url(url):
    """edit.wallet.../config/wallet_sub_add：抽风跳转，取 .done= 回结算，不是钱包二次验证。"""
    host, path = yahoo_host_path(url)
    if "wallet.yahoo.co.jp" not in host:
        return False
    return "/wallet_sub_add" in path


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
    """取出 .done= / done= 回跳地址。parse_qs 已解码一层，不要再 unquote，以免嵌套 .done 的 & 被拆开。"""
    try:
        query = parse_qs(urlparse(url or "").query, keep_blank_values=True)
        raw = (query.get(".done") or query.get("done") or [""])[0]
        if raw:
            return raw
    except Exception:
        pass
    try:
        m = re.search(r"[?&]\.?done=([^&]+)", url or "", re.I)
        if m:
            return unquote(m.group(1))
    except Exception:
        pass
    return ""


def yahoo_checkout_done_url_from(url):
    """login → wallet_sub_add → 结算，层层解开 .done=，直到拍卖结算地址。"""
    current = url or ""
    seen = set()
    for _ in range(6):
        if not current or current in seen:
            break
        seen.add(current)
        if is_safe_yahoo_done_url(current):
            return current
        nxt = yahoo_done_url_from(current)
        if not nxt or nxt == current:
            break
        current = nxt
    return current if is_safe_yahoo_done_url(current) else ""


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


YAHOO_SYSTEM_ERROR_TEXT = "システムでエラーが発生しました"


class WaitMixin:
    async def _yahoo_system_error_visible(self, page):
        if page is None:
            return False
        loc = page.locator("text=システムでエラーが発生しました")
        try:
            if await self.has_visible(loc):
                return True
        except Exception:
            pass
        try:
            body = await page.locator("body").inner_text()
        except Exception:
            return False
        text = body or ""
        return YAHOO_SYSTEM_ERROR_TEXT in text

    async def _wait_visible_reload_syserr(self, page, locator, timeout_ms=WAIT_HANG_MS):
        """等目标出现；若是系统错误页则刷新后再等。"""

        async def ready():
            await self._reload_if_yahoo_system_error(page)
            try:
                return await locator.first.is_visible()
            except Exception:
                return False

        return await self.wait_until(ready, timeout_ms=timeout_ms)

    async def _reload_if_yahoo_system_error(self, page, max_tries=3):
        """系统错误页刷新即可跳过，不要当失败停住。"""
        if page is None:
            return False
        if hasattr(self, "_page_has_no_purchase_data"):
            try:
                if await self._page_has_no_purchase_data(page):
                    return False
            except Exception:
                pass
        refreshed = False
        for i in range(max_tries):
            try:
                if not await self._yahoo_system_error_visible(page):
                    return refreshed
            except Exception:
                return refreshed
            refreshed = True
            await self.log(
                f"雅虎系统错误（再度お試しください），刷新页面 ({i + 1}/{max_tries})...",
                "WARNING",
            )
            url = page.url or ""
            try:
                await page.reload(wait_until="domcontentloaded", timeout=WAIT_HANG_MS)
            except Exception as e:
                await self.log(f"刷新超时，改用重新打开当前地址: {e}", "WARNING")
                if url:
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=WAIT_HANG_MS)
                    except Exception:
                        pass
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=WAIT_CLICK_MS)
            except Exception:
                pass
        if refreshed and await self._yahoo_system_error_visible(page):
            await self.log("多次刷新后仍是雅虎系统错误页。", "WARNING")
        elif refreshed:
            await self.log("刷新后系统错误已消失，继续。")
        return refreshed

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

    async def _yahoo_login_account_visible(self, page):
        """页上能看到 ad / ym 邮箱或 ID（如 adxez09955でログイン中です）。"""
        if page is None:
            return False
        tokens = yahoo_account_login_tokens()
        if not tokens:
            return False
        loc_parts = ["text=でログイン中です"]
        loc_parts.extend(f"text={t}" for t in tokens)
        if await self.has_visible(page.locator(", ".join(loc_parts))):
            return True
        try:
            body = await page.locator("body").inner_text()
        except Exception:
            body = ""
        text = (body or "").lower()
        if "でログイン中です" in (body or ""):
            return True
        return any(t in text for t in tokens)

    async def _yahoo_login_form_visible(self, page):
        """ID 表单 / 已登录提示 / 验证码 / ad·ym 邮箱。"""
        if page is None:
            return False
        loc = page.locator(YAHOO_LOGIN_FORM_SEL)
        if await self.has_visible(loc):
            return True
        if await self._yahoo_login_account_visible(page):
            try:
                body = await page.locator("body").inner_text()
            except Exception:
                body = ""
            text = body or ""
            if (
                "でログイン中です" in text
                or "新規取得" in text
                or "携帯電話番号/メールアドレス/ID" in text
                or "キャプチャ" in text
                or "画像認証" in text
            ):
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

    async def _goto_yahoo_done_url(self, page, done, why):
        if not is_safe_yahoo_done_url(done):
            return False
        await self.log(f"{why}改打开 .done 回跳: {done}")
        try:
            await page.goto(done, wait_until="commit", timeout=WAIT_HANG_MS)
        except Exception as e:
            await self.log(f"打开 .done 超时: {e}", "WARNING")
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=WAIT_HANG_MS)
        except Exception:
            pass
        if is_yahoo_login_or_reauth_url(page.url or ""):
            await self.log(f"打开 .done 后仍在登录/钱包页: {page.url}", "WARNING")
            return False
        await self.log(f"已绕过抽风页: {page.url}")
        return True

    async def _try_skip_nested_done_to_checkout(self, page):
        """login.yahoo 的 .done 常套一层 wallet_sub_add，再套结算地址。直接打开最内层结算 URL。"""
        url = page.url if page else ""
        if not is_yahoo_login_or_reauth_url(url) and not is_yahoo_wallet_sub_add_url(url):
            return False
        done = yahoo_checkout_done_url_from(url)
        if not done:
            return False
        return await self._goto_yahoo_done_url(page, done, "登录/钱包页嵌套 .done，")

    async def _try_skip_wallet_sub_add(self, page):
        """edit.wallet.../wallet_sub_add：解开 .done= 后直接访问，不填卡、不等二次验证。"""
        url = page.url if page else ""
        if await self._try_skip_nested_done_to_checkout(page):
            return True
        if not is_yahoo_wallet_sub_add_url(url):
            return False
        done = yahoo_checkout_done_url_from(url) or yahoo_done_url_from(url)
        if not is_safe_yahoo_done_url(done):
            await self.log("钱包 wallet_sub_add 无可用结算 .done。", "WARNING")
            return False
        return await self._goto_yahoo_done_url(page, done, "钱包 wallet_sub_add，")

    async def _try_skip_ghost_yahoo_login(self, page):
        """会话还在却画出登录表单：取出 .done= 直接打开回跳地址，不填账号、不点次へ。"""
        url = page.url if page else ""
        if await self._try_skip_nested_done_to_checkout(page):
            return True
        if await self._try_skip_wallet_sub_add(page):
            return True
        if not is_yahoo_login_or_reauth_url(url):
            return False
        host, _path = yahoo_host_path(url)
        if "wallet.yahoo.co.jp" in host:
            return False
        if not (
            await self._yahoo_login_form_visible(page)
            or await self._yahoo_login_account_visible(page)
        ):
            return False
        done = yahoo_checkout_done_url_from(url) or yahoo_done_url_from(url)
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
        return await self._goto_yahoo_done_url(
            page, done, f"登录页出现ログイン表单{hint}，会话多半还在。"
        )

    async def wait_out_yahoo_login_or_reauth(self, page, why=""):
        """钱包/登录二次验证：嵌套 .done 与登录抽风先回跳；不行再等人登完并飞书。"""
        if not is_yahoo_login_or_reauth_url(page.url if page else ""):
            return True
        if await self._try_skip_nested_done_to_checkout(page):
            return True
        if await self._try_skip_wallet_sub_add(page):
            return True
        if await self._try_skip_ghost_yahoo_login(page):
            return True
        hint = f"（{why}）" if why else ""
        host, path = yahoo_host_path(page.url if page else "")
        await self.log(
            f"检测到 Yahoo 登录/钱包验证页{hint}，请在该窗口完成登录。最长等 5 分钟。 当前: {host}{path}",
            "WARNING",
        )
        try:
            await self.trigger_sound("attention")
        except Exception:
            pass
        try:
            aid = ""
            done = yahoo_checkout_done_url_from(page.url or "")
            q = parse_qs(urlparse(done or "").query)
            aid = (q.get("aid") or q.get("auctionId") or [""])[0]
            self.notify_feishu(
                f"雅虎登录/钱包验证需要人工完成{hint}。商品 {aid or '-'}。"
                "请在当前窗口登录后继续。"
            )
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
