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
