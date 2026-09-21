import re

from playwright.async_api import Page

from .exceptions import BotStopped


PAY_BTN_SELECTORS = [
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

SUCCESS_TOKENS = (
    "thank-you",
    "購入完了",
    "購入が完了",
    "手続きの完了",
    "購入が完了しました",
    "ご注文ありがとう",
    "ご購入ありがとうございます",
    "/order/thank-you",
)


class YahooPurchaseMixin:
    """阶段3：雅虎拍卖下单 V6.0（同捆、入口、结算、确认支付）。"""

    async def execute_yahoo_purchase(self, yahoo_page: Page, order_info, test_mode=True):
        try:
            return await self._execute_yahoo_purchase_logic(yahoo_page, order_info, test_mode)
        except BotStopped:
            raise
        except Exception as e:
            await self.log(f"雅虎下单执行异常: {e}", "ERROR")
            await self.save_error_snapshot(yahoo_page, "purchase_exception")
            return {"status": "ERROR", "error": str(e)}

    async def _check_combined_shipping(self, yahoo_page: Page):
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

    async def _handle_bundle_confirmation_interstitial(self, yahoo_page: Page, order_info):
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

    async def _click_pay_buttons(self, yahoo_page: Page, test_mode=True):
        clicked = False
        for sel in PAY_BTN_SELECTORS:
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
        return clicked

    async def _detect_purchase_success(self, yahoo_page: Page):
        try:
            title = await yahoo_page.title()
            url = yahoo_page.url or ""
            body_snippet = ""
            try:
                body_snippet = await yahoo_page.locator("body").inner_text()
            except Exception:
                body_snippet = ""
            if any(t in url or t in title or t in body_snippet for t in SUCCESS_TOKENS):
                await self.log(f"快速检测到成功状态 (URL/Title): {title} {url}")
                await self.trigger_sound("success")
                return True
        except Exception as e:
            await self.log(f"Success Detection Failed with Error: {e}", "DEBUG")
        return False

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

        try:
            if await self._check_combined_shipping(yahoo_page):
                self.add_risk_history_entry(order_info.get("order_id"), "人工处理: 发现同捆提示")
                return {"status": "SKIPPED_COMBINED_SHIPPING"}
        except Exception as e:
            await self.log(f"同捆验证异常: {e}", "DEBUG")

        try:
            bundle_res = await self._handle_bundle_confirmation_interstitial(yahoo_page, order_info)
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

        await self._uncheck_store_newsletter(yahoo_page)

        addr_fail = await self._ensure_checkout_address(yahoo_page, order_info, suffix)
        if addr_fail:
            return addr_fail

        ship_fail = await self._select_shipping_option(yahoo_page, order_info, need_track)
        if ship_fail:
            return ship_fail

        await self._disable_okihai(yahoo_page)

        cc_fail = await self._force_credit_card(yahoo_page, order_info)
        if cc_fail:
            return cc_fail

        if "着払" in body_text or "着払い" in body_text:
            early_cod_detected = True
            await self.log(f"EARLY COD DETECTION: 着払 -> Marking as COD.")

        await self.extract_seller_messages(yahoo_page, order_id, is_store=is_store)

        await self._click_pay_buttons(yahoo_page, test_mode)

        if "paypay-card" in (yahoo_page.url or ""):
            await self.log("Redirected to PayPay Card Signup! Backtracking...")
            await yahoo_page.go_back()
            return {"status": "REDIRECT_PAYPAY_CARD"}

        is_success = await self._detect_purchase_success(yahoo_page)

        if not is_success and not test_mode:
            await self.log(
                "购买未确认：未检测到「購入が完了しました」成功页面。可能未完成支付，需人工确认。",
                "WARNING",
            )
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
