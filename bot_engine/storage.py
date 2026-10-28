import datetime
import json
import os
import re

from playwright.async_api import Page

from .constants import DEFAULT_RISK_KEYWORDS


class StorageMixin:
    """黑名单 / 违禁词 / 风险历史 / 卖家留言的本地 JSON。"""

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

    def load_risk_keywords(self):
        try:
            if os.path.exists(self.RISK_KEYWORDS_FILE):
                with open(self.RISK_KEYWORDS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return data
        except Exception as e:
            print(f"Error loading risk keywords: {e}")
        return list(DEFAULT_RISK_KEYWORDS)

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

    def load_wait_shipping_queue(self):
        """本机点过「等待商家确认运费」的订单，闲时再查卖家是否已报运费。"""
        try:
            if os.path.exists(self.WAIT_SHIPPING_QUEUE_FILE):
                with open(self.WAIT_SHIPPING_QUEUE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return data
        except Exception as e:
            print(f"Error loading wait-shipping queue: {e}")
        return []

    def save_wait_shipping_queue(self):
        try:
            with open(self.WAIT_SHIPPING_QUEUE_FILE, "w", encoding="utf-8") as f:
                json.dump(self.wait_shipping_queue, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving wait-shipping queue: {e}")

    def enqueue_wait_shipping(self, order, yahoo_result=None):
        """地址已提交、已点 QBT「等待商家确认运费」后入队。不扫「未购买待确认」整表。"""
        info = dict(order or {})
        extra = dict(yahoo_result or {})
        oid = str(info.get("order_id") or extra.get("order_id") or "").strip()
        if not oid:
            return False
        for item in self.wait_shipping_queue:
            if str(item.get("order_id") or "") == oid:
                return False
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.wait_shipping_queue.append(
            {
                "order_id": oid,
                "product_id": str(info.get("product_id") or extra.get("product_id") or ""),
                "account_key": str(info.get("account_key") or extra.get("account_key") or ""),
                "bid_email": str(info.get("bid_email") or extra.get("bid_email") or ""),
                "seller_id": str(info.get("seller_id") or extra.get("seller_id") or ""),
                "user_name": str(info.get("user_name") or extra.get("user_name") or ""),
                "storage_code": str(info.get("storage_code") or extra.get("storage_code") or ""),
                "added_at": now,
                "last_checked": "",
                "status": "waiting",
            }
        )
        self.save_wait_shipping_queue()
        return True

    def touch_wait_shipping(self, order_id):
        oid = str(order_id or "").strip()
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        changed = False
        for item in self.wait_shipping_queue:
            if str(item.get("order_id") or "") == oid:
                item["last_checked"] = now
                changed = True
                break
        if changed:
            self.save_wait_shipping_queue()
        return changed

    def remove_wait_shipping(self, order_id):
        oid = str(order_id or "").strip()
        before = len(self.wait_shipping_queue)
        self.wait_shipping_queue = [
            x for x in self.wait_shipping_queue if str(x.get("order_id") or "") != oid
        ]
        if len(self.wait_shipping_queue) < before:
            self.save_wait_shipping_queue()
            return True
        return False

    def _walk_trade_message_root(self, obj):
        """在 Next.js 状态树里找到 top.message（含 messages 列表）。"""
        if not isinstance(obj, dict):
            return None
        top = obj.get("top")
        if isinstance(top, dict):
            msg = top.get("message")
            if isinstance(msg, dict) and isinstance(msg.get("messages"), list):
                return msg
        for val in obj.values():
            if isinstance(val, dict):
                found = self._walk_trade_message_root(val)
                if found:
                    return found
        return None

    def _messages_from_next_data_payload(self, payload):
        """把 __NEXT_DATA__ 里按日分组的对话摊平成 [{sender, time, text}]。不必点「メッセージ」tab。"""
        root = self._walk_trade_message_root(payload)
        if not root:
            return []
        out = []
        for day in root.get("messages") or []:
            date = (day.get("date") or "").strip()
            for item in day.get("dailyMessages") or []:
                body = (item.get("body") or "").strip()
                if not body:
                    continue
                if item.get("isSystem"):
                    sender = "系统"
                elif item.get("isOwn"):
                    sender = "买家"
                else:
                    sender = (item.get("name") or "").strip() or "卖家"
                time_str = " ".join(p for p in (date, (item.get("time") or "").strip()) if p)
                out.append({"sender": sender, "time": time_str, "text": body})
        return out

    async def _messages_from_next_data(self, yahoo_page: Page):
        """新版取引ナビ：对话在 #__NEXT_DATA__，第一个「取引」tab 的 DOM 里没有气泡。"""
        try:
            loc = yahoo_page.locator("#__NEXT_DATA__")
            if await loc.count() == 0:
                return []
            raw = await loc.inner_text(timeout=3000)
            if not raw or not raw.strip():
                return []
            return self._messages_from_next_data_payload(json.loads(raw))
        except Exception:
            return []

    async def extract_seller_messages(self, yahoo_page: Page, order_id, is_store: bool = False):
        """从雅虎交易页提取卖家留言。新版优先读 __NEXT_DATA__，旧版再刮 #messagelist。"""
        messages = []
        seller_type = "店铺" if is_store else "个人"
        source = ""
        try:
            messages = await self._messages_from_next_data(yahoo_page)
            if messages:
                source = "__NEXT_DATA__"
            elif is_store:
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
                    if messages:
                        source = "store-dom"
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
                    source = "messagelist"
            already = any(str(m.get("order_id")) == str(order_id) for m in self.seller_messages)
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
                via = f" via {source}" if source else ""
                await self.log(
                    f"卖家留言提取: 订单 {order_id} ({seller_type}) 发现 {len(messages)} 条留言{via}",
                    "INFO",
                )
                await self.log(f"  留言内容: [{messages[0].get('sender', '')}] {preview}...")
            elif already:
                await self.log(
                    f"卖家留言提取: 订单 {order_id} 本页无新气泡（沿用已保存记录）",
                    "DEBUG",
                )
            else:
                await self.log(f"卖家留言提取: 订单 {order_id} ({seller_type}) 无留言")
        except Exception as e:
            await self.log(f"卖家留言提取异常: {e}", "ERROR")
