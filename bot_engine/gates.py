"""Gate：只判断 allow/deny，原则上不改下单主路径。

新拦截规则加在这里，不要塞进结算 Pipeline。
"""
import re

from .flow import deny
from .qbt import parse_storage_code
from .ticket import create_work_order
from .waits import is_yahoo_login_or_reauth_url


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
        page = ctx.yahoo_page
        url = page.url if page else ""
        if not is_yahoo_login_or_reauth_url(url):
            return None
        if hasattr(self, "_try_skip_ghost_yahoo_login"):
            skipped = await self._try_skip_ghost_yahoo_login(page)
            if skipped or not is_yahoo_login_or_reauth_url(page.url or ""):
                return None
        await self.log("严重错误：雅虎未登录！停止运行。", "ERROR")
        await self.trigger_sound("error")
        self.is_running = False
        self.notify_feishu(
            f"雅虎未登录，已停止。订单 {ctx.order.get('order_id', '')} "
            f"账号 {ctx.order.get('account_key') or ctx.order.get('bid_email') or ''}"
        )
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
            await modal.wait_for(state="visible", timeout=1200)
            if await modal.count() and await modal.is_visible():
                text = await modal.inner_text()
                await self.log(f"发现同捆发货提示: {text[:80]}", "WARNING")
                return True
        except Exception:
            pass
        return False

    async def gate_item_option(self, ctx):
        """店铺 /order/select-option：规格/颜色必须用户确认，不能自动选。"""
        page = ctx.yahoo_page
        url = page.url or ""
        if "select-option" not in url:
            try:
                await page.wait_for_url(
                    re.compile(r".*/order/select-option.*"),
                    timeout=2500,
                )
                url = page.url or ""
            except Exception:
                pass
        hinted = "select-option" in (url or "")
        body = ""
        if not hinted:
            try:
                body = await page.locator("body").inner_text()
            except Exception:
                body = ""
            hinted = "商品オプションを選択してください" in body
        if not hinted:
            return None
        oid = ctx.order_info.get("order_id") or ctx.order.get("order_id") or ""
        pid = ctx.order_info.get("product_id") or ctx.order.get("product_id") or ""
        choices = []
        try:
            labels = page.locator("main fieldset label, main legend")
            n = min(await labels.count(), 12)
            for i in range(n):
                t = " ".join(((await labels.nth(i).inner_text()) or "").split())
                if t:
                    choices.append(t)
        except Exception:
            choices = []
        choice_txt = "、".join(choices[:8]) if choices else "（页上有规格选项）"
        await self.log(
            f"店铺商品需要选择规格/款式，已停止自动购买。请建工单联系用户确认。选项: {choice_txt}",
            "ERROR",
        )
        self.add_risk_history_entry(oid, "人工处理: 需联系用户选择商品款式")
        storage_code = parse_storage_code(
            ctx.order_info.get("storage_code") or ctx.order.get("storage_code") or ""
        ) or parse_storage_code(
            ctx.order_info.get("user_name") or ctx.order.get("user_name") or ""
        )
        issue = (
            f"雅虎店铺结算出现规格选择（オプション選択），无法自动下单。"
            f"商品 {pid}。请确认要购买的款式。选项: {choice_txt}"
        )
        if storage_code:
            ok, err = await create_work_order(oid, storage_code, [issue], send_message=1)
            if ok:
                await self.log(f"已创建工单（入库码 {storage_code}，已站内通知用户）。")
            else:
                await self.log(f"创建工单失败: {err}", "WARNING")
        else:
            await self.log("详情页未解析到入库码，跳过创建工单。", "WARNING")
        self.notify_feishu(
            f"订单 {oid} / 商品 {pid} / 入库码 {storage_code or '(未解析)'} 需要人工客服：\n"
            "点「購入手続きへ」后出现规格选择页，请联系用户选择款式后再买。不要自动选。\n"
            f"选项: {choice_txt}"
        )
        return deny("SKIPPED_NEED_USER_OPTION")

    async def gate_combined_shipping(self, ctx):
        """雅虎同捆弹窗：发现则转人工。超时视为没有弹窗。"""
        try:
            if await self._combined_shipping_visible(ctx):
                oid = ctx.order_info.get("order_id")
                self.add_risk_history_entry(oid, "人工处理: 发现同捆提示")
                self.notify_feishu(f"订单 {oid} 出现雅虎同捆提示，已跳过转人工。")
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

    async def _3ds_visible_on_page(self, page):
        """真正要人输入验证码的 3DS（邮件 OTP / 发卡行 ACS）。

        确认页常驻 #challengeAuthParentFrame /「クレジットカード認証」父框，
        摩擦式 3DS 会一闪而过然后購入完了，不能据此发飞书。
        """
        if page is None:
            return False
        try:
            for fr in page.frames:
                url = (fr.url or "").lower()
                if "dnp-cdms" in url or "/gab0/" in url:
                    return True
                try:
                    n = await fr.locator(
                        ".v-challengezone, "
                        "input.v-inputcode, "
                        "h1:has-text('eメールによる認証'), "
                        "text=eメールによる認証, "
                        "text=認証コード"
                    ).count()
                    if n:
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    async def _notify_need_3ds(self, ctx, page=None):
        oid = ""
        pid = ""
        acc = ""
        if ctx is not None:
            oid = ctx.order_info.get("order_id") or ctx.order.get("order_id") or ""
            pid = ctx.order_info.get("product_id") or ctx.order.get("product_id") or ""
            acc = ctx.order.get("account_key") or ctx.order_info.get("account_key") or ""
        key = str(oid) or ((page.url if page is not None else "") or "3ds")
        notified = getattr(self, "_3ds_notified_orders", None)
        if notified is None:
            notified = set()
            self._3ds_notified_orders = notified
        if key in notified:
            return
        notified.add(key)
        await self.log("出现信用卡 3DS 认证弹窗，请人工输入验证码。", "WARNING")
        self.add_risk_history_entry(oid, "人工处理: 信用卡3DS认证")
        try:
            await self.trigger_sound("attention")
        except Exception:
            pass
        self.notify_feishu(
            f"订单 {oid} / 商品 {pid} / 账号 {acc} 出现信用卡 3DS 认证（eメールによる認証）。"
            "请在当前窗口弹窗中输入验证码后点「確認」。机器人会继续等待购买完成。"
        )

    async def gate_need_3ds_timeout(self, ctx):
        """点完确定后 3DS 仍未完成：飞书已在出现时发出，本单跳过转人工。"""
        page = ctx.yahoo_page
        if page is None:
            return None
        try:
            if not await self._3ds_visible_on_page(page):
                return None
        except Exception:
            return None
        await self._notify_need_3ds(ctx, page)
        await self.log("3DS 认证仍未完成，转人工。本单跳过。", "WARNING")
        return deny("SKIPPED_NEED_3DS")

    async def _cvv_required_on_page(self, page):
        dummy = page.locator("input[name='dummy_code']")
        if await self.has_visible(dummy):
            return True
        err = page.locator(
            "[aria-label='error'], .gv-Message--error"
        ).filter(has_text="入力・選択された内容に誤り")
        code_hint = page.locator(
            "text=セキュリティコードとは, text=セキュリティコード, p:has-text('入力してください')"
        )
        try:
            if await self.has_visible(err) and await code_hint.count():
                return True
        except Exception:
            pass
        return False

    async def gate_need_cvv(self, ctx):
        """需要信用卡安全码：飞书转人工，跳过本单。不要自动填、不要继续点确认。"""
        page = ctx.yahoo_page
        if page is None:
            return None
        try:
            if not await self._cvv_required_on_page(page):
                return None
        except Exception:
            return None
        oid = ctx.order_info.get("order_id") or ctx.order.get("order_id") or ""
        pid = ctx.order_info.get("product_id") or ctx.order.get("product_id") or ""
        acc = ctx.order.get("account_key") or ctx.order_info.get("account_key") or ""
        await self.log("结算页需要填写信用卡安全码，转人工。本单跳过。", "WARNING")
        self.add_risk_history_entry(oid, "人工处理: 需要信用卡安全码")
        try:
            await self.trigger_sound("attention")
        except Exception:
            pass
        self.notify_feishu(
            f"订单 {oid} / 商品 {pid} / 账号 {acc} 结算页需要填写信用卡安全码。"
            "已跳过本单，请在该窗口人工填写后完成购买。"
        )
        return deny("SKIPPED_NEED_CVV")

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
        """简化版：去掉彩色行、跳过待确认，只留一对一可购买订单。"""
        return await self.filter_buyable_orders(scanned_items)
