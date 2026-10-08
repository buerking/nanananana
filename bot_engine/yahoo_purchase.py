"""阶段3 下单主流程 Pipeline：入口 → 结算步骤 → 确认支付。

同捆校验、待运费地址是主流程上的分支，不是另一套产品。
"""
import asyncio
import re

from playwright.async_api import Page

from .constants import YAHOO_TRADE_TOP_URL
from .exceptions import BotStopped
from .flow import OrderContext, run_chain


# 旧版「購入手続きする」与新版「購入手続きをする」并存。
PURCHASE_PROCEDURE_SELECTORS = (
    "a[href*='buyer/payment/input']",
    "a:has-text('購入手続きをする')",
    "button:has-text('購入手続きをする')",
    "a:has-text('購入手続きする')",
    "button:has-text('購入手続きする')",
)
PURCHASE_PROCEDURE_LOCATOR = ", ".join(PURCHASE_PROCEDURE_SELECTORS)

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
    async def execute_yahoo_purchase(self, yahoo_page: Page, order_info, test_mode=True):
        try:
            ctx = OrderContext(
                order=dict(order_info),
                test_mode=test_mode,
                yahoo_page=yahoo_page,
                bound_items=list(order_info.get("bound_items") or []),
                backend_total=int(order_info.get("backend_total") or 0),
            )
            ctx.order_info = dict(order_info)
            return await self._run_purchase_pipeline(ctx)
        except BotStopped:
            raise
        except Exception as e:
            await self.log(f"雅虎下单执行异常: {e}", "ERROR")
            await self.save_error_snapshot(yahoo_page, "purchase_exception")
            return {"status": "ERROR", "error": str(e)}

    async def _run_purchase_pipeline(self, ctx):
        await self.log(f"阶段3: 执行雅虎下单逻辑 (V6.0)... 订单信息: {ctx.order_info}")
        aborted = await run_chain(
            [
                self.gate_combined_shipping,
                self.step_bundle_interstitial,
                self.step_enter_purchase,
                self.step_new_trade_choice,
                self.step_wait_checkout_form,
                self.step_capture_body_and_payment_gate,
                self.step_uncheck_newsletter,
                self.step_address,
                self.step_shipping,
                self.step_okihai,
                self.step_credit_card,
                self.step_mark_cod,
                self.step_extract_messages,
                self.step_click_pay,
                self.gate_paypay_card_redirect,
                self.step_finish,
            ],
            ctx,
        )
        return aborted or ctx.result

    async def step_bundle_interstitial(self, ctx):
        yahoo_page = ctx.yahoo_page
        order_info = ctx.order_info
        try:
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
            check_buy_btn = yahoo_page.locator(PURCHASE_PROCEDURE_LOCATOR)
            try:
                await check_buy_btn.first.wait_for(state="visible", timeout=5000)
            except Exception:
                await self.log("同捆验证后，未发现 '购买手续' 按钮。可能卖家尚未同意。", "WARNING")
                self.add_risk_history_entry(order_info.get("order_id"), "人工处理: 同捆未就绪")
                return {"status": "SKIPPED_BUNDLE_NOT_READY"}
            return None
        except Exception as e:
            await self.log(f"同捆验证流程异常: {e}", "ERROR")
            return {"status": "BUNDLE_EXCEPTION"}

    async def _follow_click(self, ctx, locator, wait_locator=None, timeout=15000):
        """点击可能打开新标签的链接，并切到新页。避免还停在拍品页找按钮。"""
        page = ctx.yahoo_page
        context = page.context
        pages_before = list(context.pages)
        clicked = False
        try:
            async with context.expect_page(timeout=8000) as new_page_info:
                await locator.click()
                clicked = True
            ctx.yahoo_page = await new_page_info.value
        except Exception:
            if not clicked:
                await locator.click()
            new_pages = [p for p in context.pages if p not in pages_before]
            if new_pages:
                ctx.yahoo_page = new_pages[-1]
        page = ctx.yahoo_page
        try:
            await page.wait_for_load_state("domcontentloaded")
        except Exception:
            pass
        await page.bring_to_front()
        await self.log(f"点击后当前页: {page.url}")
        if wait_locator:
            try:
                await page.locator(wait_locator).first.wait_for(state="visible", timeout=timeout)
            except Exception:
                await self.log(f"点击后未等到目标元素。URL={page.url}", "WARNING")
        return page

    async def _adopt_page_matching(self, ctx, url_parts, selector, timeout=25000):
        """新版取引ナビ是 Next.js 异步渲染，按钮可能后出现；也可能在另一个标签。"""
        deadline = asyncio.get_running_loop().time() + timeout / 1000
        last_urls = []
        while asyncio.get_running_loop().time() < deadline:
            pages = list(ctx.yahoo_page.context.pages) if ctx.yahoo_page else []
            last_urls = [p.url for p in pages if not p.is_closed()]
            for page in pages:
                if page.is_closed():
                    continue
                url = page.url or ""
                if any(part in url for part in url_parts):
                    ctx.yahoo_page = page
                    loc = page.locator(selector)
                    try:
                        await loc.first.wait_for(state="visible", timeout=2000)
                        await page.bring_to_front()
                        return loc.first
                    except Exception:
                        pass
                loc = page.locator(selector)
                try:
                    if await loc.count():
                        ctx.yahoo_page = page
                        await page.bring_to_front()
                        return loc.first
                except Exception:
                    continue
            await asyncio.sleep(0.5)
        await self.log(f"未找到目标页/按钮。已打开标签: {last_urls}", "WARNING")
        return None

    async def _goto_purchase_procedure(self, ctx, link):
        href = ""
        try:
            href = (await link.get_attribute("href")) or ""
        except Exception:
            href = ""
        if href:
            if href.startswith("/"):
                href = "https://contact.auctions.yahoo.co.jp" + href
            await self.log(f"进入购买手续页: {href}")
            await ctx.yahoo_page.goto(href, wait_until="domcontentloaded", timeout=60000)
            try:
                await ctx.yahoo_page.wait_for_url("**/buyer/payment/**", timeout=15000)
            except Exception:
                pass
            await self.log(f"购买手续页当前 URL: {ctx.yahoo_page.url}")
            return
        await self._follow_click(
            ctx,
            link,
            wait_locator="h2:has-text('お届け先'), input[name='address2'], input[name='home_address2']",
        )

    def _checkout_like_url(self, url):
        u = url or ""
        return any(
            p in u
            for p in (
                "buyer/edit",
                "buyer/payment",
                "buyer/top",
                "buyer/preview",
                "/payment/input",
            )
        )

    async def _adopt_checkout_tab(self, ctx, timeout=12000):
        deadline = asyncio.get_running_loop().time() + timeout / 1000
        while asyncio.get_running_loop().time() < deadline:
            for page in list(ctx.yahoo_page.context.pages):
                if page.is_closed():
                    continue
                if self._checkout_like_url(page.url):
                    ctx.yahoo_page = page
                    await page.bring_to_front()
                    await self.log(f"已切换到结算/地址页: {page.url}")
                    return page
            await asyncio.sleep(0.4)
        return None

    async def _dismiss_bundle_choice_page(self, ctx):
        """新版取引ナビ会先问まとめて还是单品。按既有策略走单品，不申请同捆。"""
        yahoo_page = ctx.yahoo_page
        if self._checkout_like_url(yahoo_page.url):
            return None
        adopted = await self._adopt_checkout_tab(ctx, timeout=1500)
        if adopted:
            return None
        page_btn = yahoo_page.locator("main button:has-text('単品で取引する'), main a:has-text('単品で取引する')")
        if await page_btn.count() == 0:
            page_btn = yahoo_page.locator("button:has-text('単品で取引する'), a:has-text('単品で取引する')")
        bundle_start = yahoo_page.locator(
            "a:has-text('まとめて取引をはじめる'), button:has-text('まとめて取引をはじめる')"
        )
        bundle_hint = yahoo_page.locator("text=この商品はまとめて取引が可能です")
        modal_title = yahoo_page.locator("h2:has-text('本当に単品で取引しますか')")
        try:
            has_single = await page_btn.count() > 0
            has_bundle_ui = (await bundle_hint.count() > 0) or (await bundle_start.count() > 0)
            modal_visible = await modal_title.is_visible()
        except Exception:
            return None
        if not has_single and not has_bundle_ui and not modal_visible:
            return None
        if not has_single and not modal_visible:
            await self.log("检测到新版まとめて取引页，但没有「単品で取引する」。转人工。", "ERROR")
            return {"status": "SKIPPED_COMBINED_SHIPPING"}
        confirm = yahoo_page.locator(
            "#modalArea dialog[open] button.gv-Button--primary, "
            "dialog[open] button.gv-Button--primary:has-text('単品で取引する')"
        )
        if not modal_visible:
            await self.log("检测到新版同捆选择页。点击「単品で取引する」（不申请まとめて取引）。")
            await page_btn.first.click(force=True)
            try:
                await modal_title.wait_for(state="visible", timeout=8000)
            except Exception:
                pass
        if await modal_title.is_visible() or await confirm.count():
            await self.log("确认弹窗：本当に単品で取引しますか？ 点击确定。")
            pages_before = list(yahoo_page.context.pages)
            await confirm.last.click(force=True)
            await asyncio.sleep(1)
            new_pages = [p for p in yahoo_page.context.pages if p not in pages_before and not p.is_closed()]
            if new_pages:
                ctx.yahoo_page = new_pages[-1]
                await ctx.yahoo_page.bring_to_front()
            await self._adopt_checkout_tab(ctx, timeout=8000)
            await self.log(f"单品确认后当前页: {ctx.yahoo_page.url}")
        return None

    async def _continue_personal_checkout(self, ctx):
        yahoo_page = ctx.yahoo_page
        if self._checkout_like_url(yahoo_page.url):
            await self.log(f"已在结算/地址页: {yahoo_page.url}")
            return None
        adopted = await self._adopt_checkout_tab(ctx, timeout=8000)
        if adopted:
            return None
        await self.log("等待新版取引ナビ异步渲染「購入手続きをする」...")
        link = await self._adopt_page_matching(
            ctx,
            url_parts=("contact.auctions.yahoo.co.jp", "/trade/top", "buyer/payment"),
            selector="a[href*='buyer/payment/input']",
            timeout=25000,
        )
        if link is None:
            link = await self._adopt_page_matching(
                ctx,
                url_parts=("contact.auctions.yahoo.co.jp", "/trade/top"),
                selector=PURCHASE_PROCEDURE_LOCATOR,
                timeout=8000,
            )
        if link is None:
            await self.log(
                f"取引ナビ页没有「購入手続きをする」。URL={ctx.yahoo_page.url}",
                "WARNING",
            )
        else:
            await self.log("发现个人卖家「購入手続きをする」。正在进入支付页...")
            await self._goto_purchase_procedure(ctx, link)
        yahoo_page = ctx.yahoo_page
        kantan_btn = yahoo_page.locator(
            "a:has-text('Yahoo!かんたん決済で支払う'), button:has-text('Yahoo!かんたん決済で支払う')"
        )
        if await kantan_btn.count():
            await self.log("发现 'Yahoo!かんたん決済' 按钮。正在点击...")
            await self._follow_click(ctx, kantan_btn.first)
            yahoo_page = ctx.yahoo_page
        body_text = await yahoo_page.locator("body").inner_text()
        if "送料連絡待ち" in body_text or "送料の連絡があります" in body_text:
            ctx.is_wait_for_shipping = True
            await self.log("状态：等待运费联系 (2.2)。转入个人流程处理（地址+潜在物流选择）。")
            return await self.handle_personal_address_only(yahoo_page, ctx.order_info)
        return None

    async def step_enter_purchase(self, ctx):
        """一口价 / 今すぐ落札 / 取引ナビ。待运费在此早退。"""
        yahoo_page = ctx.yahoo_page
        order_info = ctx.order_info
        test_mode = ctx.test_mode
        buy_now_btn = yahoo_page.locator("button:has-text('購入手続きへ'), a:has-text('購入手続きへ')")
        buy_now_auction_btn = yahoo_page.locator("button:has-text('今すぐ落札'), a:has-text('今すぐ落札')")
        if await buy_now_btn.count() > 0:
            await self.log("检测到一口价/即决订单 (購入手続きへ)。正在点击...")
            ctx.is_store = True
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
            product_id = (order_info.get("product_id") or "").strip()
            nav_candidates = yahoo_page.locator("a:has-text('取引ナビ'), button:has-text('取引ナビ')")
            nav_count = await nav_candidates.count()
            if product_id or nav_count > 0:
                ctx.is_personal = True
                if product_id:
                    cur = yahoo_page.url or ""
                    if product_id in cur and (
                        "trade/top" in cur or self._checkout_like_url(cur)
                    ):
                        await self.log(f"已在取引相关页，跳过重复打开: {cur}")
                    else:
                        trade_url = YAHOO_TRADE_TOP_URL.format(product_id=product_id)
                        await self.log(f"直接打开取引ナビ: {trade_url}")
                        try:
                            await yahoo_page.goto(trade_url, wait_until="commit", timeout=20000)
                            try:
                                await yahoo_page.wait_for_load_state(
                                    "domcontentloaded", timeout=12000
                                )
                            except Exception:
                                pass
                        except Exception as e:
                            await self.log(f"取引ナビ打开超时，继续用当前页: {yahoo_page.url} ({e})", "WARNING")
                        await self.log(f"点击后当前页: {yahoo_page.url}")
                else:
                    await self.log("检测到个人卖家。正在点击 '取引ナビ'...")
                    nav_link = nav_candidates.first
                    for i in range(nav_count):
                        href = (await nav_candidates.nth(i).get_attribute("href")) or ""
                        if any(k in href.lower() for k in ("trade", "closeduser", "navi")):
                            nav_link = nav_candidates.nth(i)
                            break
                    await self._follow_click(
                        ctx,
                        nav_link,
                        wait_locator=(
                            "h1:has-text('取引ナビ'), "
                            "a[href*='buyer/payment/input'], "
                            "a:has-text('購入手続きをする'), "
                            "button:has-text('単品で取引する')"
                        ),
                    )
                yahoo_page = ctx.yahoo_page
                rejected = await self.gate_seller_bundle_rejected(ctx)
                if rejected:
                    return rejected
                choice = await self._dismiss_bundle_choice_page(ctx)
                if choice:
                    self.add_risk_history_entry(order_info.get("order_id"), "人工处理: 发现同捆提示")
                    return choice
                continued = await self._continue_personal_checkout(ctx)
                if continued:
                    return continued
            elif await yahoo_page.locator(PURCHASE_PROCEDURE_LOCATOR).count():
                ctx.is_store = True
                await self.log("发现中间页店铺 '购买手续' 按钮。正在点击...")
                await yahoo_page.locator(PURCHASE_PROCEDURE_LOCATOR).first.click()
            else:
                await self.log(
                    f"未找到購入手続きへ / 今すぐ落札 / 取引ナビ。当前 URL: {yahoo_page.url}",
                    "WARNING",
                )
        return None

    async def step_new_trade_choice(self, ctx):
        """进入取引ナビ后若仍停在新版同捆选择页，再点一次单品。"""
        if self._checkout_like_url(ctx.yahoo_page.url):
            return None
        choice = await self._dismiss_bundle_choice_page(ctx)
        if choice:
            self.add_risk_history_entry(ctx.order_info.get("order_id"), "人工处理: 发现同捆提示")
            return choice
        if ctx.is_personal:
            continued = await self._continue_personal_checkout(ctx)
            if continued:
                return continued
        return None

    async def step_wait_checkout_form(self, ctx):
        yahoo_page = ctx.yahoo_page
        if self._checkout_like_url(yahoo_page.url):
            await self.log(f"已进入结算/地址页。等待表单... {yahoo_page.url}")
            return None
        form = yahoo_page.locator(
            "h2:has-text('お届け先'), h2:has-text('お届け先住所'), h2:has-text('落札者情報'), "
            "form[name='tradeForm'], input[name='address2'], input[name='home_address2'], #mBoxConfBt"
        )
        try:
            await form.first.wait_for(state="visible", timeout=10000)
            await self.log("已进入购买输入页。等待表单...")
        except Exception:
            await self.log("警告：未找到 'お届け先' 表单，页面可能不同。继续...", "WARNING")
        return None

    async def step_capture_body_and_payment_gate(self, ctx):
        try:
            ctx.body_text = await ctx.yahoo_page.locator("body").inner_text()
        except Exception:
            ctx.body_text = ""
        if "出品者から送料の連絡があります" in ctx.body_text or "送料連絡待ち" in ctx.body_text:
            ctx.is_wait_for_shipping = True
            await self.log("本页需先提交交易信息，之后等待卖家联系运费。")
        return await self.gate_payment_unsupported(ctx)

    async def step_mark_cod(self, ctx):
        if "着払" in ctx.body_text or "着払い" in ctx.body_text:
            ctx.early_cod = True
            await self.log("EARLY COD DETECTION: 着払 -> Marking as COD.")
        return None

    async def step_extract_messages(self, ctx):
        await self.extract_seller_messages(ctx.yahoo_page, ctx.order_id, is_store=ctx.is_store)
        return None

    async def step_click_pay(self, ctx):
        yahoo_page = ctx.yahoo_page
        test_mode = ctx.test_mode
        classic_confirm = yahoo_page.locator("#mBoxConfBt")
        if await classic_confirm.count() and await classic_confirm.first.is_visible():
            await self.log("发现旧版取引ナビ「決定する」(#mBoxConfBt)")
            if test_mode:
                await self.log("[TestMode] 模拟点击决定（不提交，也不点弹窗「確定する」）...")
                return None
            await classic_confirm.first.click()
            try:
                await yahoo_page.locator("#confSubmitBtn, #mBoxConf").wait_for(
                    state="visible", timeout=8000
                )
                confirm = yahoo_page.locator("#confSubmitBtn")
                if await confirm.count():
                    await self.log("确认弹窗：取引情報を確定しますか？ 点击確定する。")
                    await confirm.first.click()
            except Exception as e:
                await self.log(f"旧版确定弹窗未出现: {e}", "WARNING")
            return None
        for sel in PAY_BTN_SELECTORS:
            btn = yahoo_page.locator(sel).first
            try:
                if await btn.count() and await btn.is_visible():
                    await self.log(f"发现支付按钮 ({sel})")
                    await self._fill_cvv(yahoo_page, self.DEFAULT_CVV)
                    if test_mode:
                        await self.log("[TestMode] 模拟点击 (不提交)...")
                        break
                    await self.log("正在点击 (REAL BUY)...")
                    await btn.click()
                    await self._fill_cvv(yahoo_page, self.DEFAULT_CVV)
                    break
            except Exception:
                continue
        return None

    async def step_finish(self, ctx):
        is_success = False
        yahoo_page = ctx.yahoo_page
        try:
            title = await yahoo_page.title()
            url = yahoo_page.url or ""
            body_snippet = ""
            try:
                body_snippet = await yahoo_page.locator("body").inner_text()
            except Exception:
                body_snippet = ""
            if any(t in url or t in title or t in body_snippet for t in SUCCESS_TOKENS):
                is_success = True
                await self.log(f"快速检测到成功状态 (URL/Title): {title} {url}")
                await self.trigger_sound("success")
        except Exception as e:
            await self.log(f"Success Detection Failed with Error: {e}", "DEBUG")

        if not is_success and not ctx.test_mode:
            await self.log(
                "购买未确认：未检测到「購入が完了しました」成功页面。可能未完成支付，需人工确认。",
                "WARNING",
            )
            self.add_risk_history_entry(ctx.order_info.get("order_id"), "人工处理: 未检测到购买成功页面")
            await self.save_error_snapshot(yahoo_page, "purchase_unconfirmed")
            return {
                "status": "PURCHASE_UNCONFIRMED",
                "shipping": ctx.shipping_cost,
                "total": ctx.yahoo_total,
            }

        status = "TEST_SUCCESS" if ctx.test_mode else "PURCHASED"
        if ctx.is_wait_for_shipping:
            status = "WAIT_SHIPPING_CONTACT"
        return {
            "status": status,
            "shipping": ctx.shipping_cost,
            "total": ctx.yahoo_total,
            "is_cod": ctx.early_cod,
            "shipping_warning": ctx.early_cod,
        }
