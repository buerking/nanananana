import asyncio
import re

from playwright.async_api import Page


class OrderMixin:
    """阶段2：后台详情 → 打开雅虎商品页 → 交给阶段3下单。"""

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
