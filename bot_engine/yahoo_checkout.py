"""结算 Pipeline 步骤：地址、配送、投放、信用卡、CVV、待运费地址。"""
import asyncio
import re
from urllib.parse import unquote, urlparse, parse_qs

from playwright.async_api import Page


class YahooCheckoutMixin:
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
            body = ""
            try:
                body = await page.locator("body").inner_text()
            except Exception:
                body = ""
            if "セキュリティコード" in body or "セキュリティ番号" in body or "CVV" in body.upper():
                await self.report_missing_yahoo_ui(
                    page, "cvv_dummy_code", extra="页面提到安全码，但 input[name='dummy_code'] 不存在或不可见。",
                )
        except Exception as e:
            await self.log(f"CVV 填写异常: {e}", "DEBUG")
        return False

    async def _dismiss_reauth_note(self, page: Page):
        """店铺结算「再ログイン認証」挡层，不关掉会挡住改地址/确认。"""
        for _ in range(6):
            try:
                note = page.locator("aside").filter(has_text="再ログイン認証")
                if await note.count() == 0:
                    return
                closer = note.locator("a:has-text('閉じる'), button:has-text('閉じる')")
                if await closer.count() == 0:
                    closer = page.locator("main aside a:has-text('閉じる'), aside a:has-text('閉じる')")
                if await closer.count() == 0:
                    await asyncio.sleep(0.4)
                    continue
                loc = closer.first
                try:
                    visible = await loc.is_visible()
                except Exception:
                    visible = False
                if not visible:
                    await asyncio.sleep(0.4)
                    continue
                await loc.click(timeout=2000, force=True)
                await self.log("已关闭「再ログイン認証」提示。")
                try:
                    await note.first.wait_for(state="hidden", timeout=4000)
                except Exception:
                    pass
                await asyncio.sleep(0.2)
                return
            except Exception as e:
                await self.log(f"关闭再登录提示失败: {e}", "DEBUG")
                return

    def _store_address_change_btn(self, page: Page):
        """店铺 /order/review お届け先旁的「変更」。不要点支付/配送的「変更」。"""
        return page.locator(
            "#address a[data-cl-params*='_cl_link:address'], "
            "#address a:has-text('変更'), "
            "a[data-cl-params*='_cl_link:address']"
        )

    async def _open_store_address_edit(self, page: Page):
        """展示态没有 address2，要点 #address「変更」才进お届け先編集。"""
        btn = self._store_address_change_btn(page)
        if await btn.count() == 0:
            return False
        loc = btn.first
        await self.log("店铺：发现お届け先「変更」。正在点击...")
        try:
            await loc.click(timeout=3000)
        except Exception:
            await loc.click(force=True, timeout=3000)
        try:
            await page.wait_for_selector(
                "input[name='address2'], input[name='home_address2'], "
                "h1:has-text('お届け先編集')",
                timeout=10000,
            )
            return True
        except Exception:
            return "/order/change" in (page.url or "")

    def _is_yahoo_account_address_url(self, url):
        u = url or ""
        return "account.edit.yahoo.co.jp" in u and "address" in u

    def _home_address2_locator(self, page: Page):
        return page.locator(
            "#home_address2, input[name='home_address2'], [data-testid='home_address2']"
        )

    async def _yahoo_account_address_heading_visible(self, page: Page):
        try:
            return await page.locator(
                "h1:has-text('氏名・住所情報'), form[name='address']"
            ).count() > 0
        except Exception:
            return False

    async def _yahoo_account_address_form_ready(self, page: Page):
        try:
            if await self._home_address2_locator(page).count():
                return True
            if await self._yahoo_account_address_heading_visible(page):
                await self._home_address2_locator(page).first.wait_for(
                    state="visible", timeout=10000
                )
                return await self._home_address2_locator(page).count() > 0
        except Exception:
            pass
        return False

    async def _yahoo_account_address_error_visible(self, page: Page):
        if await self._yahoo_account_address_heading_visible(page):
            return False
        try:
            body = await page.locator("body").inner_text()
        except Exception:
            body = ""
        return "エラーが発生しました" in (body or "")

    async def _settle_yahoo_account_address_page(self, page: Page):
        """等 load + Next 水合，不在半成品 DOM 上判断/填写。"""
        for state in ("domcontentloaded", "load"):
            try:
                await page.wait_for_load_state(state, timeout=15000)
            except Exception:
                pass
        try:
            await page.wait_for_function(
                """() => {
                    const t = (document.body && document.body.innerText) || '';
                    const form = document.querySelector(
                        '#home_address2, input[name="home_address2"], form[name="address"]'
                    );
                    const title = document.querySelector('h1');
                    const named = title && title.textContent && title.textContent.includes('氏名・住所情報');
                    const err = t.includes('エラーが発生しました');
                    return !!(form || named || err);
                }""",
                timeout=15000,
            )
        except Exception:
            pass
        # 错误页有时会先闪一下再出表单，给水合留一点时间
        await asyncio.sleep(2)

    async def _reopen_yahoo_account_address_url(self, page: Page):
        """等同地址栏回车：整页重新打开当前 URL，比 reload 更容易过瞬时错误页。"""
        url = page.url or ""
        if not url:
            return
        await self.log("账号地址页按当前地址重新打开（等同回车）...")
        try:
            await page.goto(url, wait_until="commit", timeout=20000)
        except Exception as e:
            await self.log(f"账号地址页重新打开超时: {e}", "WARNING")
        await self._settle_yahoo_account_address_page(page)

    async def _recover_yahoo_account_address_page(self, page: Page):
        """点「編集する」后可能跳到账号地址页。先等加载完，仍报错再用 goto 重开最多 3 次。"""
        if not self._is_yahoo_account_address_url(page.url):
            return False
        await self._settle_yahoo_account_address_page(page)
        for i in range(3):
            heading = await self._yahoo_account_address_heading_visible(page)
            ready = await self._yahoo_account_address_form_ready(page)
            if heading:
                if ready:
                    await self.log("已进入 Yahoo 账号地址页。")
                    return True
                await self.log("账号地址页标题已出，继续等表单，不再重新打开。")
                try:
                    await self._home_address2_locator(page).first.wait_for(
                        state="visible", timeout=10000
                    )
                except Exception:
                    pass
                return True
            if ready:
                await self.log("已进入 Yahoo 账号地址页。")
                return True
            await self.log(f"Yahoo 账号地址页仍异常，重新打开 ({i + 1}/3)...")
            await self._reopen_yahoo_account_address_url(page)
        if await self._yahoo_account_address_form_ready(page) and not await self._yahoo_account_address_error_visible(page):
            await self.log("Yahoo 账号地址页重新打开后表单已出现。")
            return True
        await self.log("Yahoo 账号地址页重新打开 3 次后仍异常。", "WARNING")
        return False

    def _is_checkout_after_address_url(self, url):
        u = url or ""
        if self._is_yahoo_account_address_url(u):
            return False
        return any(
            p in u
            for p in (
                "contact.auctions.yahoo.co.jp",
                "buy.auctions.yahoo.co.jp/order/",
                "/buyer/payment",
                "/order/review",
            )
        )

    def _yahoo_account_back_locator(self, page: Page):
        return page.locator(
            "a:has-text('ご利用中のサービスに戻る'), "
            "button:has-text('ご利用中のサービスに戻る'), "
            "a:has-text('サービスに戻る'), "
            "button:has-text('サービスに戻る'), "
            "a:has-text('オークションに戻る'), "
            "a[href*='contact.auctions.yahoo.co.jp']"
        )

    def _done_url_from_account_address(self, page: Page):
        u = page.url or ""
        try:
            query = parse_qs(urlparse(u).query)
            raw = (query.get(".done") or query.get("done") or [""])[0]
            if raw:
                return unquote(raw)
        except Exception:
            pass
        try:
            m = re.search(r"[?&]\.done=([^&]+)", u)
            if m:
                return unquote(m.group(1))
        except Exception:
            pass
        return ""

    async def _click_visible_account_back(self, page: Page):
        back = self._yahoo_account_back_locator(page)
        try:
            n = await back.count()
        except Exception:
            n = 0
        for i in range(n):
            loc = back.nth(i)
            try:
                if not await loc.is_visible():
                    continue
                await loc.click(timeout=4000)
                return True
            except Exception:
                try:
                    await loc.click(force=True, timeout=3000)
                    return True
                except Exception:
                    continue
        return False

    async def _return_from_yahoo_account_address(self, page: Page):
        """点「登録する」后 Yahoo 常自己跳回结算；「戻る」链接可能在 DOM 里但点不到。"""
        if self._is_checkout_after_address_url(page.url):
            return True
        if not self._is_yahoo_account_address_url(page.url):
            return True
        await self.log("从账号地址页返回结算页...")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 20
        used_done = False
        while loop.time() < deadline:
            if self._is_checkout_after_address_url(page.url):
                await self.log(f"已回到结算页: {page.url}")
                return True
            clicked = await self._click_visible_account_back(page)
            if not clicked and not used_done:
                done = self._done_url_from_account_address(page)
                if done and (
                    "contact.auctions.yahoo.co.jp" in done or "buy.auctions.yahoo.co.jp" in done
                ):
                    used_done = True
                    await self.log(f"未点到返回链接，改打开 .done 结算地址...")
                    try:
                        await page.goto(done, wait_until="commit", timeout=15000)
                    except Exception as e:
                        await self.log(f"打开 .done 超时: {e}", "WARNING")
            try:
                await page.wait_for_url(
                    re.compile(
                        r".*(contact\.auctions\.yahoo\.co\.jp|"
                        r"buy\.auctions\.yahoo\.co\.jp/order/).*"
                    ),
                    timeout=2500,
                )
            except Exception:
                pass
            await asyncio.sleep(0.4)
        if self._is_checkout_after_address_url(page.url):
            await self.log(f"已回到结算页: {page.url}")
            return True
        await self.log(f"账号地址页返回结算未完成，当前: {page.url}", "WARNING")
        return False

    async def _fill_yahoo_account_address_and_return(self, page: Page, suffix):
        """账号地址页只改自宅 building/room（#home_address2），点登録する后回到结算。"""
        ready = await self._recover_yahoo_account_address_page(page)
        if not ready:
            return False
        inp = self._home_address2_locator(page)
        try:
            await inp.first.wait_for(state="visible", timeout=8000)
        except Exception:
            return False
        current = ""
        try:
            current = (await inp.first.input_value()) or ""
        except Exception:
            current = ""
        if suffix not in current.replace("\t", "").replace(" ", ""):
            await inp.first.click()
            await inp.first.fill("")
            await inp.first.fill(suffix)
            await inp.first.blur()
            await self.log(f"账号地址页已填写建物名: {suffix}")
        else:
            await self.log(f"账号地址页建物名已含 '{suffix}'，直接登记返回。")
        save = page.locator("form[name='address'] button[type='submit']")
        if await save.count() == 0:
            save = page.locator("button[type='submit']:has-text('登録する')")
        if await save.count():
            await save.first.wait_for(state="visible", timeout=5000)
            await save.first.click()
            await self.log("账号地址已点「登録する」，等待跳回结算...")
            try:
                await page.wait_for_url(
                    re.compile(
                        r".*(contact\.auctions\.yahoo\.co\.jp|"
                        r"buy\.auctions\.yahoo\.co\.jp/order/).*"
                    ),
                    timeout=12000,
                )
            except Exception:
                pass
        if self._is_checkout_after_address_url(page.url):
            await self.log(f"登録后已在结算页: {page.url}")
            return True
        try:
            await self._return_from_yahoo_account_address(page)
        except Exception as e:
            await self.log(f"返回结算链接点击异常（若已在结算页则继续）: {e}", "WARNING")
        if self._is_checkout_after_address_url(page.url):
            return True
        if not self._is_yahoo_account_address_url(page.url):
            return True
        after = await self._visible_ship_address_text(page)
        return bool(suffix) and suffix in after

    async def _visible_ship_address_text(self, page: Page):
        """个人结算是 #bidinfo「落札者情報」旁的地址文案，店铺才是 #address。"""
        bid = page.locator("#bidinfo")
        try:
            if await bid.count():
                parent = bid.locator("xpath=..")
                text = (await parent.inner_text()) or ""
                if text.strip():
                    return text
        except Exception:
            pass
        for sel in (
            "section:has(#bidinfo)",
            "section:has(h2:has-text('落札者情報'))",
            "#address",
            "section:has(h2:has-text('お届け先'))",
        ):
            loc = page.locator(sel)
            try:
                if await loc.count():
                    text = (await loc.first.inner_text()) or ""
                    if text.strip():
                        return text
            except Exception:
                continue
        inp = page.locator(
            "#home_address2, input[name='address2'], input[name='home_address2']"
        )
        try:
            if await inp.count():
                return (await inp.first.input_value()) or ""
        except Exception:
            pass
        return ""

    async def step_uncheck_newsletter(self, ctx):
        await self._dismiss_reauth_note(ctx.yahoo_page)
        newsletter = ctx.yahoo_page.locator("input[name='mailDeliveryCheckBox']")
        try:
            if await newsletter.count() and await newsletter.first.is_checked():
                await self.log("店铺：取消勾选新闻订阅 (Force/JS)...")
                await ctx.yahoo_page.evaluate(
                    "document.querySelector(\"input[name='mailDeliveryCheckBox']\") && "
                    "document.querySelector(\"input[name='mailDeliveryCheckBox']\").click()"
                )
                await self.log("店铺：已取消勾选新闻订阅。")
        except Exception as e:
            await self.log(f"Store Newsletter Warning: {e}", "DEBUG")
        return None

    async def step_address(self, ctx):
        """地址后缀 = 订单号后5位 + 室。失败时提前结束。"""
        yahoo_page = ctx.yahoo_page
        suffix = ctx.suffix
        order_info = ctx.order_info
        try:
            await self._dismiss_reauth_note(yahoo_page)
            try:
                await yahoo_page.locator(
                    "#address, #bidinfo, h2:has-text('落札者情報'), h2:has-text('お届け先'), "
                    "input[name='address2'], input[name='home_address2']"
                ).first.wait_for(state="visible", timeout=12000)
            except Exception:
                pass
            await self._dismiss_reauth_note(yahoo_page)
            displayed = await self._visible_ship_address_text(yahoo_page)
            addr_inp = yahoo_page.locator("input[name='address2'], input[name='home_address2']")
            already_on_form = await addr_inp.count() > 0
            if suffix in displayed and not already_on_form:
                await self.log(f"地址检查: お届け先已包含正确后缀 '{suffix}'，跳过修改。")
                return None
            if hasattr(self, "_dismiss_bundle_choice_page"):
                choice = await self._dismiss_bundle_choice_page(ctx)
                if choice:
                    self.add_risk_history_entry(
                        order_info.get("order_id"), "人工处理: 发现同捆提示"
                    )
                    return choice
            yahoo_page = ctx.yahoo_page
            if not already_on_form:
                opened = await self._open_store_address_edit(yahoo_page)
                if not opened:
                    edit_btn = yahoo_page.locator(
                        "#bidinfo a:has-text('編集する'), "
                        "a[data-cl-params*='_cl_link:edit'], "
                        "#elAdressEdit, a:has-text('編集する'), input[value='編集する'], "
                        "button:has-text('編集する')"
                    )
                    visible_edit = None
                    for i in range(await edit_btn.count()):
                        loc = edit_btn.nth(i)
                        try:
                            if await loc.is_visible():
                                visible_edit = loc
                                break
                        except Exception:
                            continue
                    if visible_edit is not None:
                        await self.log("点击「編集する」展开地址表单...")
                        try:
                            await yahoo_page.wait_for_load_state("load", timeout=8000)
                        except Exception:
                            pass
                        await visible_edit.click()
                        try:
                            await yahoo_page.wait_for_url(
                                re.compile(r".*account\.edit\.yahoo\.co\.jp.*address.*"),
                                timeout=15000,
                            )
                        except Exception:
                            pass
                        if self._is_yahoo_account_address_url(yahoo_page.url):
                            ok = await self._fill_yahoo_account_address_and_return(
                                yahoo_page, suffix
                            )
                            if ok:
                                after = await self._visible_ship_address_text(yahoo_page)
                                if suffix in after:
                                    await self.log(f"账号地址已修改并回到结算页: {suffix}")
                                else:
                                    await self.log(
                                        f"已从账号地址页返回结算，お届け先展示待核对 '{suffix}'。",
                                        "WARNING",
                                    )
                                return None
                            if self._is_yahoo_account_address_url(yahoo_page.url):
                                await self._return_from_yahoo_account_address(yahoo_page)
                            after = await self._visible_ship_address_text(yahoo_page)
                            if suffix in after:
                                await self.log(
                                    f"账号地址页跳转未确认，但落札者情報已有正确后缀 '{suffix}'，继续结算。"
                                )
                                return None
                            await self.log("账号地址页未能完成修改。", "ERROR")
                            self.add_risk_history_entry(
                                order_info.get("order_id"), "人工处理: Yahoo账号地址页异常"
                            )
                            await self.report_missing_yahoo_ui(
                                yahoo_page, "address_edit", ctx,
                                extra="account.edit.yahoo.co.jp/address 未能改址，且结算页落札者情報也没有正确室号。",
                            )
                            return {"status": "ADDRESS_FAIL"}
            input_found = False
            for i in range(8):
                await self._dismiss_reauth_note(yahoo_page)
                if self._is_yahoo_account_address_url(yahoo_page.url):
                    ok = await self._fill_yahoo_account_address_and_return(yahoo_page, suffix)
                    if ok:
                        return None
                    await self.log("账号地址页未能完成修改。", "ERROR")
                    return {"status": "ADDRESS_FAIL"}
                inp = yahoo_page.locator("input[name='address2'], input[name='home_address2']")
                if await inp.count():
                    await inp.first.fill("")
                    await inp.first.fill(suffix)
                    await inp.first.blur()
                    await self.log(f"已填写ビル名/室号: {suffix}")
                    input_found = True
                    break
                displayed = await self._visible_ship_address_text(yahoo_page)
                if suffix in displayed and await self._store_address_change_btn(yahoo_page).count():
                    await self.log(f"地址检查: お届け先已包含正确后缀 '{suffix}'，跳过修改。")
                    return None
                opened = await self._open_store_address_edit(yahoo_page)
                if opened:
                    continue
                await self.log(f"地址输入框未找到 (第 {i + 1}/8 次)，等待お届け先「変更」...")
                await asyncio.sleep(1)
            if not input_found and hasattr(self, "_continue_personal_checkout"):
                url_now = yahoo_page.url or ""
                still_on_navi = (
                    "trade/top" in url_now
                    or "/order/status" in url_now
                    or await yahoo_page.locator("a[href*='buyer/payment/input']").count()
                    or await yahoo_page.locator("a[href*='/order/review']").count()
                    or await yahoo_page.locator("#pap a:has-text('購入手続き')").count()
                )
                if still_on_navi:
                    await self.log("仍停在取引ナビ，补进购买手续页后再找地址框...")
                    await self._continue_personal_checkout(ctx)
                    yahoo_page = ctx.yahoo_page
                    inp = yahoo_page.locator("input[name='address2'], input[name='home_address2']")
                    for i in range(5):
                        if await inp.count():
                            await inp.first.fill("")
                            await inp.first.fill(suffix)
                            await inp.first.blur()
                            input_found = True
                            break
                        await asyncio.sleep(1)
            if not input_found:
                await self.log(
                    "严重拦截：无法确认个人买家页面的地址准确性。为防止发错货已强制停止。",
                    "ERROR",
                )
                self.add_risk_history_entry(
                    order_info.get("order_id"), "人工处理: 无法修改个人卖家收件地址"
                )
                await self.report_missing_yahoo_ui(
                    yahoo_page, "address_edit", ctx,
                    extra="结算页没有お届け先「変更」/「編集する」，也没有 address2/home_address2。",
                )
                return {"status": "ADDRESS_FAIL"}
            save_btn = yahoo_page.locator(
                "#confirm a:has-text('変更する'), "
                "a[data-cl-params*='_cl_link:update'], "
                "a:has-text('変更する'), button:has-text('変更する'), input[value='変更する'], "
                "button:has-text('登録する')"
            )
            if await save_btn.count():
                await save_btn.first.click()
                try:
                    await yahoo_page.wait_for_selector(
                        "#address, h1:has-text('購入内容の確認')", timeout=10000
                    )
                except Exception:
                    pass
            after = await self._visible_ship_address_text(yahoo_page)
            if suffix in after:
                await self.log(f"店铺地址已修改并验证: {suffix}")
            else:
                await self.log(
                    f"已提交改地址，但お届け先展示里还没看到 '{suffix}'。继续结算。",
                    "WARNING",
                )
        except Exception as e:
            url_now = yahoo_page.url or ""
            if self._is_checkout_after_address_url(url_now):
                await self.log(
                    f"地址步骤异常，但当前已是结算页，继续下单。异常: {e}",
                    "WARNING",
                )
                return None
            if self._is_yahoo_account_address_url(url_now):
                try:
                    await self._return_from_yahoo_account_address(yahoo_page)
                except Exception:
                    pass
                if self._is_checkout_after_address_url(yahoo_page.url):
                    await self.log("地址页返回结算成功，继续下单。", "WARNING")
                    return None
            await self.log(f"个人地址修改失败或异常: {e}", "ERROR")
            self.add_risk_history_entry(order_info.get("order_id"), "人工处理: 个人地址块异常宕机")
            return {"status": "ADDRESS_FAIL"}
        return None

    async def step_shipping(self, ctx):
        """优先可追踪配送，排除到店自提。"""
        yahoo_page = ctx.yahoo_page
        order_info = ctx.order_info
        need_track = ctx.need_track
        if getattr(ctx, "is_wait_for_shipping", False):
            await self.log("待运费页：配送由卖家事后联系，跳过选择配送方式。")
            return None
        try:
            pickup_keywords = ("店頭受取", "店頭受け取り", "来店", "手渡し", "店舗等で受け取る")
            radios = await yahoo_page.locator("input[name='shipMethodName']").all()
            if not radios:
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
                    is_trackable = any(
                        k in p_str
                        for k in ("追跡", "宅配", "宅急便", "ゆうパック", "ゆうパケット", "ネコポス", "匿名配送")
                    )
                    if need_track and not is_trackable:
                        continue
                    candidates.append(
                        {"element": radio, "price": price, "text": p_str[:80], "trackable": is_trackable}
                    )
            if all_options_count > 0 and pickup_options_count == all_options_count:
                await self.log("严重错误：仅有「到店自提(店頭受取)」这一种配送方式。请人工处理。", "ERROR")
                self.add_risk_history_entry(order_info.get("order_id"), "人工处理: 仅支持到店自提")
                await self.report_missing_yahoo_ui(
                    yahoo_page, "store_pickup", ctx,
                    extra="配送选项全部是店頭受取/来店/手渡し，没有可追踪快递。",
                )
                return {"status": "STORE_PICKUP_ONLY_ERROR"}
            if all_options_count == 0 and getattr(ctx, "is_store", False):
                shown = yahoo_page.locator(
                    "#dlvry, "
                    "header:has(h2:has-text('配送方法')), "
                    "section:has(> header h2:has-text('配送方法'))"
                )
                shown_text = ""
                try:
                    if await shown.count():
                        shown_text = (await shown.first.inner_text()) or ""
                except Exception:
                    shown_text = ""
                if not shown_text.strip() and await yahoo_page.locator("h2:has-text('配送方法')").count():
                    try:
                        shown_text = (
                            await yahoo_page.locator("h2:has-text('配送方法')").first.evaluate(
                                "el => (el.closest('section,div') || el.parentElement || el).innerText || ''"
                            )
                        ) or ""
                    except Exception:
                        shown_text = "配送方法"
                if shown_text.strip():
                    compact = " ".join(shown_text.split())
                    await self.log(f"店铺配送为卖家指定展示项（无 radio）: {compact[:100]}")
                    if any(k in shown_text for k in pickup_keywords) and not any(
                        k in shown_text
                        for k in ("ゆうパック", "ゆうパケット", "宅配", "宅急便", "ネコポス", "追跡")
                    ):
                        await self.log("严重错误：展示的配送方式仅为到店自提。请人工处理。", "ERROR")
                        self.add_risk_history_entry(order_info.get("order_id"), "人工处理: 仅支持到店自提")
                        return {"status": "STORE_PICKUP_ONLY_ERROR"}
                    price_m = re.search(r"([\d,]+)円", shown_text)
                    if price_m:
                        ctx.shipping_cost = int(price_m.group(1).replace(",", ""))
                else:
                    await self.report_missing_yahoo_ui(
                        yahoo_page, "shipping_radios", ctx,
                        extra="店铺结算页没有配送 radio，也没有「配送方法」展示块。",
                    )
            if candidates:
                best = sorted(candidates, key=lambda c: (0 if c["trackable"] else 1, c["price"]))[0]
                await best["element"].evaluate(
                    "el => { el.checked = true; el.click(); "
                    "el.dispatchEvent(new Event('change', {bubbles: true})); }"
                )
                ctx.shipping_cost = int(best["price"] or 0)
                await self.log(f"店铺：选择最佳配送: {best['text']} ({best['price']}円)")
            if not int(getattr(ctx, "shipping_cost", 0) or 0):
                await self._capture_displayed_amounts(ctx)
        except Exception as e:
            await self.log(f"店铺配送选择异常: {e}", "DEBUG")
        return None

    async def _capture_displayed_amounts(self, ctx):
        """个人结算页配送是展示文案（おてがる配送 1,400円），没有 radio。"""
        page = ctx.yahoo_page
        chunks = []
        for sel in (
            "section:has(h2:has-text('配送方法'))",
            "#orderPrice",
            "section:has(h2:has-text('ご購入内容詳細'))",
        ):
            loc = page.locator(sel)
            try:
                if await loc.count():
                    chunks.append((await loc.first.inner_text()) or "")
            except Exception:
                continue
        text = "\n".join(chunks)
        if not text.strip():
            try:
                text = await page.locator("main").inner_text()
            except Exception:
                text = ""
        if not int(getattr(ctx, "shipping_cost", 0) or 0):
            ship_m = re.search(r"送料[：:]\s*([\d,]+)\s*円", text)
            if not ship_m:
                ship_m = re.search(r"([\d,]+)\s*円（税込）", text)
            if ship_m:
                ctx.shipping_cost = int(ship_m.group(1).replace(",", ""))
                await self.log(f"结算页展示运费: {ctx.shipping_cost}円")
        if not int(getattr(ctx, "yahoo_total", 0) or 0):
            total_m = re.search(r"お支払い金額[^\d]*([\d,]+)\s*円", text)
            if total_m:
                ctx.yahoo_total = int(total_m.group(1).replace(",", ""))
                await self.log(f"结算页展示合计: {ctx.yahoo_total}円")

    async def step_okihai(self, ctx):
        try:
            okihai_select = ctx.yahoo_page.locator(
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
        return None

    async def step_credit_card(self, ctx):
        yahoo_page = ctx.yahoo_page
        order_info = ctx.order_info
        try:
            body = ctx.body_text or ""
            if not body:
                try:
                    body = await yahoo_page.locator("body").inner_text()
                except Exception:
                    body = ""
            cc_radio = yahoo_page.locator(
                "input[value='card'], input[id*='card'], label:has-text('クレジットカード')"
            )
            has_cc_control = await cc_radio.count() > 0
            if "かんたん決済" in body and not has_cc_control:
                await self.log("支付方式为 Yahoo!かんたん決済（个人取引ナビ），继续。")
                return None
            if has_cc_control:
                is_input = await cc_radio.first.evaluate(
                    "el => el.type === 'radio' || el.type === 'checkbox'"
                )
                if (not await cc_radio.first.is_checked()) if is_input else True:
                    await cc_radio.first.click()
                    await self.log("已成功切换为信用卡。")
            pay_header = yahoo_page.locator("h2:has-text('お支払い方法'), h3:has-text('お支払い方法')")
            if await pay_header.count():
                current_payment_text = await pay_header.first.evaluate(
                    """el => {
                        const heading = (el.innerText || '').trim();
                        let node = el.nextElementSibling;
                        const parts = [heading];
                        while (node && !/^H[1-6]$/i.test(node.tagName || '')) {
                            parts.push(node.innerText || '');
                            node = node.nextElementSibling;
                        }
                        return parts.join('\\n');
                    }"""
                )
                if "かんたん決済" in (current_payment_text or ""):
                    await self.log("支付方式为 Yahoo!かんたん決済（个人取引ナビ），继续。")
                elif "クレジットカード" not in (current_payment_text or ""):
                    await self.log(
                        f"支付方式非信用卡 (检测到: {(current_payment_text or '')[:40]}...)。尝试切换..."
                    )
                    change = pay_header.first.locator(
                        "xpath=following-sibling::*[1]"
                    ).locator("a:has-text('変更する'), button:has-text('変更する')")
                    if await change.count() == 0:
                        change = yahoo_page.locator(
                            "h2:has-text('お支払い方法'), h3:has-text('お支払い方法')"
                        ).locator("xpath=..").locator(
                            "a:has-text('変更する'), button:has-text('変更する')"
                        )
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

    async def handle_personal_address_only(self, yahoo_page: Page, order_info, test_mode=False):
        """待运费：只改地址后早退，仍属主流程分支。"""
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
            addr_inp = yahoo_page.locator("input[name='home_address2'], input[name='address2']")
            form_ready = False
            try:
                form_ready = await addr_inp.count() > 0 and await addr_inp.first.is_visible()
            except Exception:
                form_ready = await addr_inp.count() > 0
            if not form_ready:
                edit_btn = yahoo_page.locator(
                    "a:has-text('編集する'), input[value='編集する'], #elAdressEdit"
                )
                clicked_edit = False
                n = await edit_btn.count()
                for i in range(n):
                    loc = edit_btn.nth(i)
                    try:
                        if await loc.is_visible():
                            await loc.click()
                            clicked_edit = True
                            break
                    except Exception:
                        continue
                if clicked_edit:
                    await yahoo_page.wait_for_selector(
                        "input[name='home_address2'], input[name='address2']"
                    )
            filled = await safe_fill("input[name='home_address2']", suffix)
            if not filled:
                filled = await safe_fill("input[name='address2']", suffix)
            save_clicked = False
            if test_mode:
                await self.log("[TestMode] 待运费地址已填写，不提交「決定する」。")
            else:
                decide = yahoo_page.locator("#mBoxConfBt")
                if await decide.count() and await decide.first.is_visible():
                    await decide.first.click(force=True)
                    save_clicked = True
                else:
                    for sel in (
                        "a:has-text('決定する')",
                        "button:has-text('決定する')",
                        "input[value='決定する']",
                        "button:has-text('登録する')",
                        "input[value='登録する']",
                        "input[value='変更する']",
                    ):
                        btn = yahoo_page.locator(sel)
                        if await btn.count() and await btn.first.is_visible():
                            await btn.first.click(force=True)
                            save_clicked = True
                            break
                if save_clicked:
                    try:
                        confirm = yahoo_page.locator("#confSubmitBtn")
                        await confirm.wait_for(state="visible", timeout=8000)
                        await self.log("确认弹窗：取引情報を確定しますか？ 点击確定する。")
                        await confirm.first.click(force=True)
                    except Exception:
                        pass
                else:
                    await self.log("待运费页未找到「決定する」。", "WARNING")
            await yahoo_page.wait_for_load_state("domcontentloaded")
            body_text = await yahoo_page.locator("body").inner_text()
            if suffix in clean_str(body_text) or suffix in body_text:
                await self.log(f"地址已验证 (待发货): {suffix}")
                return {"status": "WAIT_SHIPPING_CONTACT", "shipping": 0, "total": 0}
            await self.log(f"地址验证失败！后缀 '{suffix}' 未找到。", "ERROR")
            await self.report_missing_yahoo_ui(
                yahoo_page, "wait_shipping_address", extra=f"待运费页填完后正文里没有后缀 {suffix}。",
                order=order_info,
            )
            return {"status": "ADDRESS_FAIL"}
        except Exception as e:
            await self.log(f"地址修改 (待发货) 失败: {e}", "ERROR")
            await self.report_missing_yahoo_ui(
                yahoo_page, "wait_shipping_address", extra=f"待运费改地址异常: {e}",
                order=order_info,
            )
            return {"status": "ADDRESS_FAIL"}
