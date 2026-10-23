"""阶段3 下单主流程 Pipeline：入口 → 结算步骤 → 确认支付。

同捆校验、待运费地址是主流程上的分支，不是另一套产品。
"""
import asyncio
import json
import re
from urllib.parse import parse_qs, urlparse

from playwright.async_api import Page

from .constants import (
    WAIT_CLICK_MS,
    WAIT_HANG_MS,
    YAHOO_STORE_TRADE_STATUS_URL,
    YAHOO_TRADE_NAV_URL,
    YAHOO_TRADE_TOP_URL,
)
from .waits import yahoo_host_path
from .exceptions import BotStopped
from .flow import OrderContext, deny, run_chain


# 旧版「購入手続きする」与新版「購入手続きをする」并存。
# 店铺落札后取引ナビ是 #pap「購入手続きする」→ /order/review（没有「を」）。
# 可同捆时变成「まとめて購入手続きする」/「単品で購入手続きする」：跳过转人工，两按钮都不点。
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
STORE_BUNDLE_CHOICE_SEL = (
    "#bndl a, "
    "a[data-cl-params*='_cl_link:bndlpap'], "
    "a[data-cl-params*='_cl_link:sglpap'], "
    "a:has-text('まとめて購入手続きする'), "
    "button:has-text('まとめて購入手続きする'), "
    "a:has-text('単品で購入手続きする'), "
    "button:has-text('単品で購入手続きする'), "
    "h2:has-text('まとめて購入手続きできる商品'), "
    "text=まとめて購入手続き出来る商品があります, "
    "aside:has-text('まとめて購入手続きができます')"
)
STORE_STATUS_READY_SEL = (
    "h1:has-text('取引ナビ'), "
    "#pap a, "
    "a[href*='/order/review'], "
    "#bndl a, "
    "a:has-text('まとめて購入手続きする'), "
    "a:has-text('単品で購入手続きする')"
)
PERSONAL_PROCEDURE_SEL = (
    "a[href*='buyer/payment/input'], "
    "a:has-text('購入手続きをする'), "
    "button:has-text('購入手続きをする')"
)
PERSONAL_BUNDLE_SEL = (
    "main button:has-text('単品で取引する'), "
    "main a:has-text('単品で取引する'), "
    "button:has-text('単品で取引する'), "
    "a:has-text('単品で取引する'), "
    "a:has-text('まとめて取引をはじめる'), "
    "button:has-text('まとめて取引をはじめる')"
)
TRADE_KIND_LABEL = {
    "login": "登录页",
    "id_mismatch": "账号不符",
    "no_purchase_data": "无购买数据",
    "wait_shipping_form": "待运费地址页",
    "checkout": "结算页",
    "store_bundle": "店铺同捆选择",
    "store_status": "店铺取引ナビ",
    "personal_bundle": "个人同捆选择",
    "personal_nav": "个人取引ナビ",
    "unknown": "未识别页",
}

# 未中标 / 卖家取消 / 落札数据未反映：不是系统错误，也不要当改版缺按钮。
NO_PURCHASE_DATA_HINTS = (
    "購入データが確認できません",
    "システムに情報の反映が遅延している可能性があります",
)

# 购买手续跳转：人手点很快；goto 等 domcontentloaded 会整页卡死。
# 卡住时最多等 5 分钟，刷新后再判断是否已到下一页，否则再点，最多 3 次。
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
    "支払いが完了しました",
    "支払い完了の連絡を行いました",
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

# 尚未付款的结算表单。confirm/complete 要打开订单详情取运费。
UNPAID_CHECKOUT_PATHS = (
    "/order/review",
    "/order/select-option",
    "/buyer/payment/input",
    "/buyer/edit",
)


def _parse_yen(value):
    if value is None or value is False:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).replace("，", ",")
    found = re.search(r"([\d,]+)", text)
    if not found:
        return None
    return int(found.group(1).replace(",", ""))


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

        def wrap(step):
            async def wrapped(inner_ctx):
                page = inner_ctx.yahoo_page
                if page is not None:
                    if await self._page_has_no_purchase_data(page):
                        return await self._skip_no_purchase_data(inner_ctx)
                    await self._reload_if_yahoo_system_error(page)
                    await self._try_skip_wallet_sub_add(page)
                return await step(inner_ctx)

            return wrapped

        aborted = await run_chain(
            [
                wrap(self.gate_combined_shipping),
                wrap(self.step_bundle_interstitial),
                wrap(self.step_enter_purchase),
                wrap(self.step_new_trade_choice),
                wrap(self.gate_item_option),
                wrap(self.step_wait_checkout_form),
                wrap(self.step_capture_body_and_payment_gate),
                wrap(self.step_uncheck_newsletter),
                wrap(self.step_address),
                wrap(self.step_shipping),
                wrap(self.step_okihai),
                wrap(self.step_credit_card),
                wrap(self.gate_need_cvv),
                wrap(self.step_mark_cod),
                wrap(self.step_extract_messages),
                wrap(self.step_click_pay),
                wrap(self.gate_paypay_card_redirect),
                wrap(self.step_finish),
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

    async def _follow_click(self, ctx, locator, wait_locator=None, timeout=WAIT_HANG_MS):
        """点击可能打开新标签的链接，并切到新页。避免还停在拍品页找按钮。"""
        page = ctx.yahoo_page
        context = page.context
        pages_before = list(context.pages)
        clicked = False
        try:
            async with context.expect_page(timeout=WAIT_CLICK_MS) as new_page_info:
                if not await self.click_visible(locator):
                    await locator.click(timeout=WAIT_CLICK_MS)
                clicked = True
            ctx.yahoo_page = await new_page_info.value
        except Exception:
            if not clicked:
                if not await self.click_visible(locator):
                    await locator.click(timeout=WAIT_CLICK_MS)
            new_pages = [p for p in context.pages if p not in pages_before]
            if new_pages:
                ctx.yahoo_page = new_pages[-1]
        page = ctx.yahoo_page
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=WAIT_HANG_MS)
        except Exception:
            pass
        await page.bring_to_front()
        await self.log(f"点击后当前页: {page.url}")
        if wait_locator:
            if not await self.wait_visible(page.locator(wait_locator), timeout_ms=timeout):
                await self.log(f"点击后未等到目标元素。URL={page.url}", "WARNING")
        return page

    async def _adopt_page_matching(self, ctx, url_parts, selector, timeout=WAIT_HANG_MS):
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

    async def _wait_for_checkout_page(self, page, timeout_ms=WAIT_HANG_MS):
        if self._is_yahoo_login_or_reauth_url(page.url):
            ok = await self.wait_out_yahoo_login_or_reauth(page, "进入购买手续")
            if not ok and self._is_yahoo_login_or_reauth_url(page.url):
                return False

        async def ready():
            await self._reload_if_yahoo_system_error(page)
            if self._checkout_like_url(page.url):
                return True
            try:
                return await page.locator(CHECKOUT_FORM_SEL).first.is_visible()
            except Exception:
                return False

        return await self.wait_until(ready, timeout_ms=timeout_ms)

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
            await page.reload(wait_until="commit", timeout=WAIT_HANG_MS)
        except Exception as e:
            await self.log(f"刷新超时，继续用当前页: {page.url} ({e})", "WARNING")
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=WAIT_HANG_MS)
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
            if await self.click_visible(link, timeout_ms=WAIT_CLICK_MS):
                return
            await link.click(timeout=WAIT_CLICK_MS)
            return
        except Exception as e:
            await self.log(f"点击购买手续入口失败，改用打开链接: {e}", "DEBUG")
        if href:
            try:
                await ctx.yahoo_page.goto(href, wait_until="commit", timeout=WAIT_HANG_MS)
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
            await self.log("等待进入购买手续页（最长 5 分钟）...")
            if await self._wait_for_checkout_page(ctx.yahoo_page, WAIT_HANG_MS):
                break
            if await self._reload_stuck_page(ctx.yahoo_page):
                break
        await self.wait_visible(ctx.yahoo_page.locator(CHECKOUT_FORM_SEL), timeout_ms=WAIT_HANG_MS)
        await self.log(f"购买手续页当前 URL: {ctx.yahoo_page.url}")
        self._remember_yahoo_trade_ids(ctx)

    def _checkout_like_url(self, url):
        """真正能改地址/付款的页。/order/status 只是店铺取引ナビ，不算结算。"""
        if self._is_yahoo_login_or_reauth_url(url):
            return False
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

    async def _page_has_no_purchase_data(self, page):
        """未中标、卖家取消、或落札数据未反映：页上写購入データが確認できません。"""
        if page is None:
            return False
        try:
            if page.is_closed():
                return False
        except Exception:
            return False
        try:
            loc = page.locator("text=購入データが確認できません")
            if await self.has_visible(loc):
                return True
        except Exception:
            pass
        try:
            body = await page.locator("body").inner_text()
        except Exception:
            return False
        text = body or ""
        return any(h in text for h in NO_PURCHASE_DATA_HINTS)

    async def _skip_no_purchase_data(self, ctx):
        page = ctx.yahoo_page if ctx else None
        oid = ""
        pid = ""
        acc = ""
        if ctx:
            oid = str(ctx.order.get("order_id") or ctx.order_info.get("order_id") or "")
            pid = str(ctx.order.get("product_id") or ctx.order_info.get("product_id") or "")
            acc = str(ctx.order.get("account_key") or ctx.order_info.get("account_key") or "")
        url = ""
        try:
            url = (page.url if page else "") or ""
        except Exception:
            url = ""
        await self.log(
            f"雅虎无购买数据（可能未中标或卖家取消）。订单 {oid} 商品 {pid} URL={url}",
            "WARNING",
        )
        if oid and hasattr(self, "add_risk_history_entry"):
            self.add_risk_history_entry(oid, "跳过: 雅虎无购买数据（未中标/卖家取消）")
        self.notify_feishu(
            f"订单 {oid} 跳过：雅虎无购买数据（可能未中标或卖家取消）\n"
            f"商品 {pid} 账号 {acc}\n"
            "ページ：購入データが確認できません"
        )
        return deny("SKIPPED_NO_PURCHASE_DATA")

    async def _store_status_page_outcome(self, page):
        if await self._page_has_no_purchase_data(page):
            return "no_purchase_data"
        if await self._page_has_yahoo_id_mismatch(page):
            return "id_mismatch"
        if await self._store_bundle_choice_visible(page):
            return "bundle"
        if await self.has_visible(page.locator(STORE_STATUS_READY_SEL)):
            return "ready"
        return ""

    async def _wait_store_status_outcome(self, page, timeout_ms=WAIT_HANG_MS):
        found = {"v": ""}

        async def done():
            if await self._page_has_no_purchase_data(page):
                found["v"] = "no_purchase_data"
                return True
            if await self._yahoo_system_error_visible(page):
                await self._reload_if_yahoo_system_error(page, max_tries=1)
            found["v"] = await self._store_status_page_outcome(page)
            return bool(found["v"])

        await self.wait_until(done, timeout_ms=timeout_ms)
        return found["v"]

    async def _store_bundle_choice_visible(self, page):
        """店铺取引ナビ把「購入手続きする」换成了まとめて/単品二选一。"""
        loc = page.locator(STORE_BUNDLE_CHOICE_SEL)
        if await self.has_visible(loc):
            return True
        try:
            body = await page.locator("main, #__next, body").first.inner_text()
        except Exception:
            body = ""
        text = body or ""
        return (
            "まとめて購入手続きする" in text
            or "単品で購入手続きする" in text
            or "まとめて購入手続きできる商品" in text
            or "まとめて購入手続き出来る商品" in text
        )

    async def _skip_if_store_bundle_choice(self, ctx):
        """可同捆：不点まとめて、也不点単品，转人工。后台一单 ≠ 雅虎两件一起买。"""
        page = ctx.yahoo_page
        if page is None:
            return None
        if not self._is_store_status_url(page.url or ""):
            return None
        if not await self._store_bundle_choice_visible(page):
            return None
        oid = ctx.order_info.get("order_id") or ctx.order.get("order_id") or ""
        pid = ctx.order_info.get("product_id") or ctx.order.get("product_id") or ""
        await self.log(
            "店铺取引ナビ出现「まとめて購入手続き」（可同捆）。本单不自动点まとめて或単品，转人工。",
            "WARNING",
        )
        self.add_risk_history_entry(oid, "人工处理: 发现同捆提示")
        try:
            self.notify_feishu(
                f"订单 {oid} / 商品 {pid} 店铺取引ナビ出现「まとめて購入手続き」，已跳过转人工。"
            )
        except Exception:
            pass
        return deny("SKIPPED_COMBINED_SHIPPING")

    async def _store_procedure_link(self, page):
        """普通店铺入口：/order/review 或「購入手続きする」。同捆の単品/まとめて不在这里点。"""
        href_link = page.locator("a[href*='/order/review']")
        n = 0
        try:
            n = await href_link.count()
        except Exception:
            n = 0
        for i in range(min(n, 8)):
            loc = href_link.nth(i)
            try:
                if not await loc.is_visible():
                    continue
                href = (await loc.get_attribute("href")) or ""
                if "buyer/payment" in href:
                    continue
                return loc, "購入手続きする"
            except Exception:
                continue
        candidates = page.locator(
            "#pap a:has-text('購入手続きする'), "
            "a:has-text('購入手続きをする'), "
            "button:has-text('購入手続きをする'), "
            "a:has-text('購入手続きする'), "
            "button:has-text('購入手続きする')"
        )
        try:
            n = await candidates.count()
        except Exception:
            n = 0
        for i in range(min(n, 8)):
            loc = candidates.nth(i)
            try:
                if not await loc.is_visible():
                    continue
                text = " ".join(((await loc.inner_text()) or "").split())
                if "まとめて" in text or "単品で" in text:
                    continue
                href = (await loc.get_attribute("href")) or ""
                if "buyer/payment" in href:
                    continue
                return loc, text or "購入手続きする"
            except Exception:
                continue
        return None, ""

    async def _click_store_purchase_procedure(self, ctx):
        """店铺取引ナビ #pap「購入手続きする」→ /order/review。同捆二选一不在这里点。"""
        skipped = await self._skip_if_store_bundle_choice(ctx)
        if skipped:
            return skipped
        page = ctx.yahoo_page
        link, label = await self._store_procedure_link(page)
        if link is None:
            return False
        ctx.is_store = True
        ctx.is_personal = False
        await self.log(f"店铺取引ナビ：发现「{label}」，进入 /order/review")
        await self._goto_purchase_procedure(ctx, link)
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
            outcome = await self._wait_store_status_outcome(yahoo_page)
            if outcome == "no_purchase_data":
                return await self._skip_no_purchase_data(ctx)
            clicked = await self._click_store_purchase_procedure(ctx)
            if isinstance(clicked, dict):
                return clicked
            if clicked:
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
                await yahoo_page.goto(href, wait_until="commit", timeout=WAIT_HANG_MS)
            except Exception as e:
                await self.log(f"店铺取引ナビ打开失败: {e}", "WARNING")
                return False
            if "/order/review" in (yahoo_page.url or "") or "/order/change" in (yahoo_page.url or ""):
                return True
            outcome = await self._wait_store_status_outcome(yahoo_page)
            if outcome == "no_purchase_data":
                return await self._skip_no_purchase_data(ctx)
            if outcome == "id_mismatch":
                await self.log("店铺取引ナビ返回账号不符页，不按店铺处理。", "WARNING")
                return False
            clicked = await self._click_store_purchase_procedure(ctx)
            if isinstance(clicked, dict):
                return clicked
            if clicked:
                return True
            return True

        if not product_id:
            return False

        status_url = YAHOO_STORE_TRADE_STATUS_URL.format(product_id=product_id)
        await self.log(f"尝试店铺取引ナビ: {status_url}")
        try:
            await yahoo_page.goto(status_url, wait_until="commit", timeout=WAIT_HANG_MS)
        except Exception as e:
            await self.log(f"店铺取引ナビ打开超时: {e}", "WARNING")
            return False
        outcome = await self._wait_store_status_outcome(yahoo_page)
        if outcome == "no_purchase_data":
            return await self._skip_no_purchase_data(ctx)
        if outcome == "id_mismatch" or await self._page_has_yahoo_id_mismatch(yahoo_page):
            await self.log("店铺 /order/status 返回账号不符页，不按店铺处理。", "WARNING")
            return False
        clicked = await self._click_store_purchase_procedure(ctx)
        if isinstance(clicked, dict):
            return clicked
        if clicked:
            return True
        await self.log("店铺 /order/status 没有「購入手続きする」，改走个人取引ナビ。")
        return False

    async def _adopt_checkout_tab(self, ctx, timeout=WAIT_HANG_MS):
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
            await self._adopt_checkout_tab(ctx, timeout=WAIT_HANG_MS)
            await self.log(f"单品确认后当前页: {ctx.yahoo_page.url}")
        return None

    async def _visible_action(self, page, selector):
        """当前页已经看得见的按钮，不空等。跳过まとめて/単品で購入。"""
        loc = page.locator(selector)
        try:
            n = await loc.count()
        except Exception:
            return None
        for i in range(min(n, 8)):
            item = loc.nth(i)
            try:
                if not await item.is_visible():
                    continue
                text = " ".join(((await item.inner_text()) or "").split())
                if "まとめて" in text or "単品で購入" in text:
                    continue
                return item
            except Exception:
                continue
        return None

    async def _classify_trade_page(self, page):
        """先看 URL，再看页上已有按钮。不先等一种入口再换另一种。"""
        url = page.url or ""
        if self._is_yahoo_login_or_reauth_url(url):
            return "login"
        if await self._yahoo_login_form_visible(page) and await self._yahoo_login_account_visible(page):
            return "login"
        if await self._page_has_yahoo_id_mismatch(page):
            return "id_mismatch"
        if await self._page_has_no_purchase_data(page):
            return "no_purchase_data"
        if await self._page_is_wait_shipping_address_form(page):
            return "wait_shipping_form"
        if self._checkout_like_url(url):
            return "checkout"
        if self._is_store_status_url(url) or (
            "buy.auctions.yahoo.co.jp" in url and "/order/" in url
        ):
            if await self._store_bundle_choice_visible(page):
                return "store_bundle"
            return "store_status"
        if "contact.auctions.yahoo.co.jp" in url:
            if await self.has_visible(page.locator(PERSONAL_BUNDLE_SEL)):
                return "personal_bundle"
            return "personal_nav"
        if await self.has_visible(page.locator(PERSONAL_BUNDLE_SEL)):
            return "personal_bundle"
        if await self._visible_action(page, PERSONAL_PROCEDURE_SEL):
            return "personal_nav"
        if await self._store_bundle_choice_visible(page):
            return "store_bundle"
        return "unknown"

    async def _after_purchase_entry(self, ctx):
        yahoo_page = ctx.yahoo_page
        if await self._page_is_wait_shipping_address_form(yahoo_page):
            return await self._mark_wait_shipping_form(
                ctx, "购买手续页是待运费地址页：先提交取引情報，卖家再报运费。"
            )
        kantan_btn = yahoo_page.locator(
            "a:has-text('Yahoo!かんたん決済で支払う'), button:has-text('Yahoo!かんたん決済で支払う')"
        )
        if await self.has_visible(kantan_btn):
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
        return None

    async def _open_personal_procedure(self, ctx):
        page = ctx.yahoo_page
        link = await self._visible_action(page, PERSONAL_PROCEDURE_SEL)
        if link is None:
            btn = page.locator(PERSONAL_PROCEDURE_SEL)
            if await self.wait_visible(btn, timeout_ms=WAIT_CLICK_MS):
                link = await self._visible_action(page, PERSONAL_PROCEDURE_SEL)
        if link is None:
            if await self._page_has_no_purchase_data(page):
                return await self._skip_no_purchase_data(ctx)
            await self.log(
                f"个人取引ナビ没有「購入手続きをする」。URL={page.url}",
                "WARNING",
            )
            await self.report_missing_yahoo_ui(
                page, "personal_purchase_procedure", ctx,
                extra="已判定个人取引ナビ，页上没有「購入手続きをする」。",
            )
            return None
        ctx.is_personal = True
        ctx.is_store = False
        await self.log("个人取引ナビ：立即点「購入手続きをする」。")
        await self._goto_purchase_procedure(ctx, link)
        return await self._after_purchase_entry(ctx)

    async def _continue_personal_checkout(self, ctx):
        """看当前是哪一页，立刻走对应处理。"""
        yahoo_page = ctx.yahoo_page
        kind = await self._classify_trade_page(yahoo_page)
        label = TRADE_KIND_LABEL.get(kind, kind)
        await self.log(f"取引后续：当前是{label} {yahoo_page.url}")

        if kind == "login":
            await self.wait_out_yahoo_login_or_reauth(yahoo_page, "取引后续")
            if self._is_yahoo_login_or_reauth_url(ctx.yahoo_page.url):
                return deny("YAHOO_REAUTH")
            return await self._continue_personal_checkout(ctx)

        if kind == "wait_shipping_form":
            return await self._mark_wait_shipping_form(
                ctx, "已在待运费地址页：先提交取引情報，卖家再报运费。"
            )

        if kind == "checkout":
            await self.log(f"已在结算/地址页: {yahoo_page.url}")
            return await self._after_purchase_entry(ctx)

        if kind == "no_purchase_data":
            return await self._skip_no_purchase_data(ctx)

        if kind == "id_mismatch":
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

        if kind == "store_bundle":
            skipped = await self._skip_if_store_bundle_choice(ctx)
            return skipped or deny("SKIPPED_COMBINED_SHIPPING")

        if kind == "store_status":
            ctx.is_store = True
            ctx.is_personal = False
            clicked = await self._click_store_purchase_procedure(ctx)
            if isinstance(clicked, dict):
                return clicked
            if clicked:
                return await self._after_purchase_entry(ctx)
            await self.log("店铺取引ナビ没有「購入手続きする」。", "WARNING")
            return None

        if kind == "personal_bundle":
            choice = await self._dismiss_bundle_choice_page(ctx)
            if choice:
                self.add_risk_history_entry(
                    ctx.order_info.get("order_id"), "人工处理: 发现同捆提示"
                )
                return choice
            return await self._after_purchase_entry(ctx)

        if kind == "personal_nav":
            return await self._open_personal_procedure(ctx)

        await self.log(f"取引后续无法判定页面，不空等。URL={yahoo_page.url}", "WARNING")
        await self.report_missing_yahoo_ui(
            yahoo_page, "personal_purchase_procedure", ctx,
            extra="无法判定是个人还是店铺取引ナビ。",
        )
        return None

    def _parse_item_postage_text(self, text):
        """拍品页 #itemPostage：全国一律 / 大阪府は / 送料無料。"""
        raw = text or ""
        if "着払" in raw:
            return None, "着払"
        m = re.search(r"全国一律[^\d]{0,16}([\d,]+)\s*円", raw)
        if m:
            return int(m.group(1).replace(",", "")), "全国一律"
        m = re.search(r"大阪府は[^\d]{0,12}([\d,]+)\s*円", raw)
        if m:
            return int(m.group(1).replace(",", "")), "大阪府"
        if re.search(r"送料無料|送料[：:]\s*無料", raw):
            return 0, "送料無料"
        m = re.search(r"送料[：:\s]*([\d,]+)\s*円", raw)
        if m:
            return int(m.group(1).replace(",", "")), "送料"
        m = re.search(r"([\d,]+)\s*円（税込）", raw)
        if m:
            return int(m.group(1).replace(",", "")), "税込"
        return None, ""

    async def _capture_auction_page_postage(self, ctx):
        """点取引ナビ之前，拍品页 #itemPostage 已展示送料。"""
        page = ctx.yahoo_page
        if page is None:
            return
        url = page.url or ""
        if "/jp/auction/" not in url and "auctions.yahoo.co.jp" not in url:
            return
        text = ""
        loc = page.locator("#itemPostage")
        try:
            await loc.first.wait_for(state="visible", timeout=8000)
            text = (await loc.first.inner_text()) or ""
        except Exception:
            text = ""
        if not text.strip():
            loc2 = page.locator("dt:has-text('送料')")
            try:
                if await loc2.count():
                    text = (
                        await loc2.first.evaluate(
                            "el => (el.closest('dl,section,div') || el.parentElement || el).innerText || ''"
                        )
                    ) or ""
            except Exception:
                text = ""
        if not text.strip():
            return
        compact = " ".join(text.split())
        fee, how = self._parse_item_postage_text(text)
        if how == "着払":
            await self.log(f"拍品页送料为着払，不在此记入运费: {compact[:80]}")
            return
        if fee is None:
            await self.log(f"拍品页 #itemPostage 未能解析金额: {compact[:100]}", "WARNING")
            return
        ctx.shipping_cost = int(fee)
        ctx.shipping_known = True
        await self.log(f"拍品页运费 ({how}): {fee}円  {compact[:80]}")

    async def step_enter_purchase(self, ctx):
        """一口价 / 今すぐ落札 / 取引ナビ。待运费在此早退。"""
        yahoo_page = ctx.yahoo_page
        order_info = ctx.order_info
        test_mode = ctx.test_mode
        await self._capture_auction_page_postage(ctx)
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
            await self.click_visible(buy_now_btn)
            await self.wait_url(
                yahoo_page,
                r".*(/order/review|/order/select-option|/order/change).*",
                timeout_ms=WAIT_HANG_MS,
            )
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
                            await yahoo_page.wait_for_selector("text=おめでとうございます", timeout=WAIT_HANG_MS)
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
                            await yahoo_page.goto(href, wait_until="commit", timeout=WAIT_HANG_MS)
                            try:
                                await yahoo_page.wait_for_load_state(
                                    "domcontentloaded", timeout=WAIT_HANG_MS
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
                            await yahoo_page.goto(trade_url, wait_until="commit", timeout=WAIT_HANG_MS)
                            try:
                                await yahoo_page.wait_for_load_state(
                                    "domcontentloaded", timeout=WAIT_HANG_MS
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
        """留言后若仍停在取引ナビ，按当前页类型立即处理。"""
        if self._checkout_like_url(ctx.yahoo_page.url):
            return None
        return await self._continue_personal_checkout(ctx)

    async def step_wait_checkout_form(self, ctx):
        yahoo_page = ctx.yahoo_page
        await self._reload_if_yahoo_system_error(yahoo_page)
        form = yahoo_page.locator(
            "#address, h2:has-text('お届け先'), h2:has-text('お届け先住所'), h2:has-text('落札者情報'), "
            "form[name='tradeForm'], input[name='address2'], input[name='home_address2'], #mBoxConfBt"
        )
        if self._checkout_like_url(yahoo_page.url):
            await self.log(f"已进入结算/地址页。等待表单... {yahoo_page.url}")
            await self.wait_visible(form, timeout_ms=WAIT_HANG_MS)
            return None
        try:
            await form.first.wait_for(state="visible", timeout=WAIT_HANG_MS)
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

    def _store_second_confirm_locator(self, yahoo_page):
        """店铺二次确认「購入を確定しますか？」。个人确认页 #fconfirm 也有「購入を確定する」，不能单靠这句话。"""
        return yahoo_page.locator(
            "aside:has-text('購入を確定しますか？') a:has-text('購入を確定する'), "
            "aside:has-text('購入を確定しますか？') button:has-text('購入を確定する'), "
            "#fconfirm:has-text('購入を確定しますか？') a:has-text('購入を確定する'), "
            "#fconfirm:has-text('購入を確定しますか？') button:has-text('購入を確定する')"
        )

    def _second_confirm_locator(self, yahoo_page):
        return yahoo_page.locator(
            "#fconfirm a:has-text('購入を確定する'), "
            "#fconfirm button:has-text('購入を確定する'), "
            "aside:has-text('購入を確定しますか？'), "
            "a:has-text('上記に同意のうえ'), "
            "button:has-text('上記に同意のうえ'), "
            "h3:has-text('購入が完了しました')"
        )

    async def _has_visible_second_confirm(self, yahoo_page):
        return await self.has_visible(self._second_confirm_locator(yahoo_page))

    async def _click_store_purchase_confirm(self, yahoo_page, test_mode, ctx=None):
        """店铺点完「確認する」后的二次确认：「購入を確定しますか？」→「購入を確定する」。"""
        if await self._3ds_visible_on_page(yahoo_page):
            await self._notify_need_3ds(ctx, yahoo_page)
            return True
        buy = self._store_second_confirm_locator(yahoo_page)
        try:
            if not await self.has_visible(buy):
                return False
            await self.log("发现店铺二次确认弹窗「購入を確定しますか？」")
            if test_mode:
                await self.log("[TestMode] 二次确认弹窗已出现，不点「購入を確定する」。")
                return True
            if not await self.click_visible(buy, force=True):
                await self.log("店铺二次确认按钮可见但点击失败。", "WARNING")
                return False
            await self._watch_3ds_after_confirm(yahoo_page, ctx)
            return True
        except Exception as e:
            await self.log(f"店铺二次确认点击失败: {e}", "DEBUG")
            return False

    async def _click_personal_final_confirm(self, yahoo_page, test_mode, ctx=None):
        """个人 /buyer/payment/confirm：「上記に同意のうえ 購入を確定する」。"""
        if await self._3ds_visible_on_page(yahoo_page):
            await self._notify_need_3ds(ctx, yahoo_page)
            return True
        url = yahoo_page.url or ""
        agree = yahoo_page.locator(
            "button:has-text('上記に同意のうえ'):not([disabled]), "
            "a:has-text('上記に同意のうえ'):not([disabled])"
        )
        on_confirm = "/buyer/payment/confirm" in url
        visible = await self.has_visible(agree)
        if not on_confirm and not visible:
            return False
        if not visible:
            return False
        await self.log("发现个人确认页「上記に同意のうえ 購入を確定する」。")
        if test_mode:
            await self.log("[TestMode] 不点最终确定。")
            return True
        if not await self.click_visible(agree):
            await self.log("个人确认按钮可见但点击失败。", "WARNING")
            return False
        await self._watch_3ds_after_confirm(yahoo_page, ctx)
        return True

    async def _watch_3ds_after_confirm(self, page, ctx=None, timeout_ms=15000):
        """点完确定后 3DS 同页弹出，立刻轮询 DOM，不要等 navigation。"""
        await self.log("点完确定，检测是否出现 3DS 验证码（发卡行/邮件 OTP）...")

        async def present():
            return await self._3ds_visible_on_page(page)

        await self.wait_until(present, timeout_ms=timeout_ms, interval=0.2)
        if await self._3ds_visible_on_page(page):
            await self._notify_need_3ds(ctx, page)
            return True
        return False

    async def _await_and_click_second_confirm(self, yahoo_page, test_mode, ctx=None):
        """点完「確認する」后：关掉再登录提示，检测二次确认并点击。"""
        if hasattr(self, "_dismiss_reauth_note"):
            await self._dismiss_reauth_note(yahoo_page)

        syserr_reloads = 0

        async def ready():
            nonlocal syserr_reloads
            if await self._yahoo_system_error_visible(yahoo_page) and syserr_reloads < 3:
                syserr_reloads += 1
                await self._reload_if_yahoo_system_error(yahoo_page, max_tries=1)
                return False
            if await self._cvv_required_on_page(yahoo_page):
                return True
            if await self._3ds_visible_on_page(yahoo_page):
                return True
            url = yahoo_page.url or ""
            if self._is_purchase_complete_url(url):
                return True
            _host, path = yahoo_host_path(url)
            if "/buyer/payment/confirm" in path or "/buyer/payment/complete" in path:
                return True
            if "/order/thank-you" in path:
                return True
            return await self._has_visible_second_confirm(yahoo_page)

        await self.log("点完確認する，检测二次确认弹窗...")
        await self.wait_until(ready, timeout_ms=WAIT_HANG_MS)
        if ctx is not None:
            skipped = await self.gate_need_cvv(ctx)
            if skipped:
                return skipped
        if await self._3ds_visible_on_page(yahoo_page):
            await self._notify_need_3ds(ctx, yahoo_page)
            if ctx is not None:
                ctx.trade_info_submitted = True
            return True
        if test_mode or self._is_purchase_complete_url(yahoo_page.url or ""):
            return False
        clicked = await self._click_store_purchase_confirm(yahoo_page, test_mode, ctx)
        if not clicked:
            clicked = await self._click_personal_final_confirm(yahoo_page, test_mode, ctx)
        if clicked:
            await self.log("已点击二次确认「購入を確定する」。")
            if ctx is not None:
                ctx.trade_info_submitted = True
        else:
            await self.log("未见二次确认弹窗，继续等購入完了。")
        return clicked

    async def _wait_shipping_submit_settled(self, page, ctx):
        """待运费没有購入完了页：確定する之后决定按钮消失或出现送料連絡待ち即可。"""
        if not ctx or not getattr(ctx, "is_wait_for_shipping", False):
            return False
        try:
            loc = page.locator("text=送料連絡待ち")
            if await loc.count() and await loc.first.is_visible():
                return True
        except Exception:
            pass
        if not getattr(ctx, "trade_info_submitted", False):
            return False
        try:
            decide = page.locator("#mBoxConfBt")
            if await decide.count() == 0 or not await decide.first.is_visible():
                return True
        except Exception:
            return True
        return False

    async def _wait_purchase_complete(self, page, timeout_ms=WAIT_HANG_MS, ctx=None):
        """点最终确定后等完成页。匹配到立刻返回，上限 5 分钟。3DS 出现时飞书通知并继续等。"""
        notified_3ds = False
        syserr_reloads = 0
        wait_shipping = bool(
            ctx and getattr(ctx, "is_wait_for_shipping", False)
            and getattr(ctx, "trade_info_submitted", False)
        )
        if wait_shipping:
            timeout_ms = min(int(timeout_ms or WAIT_HANG_MS), WAIT_CLICK_MS * 2)

        async def arrived():
            nonlocal notified_3ds, syserr_reloads
            if await self._yahoo_system_error_visible(page) and syserr_reloads < 3:
                syserr_reloads += 1
                await self._reload_if_yahoo_system_error(page, max_tries=1)
                return False
            if await self._wait_shipping_submit_settled(page, ctx):
                return True
            if await self._cvv_required_on_page(page):
                return True
            if await self._3ds_visible_on_page(page):
                if not notified_3ds:
                    notified_3ds = True
                    await self._notify_need_3ds(ctx, page)
                return False
            url = page.url or ""
            if self._is_yahoo_login_or_reauth_url(url):
                return False
            if self._is_purchase_complete_url(url):
                return True
            _host, path = yahoo_host_path(url)
            if "/order/thank-you" in path or "/order/status" in path:
                return True
            try:
                loc = page.locator(
                    "h3:has-text('購入が完了しました'), h1:has-text('完了'), "
                    "text=購入が完了しました, text=ご購入ありがとうございます"
                )
                if await loc.count() and await loc.first.is_visible():
                    return True
            except Exception:
                pass
            return False

        if wait_shipping:
            await self.log("待运费单已提交取引情報，确认页面收妥（不等購入完了页）...")
        else:
            await self.log("等待购买完成页（最长 5 分钟）...")
        return await self.wait_until(arrived, timeout_ms=timeout_ms)

    def _remember_yahoo_trade_ids(self, ctx, url=None):
        page = ctx.yahoo_page
        u = url or ((page.url if page else "") or "")
        try:
            query = parse_qs(urlparse(u).query)
        except Exception:
            return
        oid = (query.get("oid") or [""])[0]
        if oid:
            ctx.yahoo_oid = oid
        aid = (query.get("aid") or query.get("auctionId") or [""])[0]
        if aid and not (ctx.order.get("product_id") or ctx.order_info.get("product_id")):
            ctx.order["product_id"] = aid
            ctx.order_info["product_id"] = aid

    def _product_id(self, ctx):
        return str(
            ctx.order.get("product_id")
            or ctx.order_info.get("product_id")
            or ""
        ).strip()

    def _order_detail_kind(self, ctx):
        url = (ctx.yahoo_page.url if ctx.yahoo_page else "") or ""
        if ctx.is_store and not ctx.is_personal:
            return "store"
        if ctx.is_personal and not ctx.is_store:
            return "personal"
        host, path = yahoo_host_path(url)
        if "buy.auctions.yahoo.co.jp" in host and "/order/" in path:
            return "store"
        if "contact.auctions.yahoo.co.jp" in host:
            return "personal"
        return "store" if ctx.is_store else "personal"

    def _order_detail_url(self, ctx):
        product_id = self._product_id(ctx)
        kind = self._order_detail_kind(ctx)
        if kind == "store":
            if product_id:
                return YAHOO_STORE_TRADE_STATUS_URL.format(product_id=product_id)
            return ""
        oid = (getattr(ctx, "yahoo_oid", "") or "").strip()
        if product_id and oid:
            return (
                "https://contact.auctions.yahoo.co.jp/trade/top"
                f"?aid={product_id}&oid={oid}"
            )
        url = (ctx.yahoo_page.url if ctx.yahoo_page else "") or ""
        if "/trade/top" in url:
            return url.split("#")[0]
        if product_id:
            return YAHOO_TRADE_TOP_URL.format(product_id=product_id)
        return ""

    def _already_on_order_detail(self, url, ctx):
        if self._is_yahoo_login_or_reauth_url(url):
            return False
        _host, path = yahoo_host_path(url)
        pid = self._product_id(ctx)
        kind = self._order_detail_kind(ctx)
        query = ""
        try:
            query = urlparse(url or "").query
        except Exception:
            query = ""
        if kind == "store":
            return "/order/status" in path and (not pid or pid in query or pid in path)
        return "/trade/top" in path and (not pid or pid in query or pid in path)

    async def _open_personal_info_tab(self, page):
        """个人取引ナビ运费表在「情報」Tab，点击后 DOM 才出现お支払い情報。"""
        info_btn = page.locator("button:has-text('情報')")
        if not await info_btn.count():
            return
        try:
            cls = (await info_btn.first.get_attribute("class")) or ""
            if "current" in cls:
                return
            await info_btn.first.click()
            await asyncio.sleep(0.8)
        except Exception:
            try:
                await info_btn.first.click()
                await asyncio.sleep(0.8)
            except Exception:
                pass

    async def _expand_store_order_detail(self, page):
        """店铺 /order/status 运费在 #dtlinfo，可能要展开「お届け情報・お支払い情報」。"""
        dtl = page.locator("#dtlinfo")
        shown = ""
        try:
            if await dtl.count():
                shown = (await dtl.inner_text()) or ""
        except Exception:
            shown = ""
        if re.search(r"送料", shown):
            return
        toggle = page.locator("text=お届け情報・お支払い情報などを確認する")
        if await toggle.count():
            try:
                await toggle.first.click()
                await asyncio.sleep(0.6)
            except Exception:
                pass

    async def _read_next_data(self, page):
        loc = page.locator("#__NEXT_DATA__")
        try:
            if not await loc.count():
                return None
            raw = await loc.first.inner_text()
            return json.loads(raw) if raw else None
        except Exception:
            return None

    def _trade_info_from_next_data(self, data):
        stack = [data]
        seen = 0
        while stack and seen < 40:
            seen += 1
            cur = stack.pop()
            if not isinstance(cur, dict):
                continue
            trade = cur.get("tradeInfo")
            if isinstance(trade, dict) and (trade.get("paymentInfo") or trade.get("sendInfo")):
                return trade
            top = cur.get("top")
            if isinstance(top, dict) and isinstance(top.get("tradeInfo"), dict):
                return top.get("tradeInfo") or {}
            for key in ("props", "pageProps", "initialState", "initialProps"):
                if isinstance(cur.get(key), dict):
                    stack.append(cur[key])
        return {}

    async def _parse_order_detail_amounts(self, page, ctx):
        shipping = None
        total = None
        item = None
        next_data = await self._read_next_data(page)
        trade = self._trade_info_from_next_data(next_data) if next_data else {}
        pay = trade.get("paymentInfo") or {}
        send = (trade.get("sendInfo") or {}).get("shipMethod") or {}
        shipping = _parse_yen(pay.get("shippingFee") or send.get("fee"))
        total = _parse_yen(pay.get("totalPrice"))
        item = _parse_yen(pay.get("price"))

        chunks = []
        for sel in (
            "#dtlinfo",
            "section:has(h3:has-text('ストアへのお支払い金額'))",
            "section:has(h3:has-text('お支払い情報'))",
            "table:has-text('お支払い情報')",
            "main",
        ):
            loc = page.locator(sel)
            try:
                if await loc.count():
                    chunks.append((await loc.first.inner_text()) or "")
            except Exception:
                continue
        text = "\n".join(chunks)
        if shipping is None:
            ship_m = re.search(r"送料[：:\s]*([\d,]+)\s*円", text)
            if ship_m:
                shipping = int(ship_m.group(1).replace(",", ""))
            elif "送料無料" in text or "送料：無料" in text:
                shipping = 0
        if total is None:
            total_m = re.search(
                r"(?:合計金額（税込）|支払い金額\s*\(税込\)|お支払い金額)[^\d]*([\d,]+)\s*円",
                text,
            )
            if total_m:
                total = int(total_m.group(1).replace(",", ""))
        if item is None:
            item_m = re.search(
                r"(?:商品合計|落札金額|商品金額（税込）|商品金額)[^\d]*([\d,]+)\s*円",
                text,
            )
            if item_m:
                item = int(item_m.group(1).replace(",", ""))
        if total is None and item is not None and shipping is not None:
            total = item + shipping
        return {"shipping": shipping, "total": total, "item": item}

    async def _log_order_fee_banner(self, ctx, kind, url, amounts):
        kind_label = "店铺" if kind == "store" else "个人"
        ship = amounts.get("shipping")
        total = amounts.get("total")
        item = amounts.get("item")
        item_txt = f"{item}円" if item is not None else "-"
        ship_txt = f"{ship}円" if ship is not None else "未读到"
        total_txt = f"{total}円" if total is not None else "-"
        lines = [
            "========================================",
            f"【订单详情运费】{kind_label}卖家",
            f"订单 {ctx.order_id} / 商品 {self._product_id(ctx)}",
            f"详情页 {url}",
            f"商品金额 {item_txt}  运费 {ship_txt}  合计 {total_txt}",
            "========================================",
        ]
        for line in lines:
            await self.log(line, "WARNING")

    async def _yahoo_shows_purchase_complete(self, page):
        """雅虎已经能看到購入完了（完成 URL 或完成文案）。"""
        if page is None:
            return False
        url = page.url or ""
        if self._is_yahoo_login_or_reauth_url(url):
            return False
        if self._is_purchase_complete_url(url):
            return True
        _host, path = yahoo_host_path(url)
        if "/order/thank-you" in path:
            return True
        try:
            loc = page.locator(
                "h3:has-text('購入が完了しました'), "
                "h1:has-text('購入手続きの完了'), "
                "text=購入が完了しました, "
                "text=ご購入ありがとうございます"
            )
            return await loc.count() > 0 and await loc.first.is_visible()
        except Exception:
            return False

    async def _open_order_detail_and_capture_fee(self, ctx):
        """購入完了后拼接订单详情 URL，打开并读取运费，供 QBT 回填。"""
        page = ctx.yahoo_page
        if page is None:
            return
        self._remember_yahoo_trade_ids(ctx)
        target = self._order_detail_url(ctx)
        kind = self._order_detail_kind(ctx)
        kind_label = "店铺" if kind == "store" else "个人"
        if not target:
            await self.log("拼不出订单详情 URL，无法读取运费。", "WARNING")
            return
        await self.log(f"購入完了 → 拼接{kind_label}订单详情: {target}")
        cur = page.url or ""
        if not self._already_on_order_detail(cur, ctx):
            await self.log(f"打开订单详情取运费: {target}")
            try:
                await page.goto(target, wait_until="commit", timeout=WAIT_HANG_MS)
            except Exception as e:
                await self.log(f"打开订单详情超时，继续用当前页解析: {e}", "WARNING")
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=WAIT_HANG_MS)
            except Exception:
                pass
            await self.wait_visible(
                page.locator("h1:has-text('取引ナビ'), #dtlinfo, button:has-text('情報')"),
                timeout_ms=WAIT_HANG_MS,
            )
            await asyncio.sleep(1.2)
        else:
            await self.log(f"已在订单详情页: {cur}")
        if kind == "store":
            await self._expand_store_order_detail(page)
        else:
            await self._open_personal_info_tab(page)
        amounts = await self._parse_order_detail_amounts(page, ctx)
        if amounts.get("shipping") is not None:
            old = int(getattr(ctx, "shipping_cost", 0) or 0)
            ctx.shipping_cost = int(amounts["shipping"])
            ctx.shipping_known = True
            if old and old != ctx.shipping_cost:
                await self.log(f"详情运费 {ctx.shipping_cost}円（拍品页曾记 {old}円）")
        if amounts.get("total"):
            ctx.yahoo_total = int(amounts["total"])
        if amounts.get("shipping") is None:
            if getattr(ctx, "shipping_known", False):
                await self.log(
                    f"订单详情未读到送料，沿用拍品页运费 {ctx.shipping_cost}円 回填 QBT。",
                    "WARNING",
                )
            else:
                await self.log("订单详情页未读到送料，QBT 运费请人工核对。", "WARNING")
        await self._log_order_fee_banner(ctx, kind, page.url or target, amounts)

    async def step_click_pay(self, ctx):
        yahoo_page = ctx.yahoo_page
        test_mode = ctx.test_mode
        skipped = await self.gate_need_cvv(ctx)
        if skipped:
            return skipped
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
        if self._is_yahoo_login_or_reauth_url(yahoo_page.url):
            await self.wait_out_yahoo_login_or_reauth(yahoo_page, "支付前")
            yahoo_page = ctx.yahoo_page
            if self._is_yahoo_login_or_reauth_url(yahoo_page.url):
                await self.log("支付前仍停在登录/钱包验证页，不找确认按钮。", "ERROR")
                return {"status": "YAHOO_REAUTH"}
        if await self._click_personal_final_confirm(yahoo_page, test_mode, ctx):
            if not test_mode:
                ctx.trade_info_submitted = True
                await self._wait_purchase_complete(yahoo_page, ctx=ctx)
                skipped = await self.gate_need_3ds_timeout(ctx)
                if skipped:
                    return skipped
            self._remember_yahoo_trade_ids(ctx)
            return None
        if await self._click_store_purchase_confirm(yahoo_page, test_mode, ctx):
            if not test_mode:
                ctx.trade_info_submitted = True
                await self._wait_purchase_complete(yahoo_page, ctx=ctx)
                skipped = await self.gate_need_3ds_timeout(ctx)
                if skipped:
                    return skipped
            self._remember_yahoo_trade_ids(ctx)
            return None
        classic_confirm = yahoo_page.locator("#mBoxConfBt")
        if await classic_confirm.count() and await classic_confirm.first.is_visible():
            await self.log("发现旧版取引ナビ「決定する」(#mBoxConfBt)")
            if test_mode:
                await self.log("[TestMode] 模拟点击决定（不提交，也不点弹窗「確定する」）...")
                return None
            await self.click_visible(classic_confirm, force=True)
            try:
                await self.wait_visible(
                    yahoo_page.locator("#confSubmitBtn, #mBoxConf"),
                    timeout_ms=WAIT_CLICK_MS * 2,
                )
                confirm = yahoo_page.locator("#confSubmitBtn")
                if await confirm.count() == 0:
                    confirm = yahoo_page.locator(
                        "#mBoxConf button:has-text('確定する'), "
                        "#mBoxConf input[value='確定する']"
                    )
                if await confirm.count():
                    await self.log("确认弹窗：取引情報を確定しますか？ 点击確定する。")
                    await self.click_visible(confirm, force=True)
                    ctx.trade_info_submitted = True
                    try:
                        await confirm.first.wait_for(state="hidden", timeout=WAIT_CLICK_MS * 2)
                    except Exception:
                        pass
                    try:
                        await yahoo_page.wait_for_load_state(
                            "domcontentloaded", timeout=WAIT_CLICK_MS * 2
                        )
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
                    skipped = await self.gate_need_cvv(ctx)
                    if skipped:
                        return skipped
                    if test_mode:
                        await self.log("[TestMode] 模拟点击 (不提交)...")
                        break
                    await self.log("正在点击 (REAL BUY)...")
                    clicked_pay = await self.click_visible(btn)
                    if not clicked_pay:
                        continue
                    try:
                        skipped = await self.gate_need_cvv(ctx)
                        if skipped:
                            return skipped
                        second = await self._await_and_click_second_confirm(
                            yahoo_page, test_mode, ctx
                        )
                        if isinstance(second, dict):
                            return second
                        skipped = await self.gate_need_cvv(ctx)
                        if skipped:
                            return skipped
                        if not test_mode:
                            await self._wait_purchase_complete(yahoo_page, ctx=ctx)
                        skipped = await self.gate_need_cvv(ctx)
                        if skipped:
                            return skipped
                        skipped = await self.gate_need_3ds_timeout(ctx)
                        if skipped:
                            return skipped
                        self._remember_yahoo_trade_ids(ctx)
                    except Exception as e:
                        await self.log(f"二次确认或等待完成失败: {e}")
                    break
            except Exception:
                continue
        else:
            if self._is_yahoo_login_or_reauth_url(yahoo_page.url):
                await self.log("当前是登录页，不按改版报确认按钮缺失。", "WARNING")
            elif not (await classic_confirm.count() and await classic_confirm.first.is_visible()):
                await self.report_missing_yahoo_ui(
                    yahoo_page, "pay_confirm_button", ctx,
                    extra="结算页找不到 #mBoxConfBt / 確認する / 決定する / 購入を確定する。",
                )
        return None

    def _page_shows_purchase_success(self, url, title="", body=""):
        if self._is_yahoo_login_or_reauth_url(url):
            return False
        if self._is_purchase_complete_url(url):
            return True
        _host, path = yahoo_host_path(url)
        if "/order/thank-you" in path:
            return True
        if (title or "").lower().startswith("loading"):
            title = ""
        text = f"{title}\n{body}"
        return any(
            t in text
            for t in (
                "購入が完了しました",
                "ご購入ありがとうございます",
                "購入手続きの完了",
                "支払いが完了しました",
                "支払い完了の連絡を行いました",
            )
        )

    async def step_finish(self, ctx):
        yahoo_page = ctx.yahoo_page
        skipped = await self.gate_need_cvv(ctx)
        if skipped:
            return skipped
        if await self._3ds_visible_on_page(yahoo_page):
            await self._notify_need_3ds(ctx, yahoo_page)
            if not ctx.test_mode:
                await self._wait_purchase_complete(yahoo_page, ctx=ctx)
            skipped = await self.gate_need_3ds_timeout(ctx)
            if skipped:
                return skipped
        if self._is_yahoo_login_or_reauth_url(yahoo_page.url):
            await self.wait_out_yahoo_login_or_reauth(yahoo_page, "购买收尾")
        path = yahoo_host_path(yahoo_page.url or "")[1]
        if not ctx.test_mode and (
            "/buyer/payment/confirm" in path or "/order/review" in path
        ):
            await self.log("仍在购买确认页，检测二次确认...")
            clicked = await self._click_store_purchase_confirm(yahoo_page, False, ctx)
            if not clicked:
                clicked = await self._click_personal_final_confirm(yahoo_page, False, ctx)
            if clicked:
                ctx.trade_info_submitted = True
            await self._wait_purchase_complete(yahoo_page, ctx=ctx)
            skipped = await self.gate_need_3ds_timeout(ctx)
            if skipped:
                return skipped
        elif not ctx.test_mode:
            wait_ms = WAIT_CLICK_MS * 2 if (
                ctx.is_wait_for_shipping and getattr(ctx, "trade_info_submitted", False)
            ) else WAIT_HANG_MS
            await self._wait_purchase_complete(yahoo_page, timeout_ms=wait_ms, ctx=ctx)
            skipped = await self.gate_need_3ds_timeout(ctx)
            if skipped:
                return skipped
        self._remember_yahoo_trade_ids(ctx)

        shows_complete = await self._yahoo_shows_purchase_complete(yahoo_page)
        if (
            not ctx.test_mode
            and not ctx.is_wait_for_shipping
            and shows_complete
        ):
            if self._is_yahoo_login_or_reauth_url(yahoo_page.url):
                await self.wait_out_yahoo_login_or_reauth(yahoo_page, "購入完了后取运费")
            await self.log("雅虎已显示購入完了，拼接订单详情 URL 读取运费，再回填 QBT。")
            await self._open_order_detail_and_capture_fee(ctx)
        elif (
            not ctx.test_mode
            and not ctx.is_wait_for_shipping
            and getattr(ctx, "trade_info_submitted", False)
        ):
            _host, path = yahoo_host_path(yahoo_page.url or "")
            unpaid = any(p in path for p in UNPAID_CHECKOUT_PATHS)
            if not unpaid and not self._is_yahoo_login_or_reauth_url(yahoo_page.url):
                await self.log("已点确认，打开订单详情核对运费。")
                await self._open_order_detail_and_capture_fee(ctx)

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

        is_success = shows_complete or self._page_shows_purchase_success(url, title, body_snippet)
        if is_success:
            await self.log(f"快速检测到成功状态 (URL/Title): {title} {url}")
            await self.trigger_sound("success")
            if not getattr(ctx, "shipping_known", False) and not ctx.test_mode:
                await self.log(
                    "購入完了后仍未读到运费。请人工改 QBT。",
                    "WARNING",
                )

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
