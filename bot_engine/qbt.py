import re

from playwright.async_api import Page

from .constants import RISK_STATUS_SEARCH_URL


class QbtMixin:
    """阶段1 扫未购买列表 + 阶段4 回填 qbt 后台。"""

    async def find_backend_tab(self):
        """Finds the tab that looks like the internal backend list"""
        if not self.browser_context:
            return None
        for page in self.browser_context.pages:
            url = page.url or ""
            title = ""
            try:
                title = await page.title()
            except Exception:
                title = ""
            if "qbt.jp" in url and "shopAdmin/order" in url:
                return page
            if any(k in title for k in ("未购买", "订单列表", "雅虎拍卖")) and "qbt.jp" in url:
                return page
        return None

    async def check_risk_order_status(self, order_id):
        """
        Check the status of a risk-blocked order in the backend system.
        Returns: "Processed" (if '已购买') or "Unprocessed" (if '未购买' or other)
        """
        if not self.browser_context:
            return {"success": False, "error": "Browser not started"}
        page = None
        try:
            page = await self.browser_context.new_page()
            await page.goto(RISK_STATUS_SEARCH_URL, wait_until="domcontentloaded")
            await page.locator("#id").fill(str(order_id))
            await page.locator("button:has-text('搜索')").click()
            target_row = page.locator(f"tr.data-tr[data-id='{order_id}']")
            try:
                await target_row.wait_for(state="visible", timeout=8000)
            except Exception:
                await self.log(f"Status Check: Order {order_id} not found in list.", "WARNING")
                return {"success": True, "status": "Unprocessed", "detail": "Order Not Found"}
            cols = target_row.locator("td")
            status_text = (await cols.nth(max(await cols.count() - 1, 0)).inner_text()).strip()
            new_status = "Processed" if "已购买" in status_text else "Unprocessed"
            updated = False
            for entry in self.blocked_risk_orders:
                if str(entry.get("order_id")) == str(order_id):
                    entry["status"] = new_status
                    updated = True
            if updated:
                self.save_risk_history()
            return {"success": True, "status": new_status, "detail": status_text}
        except Exception as e:
            await self.log(f"Status check failed: {e}", "ERROR")
            return {"success": False, "error": str(e)}
        finally:
            if page:
                await page.close()

    async def scan_backend_list(self, page: Page, target_order_id=None):
        """Scans the table for processable orders (Stage 1)"""
        await page.bring_to_front()
        orders = []
        rows = await page.locator("table.public_table tr").all()
        scanned_items = []
        for i, row in enumerate(rows):
            self.ensure_running()
            try:
                if await row.locator("th").count() > 0:
                    continue
                purchase_link = row.locator("a:has-text('购买')")
                if await purchase_link.count() == 0:
                    continue
                has_split_btn = await row.locator("text=拆分").count() > 0
                status_cell = row.locator("td:has-text('未购买')")
                status_text_check = ""
                if await status_cell.count() > 0:
                    status_text_check = (await status_cell.first.inner_text()).strip()
                seller_input = row.locator("input.yahoo-store")
                seller_id = "UNKNOWN"
                if await seller_input.count() > 0:
                    seller_id = await seller_input.first.get_attribute("value") or "UNKNOWN"
                order_id = "00000"
                cells = await row.locator("td").all()
                for cell in cells[:4]:
                    id_text = (await cell.inner_text()).strip()
                    id_match = re.search(r"\b(\d{5,8})\b", id_text)
                    if id_match:
                        order_id = id_match.group(1)
                        break
                is_match = (not target_order_id) or (str(target_order_id) == str(order_id))
                if target_order_id and is_match:
                    await self.log(f"🎯 单单测试模式: 匹配到目标订单 {order_id}")
                row_text = (await row.inner_text()).strip()
                onclick_val = await purchase_link.first.get_attribute("onclick") or ""
                match = re.search(r"key_a?(\d+)", onclick_val)
                status_text = "未购买"
                if "待确认" in status_text_check:
                    status_text = "未购买 待确认"
                option_info = "普通快递（有快递单号可追踪）"
                if "普通快递" in row_text or "有快递单号" in row_text or "可追踪" in row_text:
                    option_info = "普通快递（有快递单号可追踪）"
                storage_code = ""
                scanned_items.append(
                    {
                        "row": row,
                        "purchase_link": purchase_link.first,
                        "seller_id": seller_id,
                        "order_id": order_id,
                        "status": status_text,
                        "has_split_btn": has_split_btn,
                        "option_info": option_info,
                        "storage_code": storage_code,
                    }
                )
            except Exception:
                continue

        if target_order_id:
            found_target_bundle = None
            for item in scanned_items:
                if str(item.get("order_id")) == str(target_order_id):
                    found_target_bundle = item
                    break
            if found_target_bundle:
                return [found_target_bundle]
            await self.log(f"单单测试: 未能在扫描结果中找到订单 {target_order_id}。")
            return []

        bundles = {}
        for item in scanned_items:
            key = f"{item.get('seller_id')}_{item.get('storage_code', '')}"
            bundles.setdefault(key, []).append(item)

        process_queue = []
        for key, items in bundles.items():
            if len(items) <= 1:
                process_queue.extend(items)
                continue
            is_manual_bundle = any(itm.get("has_split_btn") for itm in items) and all(
                "待确认" in str(itm.get("status", "")) for itm in items
            )
            valid_in_bundle = [itm for itm in items if "未购买" in str(itm.get("status", ""))]
            if is_manual_bundle and valid_in_bundle:
                main_item = valid_in_bundle[0]
                await self.log(
                    f"发现已确认同捆组 (卖家: {main_item.get('seller_id')}, 用户: {key}, "
                    f"数量: {len(valid_in_bundle)})。优先处理首项 ID: {main_item.get('order_id')}"
                )
                process_queue.append(main_item)
            else:
                bundle_sig = key
                if bundle_sig not in self.warned_bundles:
                    self.warned_bundles.add(bundle_sig)
                    await self.log(
                        f"同捆风控: 发现来自同一卖家 ({items[0].get('seller_id')}) 的 "
                        f"{len(items)} 笔订单，但未识别为‘已确认同捆’(无拆分按钮)。已跳过以防误操作。",
                        "WARNING",
                    )
        return process_queue

    async def backfill_order(self, backend_page: Page, yahoo_result, backend_total=0, test_mode=True):
        await self.log("Stage 4: Backfilling Data to Backend...")
        if not yahoo_result:
            return
        status = yahoo_result.get("status")
        try:
            if status in ("WAIT_SHIPPING", "WAIT_SHIPPING_CONTACT"):
                btn = backend_page.locator(
                    "input[value='等待商家确认运费'], button:has-text('等待商家确认运费'), "
                    "button[onclick='waitBuyerConfirm()']"
                )
                if test_mode:
                    await self.log("测试模式：已填写物流偏好。请手动点击 '等待商家确认运费'。")
                    try:
                        await btn.first.wait_for(state="hidden", timeout=20000)
                        await self.log("测试模式：'等待商家确认运费' 已手动确认。")
                    except Exception:
                        await self.log("测试模式：未检测到手动确认 (Bot 将继续)。", "WARNING")
                elif await btn.count():
                    await self.log("回填：正在点击 '等待商家确认运费'...")
                    await btn.first.evaluate("element => element.click()")
                    await self.log("回填：已点击 '等待商家确认运费'。")
                else:
                    await self.log("回填警告：未找到 '等待商家确认运费' 按钮。", "WARNING")
                return

            if status not in ("PURCHASED", "TEST_SUCCESS"):
                return

            shipping = int(yahoo_result.get("shipping") or 0)
            yahoo_total = int(yahoo_result.get("total") or 0)
            calculated_total = int(backend_total or 0) + shipping
            if yahoo_total and backend_total and calculated_total != yahoo_total:
                await self.log(
                    f"严重错误：金额不匹配! 后台({backend_total}) + 运费({shipping}) != 雅虎({yahoo_total})",
                    "ERROR",
                )
                await self.log("停止回填。转为人工确认。")
                return
            if yahoo_total:
                await self.log(f"金额已验证: {calculated_total} == {yahoo_total}")

            fee_input = backend_page.locator("input.jpWaybillFee")
            if await fee_input.count():
                await fee_input.first.fill(str(shipping))

            is_cod = bool(yahoo_result.get("is_cod"))
            cod_val = "1" if is_cod else "0"
            cod_radio = backend_page.locator(f"input[name='is_cod'][value='{cod_val}']")
            if await cod_radio.count():
                await cod_radio.first.check()
                await self.log(f"回填：设置 COD 为 {'YES' if is_cod else 'NO'}")
            else:
                await self.log("回填：未找到 COD 选项。", "WARNING")

            card_sel = backend_page.locator("select.cardId")
            if await card_sel.count():
                try:
                    await card_sel.first.select_option(self.DEFAULT_CARD_ID)
                except Exception:
                    pass

            if await backend_page.locator("text=上传订单截图").count():
                oid = yahoo_result.get("order_id") or "Unknown"
                self.screenshot_pending_orders.add(str(oid))
                await self.log(f"警告: 订单 {oid} 需要手动上传截图！请人工处理。", "WARNING")
                await self.trigger_sound("attention")

            seller_id = yahoo_result.get("seller_id") or ""
            if seller_id in self.FORCE_SHIPPING_WARNING_SELLERS or yahoo_result.get("shipping_warning"):
                await self.log(f"特殊店铺 {seller_id} -> 强制添加运费变更备注")
                warning_msg = "商家后期可能会变更运费,麻烦留意一下,谢谢。"
                log_area = backend_page.locator("textarea.log_content")
                if await log_area.count():
                    await log_area.first.fill(warning_msg)
                    await self.log("回填：已在操作日志中添加运费变更提示。")
                else:
                    await self.log("回填警告：未找到 '操作日志' 输入框 (textarea.log_content)。", "WARNING")

            confirm_btn = backend_page.locator("button.sure-but-btn, button[onclick='sureClick()']")
            if test_mode:
                await self.log("测试模式：回填表单已填写。请手动点击 '确定'。")
                try:
                    await confirm_btn.first.wait_for(state="hidden", timeout=30000)
                    await self.log("测试模式：回填已手动确认。")
                except Exception:
                    await self.log("测试模式：超时或未检测到手动确认 (Bot 将继续)。", "WARNING")
                return
            if await confirm_btn.count():
                await self.log("回填：找到 '确定' 按钮。尝试点击...")
                try:
                    await confirm_btn.first.evaluate("element => element.click()")
                    await self.log("回填：已触发 JS Click。")
                except Exception:
                    await confirm_btn.first.click(force=True)
                    await self.log("回填：已触发 Force Click。")
                await self.log(f"回填：已填写运费 {shipping}, 订单已确认。")
            else:
                await self.log("回填错误：未找到 '确定' 按钮 (Selector: .sure-but-btn)。", "ERROR")
                await self.save_error_snapshot(backend_page, "backfill_btn_missing")
        except Exception as e:
            await self.log(f"回填错误: {e}", "ERROR")
