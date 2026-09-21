# Restored from YahooAutoBot_v2.0.exe (Python 3.9 / PyInstaller, unencrypted)
# Purchase flow reconstructed from bytecode constants + pycdas; retry/debug branches condensed.
import asyncio
import datetime
import json
import os
import random
import re
import sys
import time

import aiofiles
from playwright.async_api import BrowserContext, Page, async_playwright


class BotStopped(Exception):
    pass


class YahooAutoBot:
    DEFAULT_CVV = "059"
    DEFAULT_CARD_ID = "23"
    FORCE_SHIPPING_WARNING_SELLERS = ["82meTj3FgR5hnjCcmaDn6vbxatPWC"]
    REMARK_WHITELIST_PATTERNS = [
        r"出价\d+日元失败：竞拍结束。",
        "dxez09955@yahoo.co.jp",
    ]
    DEFAULT_RISK_KEYWORDS = [
        "直接引取",
        "来店引取",
        "西濃運輸",
        "会社名義",
        "個人宅配達不可",
        "個人様名義",
        "西濃",
    ]

    def __init__(self, log_callback):
        self.log_callback = log_callback
        self.is_running = False
        self.is_paused = False
        self.playwright = None
        self.browser_context = None
        self.page = None
        if getattr(sys, "frozen", False):
            self.BASE_DIR = os.path.dirname(sys.executable)
            self.BUNDLE_DIR = getattr(sys, "_MEIPASS", self.BASE_DIR)
        else:
            self.BASE_DIR = os.path.dirname(os.path.abspath(__file__))
            self.BUNDLE_DIR = self.BASE_DIR

        self.USER_DATA_DIR = os.path.join(self.BASE_DIR, "user_data")
        self.BACKEND_URL = "https://www.qbt.jp/yii/web/index.php?r=shopAdmin/order/get-auction-not-buy-list"
        self.RISK_KEYWORDS_FILE = os.path.join(self.BASE_DIR, "risk_keywords.json")
        internal_risk_keywords = os.path.join(self.BUNDLE_DIR, "risk_keywords.json")
        if (not os.path.exists(self.RISK_KEYWORDS_FILE)) and os.path.exists(internal_risk_keywords):
            self.RISK_KEYWORDS_FILE = internal_risk_keywords
        self.RISK_KEYWORDS = self.load_risk_keywords()
        self.yahoo_password = None
        self.attempted_orders = set()
        self.blocked_risk_orders = []
        self.warned_bundles = set()
        self.screenshot_pending_orders = set()
        self.seller_messages = []
        self.RISK_HISTORY_FILE = os.path.join(self.BASE_DIR, "risk_history.json")
        self.load_risk_history()
        self.LOGS_DIR = os.path.join(self.BASE_DIR, "logs")
        try:
            os.makedirs(self.LOGS_DIR, exist_ok=True)
        except Exception as e:
            print(f"Failed to create logs dir: {e}")
        self.MESSAGES_DIR = os.path.join(self.BASE_DIR, "seller_messages")
        try:
            os.makedirs(self.MESSAGES_DIR, exist_ok=True)
        except Exception as e:
            print(f"Failed to create seller_messages dir: {e}")
        self.BLACKLIST_FILE = os.path.join(self.BASE_DIR, "seller_blacklist.json")
        self.seller_blacklist = self.load_seller_blacklist()
        self.load_seller_messages_today()

    def load_seller_blacklist(self):
        """加载店铺黑名单 [{seller_id, note, added_time}]"""
        try:
            if os.path.exists(self.BLACKLIST_FILE):
                with open(self.BLACKLIST_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            print(f"Error loading blacklist: {e}")
        return []

    def save_seller_blacklist(self):
        try:
            with open(self.BLACKLIST_FILE, "w", encoding="utf-8") as f:
                json.dump(self.seller_blacklist, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving blacklist: {e}")

    def add_to_blacklist(self, seller_id, note=""):
        """添加卖家到黑名单"""
        if any(b.get("seller_id") == seller_id for b in self.seller_blacklist):
            return False
        self.seller_blacklist.append(
            {
                "seller_id": seller_id,
                "note": note,
                "added_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        self.save_seller_blacklist()
        return True

    def remove_from_blacklist(self, seller_id):
        """从黑名单移除卖家"""
        original = len(self.seller_blacklist)
        self.seller_blacklist = [b for b in self.seller_blacklist if b.get("seller_id") != seller_id]
        if len(self.seller_blacklist) < original:
            self.save_seller_blacklist()
            return True
        return False

    def is_blacklisted(self, seller_id):
        return any(b.get("seller_id") == seller_id for b in self.seller_blacklist)

    def load_risk_history(self):
        try:
            if os.path.exists(self.RISK_HISTORY_FILE):
                with open(self.RISK_HISTORY_FILE, "r", encoding="utf-8") as f:
                    self.blocked_risk_orders = json.load(f)
                    return
        except Exception as e:
            print(f"Error loading risk history: {e}")
        self.blocked_risk_orders = []

    def save_risk_history(self):
        try:
            with open(self.RISK_HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(self.blocked_risk_orders, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving risk history: {e}")

    def load_seller_messages_today(self):
        """NOTE: 启动时加载当天的留言数据"""
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        self.seller_messages = self.load_seller_messages_by_date(today)

    def load_seller_messages_by_date(self, date_str):
        """按日期加载留言数据，格式: YYYY-MM-DD"""
        filepath = os.path.join(self.MESSAGES_DIR, f"{date_str}.json")
        try:
            if os.path.exists(filepath):
                with open(filepath, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            print(f"Error loading seller messages for {date_str}: {e}")
        return []

    def save_seller_messages(self):
        """保存当前留言到当天的 JSON 文件"""
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        filepath = os.path.join(self.MESSAGES_DIR, f"{today}.json")
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(self.seller_messages, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving seller messages: {e}")

    def delete_seller_message(self, order_id):
        """按订单号删除留言记录"""
        original_count = len(self.seller_messages)
        self.seller_messages = [m for m in self.seller_messages if str(m.get("order_id")) != str(order_id)]
        if len(self.seller_messages) < original_count:
            self.save_seller_messages()
            return True
        return False

    def get_seller_message_dates(self):
        """获取所有有留言记录的日期列表"""
        dates = []
        if os.path.exists(self.MESSAGES_DIR):
            for f in os.listdir(self.MESSAGES_DIR):
                if f.endswith(".json"):
                    dates.append(f.replace(".json", ""))
        dates.sort(reverse=True)
        return dates

    async def extract_seller_messages(self, yahoo_page: Page, order_id, is_store: bool = False):
        """从雅虎交易页面提取卖家留言"""
        messages = []
        seller_type = "店铺" if is_store else "个人"
        try:
            if is_store:
                msg_header = yahoo_page.locator("h2:has-text('メッセージ'), h3:has-text('メッセージ')").first
                if await msg_header.count() > 0:
                    msg_section = msg_header.locator("xpath=./ancestor::section")
                    if await msg_section.count() == 0:
                        msg_section = msg_header.locator("xpath=./ancestor::div[contains(@class,'sc-')]")
                    if await msg_section.count() == 0:
                        msg_section = msg_header.locator("xpath=../..")
                    dd_elements = await msg_section.locator("dd").all()
                    for dd in dd_elements:
                        text = (await dd.inner_text()).strip()
                        if not text:
                            continue
                        sys_texts = (
                            bool(re.match(r"^\d+月\d+日\s*\d+時\d+分$", text))
                            or bool(re.match(r"^[a-zA-Z]?\d{5,}$", text))
                            or bool(re.match(r"^[\d,]+円?$", text))
                            or "ストア" in text
                        )
                        if sys_texts:
                            continue
                        messages.append({"sender": "卖家", "time": "", "text": text})
            else:
                dl_elements = await yahoo_page.locator("#messagelist dl").all()
                await self.log(f"个人留言: 找到 {len(dl_elements)} 个 dl 消息块", "DEBUG")
                if not dl_elements:
                    await self.log("个人留言: 未找到 #messagelist 区域", "DEBUG")
                for dl in dl_elements:
                    sender = "卖家"
                    sender_loc = dl.locator("dt p")
                    if await sender_loc.count() > 0:
                        sender = (await sender_loc.first.inner_text()).strip() or sender
                    time_str = ""
                    time_loc = dl.locator("dt span.decTime")
                    if await time_loc.count() > 0:
                        time_str = (await time_loc.first.inner_text()).strip()
                    body = ""
                    body_loc = dl.locator("dd")
                    if await body_loc.count() > 0:
                        body = (await body_loc.first.inner_text()).strip()
                    if body:
                        messages.append({"sender": sender, "time": time_str, "text": body})
            if messages:
                existing = [m for m in self.seller_messages if str(m.get("order_id")) == str(order_id)]
                entry = {
                    "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "order_id": str(order_id),
                    "seller_type": seller_type,
                    "messages": messages,
                }
                if existing:
                    existing[0].update(entry)
                else:
                    self.seller_messages.insert(0, entry)
                self.save_seller_messages()
                preview = messages[0].get("text", "")[:80]
                await self.log(
                    f"卖家留言提取: 订单 {order_id} ({seller_type}) 发现 {len(messages)} 条留言",
                    "INFO",
                )
                await self.log(f"  留言内容: [{messages[0].get('sender', '')}] {preview}...")
            else:
                await self.log(f"卖家留言提取: 订单 {order_id} ({seller_type}) 无留言")
        except Exception as e:
            await self.log(f"卖家留言提取异常: {e}", "ERROR")

    def add_risk_history_entry(self, order_id, reason):
        for entry in self.blocked_risk_orders:
            if entry.get("order_id") == str(order_id) and entry.get("reason") == reason:
                return
        entry = {
            "order_id": str(order_id),
            "reason": reason,
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "Unprocessed",
        }
        self.blocked_risk_orders.insert(0, entry)
        self.save_risk_history()

    def delete_risk_history_entry(self, order_id):
        """Deletes a risk history entry by Order ID"""
        original_count = len(self.blocked_risk_orders)
        self.blocked_risk_orders = [
            o for o in self.blocked_risk_orders if str(o.get("order_id")) != str(order_id)
        ]
        if len(self.blocked_risk_orders) < original_count:
            self.save_risk_history()
            return True
        return False

    async def check_risk_order_status(self, order_id):
        """
        Check the status of a risk-blocked order in the backend system.
        Returns: "Processed" (if '已购买') or "Unprocessed" (if '未购买' or other)
        """
        if not self.browser_context:
            return {"success": False, "error": "Browser not started"}
        page = None
        try:
            SEARCH_URL = "https://www.qbt.jp/yii/web/index.php?r=shopAdmin/order/auction-order-list"
            page = await self.browser_context.new_page()
            await page.goto(SEARCH_URL, wait_until="domcontentloaded")
            await page.locator("#id").fill(str(order_id))
            await page.locator("button:has-text('搜索')").click()
            target_row = page.locator(f"tr.data-tr[data-id='{order_id}']")
            try:
                await target_row.wait_for(state="visible", timeout=8000)
            except Exception:
                await self.log(f"Status Check: Order {order_id} not found in list.", "WARNING")
                return {"success": True, "status": "Unprocessed", "detail": "Order Not Found"}
            cols = target_row.locator("td")
            status_text = (await cols.nth(max(await cols.count() - 1, 0)).inner_text()).strip()
            new_status = "Processed" if "已购买" in status_text else "Unprocessed"
            updated = False
            for entry in self.blocked_risk_orders:
                if str(entry.get("order_id")) == str(order_id):
                    entry["status"] = new_status
                    updated = True
            if updated:
                self.save_risk_history()
            return {"success": True, "status": new_status, "detail": status_text}
        except Exception as e:
            await self.log(f"Status check failed: {e}", "ERROR")
            return {"success": False, "error": str(e)}
        finally:
            if page:
                await page.close()

    def load_risk_keywords(self):
        try:
            if os.path.exists(self.RISK_KEYWORDS_FILE):
                with open(self.RISK_KEYWORDS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return data
        except Exception as e:
            print(f"Error loading risk keywords: {e}")
        return list(self.DEFAULT_RISK_KEYWORDS)

    def save_risk_keywords(self):
        try:
            with open(self.RISK_KEYWORDS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.RISK_KEYWORDS, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving risk keywords: {e}")

    async def add_risk_keyword(self, keyword):
        if keyword not in self.RISK_KEYWORDS:
            self.RISK_KEYWORDS.append(keyword)
            self.save_risk_keywords()
            await self.log(f"已添加违禁词: {keyword}")

    async def remove_risk_keyword(self, keyword):
        if keyword in self.RISK_KEYWORDS:
            self.RISK_KEYWORDS.remove(keyword)
            self.save_risk_keywords()
            await self.log(f"已移除违禁词: {keyword}")

    async def log(self, message, level="INFO"):
        """Send log to frontend via WebSocket and save to file"""
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        log_msg = f"[{timestamp}] [{level}] {message}"
        print(log_msg)
        if self.log_callback:
            try:
                await self.log_callback(log_msg)
            except Exception:
                pass
        try:
            date_str = datetime.datetime.now().strftime("%Y-%m-%d")
            log_file = os.path.join(self.LOGS_DIR, f"{date_str}.log")
            async with aiofiles.open(log_file, mode="a", encoding="utf-8") as f:
                await f.write(log_msg + "\n")
        except Exception as e:
            print(f"File logging failed: {e}")

    async def trigger_sound(self, sound_type="error"):
        """Send sound command to frontend. Types: success, error, attention"""
        if self.log_callback:
            await self.log_callback(f"[[SOUND:{sound_type}]]")

    async def save_error_snapshot(self, page: Page, tag="error"):
        """Save HTML and Screenshot for debugging"""
        try:
            timestamp = int(time.time())
            snapshot_dir = os.path.join(self.BASE_DIR, "error_snapshots")
            os.makedirs(snapshot_dir, exist_ok=True)
            base_name = f"error_{tag}_{timestamp}"
            shot_path = os.path.join(snapshot_dir, base_name + ".png")
            try:
                await page.screenshot(path=shot_path, full_page=True)
            except Exception as e:
                await self.log(f"Snapshot Screenshot Failed: {e}", "WARNING")
            html_path = os.path.join(snapshot_dir, base_name + ".html")
            try:
                content = await page.content()
                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(content)
            except Exception as e:
                await self.log(f"Snapshot HTML Failed: {e}", "WARNING")
            await self.log(f"SNAPSHOT SAVED: {base_name} (in error_snapshots/)", "ERROR")
            await self.trigger_sound("error")
        except Exception as e:
            await self.log(f"Snapshot Logic Failed: {e}", "ERROR")

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
                    "https://auctions.yahoo.co.jp/",
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

    async def stop(self):
        """Stops the bot loop"""
        self.is_running = False
        await self.log("正在停止机器人...")

    async def close_browser(self):
        if self.browser_context:
            await self.browser_context.close()
            self.browser_context = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None

    async def _fill_cvv(self, page: Page, cvv_code=None):
        """尝试填写 CVV 安全码，返回是否成功填写"""
        if cvv_code is None:
            cvv_code = self.DEFAULT_CVV
        try:
            cvv = page.locator("input[name='dummy_code']")
            if await cvv.count() > 0 and await cvv.first.is_visible():
                await cvv.first.fill(str(cvv_code))
                await cvv.first.blur()
                await self.log(f"已填写 CVV: {cvv_code}")
                return True
        except Exception as e:
            await self.log(f"CVV 填写异常: {e}", "DEBUG")
        return False

    def ensure_running(self):
        """Checks if bot should continue running, else raises BotStopped"""
        if not self.is_running:
            raise BotStopped("Execution cancelled by user.")

    async def run_loop(self, headless=False, test_mode=True, target_order_id=None):
        """Main automation loop"""
        if self.is_running:
            return
        self.is_running = True
        self.attempted_orders.clear()
        self.warned_bundles.clear()
        run_stats = {"total_processed": 0, "success": 0, "failed": 0, "skipped": 0, "details": []}
        start_time = datetime.datetime.now()
        await self.start_browser(headless=headless)
        await self.log("自动化任务已启动。")
        try:
            while self.is_running:
                self.ensure_running()
                backend_page = await self.find_backend_tab()
                if not backend_page:
                    await self.log("未找到后台标签页。尝试打开...", "WARNING")
                    backend_page = await self.browser_context.new_page()
                    await backend_page.goto(self.BACKEND_URL, wait_until="domcontentloaded")
                orders = await self.scan_backend_list(backend_page, target_order_id)
                if not orders:
                    if target_order_id:
                        await self.log(f"单单测试: 未找到目标订单 {target_order_id}。继续扫描...")
                    else:
                        await self.log("未发现待处理订单，或触发同捆风控。休眠10秒...")
                    await asyncio.sleep(10)
                    continue
                blocked_ids = {str(item.get("order_id")) for item in self.blocked_risk_orders}
                valid_orders = [
                    o
                    for o in orders
                    if str(o.get("order_id")) not in self.attempted_orders
                    and str(o.get("order_id")) not in blocked_ids
                ]
                if not valid_orders:
                    await self.log(
                        f"All {len(orders)} pending orders have been attempted/skipped. Sleeping 10s..."
                    )
                    await asyncio.sleep(10)
                    continue
                for order in valid_orders:
                    if not self.is_running:
                        break
                    order_id_str = str(order.get("order_id", "Unknown"))
                    self.attempted_orders.add(order_id_str)
                    run_stats["total_processed"] += 1
                    try:
                        result = await self.process_order(order, backend_page, test_mode)
                        status = (result or {}).get("status", "UNKNOWN_ERROR") if result else "NoneResult"
                        if status in ("PURCHASED", "TEST_SUCCESS", "WAIT_SHIPPING", "WAIT_SHIPPING_CONTACT"):
                            run_stats["success"] += 1
                            run_stats["details"].append(f"[{order_id_str}] Success ({status})")
                        elif str(status).startswith("SKIPPED"):
                            run_stats["skipped"] += 1
                            run_stats["details"].append(f"[{order_id_str}] Skipped ({status})")
                        else:
                            run_stats["failed"] += 1
                            run_stats["details"].append(f"[{order_id_str}] Failed ({status})")
                    except BotStopped:
                        raise
                    except Exception as e:
                        run_stats["failed"] += 1
                        run_stats["details"].append(f"[{order_id_str}] Failed ({e})")
                        await self.log(f"Loop error: {e}", "ERROR")
                    if target_order_id:
                        await self.log(f"单单测试结束: {order_id_str}。自动停止。", "INFO")
                        self.is_running = False
                        break
        except BotStopped:
            await self.log("操作员收到指令：立即停止所有任务。", "INFO")
        except Exception as e:
            await self.log(f"Loop error: {e}", "ERROR")
        finally:
            end_time = datetime.datetime.now()
            duration = end_time - start_time
            summary = (
                "\n========================================\n"
                "   自动化运行报告 (Execution Summary)\n"
                "========================================\n"
                f"开始时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"结束时间: {end_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"运行时长: {str(duration).split('.')[0]}\n"
                "----------------------------------------\n"
                f"处理总数: {run_stats['total_processed']}\n"
                f"成功订单: {run_stats['success']}\n"
                f"失败订单: {run_stats['failed']}\n"
                f"跳过订单: {run_stats['skipped']}\n"
                "详细记录:\n"
            )
            if run_stats["details"]:
                summary += "".join(f" - {det}\n" for det in run_stats["details"])
            else:
                summary += " - (无处理记录)\n"
            await self.log(summary)
            self.is_running = False
            await self.log("自动化已停止。")

    async def find_backend_tab(self):
        """Finds the tab that looks like the internal backend list"""
        if not self.browser_context:
            return None
        for page in self.browser_context.pages:
            url = page.url or ""
            title = ""
            try:
                title = await page.title()
            except Exception:
                title = ""
            if "qbt.jp" in url and "shopAdmin/order" in url:
                return page
            if any(k in title for k in ("未购买", "订单列表", "雅虎拍卖")) and "qbt.jp" in url:
                return page
        return None

    async def scan_backend_list(self, page: Page, target_order_id=None):
        """Scans the table for processable orders (Stage 1)"""
        await page.bring_to_front()
        orders = []
        rows = await page.locator("table.public_table tr").all()
        scanned_items = []
        for i, row in enumerate(rows):
            self.ensure_running()
            try:
                if await row.locator("th").count() > 0:
                    continue
                purchase_link = row.locator("a:has-text('购买')")
                if await purchase_link.count() == 0:
                    continue
                has_split_btn = await row.locator("text=拆分").count() > 0
                status_cell = row.locator("td:has-text('未购买')")
                status_text_check = ""
                if await status_cell.count() > 0:
                    status_text_check = (await status_cell.first.inner_text()).strip()
                seller_input = row.locator("input.yahoo-store")
                seller_id = "UNKNOWN"
                if await seller_input.count() > 0:
                    seller_id = await seller_input.first.get_attribute("value") or "UNKNOWN"
                order_id = "00000"
                cells = await row.locator("td").all()
                for cell in cells[:4]:
                    id_text = (await cell.inner_text()).strip()
                    id_match = re.search(r"\b(\d{5,8})\b", id_text)
                    if id_match:
                        order_id = id_match.group(1)
                        break
                is_match = (not target_order_id) or (str(target_order_id) == str(order_id))
                if target_order_id and is_match:
                    await self.log(f"🎯 单单测试模式: 匹配到目标订单 {order_id}")
                row_text = (await row.inner_text()).strip()
                onclick_val = await purchase_link.first.get_attribute("onclick") or ""
                match = re.search(r"key_a?(\d+)", onclick_val)
                status_text = "未购买"
                if "待确认" in status_text_check:
                    status_text = "未购买 待确认"
                option_info = "普通快递（有快递单号可追踪）"
                if "普通快递" in row_text or "有快递单号" in row_text or "可追踪" in row_text:
                    option_info = "普通快递（有快递单号可追踪）"
                storage_code = ""
                scanned_items.append(
                    {
                        "row": row,
                        "purchase_link": purchase_link.first,
                        "seller_id": seller_id,
                        "order_id": order_id,
                        "status": status_text,
                        "has_split_btn": has_split_btn,
                        "option_info": option_info,
                        "storage_code": storage_code,
                    }
                )
            except Exception:
                continue

        if target_order_id:
            found_target_bundle = None
            for item in scanned_items:
                if str(item.get("order_id")) == str(target_order_id):
                    found_target_bundle = item
                    break
            if found_target_bundle:
                return [found_target_bundle]
            await self.log(f"单单测试: 未能在扫描结果中找到订单 {target_order_id}。")
            return []

        bundles = {}
        for item in scanned_items:
            key = f"{item.get('seller_id')}_{item.get('storage_code', '')}"
            bundles.setdefault(key, []).append(item)

        process_queue = []
        for key, items in bundles.items():
            if len(items) <= 1:
                process_queue.extend(items)
                continue
            is_manual_bundle = any(itm.get("has_split_btn") for itm in items) and all(
                "待确认" in str(itm.get("status", "")) for itm in items
            )
            valid_in_bundle = [itm for itm in items if "未购买" in str(itm.get("status", ""))]
            if is_manual_bundle and valid_in_bundle:
                main_item = valid_in_bundle[0]
                await self.log(
                    f"发现已确认同捆组 (卖家: {main_item.get('seller_id')}, 用户: {key}, "
                    f"数量: {len(valid_in_bundle)})。优先处理首项 ID: {main_item.get('order_id')}"
                )
                process_queue.append(main_item)
            else:
                bundle_sig = key
                if bundle_sig not in self.warned_bundles:
                    self.warned_bundles.add(bundle_sig)
                    await self.log(
                        f"同捆风控: 发现来自同一卖家 ({items[0].get('seller_id')}) 的 "
                        f"{len(items)} 笔订单，但未识别为‘已确认同捆’(无拆分按钮)。已跳过以防误操作。",
                        "WARNING",
                    )
        return process_queue

    async def process_order(self, order, backend_page: Page, test_mode=True):
        row = order["row"]
        purchase_link = order["purchase_link"]
        await self.log(f"开始处理订单 (卖家: {order['seller_id']})")
        if self.is_blacklisted(order["seller_id"]):
            await self.log(
                f"⛔ 卖家 {order['seller_id']} 在黑名单中，跳过订单 {order.get('order_id', '')}",
                "WARNING",
            )
            return {"status": "SKIPPED_BLACKLIST"}
        self.ensure_running()
        pages_before = len(backend_page.context.pages)
        await purchase_link.click()
        await asyncio.sleep(1)
        pages_after = len(backend_page.context.pages)
        detail_page = backend_page
        is_new_tab = False
        if pages_after > pages_before:
            detail_page = backend_page.context.pages[-1]
            await detail_page.wait_for_load_state("domcontentloaded")
            is_new_tab = True
        try:
            await detail_page.wait_for_selector(".active_form", timeout=15000)
            await self.log("已进入后台详情页")
        except Exception as e:
            await self.log(f"阶段2失败 (读取详情/打开雅虎): {e}", "ERROR")
            return {"status": "DETAIL_PAGE_ERROR"}

        try:
            remark_tables = await detail_page.locator("table.public_table").all()
            remark_content = ""
            for r_table in remark_tables:
                header_cols = await r_table.locator("th").all_text_contents()
                remark_idx = None
                for hi, h_text in enumerate(header_cols):
                    if "备注" in h_text:
                        remark_idx = hi
                        break
                if remark_idx is None:
                    continue
                r_rows = await r_table.locator("tr").all()
                for r_row in r_rows[1:2]:
                    data_cols = r_row.locator("td")
                    if await data_cols.count() > remark_idx:
                        remark_content = (await data_cols.nth(remark_idx).inner_text()).strip()
            if remark_content:
                is_whitelisted = any(
                    (p in remark_content) or re.search(p, remark_content)
                    for p in self.REMARK_WHITELIST_PATTERNS
                )
                if is_whitelisted:
                    await self.log(f"备注符合白名单规则，已忽略并继续: {remark_content}")
                else:
                    await self.log(f"检测到未授权或异常后台订单备注: {remark_content}", "WARNING")
                    self.add_risk_history_entry(order.get("order_id"), f"后台备注拦截: {remark_content}")
                    await self.log("订单存在异常备注，跳过处理。", "WARNING")
                    return {"status": "SKIPPED_BACKEND_REMARK"}
        except Exception as rem_e:
            await self.log(f"备注检查异常 (Non-critical): {rem_e}", "DEBUG")

        bound_items = []
        backend_total = 0
        option_info = order.get("option_info")
        try:
            detail_rows = await detail_page.locator("tr.auction").all()
            for idx, d_row in enumerate(detail_rows):
                cols = d_row.locator("td")
                count = await cols.count()
                item_data = {"product_id": "", "price": 0}
                p_text = (await d_row.inner_text()).replace(",", "")
                p_val = re.search(r"(\d+)", p_text)
                if idx == 0:
                    first_id_text = (await cols.nth(0).inner_text()) if count else ""
                    first_id_match = re.search(r"(\d+)", first_id_text or "")
                    if first_id_match:
                        first_id = first_id_match.group(1)
                        if str(order.get("order_id")) != first_id:
                            await self.log(
                                f"同捆订单：将主要 ID 从 {order.get('order_id')} 切换为第一项 ID {first_id}"
                            )
                            order["order_id"] = first_id
                bound_items.append(item_data)
            await self.log(f"后台详情解析: 共 {len(bound_items)} 个商品, 总金额 {backend_total}")
            if option_info:
                await self.log(f"从详情页更新选项信息: {option_info}")
        except Exception as e:
            await self.log(f"提取首项 ID 失败: {e}", "DEBUG")

        product_link = detail_page.locator("a.goods_name").first
        if await product_link.count() == 0:
            await self.log("Product link not found!", "ERROR")
            return {"status": "PRODUCT_LINK_NOT_FOUND"}

        yahoo_page = None
        try:
            async with detail_page.expect_page() as new_page_info:
                await product_link.click()
            yahoo_page = await new_page_info.value
            await yahoo_page.wait_for_load_state("domcontentloaded")
            await self.log(f"已打开雅虎页面: {yahoo_page.url}")
        except Exception as e:
            await self.log(f"阶段2失败 (读取详情/打开雅虎): {e}", "ERROR")
            return {"status": "DETAIL_PAGE_ERROR"}

        if "login.yahoo.co.jp" in (yahoo_page.url or ""):
            await self.log("严重错误：雅虎未登录！停止运行。", "ERROR")
            await self.trigger_sound("error")
            self.is_running = False
            return {"status": "YAHOO_NOT_LOGGED_IN"}

        try:
            risk_loc = yahoo_page.locator("dl.Price__delivery, div.Price")
            try:
                await risk_loc.first.wait_for(state="visible", timeout=8000)
            except Exception:
                pass
            risk_text = await yahoo_page.locator("body").inner_text()
            risk_found = False
            matched_keyword = ""
            for keyword in self.RISK_KEYWORDS:
                if keyword and keyword in risk_text:
                    risk_found = True
                    matched_keyword = keyword
                    break
            if risk_found:
                msg = f"风险检测：发现敏感词 '{matched_keyword}'。跳过订单。"
                await self.log(msg, "WARNING")
                self.add_risk_history_entry(order.get("order_id", "Unknown"), matched_keyword)
                return {"status": "SKIPPED_RISK"}
        except Exception as e:
            await self.log(f"Risk Check Extraction Error: {e}", "DEBUG")

        order_info = dict(order)
        order_info["bound_items"] = bound_items
        order_info["backend_total"] = backend_total
        result = await self.execute_yahoo_purchase(yahoo_page, order_info, test_mode)
        try:
            await self.backfill_order(detail_page, result, backend_total, test_mode)
        except Exception as e:
            await self.log(f"Navigation error after processing: {e}", "ERROR")
        if is_new_tab:
            try:
                if not detail_page.is_closed():
                    await self.log("Cleanup: Closing Backend Detail Page (Tab).")
                    await detail_page.close()
                    await backend_page.goto(self.BACKEND_URL, wait_until="domcontentloaded")
            except Exception:
                pass
        return result

    async def execute_yahoo_purchase(self, yahoo_page: Page, order_info, test_mode=True):
        try:
            return await self._execute_yahoo_purchase_logic(yahoo_page, order_info, test_mode)
        except BotStopped:
            raise
        except Exception as e:
            await self.log(f"雅虎下单执行异常: {e}", "ERROR")
            await self.save_error_snapshot(yahoo_page, "purchase_exception")
            return {"status": "ERROR", "error": str(e)}

    async def _execute_yahoo_purchase_logic(self, yahoo_page: Page, order_info, test_mode=True):
        await self.log(f"阶段3: 执行雅虎下单逻辑 (V6.0)... 订单信息: {order_info}")
        early_cod_detected = False
        is_wait_for_shipping_status = False
        is_store = False
        is_personal = False
        shipping_cost = 0
        yahoo_total_amount = 0
        order_id = str(order_info.get("order_id", "00000"))
        suffix = f"{order_id[-5:]}室"
        backend_total = int(order_info.get("backend_total") or 0)
        need_track = "可追踪" in str(order_info.get("option_info") or "") or "快递单号" in str(
            order_info.get("option_info") or ""
        )

        async def check_combined_shipping():
            await self.log("正在检测同捆发货弹窗...")
            try:
                modal = yahoo_page.locator("text=まとめて購入手続きができます").first
                await modal.wait_for(state="visible", timeout=3000)
                if await modal.count() and await modal.is_visible():
                    text = await modal.inner_text()
                    await self.log(f"发现同捆发货提示: {text[:80]}", "WARNING")
                    return True
            except Exception:
                pass
            return False

        async def handle_bundle_confirmation_interstitial():
            bundle_view_btn = yahoo_page.locator("a:has-text('まとめて取引する商品を見る')")
            if await bundle_view_btn.count() == 0:
                return None
            await self.log("检测到 '查看同捆商品' 按钮。开始验证同捆信息...")
            await bundle_view_btn.first.click()
            await yahoo_page.wait_for_load_state("domcontentloaded")
            yahoo_items = []
            item_rows = await yahoo_page.locator("div.sc-bbfc97d-2").all()
            for row in item_rows:
                link = row.locator("a[href*='/auction/']")
                href = await link.first.get_attribute("href") if await link.count() else ""
                pid_match = re.search(r"/auction/(\w+)", href or "")
                pid = pid_match.group(1) if pid_match else "UNKNOWN"
                text_content = await row.inner_text()
                price_match = re.search(r"落札価格.*?([\d,]+)円", text_content)
                price = int(price_match.group(1).replace(",", "")) if price_match else 0
                yahoo_items.append({"pid": pid, "price": price})
                await self.log(f"雅虎同捆项: {pid} - {price}円")
            backend_items = order_info.get("bound_items") or []
            y_ids = {i["pid"] for i in yahoo_items}
            b_ids = {str(i.get("product_id")) for i in backend_items if i.get("product_id")}
            if not y_ids:
                await self.log("同捆验证失败：无法提取商品ID。", "ERROR")
                return {"status": "BUNDLE_ERROR"}
            if b_ids and y_ids != b_ids:
                await self.log(f"同捆不一致! 雅虎: {y_ids}, 后台: {b_ids}", "ERROR")
                self.add_risk_history_entry(order_info.get("order_id"), "同捆商品不一致")
                return {"status": "BUNDLE_MISMATCH"}
            y_total = sum(i["price"] for i in yahoo_items)
            b_total = int(order_info.get("backend_total") or 0)
            if b_total and abs(y_total - b_total) > 0:
                await self.log(f"同捆总价不一致! 雅虎: {y_total}, 后台: {b_total}", "WARNING")
            else:
                await self.log("同捆商品验证通过！")
            back_btn = yahoo_page.locator("a:has-text('取引ナビに戻る')")
            if await back_btn.count():
                await back_btn.first.click()
                await yahoo_page.wait_for_load_state("domcontentloaded")
            else:
                await self.log("未找到 '返回交易导航' 按钮。", "ERROR")
                return {"status": "nav_ERROR"}
            check_buy_btn = yahoo_page.locator("a:has-text('購入手続きする')")
            try:
                await check_buy_btn.first.wait_for(state="visible", timeout=5000)
            except Exception:
                await self.log("同捆验证后，未发现 '购买手续' 按钮。可能卖家尚未同意。", "WARNING")
                self.add_risk_history_entry(order_info.get("order_id"), "人工处理: 同捆未就绪")
                return {"status": "SKIPPED_BUNDLE_NOT_READY"}
            return None

        try:
            if await check_combined_shipping():
                self.add_risk_history_entry(order_info.get("order_id"), "人工处理: 发现同捆提示")
                return {"status": "SKIPPED_COMBINED_SHIPPING"}
        except Exception as e:
            await self.log(f"同捆验证异常: {e}", "DEBUG")

        try:
            bundle_res = await handle_bundle_confirmation_interstitial()
            if bundle_res:
                return bundle_res
        except Exception as e:
            await self.log(f"同捆验证流程异常: {e}", "ERROR")
            return {"status": "BUNDLE_EXCEPTION"}

        buy_now_btn = yahoo_page.locator("button:has-text('購入手続きへ')")
        buy_now_auction_btn = yahoo_page.locator("button:has-text('今すぐ落札')")
        if await buy_now_btn.count() > 0:
            await self.log("检测到一口价/即决订单 (購入手続きへ)。正在点击...")
            is_store = True
            await buy_now_btn.first.click()
        elif await buy_now_auction_btn.count() > 0:
            await self.log("检测到竞拍/一口价共存订单 (今すぐ落札)。")
            page_text = await yahoo_page.locator("body").inner_text()
            match = re.search(r"即決.*?([\d,]+)円", page_text)
            if match:
                price_int = int(match.group(1).replace(",", ""))
                backend_price = int(order_info.get("backend_total") or 0)
                await self.log(f"价格核对: 雅虎即决={price_int}, 后台金额={backend_price}")
                if backend_price and price_int != backend_price:
                    await self.log("价格不一致，放弃立即购买。转为普通处理（可能导致跳过或竞拍）。")
                else:
                    await self.log("价格一致！执行立即购买。")
                    await buy_now_auction_btn.first.click()
                    try:
                        await yahoo_page.locator("h3:has-text('入札内容の確認')").wait_for(timeout=5000)
                        newsletter = yahoo_page.locator("input[name='Newsletter']")
                        if await newsletter.count() and await newsletter.first.is_checked():
                            await newsletter.first.uncheck()
                            await self.log("已取消订阅 Newsletter 勾选。")
                        win_btn = yahoo_page.locator("button:has-text('落札する')")
                        if await win_btn.count():
                            if test_mode:
                                await self.log("[TestMode] 模拟点击 (不提交)...")
                            else:
                                await win_btn.first.click()
                            await yahoo_page.wait_for_selector("text=おめでとうございます", timeout=15000)
                    except Exception as e:
                        await self.log(f"立即购买流程出错: {e}", "WARNING")
            else:
                await self.log("未提取到即决价格，无法核对。跳过立即购买。")
        else:
            nav_link = yahoo_page.locator("a:has-text('取引ナビ')")
            store_btn = yahoo_page.locator("button:has-text('購入手続きへ')")
            if await nav_link.count() > 0:
                is_personal = True
                await self.log("检测到个人卖家。正在点击 '取引ナビ'...")
                await nav_link.first.click()
                try:
                    await yahoo_page.wait_for_load_state("networkidle")
                except Exception:
                    pass
                try:
                    rejection_text = "出品者が単品での取引を希望したため、商品ごとに取引を行ってください"
                    if rejection_text in (await yahoo_page.locator("body").inner_text()):
                        await self.log(
                            f"检测到卖家拒绝同捆提示：'{rejection_text[:20]}...'。跳过订单，转为人工处理。",
                            "ERROR",
                        )
                        close_btn = yahoo_page.locator(
                            "button:has-text('閉じる'), a:has-text('閉じる'), input[value='閉じる']"
                        )
                        if await close_btn.count():
                            await close_btn.first.click()
                        self.add_risk_history_entry(order_info.get("order_id"), "卖家拒绝同捆: 要求单品交易")
                        return {"status": "SKIPPED_BUNDLE_REJECTED"}
                except Exception as e:
                    await self.log(f"同捆拒绝检测异常: {e}", "DEBUG")
                purchase_btn = yahoo_page.locator("a:has-text('購入手続きする'), button:has-text('購入手続きする')")
                if await purchase_btn.count():
                    await self.log("发现个人卖家 '购买手续' (蓝色按钮)。正在点击...")
                    await purchase_btn.first.click()
                kantan_btn = yahoo_page.locator("a:has-text('Yahoo!かんたん決済で支払う')")
                if await kantan_btn.count():
                    await self.log("发现 'Yahoo!かんたん決済' 按钮。正在点击...")
                    await kantan_btn.first.click()
                body_text = await yahoo_page.locator("body").inner_text()
                if "送料連絡待ち" in body_text or "送料の連絡があります" in body_text:
                    is_wait_for_shipping_status = True
                    await self.log("状态：等待运费联系 (2.2)。转入个人流程处理（地址+潜在物流选择）。")
                    return await self.handle_personal_address_only(yahoo_page, order_info)
            elif await yahoo_page.locator("a:has-text('購入手続きする')").count():
                is_store = True
                await self.log("发现中间页店铺 '购买手续' 按钮。正在点击...")
                await yahoo_page.locator("a:has-text('購入手続きする')").first.click()

        try:
            await yahoo_page.wait_for_selector("text=お届け先", timeout=8000)
            await self.log("已进入购买输入页。等待表单...")
        except Exception:
            await self.log("警告：未找到 'お届け先'，页面可能不同。继续...", "WARNING")

        body_text = ""
        try:
            body_text = await yahoo_page.locator("body").inner_text()
        except Exception:
            body_text = ""
        if "クレジットカードはご利用できません" in body_text or (
            "この商品カテゴリではPayPay残高、PayPayクレジット、クレジットカードはご利用できません" in body_text
        ):
            await self.log("严重错误：此类目不支持信用卡支付。停止运行。", "ERROR")
            self.add_risk_history_entry(order_info.get("order_id"), "不支持信用卡")
            await self.trigger_sound("error")
            return {"status": "PAYMENT_UNSUPPORTED"}

        newsletter = yahoo_page.locator("input[name='mailDeliveryCheckBox']")
        try:
            if await newsletter.count():
                await self.log("店铺：取消勾选新闻订阅 (Force/JS)...")
                await yahoo_page.evaluate(
                    "document.querySelector(\"input[name='mailDeliveryCheckBox']\") && "
                    "document.querySelector(\"input[name='mailDeliveryCheckBox']\").click()"
                )
                await self.log("店铺：已取消勾选新闻订阅。")
        except Exception as e:
            await self.log(f"Store Newsletter Warning: {e}", "DEBUG")

        # Address suffix: last 5 digits of order id + 室
        try:
            addr_ok = suffix in (await yahoo_page.content())
            if addr_ok:
                await self.log(f"地址检查: 页面已包含正确地址后缀 '{suffix}'，跳过修改。")
            else:
                change_btn = yahoo_page.locator("h2:has-text('お届け先')").locator("a:has-text('変更')")
                edit_btn = yahoo_page.locator("a:has-text('編集する'), input[value='編集する'], button:has-text('編集する')")
                if await change_btn.count():
                    await self.log("店铺：发现地址变更按钮。正在点击...")
                    await change_btn.first.click()
                elif await edit_btn.count():
                    await edit_btn.first.click()
                input_found = False
                for i in range(3):
                    inp = yahoo_page.locator("input[name='address2'], input[name='home_address2']")
                    if await inp.count():
                        await inp.first.fill("")
                        await inp.first.fill(suffix)
                        await inp.first.blur()
                        input_found = True
                        break
                    await self.log(f"地址输入框未找到 (第 {i + 1}/3 次)，等待页面继续加载...")
                    await asyncio.sleep(1)
                if not input_found:
                    await self.log("严重拦截：无法确认个人买家页面的地址准确性。为防止发错货已强制停止。", "ERROR")
                    self.add_risk_history_entry(order_info.get("order_id"), "人工处理: 无法修改个人卖家收件地址")
                    await self.save_error_snapshot(yahoo_page, "personal_address_input_missing")
                    return {"status": "ADDRESS_FAIL"}
                save_btn = yahoo_page.locator(
                    "a:has-text('変更する'), button:has-text('変更する'), input[value='変更する'], "
                    "button:has-text('登録する'), input[value='決定する']"
                )
                if await save_btn.count():
                    await save_btn.first.click()
                    await self.log(f"店铺地址已修改并验证: {suffix}")
        except Exception as e:
            await self.log(f"个人地址修改失败或异常: {e}", "ERROR")
            self.add_risk_history_entry(order_info.get("order_id"), "人工处理: 个人地址块异常宕机")
            return {"status": "ADDRESS_FAIL"}

        # Shipping: prefer trackable, exclude store pickup
        try:
            pickup_keywords = ("店頭受取", "店頭受け取り", "来店", "手渡し")
            radios = await yahoo_page.locator("input[type='radio']").all()
            candidates = []
            pickup_options_count = 0
            all_options_count = 0
            for radio in radios:
                parent = radio.locator("xpath=../..")
                p_str = await parent.inner_text()
                if any(k in p_str for k in ("配送", "お届け", "円")):
                    all_options_count += 1
                    if any(k in p_str for k in pickup_keywords):
                        pickup_options_count += 1
                        await self.log(f"店铺：发现到店自提/当面交易方式，已排除该选项: {p_str[:40]}")
                        continue
                    price_m = re.search(r"([\d,]+)", p_str)
                    price = int(price_m.group(1).replace(",", "")) if price_m else 0
                    is_trackable = any(k in p_str for k in ("追跡", "宅配", "宅急便", "ゆうパック", "ネコポス"))
                    if need_track and not is_trackable:
                        continue
                    candidates.append({"element": radio, "price": price, "text": p_str[:80], "trackable": is_trackable})
            if all_options_count > 0 and pickup_options_count == all_options_count:
                await self.log("严重错误：仅有「到店自提(店頭受取)」这一种配送方式。请人工处理。", "ERROR")
                self.add_risk_history_entry(order_info.get("order_id"), "人工处理: 仅支持到店自提")
                await self.save_error_snapshot(yahoo_page, "store_pickup_only")
                return {"status": "STORE_PICKUP_ONLY_ERROR"}
            if candidates:
                best = sorted(candidates, key=lambda c: (0 if c["trackable"] else 1, c["price"]))[0]
                await best["element"].evaluate(
                    "el => { el.checked = true; el.click(); "
                    "el.dispatchEvent(new Event('change', {bubbles: true})); }"
                )
                await self.log(f"店铺：选择最佳配送: {best['text']} ({best['price']}円)")
        except Exception as e:
            await self.log(f"店铺配送选择异常: {e}", "DEBUG")

        # Okihai off
        try:
            okihai_select = yahoo_page.locator("select[name='okihaiTypeYamato'], select[name='okihaiTypeSelect'], #ymtokhi select")
            if await okihai_select.count():
                await self.log("检测到 '置き配' (投放配置) 选项。正在关闭 (选择 '利用しない')...")
                try:
                    await okihai_select.first.select_option(label=re.compile("しない|希望しない|置き配を設定しない"))
                except Exception:
                    await okihai_select.first.select_option(index=1)
        except Exception as e:
            await self.log(f"Okihai setting error (Non-critical): {e}", "DEBUG")

        # Force credit card
        try:
            cc_radio = yahoo_page.locator(
                "input[value='card'], input[id*='card'], label:has-text('クレジットカード')"
            )
            if await cc_radio.count():
                if not await cc_radio.first.is_checked() if await cc_radio.first.evaluate("el => el.type === 'radio' || el.type === 'checkbox'") else True:
                    await cc_radio.first.click()
                    await self.log("已成功切换为信用卡。")
            pay_header = yahoo_page.locator("h2:has-text('お支払い方法'), h3:has-text('お支払い方法')")
            if await pay_header.count():
                current_payment_text = await pay_header.first.locator("xpath=ancestor::section").inner_text()
                if "クレジットカード" not in current_payment_text:
                    await self.log(f"支付方式非信用卡 (检测到: {current_payment_text[:40]}...)。尝试切换...")
                    change = yahoo_page.locator("a:has-text('変更する'), button:has-text('変更する')")
                    if await change.count():
                        await change.first.click()
                        label = yahoo_page.locator("label:has-text('クレジットカード')")
                        if await label.count():
                            await label.first.click()
                            await self.log("支付方式已更新为信用卡。")
                        else:
                            await self.log("严重风险: 该店铺不支持信用卡支付 (或未绑定有效卡)。停止下单。", "ERROR")
                            self.add_risk_history_entry(order_info.get("order_id"), "人工处理: 店铺不支持信用卡")
                            return {"status": "RISK_STOP_NO_CC"}
        except Exception as e:
            await self.log(f"Payment Method Check Error: {e}", "DEBUG")

        if "着払" in body_text or "着払い" in body_text:
            early_cod_detected = True
            await self.log(f"EARLY COD DETECTION: 着払 -> Marking as COD.")

        await self.extract_seller_messages(yahoo_page, order_id, is_store=is_store)

        def parse_yen(text):
            match = re.search(r"([\d,]+)", text or "")
            return int(match.group(1).replace(",", "")) if match else 0

        # Confirm / pay buttons
        pay_btn_selectors = [
            "input[value='確認する']",
            "button:has-text('確認する')",
            "a:has-text('確認する'):not([href*='paypay']):not([href*='campaign'])",
            "input[value='決定する']",
            "button:has-text('決定する')",
            "a:has-text('決定する')",
            "#mBoxConfBt",
            "a.gv-Button--primary",
            "a:has-text('購入を確定する')",
            "button:has-text('購入を確定する')",
        ]
        clicked = False
        for sel in pay_btn_selectors:
            btn = yahoo_page.locator(sel).first
            try:
                if await btn.count() and await btn.is_visible():
                    await self.log(f"发现支付按钮 ({sel})")
                    await self._fill_cvv(yahoo_page, self.DEFAULT_CVV)
                    if test_mode:
                        await self.log("[TestMode] 模拟点击 (不提交)...")
                        clicked = True
                        break
                    await self.log("正在点击 (REAL BUY)...")
                    await btn.click()
                    clicked = True
                    await self._fill_cvv(yahoo_page, self.DEFAULT_CVV)
                    break
            except Exception:
                continue

        if "paypay-card" in (yahoo_page.url or ""):
            await self.log("Redirected to PayPay Card Signup! Backtracking...")
            await yahoo_page.go_back()
            return {"status": "REDIRECT_PAYPAY_CARD"}

        # Success detection
        is_success = False
        try:
            title = await yahoo_page.title()
            url = yahoo_page.url or ""
            body_snippet = ""
            try:
                body_snippet = await yahoo_page.locator("body").inner_text()
            except Exception:
                body_snippet = ""
            success_tokens = (
                "thank-you",
                "購入完了",
                "購入が完了",
                "手続きの完了",
                "購入が完了しました",
                "ご注文ありがとう",
                "ご購入ありがとうございます",
                "/order/thank-you",
            )
            if any(t in url or t in title or t in body_snippet for t in success_tokens):
                is_success = True
                await self.log(f"快速检测到成功状态 (URL/Title): {title} {url}")
                await self.trigger_sound("success")
        except Exception as e:
            await self.log(f"Success Detection Failed with Error: {e}", "DEBUG")

        if not is_success and not test_mode:
            await self.log("购买未确认：未检测到「購入が完了しました」成功页面。可能未完成支付，需人工确认。", "WARNING")
            self.add_risk_history_entry(order_info.get("order_id"), "人工处理: 未检测到购买成功页面")
            await self.save_error_snapshot(yahoo_page, "purchase_unconfirmed")
            return {"status": "PURCHASE_UNCONFIRMED", "shipping": shipping_cost, "total": yahoo_total_amount}

        status = "TEST_SUCCESS" if test_mode else "PURCHASED"
        if is_wait_for_shipping_status:
            status = "WAIT_SHIPPING_CONTACT"
        return {
            "status": status,
            "shipping": shipping_cost,
            "total": yahoo_total_amount,
            "is_cod": early_cod_detected,
            "shipping_warning": early_cod_detected,
        }

    async def handle_personal_address_only(self, yahoo_page: Page, order_info):
        """Helper to edit address when status is Wait for Shipping"""
        order_id = order_info.get("order_id", "00000")
        suffix = f"{str(order_id)[-5:]}室"

        async def safe_fill(selector, value):
            try:
                loc = yahoo_page.locator(selector)
                if await loc.count() == 0:
                    return False
                current_val = await loc.first.input_value()
                if value in (current_val or ""):
                    await self.log(f"地址字段 {selector} 已验证: {value}")
                    return True
                await loc.first.fill(value)
                await self.log(f"填充 {selector}")
                return True
            except Exception as e:
                await self.log(f"填充 {selector} 错误: {e}", "WARNING")
                return False

        def clean_str(s):
            return (s or "").replace(" ", "").replace("\n", "").strip()

        try:
            edit_btn = yahoo_page.locator("a:has-text('編集する'), input[value='編集する']")
            if await edit_btn.count():
                await edit_btn.first.click()
                await yahoo_page.wait_for_selector("input[name='home_address2'], input[name='address2']")
            filled = await safe_fill("input[name='home_address2']", suffix)
            if not filled:
                filled = await safe_fill("input[name='address2']", suffix)
            save_clicked = False
            for sel in (
                "button:has-text('登録する')",
                "input[value='登録する']",
                "input[value='決定する'], button:has-text('決定する'), a:has-text('決定する')",
                "input[value='変更する']",
                "input[type='submit']",
                "#confSubmitBtn",
            ):
                btn = yahoo_page.locator(sel)
                if await btn.count():
                    await btn.first.click()
                    save_clicked = True
                    break
            if not save_clicked:
                await self.log("Form Save/Decision button not found! Attempting generic submit...", "WARNING")
            await yahoo_page.wait_for_load_state("domcontentloaded")
            body_text = await yahoo_page.locator("body").inner_text()
            if suffix in clean_str(body_text) or suffix in body_text:
                await self.log(f"地址已验证 (待发货): {suffix}")
                return {"status": "WAIT_SHIPPING_CONTACT", "shipping": 0, "total": 0}
            await self.log(f"地址验证失败！后缀 '{suffix}' 未找到。", "ERROR")
            return {"status": "ADDRESS_FAIL"}
        except Exception as e:
            await self.log(f"地址修改 (待发货) 失败: {e}", "ERROR")
            await self.save_error_snapshot(yahoo_page, "address_mod_exception")
            return {"status": "ADDRESS_FAIL"}

    async def backfill_order(self, backend_page: Page, yahoo_result, backend_total=0, test_mode=True):
        await self.log("Stage 4: Backfilling Data to Backend...")
        if not yahoo_result:
            return
        status = yahoo_result.get("status")
        try:
            if status in ("WAIT_SHIPPING", "WAIT_SHIPPING_CONTACT"):
                btn = backend_page.locator(
                    "input[value='等待商家确认运费'], button:has-text('等待商家确认运费'), "
                    "button[onclick='waitBuyerConfirm()']"
                )
                if test_mode:
                    await self.log("测试模式：已填写物流偏好。请手动点击 '等待商家确认运费'。")
                    try:
                        await btn.first.wait_for(state="hidden", timeout=20000)
                        await self.log("测试模式：'等待商家确认运费' 已手动确认。")
                    except Exception:
                        await self.log("测试模式：未检测到手动确认 (Bot 将继续)。", "WARNING")
                elif await btn.count():
                    await self.log("回填：正在点击 '等待商家确认运费'...")
                    await btn.first.evaluate("element => element.click()")
                    await self.log("回填：已点击 '等待商家确认运费'。")
                else:
                    await self.log("回填警告：未找到 '等待商家确认运费' 按钮。", "WARNING")
                return

            if status not in ("PURCHASED", "TEST_SUCCESS"):
                return

            shipping = int(yahoo_result.get("shipping") or 0)
            yahoo_total = int(yahoo_result.get("total") or 0)
            calculated_total = int(backend_total or 0) + shipping
            if yahoo_total and backend_total and calculated_total != yahoo_total:
                await self.log(
                    f"严重错误：金额不匹配! 后台({backend_total}) + 运费({shipping}) != 雅虎({yahoo_total})",
                    "ERROR",
                )
                await self.log("停止回填。转为人工确认。")
                return
            if yahoo_total:
                await self.log(f"金额已验证: {calculated_total} == {yahoo_total}")

            fee_input = backend_page.locator("input.jpWaybillFee")
            if await fee_input.count():
                await fee_input.first.fill(str(shipping))

            is_cod = bool(yahoo_result.get("is_cod"))
            cod_val = "1" if is_cod else "0"
            cod_radio = backend_page.locator(f"input[name='is_cod'][value='{cod_val}']")
            if await cod_radio.count():
                await cod_radio.first.check()
                await self.log(f"回填：设置 COD 为 {'YES' if is_cod else 'NO'}")
            else:
                await self.log("回填：未找到 COD 选项。", "WARNING")

            card_sel = backend_page.locator("select.cardId")
            if await card_sel.count():
                try:
                    await card_sel.first.select_option(self.DEFAULT_CARD_ID)
                except Exception:
                    pass

            if await backend_page.locator("text=上传订单截图").count():
                oid = yahoo_result.get("order_id") or "Unknown"
                self.screenshot_pending_orders.add(str(oid))
                await self.log(f"警告: 订单 {oid} 需要手动上传截图！请人工处理。", "WARNING")
                await self.trigger_sound("attention")

            seller_id = yahoo_result.get("seller_id") or ""
            if seller_id in self.FORCE_SHIPPING_WARNING_SELLERS or yahoo_result.get("shipping_warning"):
                await self.log(f"特殊店铺 {seller_id} -> 强制添加运费变更备注")
                warning_msg = "商家后期可能会变更运费,麻烦留意一下,谢谢。"
                log_area = backend_page.locator("textarea.log_content")
                if await log_area.count():
                    await log_area.first.fill(warning_msg)
                    await self.log("回填：已在操作日志中添加运费变更提示。")
                else:
                    await self.log("回填警告：未找到 '操作日志' 输入框 (textarea.log_content)。", "WARNING")

            confirm_btn = backend_page.locator("button.sure-but-btn, button[onclick='sureClick()']")
            if test_mode:
                await self.log("测试模式：回填表单已填写。请手动点击 '确定'。")
                try:
                    await confirm_btn.first.wait_for(state="hidden", timeout=30000)
                    await self.log("测试模式：回填已手动确认。")
                except Exception:
                    await self.log("测试模式：超时或未检测到手动确认 (Bot 将继续)。", "WARNING")
                return
            if await confirm_btn.count():
                await self.log("回填：找到 '确定' 按钮。尝试点击...")
                try:
                    await confirm_btn.first.evaluate("element => element.click()")
                    await self.log("回填：已触发 JS Click。")
                except Exception:
                    await confirm_btn.first.click(force=True)
                    await self.log("回填：已触发 Force Click。")
                await self.log(f"回填：已填写运费 {shipping}, 订单已确认。")
            else:
                await self.log("回填错误：未找到 '确定' 按钮 (Selector: .sure-but-btn)。", "ERROR")
                await self.save_error_snapshot(backend_page, "backfill_btn_missing")
        except Exception as e:
            await self.log(f"回填错误: {e}", "ERROR")

