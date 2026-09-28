import os

from playwright.async_api import async_playwright

from .constants import YAHOO_HOME_URL


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
    """Playwright 持久化浏览器：登录会话在项目 user_data。优先本机 Chrome。"""

    async def start_browser(self, headless=False, for_login=False):
        """Launches the persistent browser context"""
        if self.browser_context:
            await self.log("Browser already open.")
            return
        await self.log("正在启动浏览器...")
        try:
            self.playwright = await async_playwright().start()
            launch_kwargs = {
                "user_data_dir": self.USER_DATA_DIR,
                "headless": headless,
                "args": ["--start-maximized", "--disable-blink-features=AutomationControlled"],
                "viewport": {"width": 1920, "height": 1080},
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
            }
            chrome = _system_chrome_path()
            if chrome:
                launch_kwargs["executable_path"] = chrome
                await self.log(f"使用本机 Chrome: {chrome}")
            else:
                await self.log(
                    "未找到本机 Chrome，将使用 Playwright Chromium（需先 playwright install chromium）",
                    "WARNING",
                )
            self.browser_context = await self.playwright.chromium.launch_persistent_context(
                **launch_kwargs
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
