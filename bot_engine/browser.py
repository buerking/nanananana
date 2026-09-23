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
    """启动浏览器：默认用项目 user_data；生产可接系统 Chrome Profile / CDP。"""

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
                await self._start_via_cdp()
            else:
                await self._start_playwright_profile(headless=headless)
            await self._ensure_yahoo_and_backend_tabs()
            if for_login:
                await self.log(
                    f"浏览器已打开，请手动登录。\n1. 雅虎拍卖\n2. 后台系统 ({self.BACKEND_URL})"
                )
        except Exception as e:
            await self.log(f"Failed to launch browser: {e}", "ERROR")
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

    async def _start_via_cdp(self):
        cfg = self.browser_config
        cdp_url = cfg.get("cdp_url") or "http://127.0.0.1:9222"
        self._cdp_browser = await self._try_connect_cdp(cdp_url, retries=2, delay=0.8)
        if self._cdp_browser is None and cfg.get("launch_if_needed", True):
            await self._launch_system_chrome_for_cdp(cfg)
            self._cdp_browser = await self._try_connect_cdp(cdp_url, retries=20, delay=1.0)
        if self._cdp_browser is None:
            raise RuntimeError(
                f"无法连接系统 Chrome ({cdp_url})。请先关掉所有 Chrome，再点「启动浏览器」；"
                f"或把快捷方式加上 --remote-debugging-port=9222 后先打开 Chrome。"
            )
        contexts = self._cdp_browser.contexts
        if not contexts:
            raise RuntimeError("已连上 Chrome，但没有可用的 BrowserContext。")
        self.browser_context = contexts[0]
        self.attached_cdp = True
        await self.log(f"已连接到系统 Chrome（{cdp_url}），复用当前登录态。")

    async def _try_connect_cdp(self, cdp_url, retries=8, delay=1.0):
        last_error = None
        for attempt in range(1, retries + 1):
            try:
                return await self.playwright.chromium.connect_over_cdp(cdp_url)
            except Exception as e:
                last_error = e
                await self.log(f"CDP 未就绪 ({attempt}/{retries}): {e}", "DEBUG")
                await asyncio.sleep(delay)
        await self.log(f"CDP 连接失败: {last_error}", "WARNING")
        return None

    async def _launch_system_chrome_for_cdp(self, cfg):
        chrome_path = self._resolve_chrome_path(cfg)
        profile = cfg.get("profile_directory") or "Profile 14"
        cdp_url = cfg.get("cdp_url") or "http://127.0.0.1:9222"
        port = "9222"
        if ":" in cdp_url.rsplit("/", 1)[-1]:
            port = cdp_url.rsplit(":", 1)[-1]
        if not chrome_path or not os.path.exists(chrome_path):
            raise RuntimeError(f"找不到 Chrome: {chrome_path}")
        args = [
            chrome_path,
            f"--profile-directory={profile}",
            f"--remote-debugging-port={port}",
            "--start-maximized",
        ]
        await self.log(f"正在启动系统 Chrome：{profile}（调试端口 {port}）")
        subprocess.Popen(
            args,
            cwd=os.path.dirname(chrome_path),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

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
