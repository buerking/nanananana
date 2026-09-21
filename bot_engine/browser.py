from playwright.async_api import async_playwright

from .constants import YAHOO_HOME_URL


class BrowserMixin:
    """Playwright 持久化 Chromium：登录会话在 user_data。"""

    async def start_browser(self, headless=False, for_login=False):
        """Launches the persistent browser context"""
        if self.browser_context:
            await self.log("Browser already open.")
            return
        await self.log("正在启动浏览器...")
        try:
            self.playwright = await async_playwright().start()
            self.browser_context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir=self.USER_DATA_DIR,
                headless=headless,
                args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
                viewport={"width": 1920, "height": 1080},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            pages = self.browser_context.pages
            self.page = pages[0] if pages else await self.browser_context.new_page()
            try:
                await self.page.goto(
                    YAHOO_HOME_URL,
                    timeout=60000,
                    wait_until="domcontentloaded",
                )
            except Exception as e:
                await self.log(f"Warning: Failed to load Yahoo Auctions (Timeout): {e}", "WARNING")
            backend_page = await self.browser_context.new_page()
            try:
                await backend_page.goto(self.BACKEND_URL, timeout=60000, wait_until="domcontentloaded")
            except Exception as e:
                await self.log(f"Warning: Failed to load Backend (Timeout): {e}", "WARNING")
            if for_login:
                await self.log(f"浏览器已打开，请手动登录。\n1. 雅虎拍卖\n2. 后台系统 ({self.BACKEND_URL})")
        except Exception as e:
            await self.log(f"Failed to launch browser: {e}", "ERROR")
            raise

    async def close_browser(self):
        if self.browser_context:
            await self.browser_context.close()
            self.browser_context = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None
