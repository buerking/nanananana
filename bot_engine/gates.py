"""Gate：只判断 allow/deny，原则上不改下单主路径。

新拦截规则加在这里，不要塞进结算 Pipeline。
"""
import re

from .flow import deny


class GatesMixin:
    async def gate_blacklist(self, ctx):
        seller_id = ctx.order.get("seller_id")
        if not self.is_blacklisted(seller_id):
            return None
        await self.log(
            f"⛔ 卖家 {seller_id} 在黑名单中，跳过订单 {ctx.order.get('order_id', '')}",
            "WARNING",
        )
        return deny("SKIPPED_BLACKLIST")

    async def gate_backend_remark(self, ctx):
        """详情页备注：非白名单则跳过。异常视为非关键，放行。"""
        page = ctx.detail_page
        try:
            remark_tables = await page.locator("table.public_table").all()
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
            if not remark_content:
                return None
            is_whitelisted = any(
                (p in remark_content) or re.search(p, remark_content)
                for p in self.REMARK_WHITELIST_PATTERNS
            )
            if is_whitelisted:
                await self.log(f"备注符合白名单规则，已忽略并继续: {remark_content}")
                return None
            await self.log(f"检测到未授权或异常后台订单备注: {remark_content}", "WARNING")
            self.add_risk_history_entry(ctx.order.get("order_id"), f"后台备注拦截: {remark_content}")
            await self.log("订单存在异常备注，跳过处理。", "WARNING")
            return deny("SKIPPED_BACKEND_REMARK")
        except Exception as rem_e:
            await self.log(f"备注检查异常 (Non-critical): {rem_e}", "DEBUG")
            return None

    async def gate_yahoo_login(self, ctx):
        if "login.yahoo.co.jp" not in (ctx.yahoo_page.url or ""):
            return None
        await self.log("严重错误：雅虎未登录！停止运行。", "ERROR")
        await self.trigger_sound("error")
        self.is_running = False
        return deny("YAHOO_NOT_LOGGED_IN")

    async def gate_risk_keywords(self, ctx):
        page = ctx.yahoo_page
        try:
            risk_loc = page.locator("dl.Price__delivery, div.Price")
            try:
                await risk_loc.first.wait_for(state="visible", timeout=8000)
            except Exception:
                pass
            risk_text = await page.locator("body").inner_text()
            matched_keyword = ""
            for keyword in self.RISK_KEYWORDS:
                if keyword and keyword in risk_text:
                    matched_keyword = keyword
                    break
            if not matched_keyword:
                return None
            await self.log(f"风险检测：发现敏感词 '{matched_keyword}'。跳过订单。", "WARNING")
            self.add_risk_history_entry(ctx.order.get("order_id", "Unknown"), matched_keyword)
            return deny("SKIPPED_RISK")
        except Exception as e:
            await self.log(f"Risk Check Extraction Error: {e}", "DEBUG")
            return None

    async def _combined_shipping_visible(self, ctx):
        await self.log("正在检测同捆发货弹窗...")
        try:
            modal = ctx.yahoo_page.locator("text=まとめて購入手続きができます").first
            await modal.wait_for(state="visible", timeout=3000)
            if await modal.count() and await modal.is_visible():
                text = await modal.inner_text()
                await self.log(f"发现同捆发货提示: {text[:80]}", "WARNING")
                return True
        except Exception:
            pass
        return False

    async def gate_combined_shipping(self, ctx):
        """雅虎同捆弹窗：发现则转人工。超时视为没有弹窗。"""
        try:
            if await self._combined_shipping_visible(ctx):
                self.add_risk_history_entry(ctx.order_info.get("order_id"), "人工处理: 发现同捆提示")
                return deny("SKIPPED_COMBINED_SHIPPING")
        except Exception as e:
            await self.log(f"同捆验证异常: {e}", "DEBUG")
        return None

    async def gate_seller_bundle_rejected(self, ctx):
        rejection_text = "出品者が単品での取引を希望したため、商品ごとに取引を行ってください"
        try:
            if rejection_text not in (await ctx.yahoo_page.locator("body").inner_text()):
                return None
            await self.log(
                f"检测到卖家拒绝同捆提示：'{rejection_text[:20]}...'。跳过订单，转为人工处理。",
                "ERROR",
            )
            close_btn = ctx.yahoo_page.locator(
                "button:has-text('閉じる'), a:has-text('閉じる'), input[value='閉じる']"
            )
            if await close_btn.count():
                await close_btn.first.click()
            self.add_risk_history_entry(ctx.order_info.get("order_id"), "卖家拒绝同捆: 要求单品交易")
            return deny("SKIPPED_BUNDLE_REJECTED")
        except Exception as e:
            await self.log(f"同捆拒绝检测异常: {e}", "DEBUG")
            return None

    async def gate_payment_unsupported(self, ctx):
        body_text = ctx.body_text or ""
        blocked = "クレジットカードはご利用できません" in body_text or (
            "この商品カテゴリではPayPay残高、PayPayクレジット、クレジットカードはご利用できません"
            in body_text
        )
        if not blocked:
            return None
        await self.log("严重错误：此类目不支持信用卡支付。停止运行。", "ERROR")
        self.add_risk_history_entry(ctx.order_info.get("order_id"), "不支持信用卡")
        await self.trigger_sound("error")
        return deny("PAYMENT_UNSUPPORTED")

    async def gate_paypay_card_redirect(self, ctx):
        if "paypay-card" not in (ctx.yahoo_page.url or ""):
            return None
        await self.log("Redirected to PayPay Card Signup! Backtracking...")
        await ctx.yahoo_page.go_back()
        return deny("REDIRECT_PAYPAY_CARD")

    async def assemble_process_queue(self, scanned_items):
        """扫单后的同捆门卫：未确认同捆整组不入队；已确认同捆只留第一单。"""
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
