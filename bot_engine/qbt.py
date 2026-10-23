import re
from urllib.parse import parse_qs, unquote, urlparse

from playwright.async_api import Page

from .constants import (
    QBT_AUCTION_BUY_URL,
    QBT_BROWSER_ACCOUNT,
    RISK_STATUS_SEARCH_URL,
    WAIT_CLICK_MS,
)

_CF_EMAIL_RE = re.compile(r"[\w.+-]+@yahoo\.co\.jp", re.I)
_STORAGE_FROM_ZY_RE = re.compile(r"ZY\d+\s*/\s*([A-Za-z0-9]{5,6})", re.I)
_STORAGE_ONLY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{4,5}$")


def qbt_order_id_from_url(url):
    """auction-buy&id=724271 → 724271。列表页没有 id 则空。"""
    try:
        query = parse_qs(urlparse(url or "").query)
    except Exception:
        return ""
    return str((query.get("id") or [""])[0] or "").strip()


def parse_storage_code(text):
    """QBT「用户信息/入库码」：ZY26100805337948/HHLGY → HHLGY。不用数字用户 ID。"""
    raw = (text or "").strip()
    if not raw:
        return ""
    line = raw.split("\n")[0].strip()
    m = _STORAGE_FROM_ZY_RE.search(line.replace(" ", ""))
    if m:
        return m.group(1)
    if "/" in line:
        tail = line.split("/")[-1].strip()
        if _STORAGE_ONLY_RE.fullmatch(tail):
            return tail
    if _STORAGE_ONLY_RE.fullmatch(line):
        return line
    return ""
_ROW_COLOR_JS = """
(row) => {
  const isColored = (node) => {
    if (!node) return false;
    const s = getComputedStyle(node);
    const bg = (s.backgroundColor || "").trim();
    if (!bg || bg === "transparent" || bg === "rgba(0, 0, 0, 0)") return false;
    const m = bg.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
    if (!m) return true;
    return !(+m[1] >= 248 && +m[2] >= 248 && +m[3] >= 248);
  };
  const link = row.querySelector("a[href*='/auction/']");
  if (!link) return false;
  const td = link.closest("td") || link;
  if (isColored(td) || isColored(link)) return true;
  return [...td.querySelectorAll("*")].some(isColored);
}
"""


def decode_cf_email(cfemail):
    raw = (cfemail or "").strip()
    if len(raw) < 4:
        return ""
    try:
        key = int(raw[:2], 16)
        chars = [chr(int(raw[i : i + 2], 16) ^ key) for i in range(2, len(raw), 2)]
        return "".join(chars)
    except ValueError:
        return ""


class QbtMixin:
    """阶段1 扫未购买列表 + 阶段4 回填 qbt 后台。"""

    async def ensure_qbt_detail_is_this_order(self, ctx):
        """买哪单就打开哪单的 QBT 详情。同捆第一项、串页点到别的 auction-buy 都不能拿来改室号/点确定。"""
        want = str((ctx.order or {}).get("order_id") or "").strip()
        page = ctx.detail_page
        if not want or page is None:
            return None
        try:
            if page.is_closed():
                return {"status": "DETAIL_PAGE_ERROR"}
        except Exception:
            return {"status": "DETAIL_PAGE_ERROR"}
        got = qbt_order_id_from_url(page.url)
        if got == want:
            return None
        target = QBT_AUCTION_BUY_URL.format(order_id=want)
        await self.log(
            f"QBT 详情不是本单（当前 id={got or '无'}），改打开订单 {want}。",
            "WARNING",
        )
        try:
            await page.goto(target, wait_until="domcontentloaded", timeout=WAIT_CLICK_MS * 4)
            await page.wait_for_selector(".active_form", timeout=15000)
        except Exception as e:
            await self.log(f"打开本单 QBT 详情失败: {e}", "ERROR")
            return {"status": "DETAIL_PAGE_ERROR"}
        got = qbt_order_id_from_url(page.url)
        if got != want:
            await self.log(
                f"打开后仍不是本单 QBT 详情（当前 id={got or '无'}，本单 {want}）。",
                "ERROR",
            )
            return {"status": "DETAIL_PAGE_ERROR"}
        await self.log(f"已固定 QBT 详情到本单 {want}。")
        return None

    def _is_qbt_not_buy_list_url(self, url):
        u = unquote(url or "")
        return "qbt.jp" in u and "get-auction-not-buy-list" in u

    def _is_qbt_auction_buy_url(self, url):
        u = unquote(url or "")
        return "qbt.jp" in u and "auction-buy" in u

    async def find_backend_tab(self):
        """优先找未购买列表页；订单卡在详情时不要把 auction-buy 当拉单页。"""
        if not self.browser_context:
            return None
        any_qbt = None
        for page in self.browser_context.pages:
            url = unquote(page.url or "")
            title = ""
            try:
                title = await page.title()
            except Exception:
                title = ""
            if "qbt.jp" not in url:
                continue
            if "get-auction-not-buy-list" in url:
                return page
            if any_qbt is None and (
                "shopAdmin/order" in url
                or any(k in title for k in ("未购买", "订单列表", "雅虎拍卖"))
            ):
                any_qbt = page
        return any_qbt

    async def ensure_backend_not_buy_list(self, page=None, force=False):
        """打开 get-auction-not-buy-list。停在 auction-buy 的详情留给人工看，不要覆盖。"""
        list_page = None
        if self.browser_context:
            for p in list(self.browser_context.pages):
                try:
                    if p.is_closed():
                        continue
                except Exception:
                    continue
                if self._is_qbt_not_buy_list_url(p.url):
                    list_page = p
                    break
        if list_page is None:
            reuse = page
            try:
                if reuse is None or reuse.is_closed():
                    reuse = None
            except Exception:
                reuse = None
            if reuse is not None and self._is_qbt_auction_buy_url(reuse.url):
                reuse = None
            if reuse is None:
                found = await self.find_backend_tab()
                if found and not self._is_qbt_auction_buy_url(found.url):
                    reuse = found
            if reuse is None:
                reuse = await self.browser_context.new_page()
            list_page = reuse
        table = list_page.locator("table.public_table").first
        already_list = self._is_qbt_not_buy_list_url(list_page.url)
        buy_ready = False
        if already_list and not force:
            try:
                buy_ready = (
                    await list_page.locator("table.public_table a:has-text('购买')").count()
                    > 0
                )
            except Exception:
                buy_ready = False
        if buy_ready:
            await list_page.bring_to_front()
            return list_page
        await self.log(f"打开后台未购买列表: {self.BACKEND_URL}")
        try:
            await list_page.goto(
                self.BACKEND_URL, wait_until="domcontentloaded", timeout=WAIT_CLICK_MS * 2
            )
        except Exception as e:
            await self.log(f"打开未购买列表失败: {e}", "WARNING")
        try:
            await table.wait_for(state="visible", timeout=15000)
            await list_page.locator("table.public_table a:has-text('购买')").first.wait_for(
                state="attached", timeout=10000
            )
        except Exception as e:
            await self.log(f"未购买列表表格未出现: {e}", "WARNING")
        await list_page.bring_to_front()
        return list_page

    async def check_risk_order_status(self, order_id):
        """
        Check the status of a risk-blocked order in the backend system.
        Returns: "Processed" (if '已购买') or "Unprocessed" (if '未购买' or other)
        """
        if not self.browser_context:
            return {"success": False, "error": "Browser not started"}
        page = None
        try:
            page = await self.browser_context.new_page()
            await page.goto(RISK_STATUS_SEARCH_URL, wait_until="domcontentloaded")
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

    async def _row_has_qbt_color(self, row):
        try:
            return bool(await row.evaluate(_ROW_COLOR_JS))
        except Exception:
            return False

    async def _extract_bid_email(self, row, row_text=""):
        cf = row.locator("[data-cfemail], a.__cf_email__")
        if await cf.count() > 0:
            hexstr = await cf.first.get_attribute("data-cfemail") or ""
            email = decode_cf_email(hexstr)
            if email:
                return email
        match = _CF_EMAIL_RE.search(row_text or "")
        return match.group(0) if match else ""

    async def _extract_user_name(self, row, order_id, product_id, seller_id, bid_email):
        named = row.locator("td.user-name, td.userName, .user-name")
        if await named.count() > 0:
            text = (await named.first.inner_text()).strip()
            if text:
                return text
        skip = {
            str(order_id or ""),
            str(product_id or ""),
            str(seller_id or ""),
            str(bid_email or "").lower(),
            "未购买",
            "未购买待确认",
            "查看详情",
            "购买",
            "拆分",
            "否",
            "是",
        }
        cells = await row.locator("td").all_text_contents()
        for raw in cells:
            text = (raw or "").strip().split("\n")[0].strip()
            if not text or text in skip or text.lower() in skip:
                continue
            if "待确认" in text or "未购买" in text:
                continue
            if _CF_EMAIL_RE.search(text) or "@" in text:
                continue
            if re.fullmatch(r"\d{5,8}", text) or re.fullmatch(r"[\d,]+", text):
                continue
            if re.match(r"\d{4}-\d{2}-\d{2}", text):
                continue
            if re.match(r"ZY\d+", text, re.I):
                code = parse_storage_code(text)
                if code:
                    return code
                continue
            if re.fullmatch(r"[A-Za-z0-9_-]{16,}", text):
                continue
            if len(text) > 40:
                continue
            return text
        return ""

    async def scan_backend_list(self, page: Page, target_order_id=None):
        """阶段1：从 QBT「未购买」表抽出可点「购买」的行，再筛成一对一可购买订单。

        批量：去掉彩色行 → 跳过「未购买待确认」→ 按卖家分组审核，只留一对一。
        单单测试：命中行直接返回（待确认仍跳过；彩色仅警告）。
        """
        page = await self.ensure_backend_not_buy_list(page)
        page_total = 0
        try:
            banner = await page.locator("body").inner_text()
            m = re.search(r"当前有\s*(\d+)\s*个未购买", banner or "")
            if m:
                page_total = int(m.group(1))
        except Exception:
            page_total = 0
        self.scan_stats = {"page_total": page_total, "scanned": 0, "buyable": 0}
        rows = await page.locator("table.public_table tr").all()
        scanned_items = []
        for row in rows:
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
                product_id = ""
                auction_link = row.locator("a[href*='/auction/']")
                if await auction_link.count() > 0:
                    href = await auction_link.first.get_attribute("href") or ""
                    pid_match = re.search(r"/auction/([a-zA-Z]\d+)", href)
                    if pid_match:
                        product_id = pid_match.group(1)
                    else:
                        product_id = (await auction_link.first.inner_text()).strip()
                if target_order_id:
                    target = str(target_order_id).strip()
                    if target != str(order_id) and target.lower() != product_id.lower():
                        continue
                    await self.log(
                        f"🎯 单单测试模式: 匹配到后台ID {order_id}"
                        + (f" / 商品ID {product_id}" if product_id else "")
                    )
                row_text = (await row.inner_text()).strip()
                status_text = "未购买"
                if "待确认" in status_text_check or "待确认" in row_text:
                    status_text = "未购买待确认"
                option_info = "普通快递（有快递单号可追踪）"
                if "普通快递" in row_text or "有快递单号" in row_text or "可追踪" in row_text:
                    option_info = "普通快递（有快递单号可追踪）"
                bid_email = await self._extract_bid_email(row, row_text)
                account_key = self.account_key_from_email(bid_email) or ""
                user_name = await self._extract_user_name(
                    row, order_id, product_id, seller_id, bid_email
                )
                storage_code = parse_storage_code(row_text) or (
                    user_name if parse_storage_code(user_name) else ""
                )
                has_color = await self._row_has_qbt_color(row)
                scanned_items.append(
                    {
                        "row": row,
                        "purchase_link": purchase_link.first,
                        "seller_id": seller_id,
                        "order_id": order_id,
                        "product_id": product_id,
                        "status": status_text,
                        "has_split_btn": has_split_btn,
                        "option_info": option_info,
                        "storage_code": storage_code,
                        "bid_email": bid_email,
                        "account_key": account_key,
                        "user_name": user_name,
                        "has_color": has_color,
                    }
                )
            except Exception:
                continue

        if target_order_id:
            found = None
            target = str(target_order_id).strip()
            for item in scanned_items:
                if str(item.get("order_id")) == target:
                    found = item
                    break
                if target.lower() == str(item.get("product_id") or "").lower():
                    found = item
                    break
            if not found:
                await self.log(f"单单测试: 未能在扫描结果中找到订单 {target_order_id}。")
                return []
            if "待确认" in str(found.get("status") or ""):
                await self.log(
                    f"单单测试: 订单 {found.get('order_id')} 为「未购买待确认」，已跳过。",
                    "WARNING",
                )
                return []
            if found.get("has_color"):
                await self.log(
                    f"单单测试: 订单 {found.get('order_id')} 带后台颜色，仍按指定单处理。",
                    "WARNING",
                )
            if not found.get("account_key"):
                found["account_key"] = QBT_BROWSER_ACCOUNT
                await self.log(
                    f"单单测试: 未识别出价账号 {found.get('bid_email') or '(空)'}，"
                    f"暂用 [{QBT_BROWSER_ACCOUNT}] 浏览器。",
                    "WARNING",
                )
            return [found]

        stats = getattr(self, "scan_stats", None) or {}
        stats["scanned"] = len(scanned_items)
        if not stats.get("page_total"):
            stats["page_total"] = len({str(it.get("order_id") or "") for it in scanned_items})
        self.scan_stats = stats
        return await self.assemble_process_queue(scanned_items)

    async def filter_buyable_orders(self, scanned_items):
        """先丢掉彩色行和待确认，再按卖家分组审核，只留一对一。

        一对一：
        - 1 商品 / 1 卖家 / 1 用户
        - N 商品 / 1 用户 / N 卖家，且每个卖家在剩余列表里只出现一次
        同一卖家出现 ≥2 行（2商品1卖家，或 2用户1卖家）一律不买。
        """
        dropped_color = []
        dropped_pending = []
        remaining = []
        for item in scanned_items:
            oid = item.get("order_id")
            if item.get("has_color"):
                dropped_color.append(oid)
                continue
            if "待确认" in str(item.get("status") or ""):
                dropped_pending.append(oid)
                continue
            remaining.append(item)

        if dropped_color:
            await self.log(
                f"去掉后台彩色行 {len(dropped_color)} 笔: {dropped_color}",
                "INFO",
            )
        if dropped_pending:
            await self.log(
                f"跳过未购买待确认 {len(dropped_pending)} 笔: {dropped_pending}",
                "INFO",
            )

        by_seller = {}
        for item in remaining:
            by_seller.setdefault(item.get("seller_id") or "UNKNOWN", []).append(item)

        process_queue = []
        skipped_multi = []
        skipped_account = []
        for seller_id, items in by_seller.items():
            users = {str(it.get("user_name") or "").strip() for it in items}
            users.discard("")
            products = {str(it.get("product_id") or "").strip() for it in items}
            products.discard("")
            if len(items) != 1 or len(users) > 1:
                skipped_multi.extend(it.get("order_id") for it in items)
                sig = f"seller:{seller_id}"
                if sig not in self.warned_bundles:
                    self.warned_bundles.add(sig)
                    await self.log(
                        f"非一对一，已跳过卖家 {seller_id}："
                        f"{len(items)} 单 / 用户 {sorted(users) or ['(未解析)']} / "
                        f"商品 {sorted(products)}",
                        "WARNING",
                    )
                continue
            item = items[0]
            if not item.get("account_key"):
                skipped_account.append(item.get("order_id"))
                await self.log(
                    f"未识别出价账号，跳过订单 {item.get('order_id')} "
                    f"email={item.get('bid_email') or '(空)'}",
                    "WARNING",
                )
                continue
            process_queue.append(item)

        if skipped_multi:
            await self.log(
                f"跳过非一对一 {len(skipped_multi)} 笔: {skipped_multi}",
                "INFO",
            )
        if skipped_account:
            self.notify_feishu(
                "有订单出价账号无法对应 ad/ym，已跳过: " + ", ".join(str(x) for x in skipped_account)
            )
        stats = getattr(self, "scan_stats", None) or {}
        stats["buyable"] = len(process_queue)
        stats["dropped_color"] = len(dropped_color)
        stats["dropped_pending"] = len(dropped_pending)
        stats["skipped_multi"] = len(skipped_multi)
        stats["skipped_account"] = len(skipped_account)
        if not stats.get("page_total"):
            stats["page_total"] = len(scanned_items)
        if not stats.get("scanned"):
            stats["scanned"] = len(scanned_items)
        self.scan_stats = stats
        total = stats.get("page_total") or len(scanned_items)
        await self.log(
            f"代购买合计 {total} 笔，可以单条购买 {len(process_queue)} 笔"
            + (
                "（" + ", ".join(str(it.get("order_id")) for it in process_queue) + "）"
                if process_queue
                else ""
            )
        )
        return process_queue

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
            if yahoo_total and backend_total and calculated_total == yahoo_total:
                await self.log(f"金额已验证: 后台{backend_total} + 运费{shipping} == 雅虎{yahoo_total}")
            else:
                await self.log(
                    f"回填金额: 后台商品={backend_total} 运费={shipping} 雅虎合计={yahoo_total or '-'}"
                )

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
                self.notify_feishu(f"订单 {oid} 需要手动上传截图，请人工处理。")

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
