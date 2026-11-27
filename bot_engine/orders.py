"""阶段2：打开后台详情 / 雅虎页。纯判断走 Gate，动作走这里。"""
import asyncio
import os
import re

from playwright.async_api import Page

from .constants import (
    CLOSE_TABS_STATUSES,
    QBT_AUCTION_BUY_URL,
    QBT_BROWSER_ACCOUNT,
    SUCCESS_STATUSES,
    WAIT_CLICK_MS,
    YAHOO_AUCTION_URL,
    YAHOO_HOME_URL,
    YAHOO_STORE_TRADE_STATUS_URL,
    YAHOO_TRADE_TOP_URL,
)
from .exceptions import BotStopped
from .flow import OrderContext, run_chain
from .qbt import parse_storage_code


class OrderMixin:
    async def process_order(self, order, backend_page: Page, test_mode=False):
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

            denied = await self.gate_risk_keywords(ctx)
            if denied:
                result = denied
                return result

            opened = await self.step_open_yahoo(ctx)
            if opened:
                result = opened
                return result

            denied = await run_chain([self.gate_yahoo_login, self.gate_risk_keywords], ctx)
            if denied:
                result = denied
                return result

            ctx.sync_order_info()
            result = await self.execute_yahoo_purchase(
                ctx.yahoo_page, ctx.order_info, test_mode, detail_page=ctx.detail_page
            )
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
                    result = self._attach_order_ids(result, ctx.order)
                    result = await self._ensure_receipt_shot(ctx, result)
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

    def _attach_order_ids(self, result, order):
        payload = dict(result or {})
        src = dict(order or {})
        for key in (
            "order_id",
            "product_id",
            "account_key",
            "bid_email",
            "seller_id",
            "user_name",
            "storage_code",
        ):
            if not payload.get(key):
                payload[key] = src.get(key) or ""
        if not payload.get("receipt_shot_path"):
            payload["receipt_shot_path"] = src.get("receipt_shot_path") or ""
        return payload

    async def _ensure_receipt_shot(self, ctx, result):
        """仅当 QBT 有「上传订单截图」时，单独打开雅虎情報/支付页打码截图。"""
        payload = dict(result or {})
        qbt = getattr(ctx, "detail_page", None)
        needs = False
        try:
            needs = await self._qbt_needs_value_added(qbt)
        except Exception:
            needs = False
        if not needs:
            return payload
        await self.log("QBT 需要上传订单截图，单独打开雅虎情報/支付页截打码图。")
        if not hasattr(self, "_capture_receipt_screenshot"):
            return payload
        try:
            path = await self._capture_receipt_screenshot(ctx)
        except Exception as e:
            await self.log(f"增值服务打码截图失败: {e}", "WARNING")
            path = ""
        if path:
            payload["receipt_shot_path"] = path
            ctx.receipt_shot_path = path
        else:
            await self.log("增值服务未能截到打码图，飞书先发文字。", "WARNING")
        return payload

    async def process_wait_freight_queue(self, backend_page, test_mode=False, run_stats=None):
        """没有可买的一对一时：只处理本机 JSON 里「等待商家确认运费」的单，不扫待确认整表。"""
        pending = [
            x
            for x in (self.wait_shipping_queue or [])
            if str(x.get("status") or "waiting") == "waiting" and x.get("order_id")
        ]
        if not pending:
            return backend_page
        pending.sort(key=lambda x: x.get("last_checked") or "")
        item = pending[0]
        oid = str(item.get("order_id") or "")
        pid = str(item.get("product_id") or "")
        await self.log(
            f"待运费队列 {len(pending)} 笔，本轮检查 {oid} / {pid or '(无商品ID)'}"
        )
        try:
            result, backend_page = await self.process_wait_freight_order(
                item, backend_page, test_mode
            )
        except BotStopped:
            raise
        except Exception as e:
            await self.log(f"待运费队列检查失败 {oid}: {e}", "ERROR")
            self.touch_wait_shipping(oid)
            return backend_page
        status = (result or {}).get("status") or ""
        if status in ("PURCHASED", "TEST_SUCCESS"):
            if run_stats is not None:
                run_stats["total_processed"] = run_stats.get("total_processed", 0) + 1
                await self._record_order_result(
                    run_stats, oid, status, test_mode, gated=False
                )
        elif status in ("WAIT_SHIPPING", "WAIT_SHIPPING_CONTACT"):
            pass
        elif status:
            await self.log(f"待运费队列 {oid} 本轮结论: {status}", "WARNING")
        return backend_page

    async def process_wait_freight_order(self, item, backend_page, test_mode=False):
        """查雅虎是否已报运费；有则填 jpWaybillFee 并点确定。不再点「等待商家确认运费」。"""
        order = {
            "order_id": str(item.get("order_id") or ""),
            "product_id": str(item.get("product_id") or ""),
            "account_key": str(item.get("account_key") or ""),
            "bid_email": str(item.get("bid_email") or ""),
            "seller_id": str(item.get("seller_id") or ""),
            "user_name": str(item.get("user_name") or ""),
            "storage_code": str(item.get("storage_code") or ""),
            "status": "未购买待确认",
            "option_info": "普通快递（有快递单号可追踪）",
            "from_wait_freight": True,
            "is_store": bool(item.get("is_store")),
            "trade_kind": str(item.get("trade_kind") or ""),
            "trade_url": str(item.get("trade_url") or ""),
        }
        ctx = OrderContext(
            order=order,
            backend_page=backend_page,
            test_mode=test_mode,
            from_wait_freight=True,
        )
        result = None
        oid = order["order_id"]
        try:
            if not oid:
                result = {"status": "DETAIL_PAGE_ERROR"}
                return result, backend_page
            if not order.get("product_id"):
                await self.log(f"待运费队列 {oid} 没有商品ID，跳过。", "WARNING")
                self.touch_wait_shipping(oid)
                result = {"status": "PRODUCT_LINK_NOT_FOUND"}
                return result, backend_page
            buy_url = QBT_AUCTION_BUY_URL.format(order_id=oid)
            await backend_page.goto(buy_url, wait_until="domcontentloaded", timeout=30000)
            ctx.detail_page = backend_page
            ctx.is_new_tab = False
            pinned = await self.ensure_qbt_detail_is_this_order(ctx)
            if pinned:
                self.touch_wait_shipping(oid)
                result = pinned
                return result, backend_page
            await self.step_parse_bound_items(ctx)
            opened = await self.step_open_yahoo(ctx)
            if opened:
                self.touch_wait_shipping(oid)
                result = opened
                return result, backend_page
            # 待运费已落札过，不再用拍品页敏感词拦（否则西濃等会每 10 秒重开一页）。
            # 登录页也不要 gate_yahoo_login 把整任务停掉，本轮跳过即可。
            yahoo_url = ""
            try:
                yahoo_url = (ctx.yahoo_page.url if ctx.yahoo_page else "") or ""
            except Exception:
                yahoo_url = ""
            if hasattr(self, "_is_yahoo_login_or_reauth_url") and self._is_yahoo_login_or_reauth_url(
                yahoo_url
            ):
                if hasattr(self, "_try_skip_ghost_yahoo_login"):
                    try:
                        await self._try_skip_ghost_yahoo_login(ctx.yahoo_page)
                    except Exception:
                        pass
                try:
                    yahoo_url = (ctx.yahoo_page.url if ctx.yahoo_page else "") or ""
                except Exception:
                    yahoo_url = ""
                if self._is_yahoo_login_or_reauth_url(yahoo_url):
                    await self.log("待运费复查遇到登录页，本轮跳过，不停止任务。", "WARNING")
                    self.touch_wait_shipping(oid)
                    result = {"status": "YAHOO_REAUTH"}
                    return result, backend_page
            ctx.sync_order_info()
            result = await self.execute_yahoo_purchase(
                ctx.yahoo_page, ctx.order_info, test_mode, detail_page=ctx.detail_page
            )
            result = self._attach_order_ids(result, ctx.order)
            status = (result or {}).get("status") or ""
            if status in ("WAIT_SHIPPING", "WAIT_SHIPPING_CONTACT"):
                await self.log(f"待运费队列: {oid} 卖家尚未回传运费，本轮跳过。")
                self.touch_wait_shipping(oid)
                return result, backend_page
            if status in ("PURCHASED", "TEST_SUCCESS"):
                pinned = await self.ensure_qbt_detail_is_this_order(ctx)
                if pinned:
                    ctx.backfill_blocked = True
                    await self.log(
                        "待运费回填前购买页 URL 不是本单，不点确定。",
                        "ERROR",
                    )
                    return pinned, backend_page
                result = await self._ensure_receipt_shot(ctx, result)
                filled = await self.backfill_order(
                    ctx.detail_page, result, ctx.backend_total, test_mode
                )
                self.remove_wait_shipping(oid)
                if filled == "need_value_added":
                    await self.log(
                        f"待运费队列: {oid} 已回填运费 {result.get('shipping')}，"
                        "有「上传订单截图」，未点确定，转人工增值服务。"
                    )
                else:
                    await self.log(
                        f"待运费队列: {oid} 已回填运费 {result.get('shipping')} 并点确定。"
                    )
                return result, backend_page
            self.touch_wait_shipping(oid)
            return result, backend_page
        except BotStopped:
            raise
        finally:
            try:
                await self.mw_cleanup_after_order(ctx, result)
            except Exception:
                pass
            try:
                if ctx.backend_page and not ctx.backend_page.is_closed():
                    backend_page = ctx.backend_page
            except Exception:
                pass
        return result, backend_page

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
            try:
                order["qbt_page_text"] = await detail_page.locator("body").inner_text()
            except Exception:
                pass
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
        wait_freight = bool(
            getattr(ctx, "from_wait_freight", False)
            or ctx.order.get("from_wait_freight")
        )
        if wait_freight:
            url = self._wait_freight_entry_url(product_id, ctx.order)
            url_hint = url
        else:
            url = YAHOO_AUCTION_URL.format(product_id=product_id)
            url_hint = f"/auction/{product_id}"
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
                yahoo_ctx, url, url_hint=url_hint
            )
            await ctx.yahoo_page.bring_to_front()
            cur = (ctx.yahoo_page.url or "") if ctx.yahoo_page else ""
            if wait_freight and ctx.yahoo_page and url and url.split("#")[0] not in cur.split("#")[0]:
                await self.log(f"待运费入口不是目标页，改开: {url}")
                try:
                    await ctx.yahoo_page.goto(url, wait_until="commit", timeout=WAIT_HANG_MS)
                except Exception as e:
                    await self.log(f"待运费入口打开失败: {e}", "WARNING")
            await self.log(f"已打开雅虎页面: {ctx.yahoo_page.url}")
        except Exception as e:
            await self.log(f"阶段2失败 (读取详情/打开雅虎): {e}", "ERROR")
            return {"status": "DETAIL_PAGE_ERROR"}
        return None

    def _wait_freight_entry_url(self, product_id, order=None):
        """待运费已提交取引情報：个人走 /trade/top，店铺走 /order/status。不要开新版 /top。"""
        info = dict(order or {})
        saved = str(info.get("trade_url") or "").strip()
        if info.get("is_store") or str(info.get("trade_kind") or "") == "store":
            if "buy.auctions.yahoo.co.jp" in saved and "/order/" in saved:
                return saved.split("#")[0]
            return YAHOO_STORE_TRADE_STATUS_URL.format(product_id=product_id)
        if "/trade/top" in saved:
            return YAHOO_TRADE_TOP_URL.format(product_id=product_id)
        if "buy.auctions.yahoo.co.jp" in saved and "/order/" in saved:
            return saved.split("#")[0]
        return YAHOO_TRADE_TOP_URL.format(product_id=product_id)

    def _from_wait_freight(self, ctx):
        return bool(
            getattr(ctx, "from_wait_freight", False)
            or (ctx.order or {}).get("from_wait_freight")
            or (getattr(ctx, "order_info", None) or {}).get("from_wait_freight")
        )

    def _order_tabs_ok_to_close(self, ctx, result):
        if self._from_wait_freight(ctx):
            return True
        if getattr(ctx, "backfill_blocked", False):
            return False
        if not result:
            return False
        return result.get("status") in CLOSE_TABS_STATUSES

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
        if hasattr(self, "_page_url_matches_product") and self._page_url_matches_product(
            url, product_id, product_id
        ):
            return True
        before = getattr(ctx, "yahoo_pages_before", None) or []
        return page not in before

    def _yahoo_context_for_ctx(self, ctx):
        page = getattr(ctx, "yahoo_page", None)
        if page is not None:
            try:
                if not page.is_closed():
                    return page.context
            except Exception:
                pass
        account_key = (ctx.order or {}).get("account_key") or self.account_key_from_email(
            (ctx.order or {}).get("bid_email")
        )
        return self.context_for_account(account_key)

    async def _close_success_yahoo_tabs(self, ctx, keep_one=False):
        context = self._yahoo_context_for_ctx(ctx)
        if not context:
            return
        to_close = []
        for p in list(context.pages):
            if self._yahoo_tab_belongs_to_order(p, ctx):
                to_close.append(p)
        if not to_close:
            return
        if keep_one:
            if to_close:
                keep = to_close.pop(0)
                try:
                    await keep.bring_to_front()
                except Exception:
                    pass
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
            why = "待运费复查完毕" if self._from_wait_freight(ctx) else "本单结束"
            await self.log(f"{why}，已关闭 {closed} 个雅虎标签。")

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
                try:
                    await self._close_success_yahoo_tabs(ctx, keep_one=True)
                except Exception:
                    pass

        if ok:
            try:
                recycle = ctx.backend_page
                reuse_buy = False
                try:
                    origin = None
                    if ctx.detail_page and not ctx.detail_page.is_closed():
                        origin = ctx.detail_page.context
                    elif ctx.backend_page and not ctx.backend_page.is_closed():
                        origin = ctx.backend_page.context
                    list_page = None
                    buy_pages = []
                    if origin:
                        for p in list(origin.pages):
                            try:
                                if p.is_closed():
                                    continue
                                if self._is_qbt_not_buy_list_url(p.url):
                                    list_page = p
                                elif self._is_qbt_auction_buy_url(p.url):
                                    buy_pages.append(p)
                            except Exception:
                                continue
                    if list_page:
                        recycle = list_page
                        for p in buy_pages:
                            try:
                                await p.close()
                            except Exception:
                                continue
                        if buy_pages:
                            await self.log(f"本单成功，已关闭 {len(buy_pages)} 个 QBT 购买页。")
                    elif buy_pages:
                        recycle = buy_pages[0]
                        reuse_buy = True
                        for p in buy_pages[1:]:
                            try:
                                await p.close()
                            except Exception:
                                continue
                except Exception:
                    pass
                if ctx.backend_page or recycle:
                    await self.log("回到未购买列表...")
                    ctx.backend_page = await self.ensure_backend_not_buy_list(
                        recycle or ctx.backend_page,
                        force=True,
                        reuse_buy_page=reuse_buy,
                    )
            except Exception as e:
                await self.log(f"回到未购买列表未完成（下一轮扫描会再打开）: {e}", "WARNING")

    async def mw_cleanup_detail_tab(self, ctx):
        await self.mw_cleanup_after_order(ctx, None)
