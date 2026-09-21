import asyncio
import datetime
import os
import sys

from .browser import BrowserMixin
from .constants import (
    BACKEND_URL,
    DEFAULT_CARD_ID,
    DEFAULT_CVV,
    DEFAULT_RISK_KEYWORDS,
    FORCE_SHIPPING_WARNING_SELLERS,
    REMARK_WHITELIST_PATTERNS,
    SUCCESS_STATUSES,
)
from .exceptions import BotStopped
from .logging_utils import LoggingMixin
from .orders import OrderMixin
from .qbt import QbtMixin
from .storage import StorageMixin
from .yahoo_checkout import YahooCheckoutMixin
from .yahoo_purchase import YahooPurchaseMixin


class YahooAutoBot(
    LoggingMixin,
    StorageMixin,
    BrowserMixin,
    QbtMixin,
    YahooCheckoutMixin,
    YahooPurchaseMixin,
    OrderMixin,
):
    DEFAULT_CVV = DEFAULT_CVV
    DEFAULT_CARD_ID = DEFAULT_CARD_ID
    FORCE_SHIPPING_WARNING_SELLERS = FORCE_SHIPPING_WARNING_SELLERS
    REMARK_WHITELIST_PATTERNS = REMARK_WHITELIST_PATTERNS
    DEFAULT_RISK_KEYWORDS = DEFAULT_RISK_KEYWORDS

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
            # bot_engine/ 包的上一级才是工程根（main.py / user_data / logs）
            package_dir = os.path.dirname(os.path.abspath(__file__))
            self.BASE_DIR = os.path.dirname(package_dir)
            self.BUNDLE_DIR = self.BASE_DIR

        self.USER_DATA_DIR = os.path.join(self.BASE_DIR, "user_data")
        self.BACKEND_URL = BACKEND_URL
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

    def ensure_running(self):
        """Checks if bot should continue running, else raises BotStopped"""
        if not self.is_running:
            raise BotStopped("Execution cancelled by user.")

    async def stop(self):
        """Stops the bot loop"""
        self.is_running = False
        await self.log("正在停止机器人...")

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
                        if status in SUCCESS_STATUSES:
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
