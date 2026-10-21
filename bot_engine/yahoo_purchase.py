"""阶段3 下单主流程 Pipeline：入口 → 结算步骤 → 确认支付。

同捆校验、待运费地址是主流程上的分支，不是另一套产品。
"""
import asyncio
import re

from playwright.async_api import Page

from .constants import (
    YAHOO_STORE_TRADE_STATUS_URL,
    YAHOO_TRADE_NAV_URL,
    YAHOO_TRADE_TOP_URL,
)
from .exceptions import BotStopped
from .flow import OrderContext, deny, run_chain


# 旧版「購入手続きする」与新版「購入手続きをする」并存。
# 店铺落札后取引ナビ是 #pap「購入手続きする」→ /order/review（没有「を」）。
PURCHASE_PROCEDURE_SELECTORS = (
    "a[href*='buyer/payment/input']",
    "a[href*='/order/review']",
    "#pap a:has-text('購入手続きする')",
    "a:has-text('購入手続きをする')",
    "button:has-text('購入手続きをする')",
    "a:has-text('購入手続きする')",
    "button:has-text('購入手続きする')",
)
PURCHASE_PROCEDURE_LOCATOR = ", ".join(PURCHASE_PROCEDURE_SELECTORS)

# 购买手续跳转：人手点很快；goto 等 domcontentloaded 会整页卡死。
# 卡住时最多等 5 分钟，刷新后再判断是否已到下一页，否则再点，最多 3 次。
NAV_QUICK_MS = 20000
NAV_HANG_MS = 5 * 60 * 1000
NAV_RETRY_MAX = 3
CHECKOUT_URL_RE = re.compile(
    r".*(/order/review|/order/select-option|/order/change|/buyer/payment|"
    r"/buyer/edit|/buyer/preview|/buyer/top).*"
)
CHECKOUT_FORM_SEL = (
    "#address, h1:has-text('購入内容の確認'), "
    "h1:has-text('お届け先編集'), input[name='address2'], "
    "input[name='home_address2'], form[name='tradeForm'], "
    "#mBoxConfBt, h2:has-text('落札者情報')"
)

PAY_BTN_SELECTORS = [
    "input[value='確認する']",
    "button:has-text('確認する')",
    "#confirm a:has-text('確認する')",
    "a:has-text('確認し購入を進める')",
    "a:has-text('確認する'):not([href*='paypay']):not([href*='campaign'])",
    "input[value='決定する']",
    "button:has-text('決定する')",
    "a:has-text('決定する')",
    "#mBoxConfBt",
    "a.gv-Button--primary",
    "#fconfirm a:has-text('購入を確定する')",
    "a:has-text('購入を確定する')",
    "button:has-text('購入を確定する')",
]

SUCCESS_TOKENS = (
    "thank-you",
    "購入完了",
    "購入が完了",
    "手続きの完了",
    "購入手続きの完了",
    "購入が完了しました",
    "ご注文ありがとう",
    "ご購入ありがとうございます",
    "/order/thank-you",
    "/buyer/payment/complete",
    "/payment/complete",
)

# 个人取引ナビ：先填地址提交取引情報，卖家再报运费。
WAIT_SHIPPING_HINTS = (
    "出品者から送料の連絡があります",
    "送料の連絡があります",
    "送料連絡待ち",
    "取引情報を入力してください",
    "お届け先住所の連絡後",
)
WAIT_SHIPPING_FORM = (
    "form[name='tradeForm'], #mBoxConfBt, h2:has-text('落札者情報'), "
    "input[name='address2'], text=取引情報を入力してください"
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
                self.gate_item_option,
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
                await self.report_missing_yahoo_ui(
                    yahoo_page, "bundle_item_rows", ctx,
                    extra="已点「まとめて取引する商品を見る」，但 div.sc-bbfc97d-2 没有商品行。",
                )
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
                await self.report_missing_yahoo_ui(
                    yahoo_page, "bundle_item_rows", ctx,
                    extra="同捆列表页找不到「取引ナビに戻る」。",
                )
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
            await page.wait_for_load_state("domcontentloaded", timeout=12000)
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

    async def _wait_for_checkout_page(self, page, timeout_ms):
        try:
            await page.wait_for_url(CHECKOUT_URL_RE, timeout=timeout_ms)
            return True
        except Exception:
            pass
        if self._checkout_like_url(page.url):
            return True
        try:
            await page.locator(CHECKOUT_FORM_SEL).first.wait_for(
                state="visible", timeout=min(2000, timeout_ms)
            )
            return True
        except Exception:
            return False

    async def _page_load_unfinished(self, page, probe_ms=3000):
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=probe_ms)
            return False
        except Exception:
            return True

    async def _reload_stuck_page(self, page):
        """卡住时刷新。有概率刷新后已经在下一页。"""
        await self.log("页面加载卡住，刷新后再判断是否已进入下一页...")
        try:
            await page.reload(wait_until="commit", timeout=20000)
        except Exception as e:
            await self.log(f"刷新超时，继续用当前页: {page.url} ({e})", "WARNING")
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=12000)
        except Exception:
            pass
        if self._checkout_like_url(page.url):
            await self.log(f"刷新后已进入下一页: {page.url}")
            return True
        await self.log(f"刷新后仍在当前页: {page.url}")
        return False

    async def _click_purchase_procedure(self, ctx, link, href):
        """人手点按钮通常很快；不要用 goto 死等 domcontentloaded。"""
        try:
            await link.click(timeout=8000)
            return
        except Exception as e:
            await self.log(f"点击购买手续入口失败，改用打开链接: {e}", "DEBUG")
        if href:
            try:
                await ctx.yahoo_page.goto(href, wait_until="commit", timeout=20000)
            except Exception as e:
                await self.log(f"打开购买手续页超时，继续用当前页: {ctx.yahoo_page.url} ({e})", "WARNING")

    async def _relocate_purchase_procedure_link(self, ctx):
        page = ctx.yahoo_page
        loc = page.locator(PURCHASE_PROCEDURE_LOCATOR)
        try:
            if await loc.count():
                return loc.first
        except Exception:
            pass
        return None

    async def _goto_purchase_procedure(self, ctx, link):
        href = ""
        try:
            href = (await link.get_attribute("href")) or ""
        except Exception:
            href = ""
        if href:
            if href.startswith("/"):
                if href.startswith("/order/"):
                    href = "https://buy.auctions.yahoo.co.jp" + href
                else:
                    href = "https://contact.auctions.yahoo.co.jp" + href
        await self.log(f"进入购买手续页: {href or '(点击)'}")
        for attempt in range(1, NAV_RETRY_MAX + 1):
            if self._checkout_like_url(ctx.yahoo_page.url):
                break
            if attempt > 1:
                await self.log(f"仍未进入购买手续页，重试点击 ({attempt}/{NAV_RETRY_MAX})...")
                found = await self._relocate_purchase_procedure_link(ctx)
                if found is not None:
                    link = found
                elif not href:
                    await self.log("刷新后仍在当前页，且找不到购买手续按钮。", "WARNING")
                    break
            await self._click_purchase_procedure(ctx, link, href)
            if await self._wait_for_checkout_page(ctx.yahoo_page, NAV_QUICK_MS):
                break
            hanging = await self._page_load_unfinished(ctx.yahoo_page)
            if hanging:
                await self.log("购买手续页仍在加载，最多再等 5 分钟...")
                if await self._wait_for_checkout_page(ctx.yahoo_page, NAV_HANG_MS):
                    break
            if await self._reload_stuck_page(ctx.yahoo_page):
                break
        try:
            await ctx.yahoo_page.locator(CHECKOUT_FORM_SEL).first.wait_for(
                state="visible", timeout=10000
            )
        except Exception:
            pass
        await self.log(f"购买手续页当前 URL: {ctx.yahoo_page.url}")

    def _checkout_like_url(self, url):
        """真正能改地址/付款的页。/order/status 只是店铺取引ナビ，不算结算。"""
        u = url or ""
        return any(
            p in u
            for p in (
                "buyer/edit",
                "buyer/payment",
                "buyer/top",
                "buyer/preview",
                "/payment/input",
                "/order/review",
                "/order/select-option",
                "/order/change",
            )
        )

    def _text_says_wait_shipping(self, text):
        t = text or ""
        return any(h in t for h in WAIT_SHIPPING_HINTS)

    async def _page_is_wait_shipping_address_form(self, page):
        """先填落札者信息、点決定する，卖家之后才报运费。"""
        if page is None:
            return False
        try:
            if page.is_closed():
                return False
        except Exception:
            return False
        url = page.url or ""
        if "buyer/edit" in url:
            return True
        try:
            has_form = await page.locator(WAIT_SHIPPING_FORM).count()
            if not has_form:
                return False
            if await page.locator("text=取引情報を入力してください").count():
                return True
            body = ""
            try:
                body = await page.locator("body").inner_text()
            except Exception:
                body = ""
            return self._text_says_wait_shipping(body)
        except Exception:
            return False

    async def _mark_wait_shipping_form(self, ctx, why):
        ctx.is_wait_for_shipping = True
        ctx.is_personal = True
        await self.log(why)
        return None

    def _is_store_status_url(self, url):
        return "buy.auctions.yahoo.co.jp/order/status" in (url or "")

    def _is_trade_nav_url(self, url):
        """已结束拍品页「取引ナビ」现网是 /top?aid=，旧版是 /trade/top。

        店铺落札后是 buy.auctions /order/status。
        不用 `.../top` 当子串，以免误匹配 `/topic`。
        """
        u = (url or "").lower()
        if self._checkout_like_url(u):
            return True
        if self._is_store_status_url(u):
            return True
        if "/trade/top" in u:
            return True
        return "/top?aid=" in u or "/top&aid=" in u

    async def _page_has_yahoo_id_mismatch(self, page):
        try:
            text = await page.locator("body").inner_text()
        except Exception:
            return False
        return "取引ナビにアクセスできません" in text or "異なるYahoo! JAPAN ID" in text

    async def _click_store_purchase_procedure(self, ctx):
        """店铺取引ナビ #pap「購入手続きする」→ /order/review。"""
        page = ctx.yahoo_page
        link = page.locator("a[href*='/order/review']")
        if await link.count() == 0:
            link = page.locator(
                "#pap a:has-text('購入手続きする'), "
                "a:has-text('購入手続きする'), a:has-text('購入手続きをする')"
            )
        if await link.count() == 0:
            return False
        href = ""
        try:
            href = (await link.first.get_attribute("href")) or ""
        except Exception:
            href = ""
        if "buyer/payment" in href:
            return False
        ctx.is_store = True
        ctx.is_personal = False
        await self.log("店铺取引ナビ：发现「購入手続きする」，进入 /order/review")
        await self._goto_purchase_procedure(ctx, link.first)
        return True

    async def _try_store_won_checkout(self, ctx):
        """已落札店铺没有「購入手続きへ」，入口是 /order/status。

        True = 已按店铺处理（继续 Pipeline）；False = 不是店铺，走个人。
        dict = 提前结束。
        """
        yahoo_page = ctx.yahoo_page
        product_id = (ctx.order_info.get("product_id") or "").strip()
        url = yahoo_page.url or ""

        if "/order/review" in url or "/order/change" in url:
            ctx.is_store = True
            ctx.is_personal = False
            await self.log(f"已在店铺结算页: {url}")
            return True

        if self._is_store_status_url(url):
            ctx.is_store = True
            ctx.is_personal = False
            try:
                await yahoo_page.locator(
                    "#pap a, a[href*='/order/review'], h1:has-text('取引ナビ')"
                ).first.wait_for(state="visible", timeout=8000)
            except Exception:
                pass
            if await self._click_store_purchase_procedure(ctx):
                return True
            await self.log("店铺取引ナビ没有「購入手続きする」。", "WARNING")
            return True

        if (
            "contact.auctions.yahoo.co.jp" in url
            and not await self._page_has_yahoo_id_mismatch(yahoo_page)
        ):
            return False

        closed_nav_href = yahoo_page.locator("#closedNotice a:has-text('取引ナビ')")
        if await closed_nav_href.count():
            ch = (await closed_nav_href.first.get_attribute("href")) or ""
            if "contact.auctions" in ch or "/trade/" in ch:
                await self.log("拍品页取引ナビ指向个人 contact.auctions，不走店铺 /order/status。")
                return False

        store_link = yahoo_page.locator(
            "a[href*='/order/status'], a[href*='/order/review'], "
            "#closedNotice a[href*='buy.auctions.yahoo.co.jp']"
        )
        if await store_link.count():
            href = (await store_link.first.get_attribute("href")) or ""
            if href.startswith("/"):
                href = "https://buy.auctions.yahoo.co.jp" + href
            await self.log(f"已结束店铺拍品，打开店铺取引ナビ: {href}")
            ctx.is_store = True
            ctx.is_personal = False
            try:
                await yahoo_page.goto(href, wait_until="domcontentloaded", timeout=20000)
            except Exception as e:
                await self.log(f"店铺取引ナビ打开失败: {e}", "WARNING")
                return False
            if "/order/review" in (yahoo_page.url or "") or "/order/change" in (yahoo_page.url or ""):
                return True
            try:
                await yahoo_page.locator(
                    "#pap a, a[href*='/order/review']"
                ).first.wait_for(state="visible", timeout=8000)
            except Exception:
                pass
            if await self._click_store_purchase_procedure(ctx):
                return True
            return True

        if not product_id:
            return False

        status_url = YAHOO_STORE_TRADE_STATUS_URL.format(product_id=product_id)
        await self.log(f"尝试店铺取引ナビ: {status_url}")
        try:
            await yahoo_page.goto(status_url, wait_until="domcontentloaded", timeout=20000)
        except Exception as e:
            await self.log(f"店铺取引ナビ打开超时: {e}", "WARNING")
            return False
        try:
            await yahoo_page.locator(
                "#pap a, a[href*='/order/review'], h1:has-text('取引ナビ')"
            ).first.wait_for(state="visible", timeout=8000)
        except Exception:
            pass
        if await self._page_has_yahoo_id_mismatch(yahoo_page):
            await self.log("店铺 /order/status 返回账号不符页，不按店铺处理。", "WARNING")
            return False
        if await self._click_store_purchase_procedure(ctx):
            return True
        await self.log("店铺 /order/status 没有「購入手続きする」，改走个人取引ナビ。")
        return False

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
        """新版取引ナビ会先问まとめて还是单品。

        QBT「拆分」包裹入队后仍走这里：只买当前拍品，点「単品で取引する」，
        绝不点「まとめて取引をはじめる」。后台同捆 ≠ 雅虎同捆购买。
        """
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
        if not modal_visible:
            await self.log("检测到新版同捆选择页。点击「単品で取引する」（不申请まとめて取引）。")
            await page_btn.first.click(force=True)
            try:
                await modal_title.wait_for(state="visible", timeout=8000)
            except Exception:
                pass
        if await modal_title.is_visible():
            await self.log("确认弹窗：本当に単品で取引しますか？ 点击确定。")
            pages_before = list(yahoo_page.context.pages)
            clicked_ok = False
            confirm_btn = yahoo_page.locator("dialog[open]").get_by_role(
                "button", name="単品で取引する"
            )
            try:
                await confirm_btn.click(force=True, timeout=5000)
                clicked_ok = True
            except Exception:
                clicked_ok = await yahoo_page.evaluate(
                    """() => {
                        const h2 = [...document.querySelectorAll('h2')]
                            .find(h => (h.textContent || '').includes('本当に単品で取引しますか'));
                        const scope = (h2 && (h2.closest('dialog') || h2.closest('section'))) || document;
                        const ok = [...scope.querySelectorAll('button')]
                            .find(b => (b.textContent || '').trim() === '単品で取引する');
                        if (!ok) return false;
                        ok.click();
                        return true;
                    }"""
                )
            if not clicked_ok:
                await self.log("确认弹窗确定按钮未点到。", "ERROR")
                return {"status": "ERROR", "error": "single_item_modal_click_failed"}
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
        if await self._page_is_wait_shipping_address_form(yahoo_page):
            return await self._mark_wait_shipping_form(
                ctx, "已在待运费地址页：先提交取引情報，卖家再报运费。"
            )
        if self._checkout_like_url(yahoo_page.url):
            await self.log(f"已在结算/地址页: {yahoo_page.url}")
            return None
        if await self._page_has_yahoo_id_mismatch(yahoo_page):
            await self.log(
                "個人取引ナビ拒绝访问（Yahoo ID 与入札时不一致，或店铺单误开了 /trade/top）。改走店铺 /order/status。",
                "WARNING",
            )
            store_ok = await self._try_store_won_checkout(ctx)
            if store_ok is True:
                return None
            if isinstance(store_ok, dict):
                return store_ok
            await self.report_missing_yahoo_ui(
                yahoo_page, "trade_nav_id_mismatch", ctx,
                extra="取引ナビにアクセスできません。入札账号与当前登录 Yahoo ID 不一致。",
            )
            return deny("YAHOO_ID_MISMATCH")
        if self._is_store_status_url(yahoo_page.url):
            if await self._click_store_purchase_procedure(ctx):
                return None
        adopted = await self._adopt_checkout_tab(ctx, timeout=8000)
        if adopted:
            if await self._page_is_wait_shipping_address_form(ctx.yahoo_page):
                return await self._mark_wait_shipping_form(
                    ctx, "已切换到待运费地址页：先提交取引情報，卖家再报运费。"
                )
            return None
        store_link = await self._adopt_page_matching(
            ctx,
            url_parts=("buy.auctions.yahoo.co.jp/order/status", "/order/review"),
            selector="a[href*='/order/review'], #pap a:has-text('購入手続きする')",
            timeout=4000,
        )
        if store_link is not None:
            await self.log("发现店铺「購入手続きする」。正在进入 /order/review...")
            ctx.is_store = True
            ctx.is_personal = False
            await self._goto_purchase_procedure(ctx, store_link)
            return None
        await self.log("等待新版取引ナビ异步渲染「購入手続きをする」...")
        link = await self._adopt_page_matching(
            ctx,
            url_parts=(
                "contact.auctions.yahoo.co.jp",
                "/trade/top",
                "/top?aid=",
                "buyer/payment",
                "buyer/edit",
                "buyer/preview",
            ),
            selector="a[href*='buyer/payment/input']",
            timeout=25000,
        )
        if await self._page_is_wait_shipping_address_form(ctx.yahoo_page):
            return await self._mark_wait_shipping_form(
                ctx, "等待中已进入待运费地址页：先提交取引情報，卖家再报运费。"
            )
        if link is None:
            link = await self._adopt_page_matching(
                ctx,
                url_parts=(
                    "contact.auctions.yahoo.co.jp",
                    "/trade/top",
                    "/top?aid=",
                    "buyer/edit",
                ),
                selector=PURCHASE_PROCEDURE_LOCATOR,
                timeout=8000,
            )
        if await self._page_is_wait_shipping_address_form(ctx.yahoo_page):
            return await self._mark_wait_shipping_form(
                ctx, "已进入待运费地址页：先提交取引情報，卖家再报运费。"
            )
        if link is None:
            await self.log(
                f"取引ナビ页没有「購入手続きをする」。URL={ctx.yahoo_page.url}",
                "WARNING",
            )
            if await self._page_has_yahoo_id_mismatch(ctx.yahoo_page):
                await self.log(
                    "等待后仍是账号不符页。改走店铺 /order/status。",
                    "WARNING",
                )
                store_ok = await self._try_store_won_checkout(ctx)
                if store_ok is True:
                    return None
                if isinstance(store_ok, dict):
                    return store_ok
                return deny("YAHOO_ID_MISMATCH")
        else:
            href = ""
            try:
                href = (await link.get_attribute("href")) or ""
            except Exception:
                href = ""
            if "/order/review" in href or self._is_store_status_url(ctx.yahoo_page.url):
                ctx.is_store = True
                ctx.is_personal = False
                await self.log("发现店铺「購入手続きする」。正在进入购买手续页...")
            else:
                await self.log("发现个人卖家「購入手続きをする」。正在进入支付页...")
            await self._goto_purchase_procedure(ctx, link)
        yahoo_page = ctx.yahoo_page
        if await self._page_is_wait_shipping_address_form(yahoo_page):
            return await self._mark_wait_shipping_form(
                ctx, "购买手续页是待运费地址页：先提交取引情報，卖家再报运费。"
            )
        kantan_btn = yahoo_page.locator(
            "a:has-text('Yahoo!かんたん決済で支払う'), button:has-text('Yahoo!かんたん決済で支払う')"
        )
        has_kantan = await kantan_btn.count() > 0
        if has_kantan:
            await self.log("发现 'Yahoo!かんたん決済' 按钮。正在点击...")
            await self._follow_click(ctx, kantan_btn.first)
            yahoo_page = ctx.yahoo_page
        try:
            body_text = await yahoo_page.locator("body").inner_text()
        except Exception:
            body_text = ""
        if self._text_says_wait_shipping(body_text):
            ctx.is_wait_for_shipping = True
            has_form = await yahoo_page.locator(
                "form[name='tradeForm'], #mBoxConfBt, "
                "input[name='address2'], input[name='home_address2']"
            ).count()
            if has_form:
                await self.log("状态：等待运费联系。先填地址并提交「決定する」。")
                return None
            await self.log("已在送料連絡待ち，交易信息此前已提交。")
            return {"status": "WAIT_SHIPPING_CONTACT", "shipping": 0, "total": 0}
        if link is None and not has_kantan:
            await self.report_missing_yahoo_ui(
                yahoo_page, "personal_purchase_procedure", ctx,
                extra="已在取引ナビ，没有「購入手続きをする」，也没有かんたん決済。",
            )
        return None

    async def step_enter_purchase(self, ctx):
        """一口价 / 今すぐ落札 / 取引ナビ。待运费在此早退。"""
        yahoo_page = ctx.yahoo_page
        order_info = ctx.order_info
        test_mode = ctx.test_mode
        # 新版拍品页稳定容器是 #bidButtonGroup；按钮 class 带哈希，只用按钮文字。
        buy_now_btn = yahoo_page.locator(
            "#bidButtonGroup button:has-text('購入手続きへ'), "
            "#bidButtonGroup a:has-text('購入手続きへ'), "
            "button:has-text('購入手続きへ'), a:has-text('購入手続きへ')"
        )
        buy_now_auction_btn = yahoo_page.locator(
            "#bidButtonGroup button:has-text('今すぐ落札'), "
            "button:has-text('今すぐ落札'), a:has-text('今すぐ落札')"
        )
        if await buy_now_btn.count() > 0:
            await self.log("检测到店铺结算入口 (購入手続きへ)。正在点击...")
            ctx.is_store = True
            await buy_now_btn.first.click()
            try:
                await yahoo_page.wait_for_url(
                    re.compile(
                        r".*(/order/review|/order/select-option|/order/change).*"
                    ),
                    timeout=15000,
                )
            except Exception:
                pass
            await self.log(f"購入手続きへ后当前页: {yahoo_page.url}")
        elif await buy_now_auction_btn.count() > 0:
            has_bid = await yahoo_page.locator(
                "#bidButtonGroup button:has-text('入札'), #bidButtonGroup a:has-text('入札')"
            ).count() > 0
            await self.log(
                "检测到拍品页一口价 (今すぐ落札)"
                + ("，同时有入札入口" if has_bid else "，仅即決")
                + "。"
            )
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
                        else:
                            await self.report_missing_yahoo_ui(
                                yahoo_page, "bid_confirm", ctx,
                                extra="已进入确认页，但找不到「落札する」。",
                            )
                    except Exception as e:
                        await self.log(f"立即购买流程出错: {e}", "WARNING")
                        await self.report_missing_yahoo_ui(
                            yahoo_page, "bid_confirm", ctx,
                            extra=f"点「今すぐ落札」后未出现「入札内容の確認」或后续失败: {e}",
                        )
            else:
                await self.log("未提取到即决价格，无法核对。跳过立即购买。")
        else:
            product_id = (order_info.get("product_id") or "").strip()
            store_ok = await self._try_store_won_checkout(ctx)
            if store_ok is True:
                yahoo_page = ctx.yahoo_page
                await self.extract_seller_messages(yahoo_page, ctx.order_id, is_store=True)
                return None
            if isinstance(store_ok, dict):
                return store_ok
            closed_nav = yahoo_page.locator("#closedNotice a:has-text('取引ナビ')")
            nav_candidates = yahoo_page.locator(
                "#closedNotice a:has-text('取引ナビ'), "
                "a:has-text('取引ナビ'), button:has-text('取引ナビ')"
            )
            nav_count = await nav_candidates.count()
            closed_count = await closed_nav.count()
            if product_id or nav_count > 0:
                ctx.is_personal = True
                cur = yahoo_page.url or ""
                already_personal_nav = (
                    "contact.auctions.yahoo.co.jp" in cur
                    and (not product_id or product_id in cur)
                    and not self._is_store_status_url(cur)
                )
                if already_personal_nav:
                    await self.log(f"已在取引相关页，跳过重复打开: {cur}")
                elif closed_count > 0:
                    href = (await closed_nav.first.get_attribute("href")) or ""
                    await self.log("已结束拍品页 #closedNotice「取引ナビ」。")
                    if href:
                        if href.startswith("/"):
                            if href.startswith("/order/"):
                                href = "https://buy.auctions.yahoo.co.jp" + href
                            else:
                                href = "https://contact.auctions.yahoo.co.jp" + href
                        await self.log(f"打开取引ナビ: {href}")
                        try:
                            await yahoo_page.goto(href, wait_until="commit", timeout=20000)
                            try:
                                await yahoo_page.wait_for_load_state(
                                    "domcontentloaded", timeout=12000
                                )
                            except Exception:
                                pass
                        except Exception as e:
                            await self.log(
                                f"取引ナビ打开超时，继续用当前页: {yahoo_page.url} ({e})",
                                "WARNING",
                            )
                    else:
                        await self._follow_click(
                            ctx,
                            closed_nav.first,
                            wait_locator=(
                                "h1:has-text('取引ナビ'), "
                                "a[href*='buyer/payment/input'], "
                                "a:has-text('購入手続きをする'), "
                                "button:has-text('単品で取引する'), "
                                "form[name='tradeForm'], #mBoxConfBt, "
                                "text=取引情報を入力してください"
                            ),
                        )
                    await self.log(f"点击后当前页: {ctx.yahoo_page.url}")
                elif product_id:
                    opened = False
                    for trade_url in (
                        YAHOO_TRADE_NAV_URL.format(product_id=product_id),
                        YAHOO_TRADE_TOP_URL.format(product_id=product_id),
                    ):
                        await self.log(f"直接打开取引ナビ: {trade_url}")
                        try:
                            await yahoo_page.goto(trade_url, wait_until="commit", timeout=20000)
                            try:
                                await yahoo_page.wait_for_load_state(
                                    "domcontentloaded", timeout=12000
                                )
                            except Exception:
                                pass
                            opened = True
                            break
                        except Exception as e:
                            await self.log(
                                f"取引ナビ打开超时: {trade_url} ({e})",
                                "WARNING",
                            )
                    if not opened:
                        await self.log(f"取引ナビ打开失败，继续用当前页: {yahoo_page.url}", "WARNING")
                    await self.log(f"点击后当前页: {yahoo_page.url}")
                else:
                    await self.log("检测到个人卖家。正在点击 '取引ナビ'...")
                    nav_link = nav_candidates.first
                    for i in range(nav_count):
                        href = (await nav_candidates.nth(i).get_attribute("href")) or ""
                        href_l = href.lower()
                        if any(
                            k in href_l
                            for k in ("trade", "closeduser", "navi", "/top?aid=", "contact.auctions")
                        ):
                            nav_link = nav_candidates.nth(i)
                            break
                    await self._follow_click(
                        ctx,
                        nav_link,
                        wait_locator=(
                            "h1:has-text('取引ナビ'), "
                            "a[href*='buyer/payment/input'], "
                            "a:has-text('購入手続きをする'), "
                            "button:has-text('単品で取引する'), "
                            "form[name='tradeForm'], #mBoxConfBt, "
                            "text=取引情報を入力してください"
                        ),
                    )
                yahoo_page = ctx.yahoo_page
                # 新版对话在 trade/top 的 __NEXT_DATA__；点単品后会离开此页，须先抓
                await self.extract_seller_messages(yahoo_page, ctx.order_id, is_store=False)
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
                await self.report_missing_yahoo_ui(
                    yahoo_page, "auction_entry", ctx,
                    extra="拍品页三个入口都没找到。",
                )
        return None

    async def step_new_trade_choice(self, ctx):
        """进入取引ナビ后若仍停在新版同捆选择页，再点一次单品。"""
        if self._checkout_like_url(ctx.yahoo_page.url):
            return None
        if ctx.is_store or self._is_store_status_url(ctx.yahoo_page.url):
            if await self._click_store_purchase_procedure(ctx):
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
        form = yahoo_page.locator(
            "#address, h2:has-text('お届け先'), h2:has-text('お届け先住所'), h2:has-text('落札者情報'), "
            "form[name='tradeForm'], input[name='address2'], input[name='home_address2'], #mBoxConfBt"
        )
        if self._checkout_like_url(yahoo_page.url):
            await self.log(f"已进入结算/地址页。等待表单... {yahoo_page.url}")
            try:
                await form.first.wait_for(state="visible", timeout=10000)
            except Exception:
                pass
            return None
        try:
            await form.first.wait_for(state="visible", timeout=10000)
            await self.log("已进入购买输入页。等待表单...")
        except Exception:
            await self.log("警告：未找到 'お届け先' 表单，页面可能不同。继续...", "WARNING")
            await self.report_missing_yahoo_ui(
                yahoo_page, "checkout_address_form", ctx,
                extra="已进入结算相关 URL，但地址表单约定选择器都没命中。",
            )
        return None

    async def step_capture_body_and_payment_gate(self, ctx):
        try:
            ctx.body_text = await ctx.yahoo_page.locator("body").inner_text()
        except Exception:
            ctx.body_text = ""
        if self._text_says_wait_shipping(ctx.body_text) or await self._page_is_wait_shipping_address_form(
            ctx.yahoo_page
        ):
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

    async def _click_store_purchase_confirm(self, yahoo_page, test_mode):
        """店铺二次确认 #fconfirm「購入を確定しますか？」→「購入を確定する」。"""
        fconfirm = yahoo_page.locator("#fconfirm")
        buy = fconfirm.locator("a:has-text('購入を確定する'), button:has-text('購入を確定する')")
        try:
            if await fconfirm.count() == 0:
                return False
            try:
                if not await fconfirm.first.is_visible():
                    return False
            except Exception:
                return False
            if await buy.count() == 0:
                buy = yahoo_page.locator("#fconfirm >> text=購入を確定する")
            if await buy.count() == 0:
                return False
            await self.log("发现店铺二次确认弹窗「購入を確定しますか？」")
            if test_mode:
                await self.log("[TestMode] 二次确认弹窗已出现，不点「購入を確定する」。")
                return True
            await buy.first.click(force=True)
            return True
        except Exception as e:
            await self.log(f"店铺二次确认点击失败: {e}", "DEBUG")
            return False

    async def _click_personal_final_confirm(self, yahoo_page, test_mode):
        """个人 /buyer/payment/confirm：「上記に同意のうえ 購入を確定する」。"""
        url = yahoo_page.url or ""
        agree = yahoo_page.locator(
            "a:has-text('上記に同意のうえ'), button:has-text('上記に同意のうえ')"
        )
        on_confirm = "/buyer/payment/confirm" in url
        visible = False
        try:
            visible = await agree.count() > 0 and await agree.first.is_visible()
        except Exception:
            visible = False
        if not on_confirm and not visible:
            return False
        if not visible:
            return False
        await self.log("发现个人确认页「上記に同意のうえ 購入を確定する」。")
        if test_mode:
            await self.log("[TestMode] 不点最终确定。")
            return True
        try:
            await agree.first.click()
        except Exception:
            buy = yahoo_page.locator(
                "a:has-text('購入を確定する'), button:has-text('購入を確定する')"
            )
            await buy.first.click()
        return True

    async def _wait_purchase_complete(self, page, timeout_ms=20000):
        """点最终确定后等 /buyer/payment/complete 或「購入が完了しました」。"""
        try:
            await page.wait_for_url(
                re.compile(
                    r".*(/buyer/payment/complete|/payment/complete|"
                    r"/order/thank-you|thank-you).*"
                ),
                timeout=timeout_ms,
            )
            return True
        except Exception:
            pass
        url = page.url or ""
        title = ""
        try:
            title = await page.title()
        except Exception:
            title = ""
        if any(t in url or t in title for t in SUCCESS_TOKENS):
            return True
        try:
            await page.locator(
                "h3:has-text('購入が完了しました'), h1:has-text('完了'), "
                "text=購入が完了しました"
            ).first.wait_for(state="visible", timeout=3000)
            return True
        except Exception:
            return False

    async def step_click_pay(self, ctx):
        yahoo_page = ctx.yahoo_page
        test_mode = ctx.test_mode
        if hasattr(self, "_dismiss_reauth_note"):
            await self._dismiss_reauth_note(yahoo_page)
        if hasattr(self, "_is_yahoo_account_address_url") and self._is_yahoo_account_address_url(yahoo_page.url):
            await self.log("支付前仍停在 Yahoo 账号地址页，先刷新并返回结算...")
            ok = False
            if hasattr(self, "_fill_yahoo_account_address_and_return"):
                ok = await self._fill_yahoo_account_address_and_return(
                    yahoo_page, getattr(ctx, "suffix", "")
                )
            if not ok and hasattr(self, "_return_from_yahoo_account_address"):
                ok = await self._return_from_yahoo_account_address(yahoo_page)
            if self._is_yahoo_account_address_url(yahoo_page.url):
                await self.report_missing_yahoo_ui(
                    yahoo_page, "address_edit", ctx,
                    extra="仍停在 account.edit.yahoo.co.jp/address，未回到结算页，不点确认购买。",
                )
                return {"status": "ADDRESS_FAIL"}
            if ok:
                await self.log("已从账号地址页回到结算，继续找确认按钮。")
        if await self._click_personal_final_confirm(yahoo_page, test_mode):
            if not test_mode:
                ctx.trade_info_submitted = True
                await self._wait_purchase_complete(yahoo_page)
            return None
        if await self._click_store_purchase_confirm(yahoo_page, test_mode):
            if not test_mode:
                ctx.trade_info_submitted = True
                await self._wait_purchase_complete(yahoo_page)
            return None
        classic_confirm = yahoo_page.locator("#mBoxConfBt")
        if await classic_confirm.count() and await classic_confirm.first.is_visible():
            await self.log("发现旧版取引ナビ「決定する」(#mBoxConfBt)")
            if test_mode:
                await self.log("[TestMode] 模拟点击决定（不提交，也不点弹窗「確定する」）...")
                return None
            await classic_confirm.first.click(force=True)
            try:
                await yahoo_page.locator("#confSubmitBtn, #mBoxConf").wait_for(
                    state="visible", timeout=8000
                )
                confirm = yahoo_page.locator("#confSubmitBtn")
                if await confirm.count() == 0:
                    confirm = yahoo_page.locator(
                        "#mBoxConf button:has-text('確定する'), "
                        "#mBoxConf input[value='確定する']"
                    )
                if await confirm.count():
                    await self.log("确认弹窗：取引情報を確定しますか？ 点击確定する。")
                    await confirm.first.click(force=True)
                    ctx.trade_info_submitted = True
                    try:
                        await confirm.first.wait_for(state="hidden", timeout=10000)
                    except Exception:
                        pass
                    try:
                        await yahoo_page.wait_for_load_state("domcontentloaded", timeout=10000)
                    except Exception:
                        pass
            except Exception as e:
                await self.log(f"旧版确定弹窗未出现: {e}", "WARNING")
                await self.report_missing_yahoo_ui(
                    yahoo_page, "pay_confirm_button", ctx,
                    extra=f"已点 #mBoxConfBt，弹窗 #confSubmitBtn / #mBoxConf 未出现: {e}",
                )
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
                    try:
                        await yahoo_page.wait_for_url(
                            re.compile(
                                r".*(/buyer/payment/confirm|/buyer/payment/complete|"
                                r"/order/thank-you).*"
                            ),
                            timeout=15000,
                        )
                    except Exception:
                        pass
                    try:
                        await yahoo_page.locator(
                            "#fconfirm, a:has-text('上記に同意のうえ'), "
                            "h3:has-text('購入が完了しました')"
                        ).first.wait_for(state="visible", timeout=8000)
                    except Exception:
                        pass
                    if await self._click_personal_final_confirm(yahoo_page, test_mode):
                        ctx.trade_info_submitted = True
                    elif await self._click_store_purchase_confirm(yahoo_page, test_mode):
                        ctx.trade_info_submitted = True
                    if not test_mode:
                        await self._wait_purchase_complete(yahoo_page)
                    break
            except Exception:
                continue
        else:
            if not (await classic_confirm.count() and await classic_confirm.first.is_visible()):
                await self.report_missing_yahoo_ui(
                    yahoo_page, "pay_confirm_button", ctx,
                    extra="结算页找不到 #mBoxConfBt / 確認する / 決定する / 購入を確定する。",
                )
        return None

    async def step_finish(self, ctx):
        yahoo_page = ctx.yahoo_page
        if not ctx.test_mode and "/buyer/payment/confirm" in (yahoo_page.url or ""):
            await self.log("仍在购买确认页，补点「上記に同意のうえ 購入を確定する」...")
            clicked = await self._click_personal_final_confirm(yahoo_page, False)
            if not clicked:
                clicked = await self._click_store_purchase_confirm(yahoo_page, False)
            if clicked:
                ctx.trade_info_submitted = True
            await self._wait_purchase_complete(yahoo_page)
        elif not ctx.test_mode:
            await self._wait_purchase_complete(yahoo_page, timeout_ms=8000)
        title = ""
        url = ""
        body_snippet = ""
        try:
            title = await yahoo_page.title()
            url = yahoo_page.url or ""
            try:
                body_snippet = await yahoo_page.locator("body").inner_text()
            except Exception:
                body_snippet = ""
        except Exception as e:
            await self.log(f"Success Detection Failed with Error: {e}", "DEBUG")

        is_success = any(t in url or t in title or t in body_snippet for t in SUCCESS_TOKENS)
        if is_success:
            await self.log(f"快速检测到成功状态 (URL/Title): {title} {url}")
            await self.trigger_sound("success")

        payload = {
            "shipping": ctx.shipping_cost,
            "total": ctx.yahoo_total,
            "is_cod": ctx.early_cod,
            "shipping_warning": ctx.early_cod,
        }
        if ctx.is_wait_for_shipping:
            if ctx.test_mode:
                return {"status": "WAIT_SHIPPING_CONTACT", **payload}
            decide_still_open = False
            try:
                decide = yahoo_page.locator("#mBoxConfBt")
                decide_still_open = await decide.count() > 0 and await decide.first.is_visible()
            except Exception:
                decide_still_open = False
            submitted = bool(getattr(ctx, "trade_info_submitted", False))
            if submitted or not decide_still_open:
                await self.log("待卖家联系运费：交易信息已提交（此类订单没有「購入完了」页）。")
                return {"status": "WAIT_SHIPPING_CONTACT", **payload}
            await self.log("待运费单：决定按钮仍在，交易信息可能未提交。", "WARNING")
            self.add_risk_history_entry(
                ctx.order_info.get("order_id"), "人工处理: 待运费单未提交交易信息"
            )
            await self.report_missing_yahoo_ui(
                yahoo_page, "pay_confirm_button", ctx,
                extra="待运费单：#mBoxConfBt 仍在，交易信息可能未提交。",
            )
            return {"status": "PURCHASE_UNCONFIRMED", **payload}

        if hasattr(self, "_is_yahoo_account_address_url") and self._is_yahoo_account_address_url(url):
            await self.log("仍停在 Yahoo 账号地址页，未进入支付确认。", "WARNING")
            self.add_risk_history_entry(
                ctx.order_info.get("order_id"), "人工处理: Yahoo账号地址页未回到结算"
            )
            await self.report_missing_yahoo_ui(
                yahoo_page, "address_edit", ctx,
                extra="结束时仍在 account.edit.yahoo.co.jp/address，不是购买完成页。",
            )
            return {"status": "ADDRESS_FAIL", **payload}

        if not is_success and not ctx.test_mode:
            await self.log(
                "购买未确认：未检测到「購入が完了しました」成功页面。可能未完成支付，需人工确认。",
                "WARNING",
            )
            self.add_risk_history_entry(ctx.order_info.get("order_id"), "人工处理: 未检测到购买成功页面")
            await self.report_missing_yahoo_ui(
                yahoo_page, "purchase_success_page", ctx,
                extra="正式模式已点确认，但 URL/标题/正文都没有 thank-you、購入完了。",
            )
            return {"status": "PURCHASE_UNCONFIRMED", **payload}

        status = "TEST_SUCCESS" if ctx.test_mode else "PURCHASED"
        return {"status": status, **payload}
