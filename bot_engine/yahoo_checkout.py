import asyncio
import re

from playwright.async_api import Page


class YahooCheckoutMixin:
    """结算页：地址后缀、配送、投放、信用卡、CVV、待运费地址。"""

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

    async def _uncheck_store_newsletter(self, yahoo_page: Page):
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

    async def _ensure_checkout_address(self, yahoo_page: Page, order_info, suffix):
        """地址后缀 = 订单号后5位 + 室。失败时返回 status dict。"""
        try:
            addr_ok = suffix in (await yahoo_page.content())
            if addr_ok:
                await self.log(f"地址检查: 页面已包含正确地址后缀 '{suffix}'，跳过修改。")
            else:
                change_btn = yahoo_page.locator("h2:has-text('お届け先')").locator("a:has-text('変更')")
                edit_btn = yahoo_page.locator(
                    "a:has-text('編集する'), input[value='編集する'], button:has-text('編集する')"
                )
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
                    await self.log(
                        "严重拦截：无法确认个人买家页面的地址准确性。为防止发错货已强制停止。",
                        "ERROR",
                    )
                    self.add_risk_history_entry(
                        order_info.get("order_id"), "人工处理: 无法修改个人卖家收件地址"
                    )
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
        return None

    async def _select_shipping_option(self, yahoo_page: Page, order_info, need_track):
        """优先可追踪配送，排除到店自提。失败时返回 status dict。"""
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
                    candidates.append(
                        {"element": radio, "price": price, "text": p_str[:80], "trackable": is_trackable}
                    )
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
        return None

    async def _disable_okihai(self, yahoo_page: Page):
        try:
            okihai_select = yahoo_page.locator(
                "select[name='okihaiTypeYamato'], select[name='okihaiTypeSelect'], #ymtokhi select"
            )
            if await okihai_select.count():
                await self.log("检测到 '置き配' (投放配置) 选项。正在关闭 (选择 '利用しない')...")
                try:
                    await okihai_select.first.select_option(
                        label=re.compile("しない|希望しない|置き配を設定しない")
                    )
                except Exception:
                    await okihai_select.first.select_option(index=1)
        except Exception as e:
            await self.log(f"Okihai setting error (Non-critical): {e}", "DEBUG")

    async def _force_credit_card(self, yahoo_page: Page, order_info):
        try:
            cc_radio = yahoo_page.locator(
                "input[value='card'], input[id*='card'], label:has-text('クレジットカード')"
            )
            if await cc_radio.count():
                if (
                    not await cc_radio.first.is_checked()
                    if await cc_radio.first.evaluate("el => el.type === 'radio' || el.type === 'checkbox'")
                    else True
                ):
                    await cc_radio.first.click()
                    await self.log("已成功切换为信用卡。")
            pay_header = yahoo_page.locator("h2:has-text('お支払い方法'), h3:has-text('お支払い方法')")
            if await pay_header.count():
                current_payment_text = await pay_header.first.locator("xpath=ancestor::section").inner_text()
                if "クレジットカード" not in current_payment_text:
                    await self.log(
                        f"支付方式非信用卡 (检测到: {current_payment_text[:40]}...)。尝试切换..."
                    )
                    change = yahoo_page.locator("a:has-text('変更する'), button:has-text('変更する')")
                    if await change.count():
                        await change.first.click()
                        label = yahoo_page.locator("label:has-text('クレジットカード')")
                        if await label.count():
                            await label.first.click()
                            await self.log("支付方式已更新为信用卡。")
                        else:
                            await self.log(
                                "严重风险: 该店铺不支持信用卡支付 (或未绑定有效卡)。停止下单。",
                                "ERROR",
                            )
                            self.add_risk_history_entry(
                                order_info.get("order_id"), "人工处理: 店铺不支持信用卡"
                            )
                            return {"status": "RISK_STOP_NO_CC"}
        except Exception as e:
            await self.log(f"Payment Method Check Error: {e}", "DEBUG")
        return None

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
