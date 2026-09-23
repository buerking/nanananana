import asyncio
import json
import os
import subprocess

from playwright.async_api import async_playwright

from .constants import YAHOO_HOME_URL

DEFAULT_BROWSER_CONFIG = {
    "mode": "playwright",
    "cdp_url": "http://127.0.0.1:9222",
    "chrome_path": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "profile_directory": "Profile 14",
    "launch_if_needed": True,
}


class BrowserMixin:
    """mode=cdp：使用本机 Chrome 的 Profile 14（已登录），不是项目 user_data。"""

    def load_browser_config(self):
        cfg = dict(DEFAULT_BROWSER_CONFIG)
        path = os.path.join(self.BASE_DIR, "browser.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    cfg.update(data)
            except Exception as e:
                print(f"Error loading browser.json: {e}")
        return cfg

    def _resolve_chrome_path(self, cfg):
        candidates = [
            cfg.get("chrome_path"),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
        for path in candidates:
            if path and os.path.exists(path):
                return path
        return cfg.get("chrome_path")

    def _chrome_user_data_root(self, cfg):
        configured = (cfg.get("user_data_dir") or "").strip()
        if configured:
            return configured
        return os.path.join(
            os.environ.get("LOCALAPPDATA", ""),
            "Google",
            "Chrome",
            "User Data",
        )

    async def start_browser(self, headless=False, for_login=False):
        if self.browser_context:
            await self.log("Browser already open.")
            return
        self.browser_config = self.load_browser_config()
        mode = (self.browser_config.get("mode") or "playwright").strip().lower()
        await self.log(f"正在启动浏览器... 模式={mode}")
        try:
            self.playwright = await async_playwright().start()
            if mode == "cdp":
                if headless:
                    await self.log("CDP 模式忽略无头选项，使用已登录的系统 Chrome。", "WARNING")
                await self._start_system_chrome()
            else:
                await self._start_playwright_profile(headless=headless)
            await self._ensure_yahoo_and_backend_tabs()
            if for_login:
                await self.log(
                    f"浏览器已打开。当前应是系统 Chrome Profile 14 的登录态。\n"
                    f"1. 雅虎拍卖\n2. 后台系统 ({self.BACKEND_URL})"
                )
        except Exception as e:
            await self.log(f"Failed to launch browser: {e}", "ERROR")
            if self.playwright and not self.browser_context:
                try:
                    await self.playwright.stop()
                except Exception:
                    pass
                self.playwright = None
            raise

    async def _start_playwright_profile(self, headless=False):
        self.attached_cdp = False
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

    async def _start_system_chrome(self):
        cfg = self.browser_config
        cdp_url = cfg.get("cdp_url") or "http://127.0.0.1:9222"
        self._cdp_browser = await self._try_connect_cdp(cdp_url, retries=1, delay=0.2)
        if self._cdp_browser is not None:
            contexts = self._cdp_browser.contexts
            if not contexts:
                raise RuntimeError("已连上 Chrome，但没有可用的 BrowserContext。")
            self.browser_context = contexts[0]
            self.attached_cdp = True
            await self.log(f"已接管当前 Chrome（{cdp_url}）。")
            return

        await self._close_running_chrome()
        await self._start_chrome_persistent_profile()

    async def _close_running_chrome(self):
        await self.log("Chrome 正在占用 Profile 14，先关掉再按已登录配置重新打开（不会清账号）。")
        subprocess.run(
            ["taskkill", "/F", "/IM", "chrome.exe"],
            capture_output=True,
            text=True,
            check=False,
        )
        await asyncio.sleep(2)

    async def _start_chrome_persistent_profile(self):
        cfg = self.browser_config
        chrome_path = self._resolve_chrome_path(cfg)
        user_data_root = self._chrome_user_data_root(cfg)
        profile = cfg.get("profile_directory") or "Profile 14"
        profile_dir = os.path.join(user_data_root, profile)
        if not chrome_path or not os.path.exists(chrome_path):
            raise RuntimeError(f"找不到 Chrome: {chrome_path}")
        if not os.path.isdir(profile_dir):
            raise RuntimeError(f"找不到已登录配置: {profile_dir}")
        await self.log(f"正在打开系统 Chrome：{profile}（{profile_dir}）")
        self.attached_cdp = False
        last_error = None
        launch_kwargs = {
            "user_data_dir": user_data_root,
            "channel": "chrome",
            "headless": False,
            "args": [
                f"--profile-directory={profile}",
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",
            ],
            "ignore_default_args": ["--enable-automation"],
        }
        for attempt in range(1, 4):
            try:
                self.browser_context = await self.playwright.chromium.launch_persistent_context(
                    **launch_kwargs
                )
                await self.log("已用系统 Chrome Profile 14 启动，沿用该配置里的雅虎/后台登录。")
                return
            except Exception as e:
                last_error = e
                await self.log(f"启动 Profile 14 失败 ({attempt}/3): {e}", "WARNING")
                await self._close_running_chrome()
        raise RuntimeError(f"无法打开已登录的 Chrome Profile 14: {last_error}")

    async def _try_connect_cdp(self, cdp_url, retries=8, delay=1.0):
        last_error = None
        for attempt in range(1, retries + 1):
            try:
                return await self.playwright.chromium.connect_over_cdp(cdp_url)
            except Exception as e:
                last_error = e
                await self.log(f"CDP 未就绪 ({attempt}/{retries}): {e}", "DEBUG")
                await asyncio.sleep(delay)
        if retries > 1:
            await self.log(f"CDP 连接失败: {last_error}", "WARNING")
        return None

    async def _find_page_by_url(self, *needles):
        if not self.browser_context:
            return None
        for page in self.browser_context.pages:
            url = page.url or ""
            if any(n in url for n in needles):
                return page
        return None

    async def _ensure_yahoo_and_backend_tabs(self):
        pages = self.browser_context.pages
        self.page = pages[0] if pages else await self.browser_context.new_page()

        yahoo_page = await self._find_page_by_url("auctions.yahoo.co.jp", "yahoo.co.jp")
        if yahoo_page:
            self.page = yahoo_page
            await self.log(f"复用已打开的雅虎标签: {yahoo_page.url}")
        else:
            try:
                await self.page.goto(YAHOO_HOME_URL, timeout=60000, wait_until="domcontentloaded")
            except Exception as e:
                await self.log(f"Warning: Failed to load Yahoo Auctions (Timeout): {e}", "WARNING")

        backend_page = await self._find_page_by_url("qbt.jp")
        if backend_page:
            await self.log(f"复用已打开的后台标签: {backend_page.url}")
            return
        backend_page = await self.browser_context.new_page()
        try:
            await backend_page.goto(self.BACKEND_URL, timeout=60000, wait_until="domcontentloaded")
        except Exception as e:
            await self.log(f"Warning: Failed to load Backend (Timeout): {e}", "WARNING")

    async def close_browser(self):
        attached = getattr(self, "attached_cdp", False)
        if attached:
            self.browser_context = None
            self._cdp_browser = None
            self.attached_cdp = False
            if self.playwright:
                await self.playwright.stop()
                self.playwright = None
            return
        if self.browser_context:
            await self.browser_context.close()
            self.browser_context = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None
