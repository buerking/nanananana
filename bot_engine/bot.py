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
    QBT_BROWSER_ACCOUNT,
    REMARK_WHITELIST_PATTERNS,
    SUCCESS_STATUSES,
)
from .feishu import send_feishu_text
from .exceptions import BotStopped
from .gates import GatesMixin
from .logging_utils import LoggingMixin
from .orders import OrderMixin
from .qbt import QbtMixin
from .storage import StorageMixin
from .ui_watch import YahooUiWatchMixin
from .waits import WaitMixin
from .yahoo_checkout import YahooCheckoutMixin
from .yahoo_purchase import YahooPurchaseMixin


class YahooAutoBot(
    LoggingMixin,
    WaitMixin,
    StorageMixin,
    BrowserMixin,
    YahooUiWatchMixin,
    GatesMixin,
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
        self.yahoo_contexts = {}
        self.yahoo_browsers = {}
        self.qbt_account = QBT_BROWSER_ACCOUNT
        if getattr(sys, "frozen", False):
            self.BASE_DIR = os.path.dirname(sys.executable)
            self.BUNDLE_DIR = getattr(sys, "_MEIPASS", self.BASE_DIR)
        else:
            # bot_engine/ 包的上一级才是工程根（main.py / user_data / logs）
            package_dir = os.path.dirname(os.path.abspath(__file__))
            self.BASE_DIR = os.path.dirname(package_dir)
            self.BUNDLE_DIR = self.BASE_DIR

        self.USER_DATA_DIRS = {
            "ad": os.path.join(self.BASE_DIR, "user_data"),
            "ym": os.path.join(self.BASE_DIR, "user_data_ym"),
        }
        self.USER_DATA_DIR = self.USER_DATA_DIRS["ad"]
        self.ui_miss_reported = set()
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
        self.run_mode = "normal"
        self.waiting_confirm = False
        self.pending_order_id = ""
        self.gated_queue = []
        self.confirm_action = None
        self.confirm_event = asyncio.Event()

    def notify_feishu(self, text):
        try:
            send_feishu_text(text)
        except Exception:
            pass

    def ensure_running(self):
        """Checks if bot should continue running, else raises BotStopped"""
        if not self.is_running:
            raise BotStopped("Execution cancelled by user.")

    async def stop(self):
        """Stops the bot loop"""
        self.is_running = False
        self.confirm_action = "stop"
        try:
            self.confirm_event.set()
        except Exception:
            pass
        await self.log("正在停止机器人...")

    def submit_operator_action(self, action):
        raw = (action or "").strip().lower()
        if raw not in ("y", "n", "stop"):
            return False
        self.confirm_action = raw
        try:
            self.confirm_event.set()
        except Exception:
            pass
        return True

    def _queue_public(self, orders):
        out = []
        for o in orders or []:
            out.append(
                {
                    "order_id": str(o.get("order_id") or ""),
                    "product_id": str(o.get("product_id") or ""),
                    "seller_id": str(o.get("seller_id") or ""),
                    "account_key": str(o.get("account_key") or ""),
                    "user_name": str(o.get("user_name") or ""),
                    "bid_email": str(o.get("bid_email") or ""),
                    "backend_total": int(o.get("backend_total") or 0),
                }
            )
        return out

    async def wait_operator(self, prompt, pending_order_id="", timeout=1800):
        self.ensure_running()
        self.confirm_event = asyncio.Event()
        self.confirm_action = None
        self.waiting_confirm = True
        self.pending_order_id = str(pending_order_id or "")
        await self.log(prompt, "WARNING")
        try:
            await asyncio.wait_for(self.confirm_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            await self.log("等待确认超时（30分钟）。结束本批。", "WARNING")
            self.confirm_action = "stop"
        finally:
            self.waiting_confirm = False
        if not self.is_running:
            return "stop"
        return self.confirm_action or "stop"

    async def _log_gated_queue(self, orders):
        stats = getattr(self, "scan_stats", None) or {}
        total = stats.get("page_total") or stats.get("scanned") or len(orders)
        buyable = stats.get("buyable")
        if buyable is None:
            buyable = len(orders)
        lines = [
            f"运行成功。代购买合计 {total} 笔，可以单条购买 {buyable} 笔。",
            "以下订单可以处理:" if orders else "当前没有可单条购买的订单。",
        ]
        for i, o in enumerate(orders, 1):
            oid = o.get("order_id")
            pid = o.get("product_id") or "-"
            acc = o.get("account_key") or "?"
            user = o.get("user_name") or "?"
            seller = o.get("seller_id") or "?"
            amount = o.get("backend_total") or 0
            lines.append(
                f"  {i}. {oid} / {pid} / 卖家 {seller} / 账号 {acc} / 用户 {user}"
                + (f" / 后台金额 {amount}" if amount else "")
            )
        await self.log("\n".join(lines))

    def _verdict_line(self, status):
        if status == "PURCHASED":
            return "本单自动购买是否成功: 是（雅虎購入完了）"
        if status in ("WAIT_SHIPPING", "WAIT_SHIPPING_CONTACT"):
            return "本单自动购买是否成功: 流程成功，待卖家运费"
        if status == "PURCHASE_UNCONFIRMED":
            return "本单自动购买是否成功: 未确认（已点确认但未见成功页，请人工核对是否扣款）"
        if status == "SKIPPED_NEED_USER_OPTION":
            return "本单自动购买是否成功: 跳过（需建工单联系用户选择款式）"
        if status == "SKIPPED_NEED_CVV":
            return "本单自动购买是否成功: 跳过（需人工填写信用卡安全码）"
        if status == "SKIPPED_NEED_3DS":
            return "本单自动购买是否成功: 跳过（需人工完成信用卡3DS验证；现网改为一直等待不再跳过）"
        if status == "SKIPPED_NO_PURCHASE_DATA":
            return "本单自动购买是否成功: 跳过（雅虎无购买数据，可能未中标或卖家取消）"
        if str(status).startswith("SKIPPED"):
            return f"本单自动购买是否成功: 跳过（{status}）"
        if status == "TEST_SUCCESS":
            return "本单自动购买是否成功: 测试模式模拟成功（未真实下单）"
        return f"本单自动购买是否成功: 否（{status}）"

    async def _record_order_result(self, run_stats, order_id_str, status, test_mode, gated=False):
        if status in SUCCESS_STATUSES:
            run_stats["success"] += 1
            run_stats["details"].append(f"[{order_id_str}] Success ({status})")
            if (not test_mode or gated) and status in (
                "PURCHASED",
                "WAIT_SHIPPING",
                "WAIT_SHIPPING_CONTACT",
            ):
                self.notify_feishu(f"订单 {order_id_str} 处理成功 ({status})")
        elif str(status).startswith("SKIPPED"):
            run_stats["skipped"] += 1
            run_stats["details"].append(f"[{order_id_str}] Skipped ({status})")
        else:
            run_stats["failed"] += 1
            run_stats["details"].append(f"[{order_id_str}] Failed ({status})")
            if status in (
                "PURCHASE_UNCONFIRMED",
                "ADDRESS_FAIL",
                "STORE_PICKUP_ONLY_ERROR",
                "PAYMENT_UNSUPPORTED",
                "NO_YAHOO_BROWSER",
                "DETAIL_PAGE_ERROR",
                "YAHOO_ID_MISMATCH",
                "SKIPPED_QBT_ID_MISMATCH",
                "APPRAISAL_FAIL",
            ):
                self.notify_feishu(f"订单 {order_id_str} 需要人工: {status}")

    async def run_loop(self, headless=False, test_mode=True, target_order_id=None, run_mode="normal"):
        """Main automation loop"""
        if self.is_running:
            return
        gated = (run_mode or "normal") == "gated_live"
        if gated:
            test_mode = False
        self.run_mode = "gated_live" if gated else "normal"
        self.is_running = True
        self.waiting_confirm = False
        self.pending_order_id = ""
        self.gated_queue = []
        self.attempted_orders.clear()
        self.warned_bundles.clear()
        self.ui_miss_reported = set()
        run_stats = {"total_processed": 0, "success": 0, "failed": 0, "skipped": 0, "details": []}
        start_time = datetime.datetime.now()
        try:
            await self.start_browser(headless=headless)
            await self.log("自动化任务已启动。")
            if gated:
                self.notify_feishu("YahooAutoBot 已启动（逐单实购，真实下单）")
            else:
                self.notify_feishu(
                    "YahooAutoBot 已启动"
                    + ("（测试模式）" if test_mode else "（正式模式）")
                    + (f"\n单单测试: {target_order_id}" if target_order_id else "")
                )
            buy_next_immediately = False
            skip_next_head = False
            while self.is_running:
                self.ensure_running()
                backend_page = await self.find_backend_tab()
                if not backend_page:
                    await self.log("未找到后台标签页。尝试打开...", "WARNING")
                    backend_page = await self.browser_context.new_page()
                backend_page = await self.ensure_backend_not_buy_list(
                    backend_page, force=True
                )
                scan_target = None if gated else target_order_id
                orders = await self.scan_backend_list(backend_page, scan_target)
                if not orders:
                    if target_order_id and not gated:
                        await self.log(f"单单测试: 未找到目标订单 {target_order_id}。继续扫描...")
                    else:
                        await self.log("未发现可购买的一对一订单。休眠10秒...")
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
                if gated:
                    self.gated_queue = self._queue_public(valid_orders)
                    await self._log_gated_queue(valid_orders)
                    head = valid_orders[0]
                    order_id_str = str(head.get("order_id", "Unknown"))
                    if skip_next_head:
                        skip_next_head = False
                        self.attempted_orders.add(order_id_str)
                        run_stats["skipped"] += 1
                        run_stats["details"].append(f"[{order_id_str}] Skipped (OPERATOR_N)")
                        await self.log(f"操作员跳过订单 {order_id_str}。")
                        buy_next_immediately = False
                        continue
                    if not buy_next_immediately:
                        action = await self.wait_operator(
                            "请回复 y 确认，将真实购买队列中的第 1 单 "
                            f"{order_id_str}（账号 {head.get('account_key') or '?'}）。"
                            "输入 n 跳过，stop 结束本批。【会真实扣款】",
                            pending_order_id=order_id_str,
                        )
                        if action == "stop":
                            break
                        if action != "y":
                            self.attempted_orders.add(order_id_str)
                            run_stats["skipped"] += 1
                            run_stats["details"].append(f"[{order_id_str}] Skipped (OPERATOR_N)")
                            await self.log(f"操作员跳过订单 {order_id_str}。")
                            continue
                    buy_next_immediately = False
                    self.attempted_orders.add(order_id_str)
                    run_stats["total_processed"] += 1
                    try:
                        result = await self.process_order(head, backend_page, False)
                        status = (result or {}).get("status", "UNKNOWN_ERROR") if result else "NoneResult"
                        if status == "DETAIL_PAGE_ERROR":
                            self.attempted_orders.discard(order_id_str)
                            await self.log(
                                f"订单 {order_id_str} 未点到「购买」，不记已尝试，下一轮再试。"
                            )
                        await self.log(self._verdict_line(status), "INFO")
                        await self._record_order_result(
                            run_stats, order_id_str, status, test_mode=False, gated=True
                        )
                        if status in ("SKIPPED_NEED_CVV", "SKIPPED_NO_PURCHASE_DATA"):
                            await self.log("本单已跳过，进入下一单确认。")
                            continue
                    except BotStopped:
                        raise
                    except Exception as e:
                        run_stats["failed"] += 1
                        run_stats["details"].append(f"[{order_id_str}] Failed ({e})")
                        await self.log(f"Loop error: {e}", "ERROR")
                        await self.log(self._verdict_line("ERROR"), "ERROR")
                    after = await self.wait_operator(
                        f"人工核查窗口。订单 {order_id_str} 已跑完。"
                        "核查完毕后输入 y 开始下一单，n 跳过下一单队首，stop 结束本批。",
                        pending_order_id="",
                    )
                    if after == "stop":
                        break
                    if after == "y":
                        buy_next_immediately = True
                    else:
                        skip_next_head = True
                    continue
                for order in valid_orders:
                    if not self.is_running:
                        break
                    order_id_str = str(order.get("order_id", "Unknown"))
                    self.attempted_orders.add(order_id_str)
                    run_stats["total_processed"] += 1
                    status = ""
                    try:
                        result = await self.process_order(order, backend_page, test_mode)
                        status = (result or {}).get("status", "UNKNOWN_ERROR") if result else "NoneResult"
                        if status == "DETAIL_PAGE_ERROR":
                            self.attempted_orders.discard(order_id_str)
                            await self.log(
                                f"订单 {order_id_str} 未点到「购买」，不记已尝试，下一轮再试。"
                            )
                        await self._record_order_result(
                            run_stats, order_id_str, status, test_mode, gated=False
                        )
                    except BotStopped:
                        raise
                    except Exception as e:
                        run_stats["failed"] += 1
                        run_stats["details"].append(f"[{order_id_str}] Failed ({e})")
                        await self.log(f"Loop error: {e}", "ERROR")
                    backend_page = await self.ensure_backend_not_buy_list(
                        backend_page, force=(status == "DETAIL_PAGE_ERROR")
                    )
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
            self.notify_feishu(summary.strip())
            self.is_running = False
            self.waiting_confirm = False
            self.pending_order_id = ""
            self.gated_queue = []
            self.run_mode = "normal"
            await self.log("自动化已停止。")
