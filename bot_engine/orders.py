"""阶段2：打开后台详情 / 雅虎页。纯判断走 Gate，动作走这里。"""
import asyncio
import re

from playwright.async_api import Page

from .constants import (
    QBT_BROWSER_ACCOUNT,
    SUCCESS_STATUSES,
    WAIT_CLICK_MS,
    YAHOO_AUCTION_URL,
    YAHOO_HOME_URL,
)
from .flow import OrderContext, run_chain
from .qbt import parse_storage_code


class OrderMixin:
    async def process_order(self, order, backend_page: Page, test_mode=True):
        """队列里每一项都当「一笔雅虎拍品」跑完：开 QBT 详情 → 打开该行 product_id → 下单 → 回填。

        入队规则见 filter_buyable_orders：仅一对一。雅虎页开在对应出价账号的 Chrome。
        """
        ctx = OrderContext(order=order, backend_page=backend_page, test_mode=test_mode)
        result = None
        await self.log(
            f"开始处理订单 {order.get('order_id')} "
            f"(卖家: {order.get('seller_id')}, 账号: {order.get('account_key') or '?'}, "
            f"用户: {order.get('user_name') or '?'})"
        )
        try:
            denied = await self.gate_blacklist(ctx)
            if denied:
                result = denied
                return result

            self.ensure_running()
            opened = await self.step_open_detail(ctx)
            if opened:
                result = opened
                return result

            pinned = await self.ensure_qbt_detail_is_this_order(ctx)
            if pinned:
                result = pinned
                return result

            denied = await self.gate_backend_remark(ctx)
            if denied:
                result = denied
                return result

            await self.step_parse_bound_items(ctx)

            opened = await self.step_open_yahoo(ctx)
            if opened:
                result = opened
                return result

            denied = await run_chain([self.gate_yahoo_login, self.gate_risk_keywords], ctx)
            if denied:
                result = denied
                return result

            ctx.sync_order_info()
            result = await self.execute_yahoo_purchase(ctx.yahoo_page, ctx.order_info, test_mode)
            try:
                if result and (result.get("status") in SUCCESS_STATUSES):
                    await self._log_suffix_check(ctx)
            except Exception as e:
                await self.log(f"室号核对失败（不影响结论）: {e}", "DEBUG")
            try:
                pinned = await self.ensure_qbt_detail_is_this_order(ctx)
                if pinned:
                    ctx.backfill_blocked = True
                    await self.log(
                        "回填前购买页 URL 不是本单，不点确定，避免更新错单。",
                        "ERROR",
                    )
                else:
                    await self.backfill_order(
                        ctx.detail_page, result, ctx.backend_total, test_mode
                    )
            except Exception as e:
                ctx.backfill_blocked = True
                await self.log(f"Navigation error after processing: {e}", "ERROR")
            return result
        finally:
            try:
                await self.mw_cleanup_after_order(ctx, result)
            except Exception:
                pass

    async def _log_suffix_check(self, ctx):
        suffix = ctx.suffix
        page = ctx.yahoo_page
        if not page or page.is_closed():
            await self.log(f"室号核对: 雅虎页已关闭，无法确认 '{suffix}'。", "WARNING")
            return
        shown = ""
        if hasattr(self, "_visible_ship_address_text"):
            shown = await self._visible_ship_address_text(page)
        if not shown:
            try:
                shown = await page.locator("main, #address, #__next").first.inner_text()
            except Exception:
                shown = ""
        if suffix and suffix in (shown or ""):
            await self.log(f"室号核对: 结算页仍能看到 '{suffix}'。")
        else:
            await self.log(
                f"室号核对: 未能在展示区读到 '{suffix}'（不因此判失败，请人工看一眼）。",
                "WARNING",
            )

    async def _qbt_click_buy_for_order(self, page: Page, oid: str):
        """后台「购买」是 onclick=goBuy('key_a724235')，按订单号调 JS，不依赖可见性。"""
        oid = str(oid or "").strip()
        if not oid:
            return False
        try:
            await page.locator(
                f"table.public_table input[value='{oid}']"
            ).first.wait_for(state="attached", timeout=8000)
        except Exception:
            return False
        try:
            result = await page.evaluate(
                """(oid) => {
                  const inputs = document.querySelectorAll('table.public_table input');
                  let key = null;
                  for (const inp of inputs) {
                    if (String(inp.value || '') !== String(oid)) continue;
                    const cls = [...inp.classList].find((c) => c.startsWith('key_'));
                    if (cls) { key = cls; break; }
                  }
                  if (key && typeof goBuy === 'function') {
                    goBuy(key);
                    return {ok: true, how: 'gobuy', key};
                  }
                  const links = document.querySelectorAll('table.public_table a');
                  for (const a of links) {
                    const oc = a.getAttribute('onclick') || '';
                    const text = (a.textContent || '').trim();
                    if (oc.includes(oid) && text.includes('购买')) {
                      a.click();
                      return {ok: true, how: 'anchor', key: oc};
                    }
                  }
                  return {ok: false, how: 'none', key};
                }""",
                oid,
            )
        except Exception as e:
            await self.log(f"调用后台 goBuy({oid}) 失败: {e}", "WARNING")
            return False
        if not result or not result.get("ok"):
            await self.log(
                f"列表里没有订单 {oid} 的 goBuy 入口: {result}",
                "WARNING",
            )
            return False
        await self.log(
            f"已触发后台购买: {oid} via {result.get('how')} {result.get('key')}"
        )
        return True

    async def step_open_detail(self, ctx):
        backend_page = ctx.backend_page
        oid = str(ctx.order.get("order_id") or "").strip()
        if hasattr(self, "ensure_backend_not_buy_list"):
            backend_page = await self.ensure_backend_not_buy_list(backend_page)
            ctx.backend_page = backend_page
        pages_before = len(backend_page.context.pages)
        opened = await self._qbt_click_buy_for_order(backend_page, oid)
        if not opened and hasattr(self, "ensure_backend_not_buy_list"):
            backend_page = await self.ensure_backend_not_buy_list(
                backend_page, force=True
            )
            ctx.backend_page = backend_page
            pages_before = len(backend_page.context.pages)
            opened = await self._qbt_click_buy_for_order(backend_page, oid)
        if not opened:
            await self.log(
                f"未购买列表找不到订单 {oid} 的「购买」入口。",
                "ERROR",
            )
            return {"status": "DETAIL_PAGE_ERROR"}
        await asyncio.sleep(1)
        pages_after = len(backend_page.context.pages)
        ctx.detail_page = backend_page
        ctx.is_new_tab = False
        if pages_after > pages_before:
            ctx.detail_page = backend_page.context.pages[-1]
            await ctx.detail_page.wait_for_load_state("domcontentloaded")
            ctx.is_new_tab = True
        try:
            await ctx.detail_page.wait_for_selector(".active_form", timeout=15000)
            await self.log("已进入后台详情页")
        except Exception as e:
            await self.log(f"阶段2失败 (读取详情/打开雅虎): {e}", "ERROR")
            return {"status": "DETAIL_PAGE_ERROR"}
        return None

    async def step_parse_bound_items(self, ctx):
        detail_page = ctx.detail_page
        order = ctx.order
        option_info = order.get("option_info")
        bound_items = []
        backend_total = 0
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
                        started = str(order.get("order_id") or "")
                        if started and first_id != started:
                            await self.log(
                                f"同捆详情第一项是 {first_id}，本单仍按 {started} "
                                "改室号并回填，不切换主 ID。"
                            )
                bound_items.append(item_data)
            if detail_rows:
                try:
                    info_text = await detail_rows[0].locator("td").nth(1).inner_text()
                    code = parse_storage_code(info_text)
                    if code:
                        order["storage_code"] = code
                        if not order.get("user_name"):
                            order["user_name"] = code
                        await self.log(f"入库码: {code}")
                except Exception:
                    pass
            await self.log(f"后台详情解析: 共 {len(bound_items)} 个商品, 总金额 {backend_total}")
            if option_info:
                await self.log(f"从详情页更新选项信息: {option_info}")
        except Exception as e:
            await self.log(f"提取首项 ID 失败: {e}", "DEBUG")
        ctx.bound_items = bound_items
        ctx.backend_total = backend_total
        return None

    async def step_open_yahoo(self, ctx):
        product_id = (ctx.order.get("product_id") or "").strip()
        if not product_id:
            link = ctx.detail_page.locator("a[href*='/auction/'], a.goods_name").first
            if await link.count():
                href = (await link.get_attribute("href")) or ""
                pid_match = re.search(r"/auction/([a-zA-Z]\d+)", href)
                if pid_match:
                    product_id = pid_match.group(1)
                    ctx.order["product_id"] = product_id
        if not product_id:
            await self.log("Product link not found!", "ERROR")
            return {"status": "PRODUCT_LINK_NOT_FOUND"}
        url = YAHOO_AUCTION_URL.format(product_id=product_id)
        account_key = ctx.order.get("account_key") or self.account_key_from_email(
            ctx.order.get("bid_email")
        )
        yahoo_ctx = self.context_for_account(account_key)
        if not yahoo_ctx:
            await self.log(
                f"没有对应的雅虎 Chrome: account={account_key or '(空)'} "
                f"email={ctx.order.get('bid_email') or '(空)'}",
                "ERROR",
            )
            return {"status": "NO_YAHOO_BROWSER"}
        try:
            await self.log(f"切换雅虎浏览器 [{account_key or QBT_BROWSER_ACCOUNT}] 打开: {url}")
            ctx.yahoo_pages_before = [
                p for p in list(yahoo_ctx.pages) if not p.is_closed()
            ]
            ctx.yahoo_page = await self.reuse_or_goto(
                yahoo_ctx, url, url_hint=f"/auction/{product_id}"
            )
            await ctx.yahoo_page.bring_to_front()
            await self.log(f"已打开雅虎页面: {ctx.yahoo_page.url}")
        except Exception as e:
            await self.log(f"阶段2失败 (读取详情/打开雅虎): {e}", "ERROR")
            return {"status": "DETAIL_PAGE_ERROR"}
        return None

    def _order_tabs_ok_to_close(self, ctx, result):
        if getattr(ctx, "backfill_blocked", False):
            return False
        if not result:
            return False
        return result.get("status") in SUCCESS_STATUSES

    def _yahoo_tab_belongs_to_order(self, page, ctx):
        try:
            if page is None or page.is_closed():
                return False
        except Exception:
            return False
        url = (page.url or "").lower()
        if "qbt.jp" in url:
            return False
        product_id = (ctx.order.get("product_id") or "").strip().lower()
        if product_id and (
            f"/auction/{product_id}" in url
            or f"aid={product_id}" in url
            or f"auctionid={product_id}" in url
        ):
            return True
        before = getattr(ctx, "yahoo_pages_before", None) or []
        return page not in before

    async def _close_success_yahoo_tabs(self, ctx):
        page = ctx.yahoo_page
        if page is None:
            return
        try:
            context = page.context
        except Exception:
            return
        to_close = []
        for p in list(context.pages):
            if self._yahoo_tab_belongs_to_order(p, ctx):
                to_close.append(p)
        if not to_close:
            return
        live = []
        for p in list(context.pages):
            try:
                if not p.is_closed() and p not in to_close:
                    live.append(p)
            except Exception:
                continue
        if not live:
            keep = to_close.pop()
            try:
                await keep.goto(YAHOO_HOME_URL, wait_until="commit", timeout=WAIT_CLICK_MS * 2)
                await self.log("本单成功，雅虎窗口只留首页，避免关掉 Chrome。")
            except Exception as e:
                await self.log(f"雅虎首页保留失败，改留当前标签: {e}", "DEBUG")
                to_close.append(keep)
        closed = 0
        for p in to_close:
            try:
                if p.is_closed():
                    continue
                await p.close()
                closed += 1
            except Exception:
                continue
        if closed:
            await self.log(f"本单成功，已关闭 {closed} 个雅虎标签。")

    async def mw_cleanup_after_order(self, ctx, result):
        ok = self._order_tabs_ok_to_close(ctx, result)
        if ok:
            await self._close_success_yahoo_tabs(ctx)
            if ctx.is_new_tab:
                try:
                    if ctx.detail_page and not ctx.detail_page.is_closed():
                        await self.log("本单成功，已关闭 QBT 详情标签。")
                        await ctx.detail_page.close()
                except Exception:
                    pass
        else:
            keep_yahoo = False
            keep_detail = False
            try:
                keep_yahoo = ctx.yahoo_page is not None and not ctx.yahoo_page.is_closed()
            except Exception:
                keep_yahoo = True
            try:
                keep_detail = ctx.detail_page is not None and not ctx.detail_page.is_closed()
            except Exception:
                keep_detail = True
            if keep_yahoo or keep_detail:
                status = (result or {}).get("status") if result else "无结果"
                await self.log(f"本单有问题 ({status})，保留标签供人工审查。")

        if ok:
            try:
                if ctx.backend_page and not ctx.backend_page.is_closed():
                    await self.log("回到未购买列表...")
                    ctx.backend_page = await self.ensure_backend_not_buy_list(
                        ctx.backend_page, force=True
                    )
            except Exception as e:
                await self.log(f"回到未购买列表未完成（下一轮扫描会再打开）: {e}", "WARNING")

    async def mw_cleanup_detail_tab(self, ctx):
        await self.mw_cleanup_after_order(ctx, None)
