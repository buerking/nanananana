import os

from playwright.async_api import async_playwright

from .constants import ACCOUNT_EMAILS, QBT_BROWSER_ACCOUNT, WAIT_HANG_MS, YAHOO_HOME_URL


def _system_chrome_path():
    """本机已安装的 Chrome，避免必须再下载 Playwright Chromium。"""
    env = (os.environ.get("CHROME_PATH") or "").strip().strip('"')
    candidates = [
        env,
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


class BrowserMixin:
    """两套持久化 Chrome：ad=user_data，ym=user_data_ym。QBT 只开在 ad。"""

    def context_for_account(self, account_key):
        key = (account_key or QBT_BROWSER_ACCOUNT).strip().lower()
        return self.yahoo_contexts.get(key) or self.browser_context

    def account_key_from_email(self, email):
        needle = (email or "").strip().lower()
        if not needle or "@" not in needle:
            return ""
        for key, emails in ACCOUNT_EMAILS.items():
            if needle in [e.lower() for e in emails]:
                return key
        return ""

    async def _launch_account_context(self, account_key, headless, open_qbt=False):
        chrome = _system_chrome_path()
        data_dir = self.USER_DATA_DIRS[account_key]
        os.makedirs(data_dir, exist_ok=True)
        launch_kwargs = {
            "user_data_dir": data_dir,
            "headless": headless,
            "args": ["--start-maximized", "--disable-blink-features=AutomationControlled"],
            "viewport": {"width": 1920, "height": 1080},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
        }
        if chrome:
            launch_kwargs["executable_path"] = chrome
        context = await self.playwright.chromium.launch_persistent_context(**launch_kwargs)
        context.set_default_navigation_timeout(WAIT_HANG_MS)
        pages = context.pages
        page = pages[0] if pages else await context.new_page()
        try:
            await page.goto(YAHOO_HOME_URL, timeout=12000, wait_until="commit")
        except Exception as e:
            await self.log(f"[{account_key}] 雅虎首页未快速就绪: {e}", "WARNING")
        if open_qbt:
            backend_page = await context.new_page()
            try:
                await backend_page.goto(self.BACKEND_URL, timeout=30000, wait_until="domcontentloaded")
            except Exception as e:
                await self.log(f"[{account_key}] 后台打开失败: {e}", "WARNING")
        emails = " / ".join(ACCOUNT_EMAILS.get(account_key) or [])
        await self.log(f"已启动 Chrome [{account_key}] 目录={data_dir} 账号={emails}")
        return context, page

    async def start_browser(self, headless=False, for_login=False):
        if self.yahoo_contexts:
            await self.log("Browser already open.")
            return
        await self.log("正在启动 ad / ym 两套 Chrome...")
        try:
            self.playwright = await async_playwright().start()
            chrome = _system_chrome_path()
            if chrome:
                await self.log(f"使用本机 Chrome: {chrome}")
            else:
                await self.log(
                    "未找到本机 Chrome，将使用 Playwright Chromium（需先 playwright install chromium）",
                    "WARNING",
                )
            qbt_key = getattr(self, "qbt_account", QBT_BROWSER_ACCOUNT)
            for key in ACCOUNT_EMAILS:
                ctx, page = await self._launch_account_context(
                    key, headless, open_qbt=(key == qbt_key)
                )
                self.yahoo_contexts[key] = ctx
                if key == qbt_key:
                    self.browser_context = ctx
                    self.page = page
            if for_login:
                await self.log(
                    "请分别登录：\n"
                    f"1. Chrome [ad] 雅虎 {ACCOUNT_EMAILS['ad'][0]}\n"
                    f"2. Chrome [ym] 雅虎 {ACCOUNT_EMAILS['ym'][0]}\n"
                    f"3. QBT 后台固定在 [{qbt_key}] ({self.BACKEND_URL})"
                )
        except Exception as e:
            await self.log(f"Failed to launch browser: {e}", "ERROR")
            try:
                await self.close_browser()
            except Exception:
                pass
            raise

    async def reuse_or_goto(self, context, url, url_hint="", timeout=WAIT_HANG_MS):
        """已打开相同商品/后台页则复用，否则直接 goto，不模拟点击等新标签。"""
        hint = (url_hint or "").lower()
        for page in context.pages:
            if page.is_closed():
                continue
            current = (page.url or "").lower()
            if hint and hint in current:
                await page.bring_to_front()
                return page
            if url.lower().split("?")[0] in current.split("?")[0] and current.startswith("http"):
                await page.bring_to_front()
                return page
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="commit", timeout=timeout)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=timeout)
            except Exception:
                pass
        except Exception:
            if hint and hint in (page.url or "").lower():
                return page
            raise
        return page

    async def close_browser(self):
        for key, ctx in list((self.yahoo_contexts or {}).items()):
            try:
                if ctx:
                    await ctx.close()
            except Exception:
                pass
            self.yahoo_contexts[key] = None
        self.yahoo_contexts = {}
        self.browser_context = None
        self.page = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None
