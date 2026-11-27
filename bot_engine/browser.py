import asyncio
import os
import re
import subprocess
import urllib.error
import urllib.request

from playwright.async_api import async_playwright

from .constants import (
    ACCOUNT_EMAILS,
    CHROME_CDP_PORTS,
    CHROME_RESTART_COOLDOWN,
    CHROME_WATCHDOG_SECONDS,
    MAX_CHROME_PAGES,
    QBT_BROWSER_ACCOUNT,
    WAIT_HANG_MS,
    YAHOO_HOME_URL,
)


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
    """两套持久化 Chrome：ad=user_data，ym=user_data_ym。QBT 只开在 ad。

    Chrome 用独立进程 + CDP 连上。重启 main 只断开 Playwright，不关浏览器、不重登。
    """

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

    def _cdp_port(self, account_key):
        return int(CHROME_CDP_PORTS.get(account_key) or 0)

    def _cdp_http(self, port):
        return f"http://127.0.0.1:{port}"

    async def _cdp_ready(self, port, timeout_s=20):
        url = f"{self._cdp_http(port)}/json/version"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while loop.time() < deadline:
            try:
                def _probe():
                    req = urllib.request.urlopen(url, timeout=1)
                    req.read()
                    req.close()

                await loop.run_in_executor(None, _probe)
                return True
            except (urllib.error.URLError, OSError, TimeoutError, ValueError):
                await asyncio.sleep(0.4)
        return False

    def _spawn_detached_chrome(self, chrome, data_dir, port, headless):
        args = [
            chrome,
            f"--user-data-dir={data_dir}",
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--start-maximized",
        ]
        if headless:
            args.append("--headless=new")
        kwargs = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            kwargs["start_new_session"] = True
            kwargs["close_fds"] = True
        return subprocess.Popen(args, **kwargs)

    async def _connect_cdp_context(self, port):
        browser = await self.playwright.chromium.connect_over_cdp(self._cdp_http(port))
        contexts = browser.contexts
        if not contexts:
            raise RuntimeError(f"CDP {port} 已连上但没有 BrowserContext")
        return browser, contexts[0]

    async def _ensure_account_pages(self, context, account_key, open_qbt):
        pages = [p for p in context.pages if not p.is_closed()]
        page = pages[0] if pages else await context.new_page()
        has_yahoo = any(
            "yahoo.co.jp" in ((p.url or "").lower()) for p in context.pages if not p.is_closed()
        )
        if not has_yahoo:
            try:
                await page.goto(YAHOO_HOME_URL, timeout=12000, wait_until="commit")
            except Exception as e:
                await self.log(f"[{account_key}] 雅虎首页未快速就绪: {e}", "WARNING")
        if open_qbt:
            has_qbt = any(
                "qbt.jp" in ((p.url or "").lower()) for p in context.pages if not p.is_closed()
            )
            if not has_qbt:
                backend_page = await context.new_page()
                try:
                    await backend_page.goto(
                        self.BACKEND_URL, timeout=30000, wait_until="domcontentloaded"
                    )
                except Exception as e:
                    await self.log(f"[{account_key}] 后台打开失败: {e}", "WARNING")
        return page

    async def _launch_account_context(self, account_key, headless, open_qbt=False):
        chrome = _system_chrome_path()
        data_dir = self.USER_DATA_DIRS[account_key]
        os.makedirs(data_dir, exist_ok=True)
        port = self._cdp_port(account_key)
        if chrome and port:
            reused = await self._cdp_ready(port, timeout_s=1.2)
            if not reused:
                await self.log(f"[{account_key}] 启动独立 Chrome（CDP {port}），重启 main 不会关掉。")
                self._spawn_detached_chrome(chrome, data_dir, port, headless)
                if not await self._cdp_ready(port, timeout_s=25):
                    raise RuntimeError(f"[{account_key}] Chrome CDP {port} 未就绪")
            else:
                await self.log(f"[{account_key}] 复用已开着的 Chrome（CDP {port}）。")
            browser, context = await self._connect_cdp_context(port)
            if not hasattr(self, "yahoo_browsers"):
                self.yahoo_browsers = {}
            self.yahoo_browsers[account_key] = browser
            context.set_default_navigation_timeout(WAIT_HANG_MS)
            page = await self._ensure_account_pages(context, account_key, open_qbt)
            emails = " / ".join(ACCOUNT_EMAILS.get(account_key) or [])
            await self.log(f"已连接 Chrome [{account_key}] 目录={data_dir} 账号={emails}")
            return context, page

        await self.log(
            f"[{account_key}] 无本机 Chrome 或未配置 CDP 端口，回退 Playwright 持久化上下文（重启 main 会关掉）。",
            "WARNING",
        )
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
        page = await self._ensure_account_pages(context, account_key, open_qbt)
        emails = " / ".join(ACCOUNT_EMAILS.get(account_key) or [])
        await self.log(f"已启动 Chrome [{account_key}] 目录={data_dir} 账号={emails}")
        return context, page

    async def start_browser(self, headless=False, for_login=False):
        await self.ensure_chrome_watchdog()
        await self.log("正在连接 ad / ym 两套 Chrome（已开着则复用，掉线则拉起）...")
        try:
            await self._ensure_playwright()
            chrome = _system_chrome_path()
            if chrome:
                await self.log(f"使用本机 Chrome: {chrome}")
            else:
                await self.log(
                    "未找到本机 Chrome，将使用 Playwright Chromium（需先 playwright install chromium）",
                    "WARNING",
                )
            qbt_key = getattr(self, "qbt_account", QBT_BROWSER_ACCOUNT)
            if not hasattr(self, "yahoo_browsers"):
                self.yahoo_browsers = {}
            for key in ACCOUNT_EMAILS:
                self._mark_chrome_wanted(key, True)
                await self.ensure_account_browser(key, headless=headless)
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
                await self.close_browser(kill_chrome=False)
            except Exception:
                pass
            raise

    def _tab_kind(self, url):
        u = (url or "").lower()
        if not u or u in ("about:blank", "chrome://newtab/") or u.startswith("chrome-error"):
            return "blank"
        if "qbt.jp" in u:
            if "get-auction-not-buy-list" in u:
                return "qbt_list"
            if "auction-buy" in u:
                return "qbt_buy"
            return "qbt_other"
        if "login.yahoo" in u or "wallet.yahoo" in u:
            return "yahoo_login"
        if (
            "/jp/auction/" in u
            or "contact.auctions.yahoo.co.jp" in u
            or "buy.auctions.yahoo.co.jp" in u
        ):
            return "yahoo_product"
        if "auctions.yahoo.co.jp" in u:
            return "yahoo_home"
        return "other"

    async def _close_pages_safe(self, pages, keep):
        closed = 0
        keep = set(keep or [])
        for page in pages:
            if page in keep:
                continue
            try:
                if page.is_closed():
                    continue
                live = [p for p in page.context.pages if not p.is_closed() and p not in (page,)]
                if not live and page not in keep:
                    continue
                await page.close()
                closed += 1
            except Exception:
                continue
        return closed

    async def sweep_idle_tabs(self, keep_pages=None):
        """闲时清掉堆积的拍品/取引ナビ/QBT 购买页，避免 Chrome 标签爆炸后闪退。"""
        keep_pages = [p for p in (keep_pages or []) if p is not None]
        total_closed = 0
        for key, context in list((self.yahoo_contexts or {}).items()):
            if not context:
                continue
            try:
                closed = await self._sweep_context_tabs(context, key, keep_pages)
            except Exception as e:
                await self.log(f"[{key}] 清标签失败: {e}", "DEBUG")
                closed = 0
            total_closed += closed
        if total_closed:
            await self.log(f"闲时清理浏览器标签 {total_closed} 个，防止堆积闪退。")
        return total_closed

    async def _sweep_context_tabs(self, context, account_key, keep_pages):
        keep = set(keep_pages or [])
        try:
            pages = [p for p in list(context.pages) if not p.is_closed()]
        except Exception:
            return 0
        if len(pages) <= 1:
            return 0
        buckets = {
            "blank": [],
            "yahoo_login": [],
            "yahoo_product": [],
            "qbt_buy": [],
            "qbt_list": [],
            "yahoo_home": [],
            "qbt_other": [],
            "other": [],
        }
        for page in pages:
            if page in keep:
                continue
            try:
                kind = self._tab_kind(page.url or "")
            except Exception:
                kind = "other"
            buckets.setdefault(kind, []).append(page)
        to_close = []
        to_close.extend(buckets["blank"])
        if len(buckets["qbt_list"]) > 1:
            to_close.extend(buckets["qbt_list"][1:])
        if len(buckets["yahoo_home"]) > 1:
            to_close.extend(buckets["yahoo_home"][1:])
        closed = await self._close_pages_safe(to_close, keep)
        try:
            live = [p for p in list(context.pages) if not p.is_closed()]
        except Exception:
            live = []
        if len(live) <= MAX_CHROME_PAGES:
            return closed
        extras = []
        for kind in ("yahoo_product", "qbt_buy", "yahoo_login", "other"):
            extras.extend(buckets.get(kind) or [])
        overflow = max(0, len(live) - MAX_CHROME_PAGES)
        if overflow and extras:
            closed += await self._close_pages_safe(extras[:overflow], keep)
        return closed

    def _yahoo_entry_kind(self, url):
        u = (url or "").lower()
        if "buy.auctions.yahoo.co.jp" in u and "/order/" in u:
            return "store"
        if "/trade/top" in u:
            return "personal_old"
        if "contact.auctions.yahoo.co.jp" in u and "/top" in u:
            return "personal_new"
        if "/jp/auction/" in u or "/auction/" in u:
            return "listing"
        return ""

    def _page_url_matches_product(self, url, product_id="", url_hint="", target_url=""):
        current = (url or "").lower()
        if not current.startswith("http") or "qbt.jp" in current:
            return False
        want = self._yahoo_entry_kind(target_url or url_hint)
        have = self._yahoo_entry_kind(current)
        if want and have and want != have:
            return False
        hint = (url_hint or "").lower().strip()
        if hint and hint in current:
            return True
        pid = (product_id or "").strip().lower()
        if not pid:
            m = re.search(r"/auction/([a-z]?\d+)", ((url_hint or "") + " " + (url or "")).lower())
            pid = m.group(1) if m else ""
        if not pid:
            return False
        return (
            f"/auction/{pid}" in current
            or f"aid={pid}" in current
            or f"auctionid={pid}" in current
        )

    async def reuse_or_goto(self, context, url, url_hint="", timeout=WAIT_HANG_MS):
        """已打开相同商品/后台页则复用，重复标签顺手关掉，否则再 new_page。"""
        hint = (url_hint or "").lower()
        pid = ""
        m = re.search(r"/auction/([a-z]?\d+)", ((url or "") + " " + hint).lower())
        if m:
            pid = m.group(1)
        if not pid:
            m = re.search(r"\b([a-z]\d{8,})\b", hint)
            if m:
                pid = m.group(1)
        matched = []
        for page in list(context.pages):
            try:
                if page.is_closed():
                    continue
            except Exception:
                continue
            current = (page.url or "").lower()
            if self._page_url_matches_product(current, pid, hint, target_url=url):
                matched.append(page)
                continue
            if pid:
                continue
            if url.lower().split("?")[0] in current.split("?")[0] and current.startswith("http"):
                matched.append(page)
        if matched:
            keep = matched[0]
            closed = 0
            for page in matched[1:]:
                try:
                    if page.is_closed() or page is keep:
                        continue
                    await page.close()
                    closed += 1
                except Exception:
                    continue
            if closed:
                await self.log(f"同一商品已有多个标签，复用 1 个并关掉 {closed} 个重复页。")
            try:
                await keep.bring_to_front()
            except Exception:
                pass
            return keep
        try:
            live_n = len([p for p in context.pages if not p.is_closed()])
        except Exception:
            live_n = 0
        if live_n >= MAX_CHROME_PAGES:
            await self.sweep_idle_tabs()
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
            if pid and self._page_url_matches_product(page.url or "", pid, hint, target_url=url):
                return page
            raise
        return page

    def _chrome_guard(self):
        lock = getattr(self, "_chrome_lock", None)
        if lock is None:
            self._chrome_lock = asyncio.Lock()
            lock = self._chrome_lock
        return lock

    def _mark_chrome_wanted(self, account_key, wanted=True):
        if not hasattr(self, "chrome_wanted") or self.chrome_wanted is None:
            self.chrome_wanted = {}
        self.chrome_wanted[account_key] = bool(wanted)

    def _chrome_row_skeleton(self, account_key, **extra):
        qbt_key = getattr(self, "qbt_account", QBT_BROWSER_ACCOUNT)
        row = {
            "key": account_key,
            "label": "ad 出价 / QBT" if account_key == qbt_key else "ym 出价",
            "port": self._cdp_port(account_key),
            "emails": " / ".join(ACCOUNT_EMAILS.get(account_key) or []),
            "qbt": account_key == qbt_key,
            "cdp": False,
            "playwright": False,
            "pages": 0,
            "alive": False,
            "wanted": bool((getattr(self, "chrome_wanted", None) or {}).get(account_key)),
            "restarting": bool((getattr(self, "chrome_restarting", None) or {}).get(account_key)),
            "last_error": "",
        }
        row.update(extra)
        return row

    def _chrome_status_from_cache(self):
        cache = getattr(self, "chrome_health_cache", None) or {}
        rows = []
        for key in ACCOUNT_EMAILS:
            cached = dict(cache.get(key) or {})
            if cached:
                cached["wanted"] = bool(
                    (getattr(self, "chrome_wanted", None) or {}).get(key)
                )
                cached["restarting"] = bool(
                    (getattr(self, "chrome_restarting", None) or {}).get(key)
                )
                rows.append(cached)
            else:
                rows.append(self._chrome_row_skeleton(key))
        return rows

    def _chrome_status_sync(self):
        """不碰 Playwright，只探 CDP，给管理页兜底轮询用。"""
        rows = []
        cache = getattr(self, "chrome_health_cache", None) or {}
        for key in ACCOUNT_EMAILS:
            cached = dict(cache.get(key) or {})
            cdp_ok = False
            try:
                req = urllib.request.urlopen(
                    f"{self._cdp_http(self._cdp_port(key))}/json/version", timeout=0.4
                )
                req.read()
                req.close()
                cdp_ok = True
            except Exception:
                cdp_ok = False
            pw_ok = bool(cached.get("playwright")) if cdp_ok else False
            pages = int(cached.get("pages") or 0) if cdp_ok else 0
            rows.append(
                self._chrome_row_skeleton(
                    key,
                    cdp=cdp_ok,
                    playwright=pw_ok,
                    pages=pages,
                    alive=bool(cdp_ok and pw_ok),
                    last_error="" if cdp_ok else str(cached.get("last_error") or ""),
                )
            )
        return rows

    async def chrome_status_for_ui(self):
        rows = []
        for key in ACCOUNT_EMAILS:
            try:
                rows.append(
                    await asyncio.wait_for(self.inspect_account_chrome(key), timeout=2.2)
                )
            except Exception:
                cached = dict(
                    (getattr(self, "chrome_health_cache", None) or {}).get(key) or {}
                )
                rows.append(cached if cached else self._chrome_row_skeleton(key))
        return rows

    async def inspect_account_chrome(self, account_key):
        cdp_ok = await self._cdp_ready(self._cdp_port(account_key), timeout_s=0.8)
        pw_ok = False
        ctx_ok = False
        pages = 0
        err = ""
        browser = (getattr(self, "yahoo_browsers", None) or {}).get(account_key)
        ctx = (self.yahoo_contexts or {}).get(account_key)
        if cdp_ok:
            if browser is not None:
                try:
                    pw_ok = bool(browser.is_connected())
                except Exception as e:
                    pw_ok = False
                    err = str(e)[:160]
            if ctx is not None:
                try:
                    pages = len([p for p in ctx.pages if not p.is_closed()])
                    ctx_ok = True
                    pw_ok = True
                except Exception as e:
                    ctx_ok = False
                    pw_ok = False
                    err = str(e)[:160]
        info = self._chrome_row_skeleton(
            account_key,
            cdp=bool(cdp_ok),
            playwright=bool(pw_ok),
            pages=pages,
            alive=bool(cdp_ok and ctx_ok),
            last_error=err,
        )
        if not hasattr(self, "chrome_health_cache") or self.chrome_health_cache is None:
            self.chrome_health_cache = {}
        self.chrome_health_cache[account_key] = info
        return info

    def _pids_listening_on_port(self, port):
        pids = set()
        try:
            kwargs = {
                "text": True,
                "errors": "ignore",
                "stderr": subprocess.DEVNULL,
            }
            if os.name == "nt":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            out = subprocess.check_output(["netstat", "-ano", "-p", "tcp"], **kwargs)
        except Exception:
            return []
        suffix = f":{int(port)}"
        for line in out.splitlines():
            cols = line.split()
            if len(cols) < 5:
                continue
            if cols[-2].upper() != "LISTENING":
                continue
            local = cols[1]
            if local.endswith(suffix) or local.rsplit(":", 1)[-1] == str(port):
                try:
                    pid = int(cols[-1])
                except ValueError:
                    continue
                if pid > 0:
                    pids.add(pid)
        return list(pids)

    def _kill_chrome_on_port(self, port):
        killed = []
        my_pid = os.getpid()
        for pid in self._pids_listening_on_port(port):
            if pid == my_pid:
                continue
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                else:
                    os.kill(pid, 9)
                killed.append(pid)
            except Exception:
                continue
        return killed

    async def _ensure_playwright(self):
        if self.playwright is None:
            self.playwright = await async_playwright().start()
        if not hasattr(self, "yahoo_browsers") or self.yahoo_browsers is None:
            self.yahoo_browsers = {}
        if not hasattr(self, "yahoo_contexts") or self.yahoo_contexts is None:
            self.yahoo_contexts = {}

    async def _drop_account_playwright(self, account_key):
        qbt_key = getattr(self, "qbt_account", QBT_BROWSER_ACCOUNT)
        ctx = (self.yahoo_contexts or {}).pop(account_key, None)
        browser = (self.yahoo_browsers or {}).pop(account_key, None)
        if qbt_key == account_key:
            if self.browser_context is ctx:
                self.browser_context = None
            self.page = None
        if browser is not None:
            try:
                await asyncio.wait_for(browser.close(), timeout=3)
            except Exception:
                pass

    async def restart_account_browser(self, account_key, headless=False, reason=""):
        """某个 Chrome 掉了：清 Playwright 引用，必要时杀占用端口的僵尸进程，再拉起并连 CDP。"""
        key = (account_key or "").strip().lower()
        if key not in ACCOUNT_EMAILS:
            raise ValueError(f"未知浏览器 {account_key}")
        self._mark_chrome_wanted(key, True)
        restarting = getattr(self, "chrome_restarting", None)
        if restarting is None:
            self.chrome_restarting = {}
            restarting = self.chrome_restarting
        async with self._chrome_guard():
            if restarting.get(key):
                return False
            restarting[key] = True
            try:
                why = f"（{reason}）" if reason else ""
                await self.log(f"[{key}] Chrome 准备重启{why}...", "WARNING")
                await self._drop_account_playwright(key)
                port = self._cdp_port(key)
                cdp_ok = await self._cdp_ready(port, timeout_s=1.0)
                if not cdp_ok:
                    killed = self._kill_chrome_on_port(port)
                    if killed:
                        await self.log(
                            f"[{key}] CDP {port} 无响应，已结束占用进程 {killed}，再启动 Chrome。",
                            "WARNING",
                        )
                        await asyncio.sleep(1.2)
                await self._ensure_playwright()
                qbt_key = getattr(self, "qbt_account", QBT_BROWSER_ACCOUNT)
                ctx, page = await self._launch_account_context(
                    key, headless, open_qbt=(key == qbt_key)
                )
                self.yahoo_contexts[key] = ctx
                if key == qbt_key:
                    self.browser_context = ctx
                    self.page = page
                if not hasattr(self, "chrome_last_restart") or self.chrome_last_restart is None:
                    self.chrome_last_restart = {}
                self.chrome_last_restart[key] = asyncio.get_running_loop().time()
                await self.log(f"[{key}] Chrome 已重新连上（CDP {port}）。")
                await self.inspect_account_chrome(key)
                return True
            finally:
                restarting[key] = False

    async def ensure_account_browser(self, account_key, headless=False):
        key = (account_key or "").strip().lower()
        for _ in range(50):
            if not (getattr(self, "chrome_restarting", None) or {}).get(key):
                break
            await asyncio.sleep(0.4)
        info = await self.inspect_account_chrome(key)
        if info.get("alive"):
            self._mark_chrome_wanted(key, True)
            return True
        if info.get("cdp") and not info.get("playwright"):
            await self.log(f"[{key}] Chrome 还在，自动化连接断了，只重连 CDP。", "WARNING")
            return await self.restart_account_browser(key, headless=headless, reason="CDP重连")
        return await self.restart_account_browser(key, headless=headless, reason="窗口消失")

    async def ensure_wanted_browsers(self):
        wanted = getattr(self, "chrome_wanted", None) or {}
        last = getattr(self, "chrome_last_restart", None) or {}
        now = asyncio.get_running_loop().time()
        cooldown = float(CHROME_RESTART_COOLDOWN or 45)
        for key in ACCOUNT_EMAILS:
            if not wanted.get(key):
                await self.inspect_account_chrome(key)
                continue
            info = await self.inspect_account_chrome(key)
            if info.get("alive"):
                continue
            prev = float(last.get(key) or 0)
            if prev and (now - prev) < cooldown:
                continue
            try:
                before = float((getattr(self, "chrome_last_restart", None) or {}).get(key) or 0)
                ok = await self.ensure_account_browser(key)
                after = float((getattr(self, "chrome_last_restart", None) or {}).get(key) or 0)
                if ok and after > before:
                    try:
                        self.notify_feishu(
                            f"Chrome [{key}] 掉线后已自动重启（CDP {self._cdp_port(key)}）。"
                        )
                    except Exception:
                        pass
            except Exception as e:
                await self.log(f"[{key}] 自动重启失败: {e}", "ERROR")

    async def chrome_watchdog_loop(self):
        await self.log("浏览器看门狗已启动（监控 ad/ym，掉线会自动拉起）。")
        interval = float(CHROME_WATCHDOG_SECONDS or 8)
        while True:
            try:
                await self.ensure_wanted_browsers()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                try:
                    await self.log(f"浏览器看门狗本轮异常: {e}", "DEBUG")
                except Exception:
                    pass
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise

    async def ensure_chrome_watchdog(self):
        task = getattr(self, "_chrome_watchdog_task", None)
        if task is not None and not task.done():
            return
        self._chrome_watchdog_task = asyncio.create_task(self.chrome_watchdog_loop())

    async def close_browser(self, kill_chrome=False):
        """默认只断开 Playwright。Chrome 进程留下，下次 main 用 CDP 再连。"""
        if kill_chrome:
            for key, ctx in list((self.yahoo_contexts or {}).items()):
                try:
                    if ctx:
                        await ctx.close()
                except Exception:
                    pass
            for key, browser in list((getattr(self, "yahoo_browsers", None) or {}).items()):
                try:
                    if browser:
                        await browser.close()
                except Exception:
                    pass
        self.yahoo_contexts = {}
        self.yahoo_browsers = {}
        self.browser_context = None
        self.page = None
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass
            self.playwright = None
