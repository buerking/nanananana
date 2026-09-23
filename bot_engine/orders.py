"""阶段2：打开后台详情 / 雅虎页。纯判断走 Gate，动作走这里。"""
import asyncio
import re

from playwright.async_api import Page

from .flow import OrderContext, run_chain


class OrderMixin:
    async def process_order(self, order, backend_page: Page, test_mode=True):
        ctx = OrderContext(order=order, backend_page=backend_page, test_mode=test_mode)
        await self.log(f"开始处理订单 (卖家: {order['seller_id']})")

        denied = await self.gate_blacklist(ctx)
        if denied:
            return denied

        self.ensure_running()
        opened = await self.step_open_detail(ctx)
        if opened:
            return opened

        denied = await self.gate_backend_remark(ctx)
        if denied:
            return denied

        await self.step_parse_bound_items(ctx)

        opened = await self.step_open_yahoo(ctx)
        if opened:
            return opened

        denied = await run_chain([self.gate_yahoo_login, self.gate_risk_keywords], ctx)
        if denied:
            return denied

        ctx.sync_order_info()
        result = await self.execute_yahoo_purchase(ctx.yahoo_page, ctx.order_info, test_mode)
        try:
            await self.backfill_order(ctx.detail_page, result, ctx.backend_total, test_mode)
        except Exception as e:
            await self.log(f"Navigation error after processing: {e}", "ERROR")
        await self.mw_cleanup_detail_tab(ctx)
        return result

    async def step_open_detail(self, ctx):
        backend_page = ctx.backend_page
        purchase_link = ctx.order["purchase_link"]
        pages_before = len(backend_page.context.pages)
        await purchase_link.click()
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
                        if str(order.get("order_id")) != first_id:
                            await self.log(
                                f"同捆订单：将主要 ID 从 {order.get('order_id')} 切换为第一项 ID {first_id}"
                            )
                            order["order_id"] = first_id
                bound_items.append(item_data)
            await self.log(f"后台详情解析: 共 {len(bound_items)} 个商品, 总金额 {backend_total}")
            if option_info:
                await self.log(f"从详情页更新选项信息: {option_info}")
        except Exception as e:
            await self.log(f"提取首项 ID 失败: {e}", "DEBUG")
        ctx.bound_items = bound_items
        ctx.backend_total = backend_total
        return None

    async def step_open_yahoo(self, ctx):
        detail_page = ctx.detail_page
        product_link = detail_page.locator("a.goods_name").first
        if await product_link.count() == 0:
            await self.log("Product link not found!", "ERROR")
            return {"status": "PRODUCT_LINK_NOT_FOUND"}
        try:
            async with detail_page.expect_page() as new_page_info:
                await product_link.click()
            ctx.yahoo_page = await new_page_info.value
            await ctx.yahoo_page.wait_for_load_state("domcontentloaded")
            await self.log(f"已打开雅虎页面: {ctx.yahoo_page.url}")
        except Exception as e:
            await self.log(f"阶段2失败 (读取详情/打开雅虎): {e}", "ERROR")
            return {"status": "DETAIL_PAGE_ERROR"}
        return None

    async def mw_cleanup_detail_tab(self, ctx):
        if not ctx.is_new_tab:
            return
        try:
            if ctx.detail_page and not ctx.detail_page.is_closed():
                await self.log("Cleanup: Closing Backend Detail Page (Tab).")
                await ctx.detail_page.close()
                await ctx.backend_page.goto(self.BACKEND_URL, wait_until="domcontentloaded")
        except Exception:
            pass
