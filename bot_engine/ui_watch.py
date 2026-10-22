"""雅虎改版对照：约定元素找不到时存源码、截图、选择器，并飞书通知人工替换。"""
import json
import os
import time
from urllib.parse import urlparse

from playwright.async_api import Page

# 每个检查点 = 一条下单路径上「约定应该出现」的控件。
# 找不到时把 looking_for / selectors 原样发给飞书，方便直接替换 class、id、文本。
YAHOO_CHECKPOINTS = {
    "auction_entry": {
        "page_name": "拍品页",
        "path": "打开 https://auctions.yahoo.co.jp/jp/auction/{商品ID} 后，应能看到进入购买的入口之一",
        "looking_for": "进入购买的入口按钮（店铺「購入手続きへ」/「今すぐ落札」/「取引ナビ」）",
        "selectors": [
            "#bidButtonGroup button:has-text('購入手続きへ')",
            "#bidButtonGroup a:has-text('購入手続きへ')",
            "button:has-text('購入手続きへ')",
            "a:has-text('購入手続きへ')",
            "#bidButtonGroup button:has-text('今すぐ落札')",
            "button:has-text('今すぐ落札')",
            "a:has-text('今すぐ落札')",
            "#closedNotice a:has-text('取引ナビ')",
            "a:has-text('取引ナビ')",
            "button:has-text('取引ナビ')",
        ],
    },
    "store_select_option": {
        "page_name": "店铺规格选择",
        "path": "拍品页点「購入手続きへ」→ /order/select-option「商品オプションを選択してください」→ 不得自动选，转人工建工单联系用户选款式",
        "looking_for": "规格选择页标题/次へ（程序应跳过，不点击）",
        "selectors": [
            "text=商品オプションを選択してください",
            "a[data-cl-params*='_cl_link:next']",
            "a:has-text('次へ')",
            "h1:has-text('購入手続き')",
        ],
    },
    "store_buy_now": {
        "page_name": "店铺拍品页",
        "path": "拍品页 → 点「購入手続きへ」→ 无规格则进 /order/review；有规格则进 /order/select-option 转人工",
        "looking_for": "店铺立即结算按钮「購入手続きへ」",
        "selectors": [
            "#bidButtonGroup button:has-text('購入手続きへ')",
            "#bidButtonGroup a:has-text('購入手続きへ')",
            "button:has-text('購入手続きへ')",
            "a:has-text('購入手続きへ')",
        ],
    },
    "bid_confirm": {
        "page_name": "入札内容の確認",
        "path": "拍品页 → 点「今すぐ落札」→ 入札内容の確認 → 点「落札する」",
        "looking_for": "立即落札确认页标题「入札内容の確認」或提交按钮「落札する」",
        "selectors": [
            "h3:has-text('入札内容の確認')",
            "button:has-text('落札する')",
            "a:has-text('落札する')",
            "input[name='Newsletter']",
        ],
    },
    "bundle_item_rows": {
        "page_name": "同捆商品一览",
        "path": "取引ナビ → 点「まとめて取引する商品を見る」→ 同捆商品列表 → 点「取引ナビに戻る」",
        "looking_for": "同捆商品行（旧版用哈希 class div.sc-bbfc97d-2）",
        "selectors": [
            "div.sc-bbfc97d-2",
            "a:has-text('まとめて取引する商品を見る')",
            "a:has-text('取引ナビに戻る')",
        ],
    },
    "bundle_modal": {
        "page_name": "店铺同捆弹窗",
        "path": "拍品页点「購入手続きへ」之后，可能弹出「まとめて購入手続きができます」",
        "looking_for": "店铺同捆提示文案「まとめて購入手続きができます」",
        "selectors": [
            "text=まとめて購入手続きができます",
        ],
    },
    "personal_purchase_procedure": {
        "page_name": "个人取引ナビ /top 或 /trade/top",
        "path": "已结束拍品页 #closedNotice「取引ナビ」→ https://contact.auctions.yahoo.co.jp/top?aid={商品ID}（旧版 /trade/top）→ 点「購入手続きをする」→ buyer/payment/input",
        "looking_for": "个人卖家「購入手続きをする」（或旧版リンク buyer/payment/input）",
        "selectors": [
            "a[href*='buyer/payment/input']",
            "a:has-text('購入手続きをする')",
            "button:has-text('購入手続きをする')",
            "a:has-text('購入手続きする')",
            "button:has-text('購入手続きする')",
        ],
    },
    "store_won_trade_nav": {
        "page_name": "店铺落札后取引ナビ /order/status",
        "path": "已结束店铺拍品（没有「購入手続きへ」）→ buy.auctions.yahoo.co.jp/order/status?auctionId= → 点「購入手続きする」→ /order/review。若出现「まとめて購入手続きする」/「単品で購入手続きする」，跳过转人工，两按钮都不点。不要打开 contact.auctions /trade/top",
        "looking_for": "店铺取引ナビ「購入手続きする」；同捆二选一则跳过",
        "selectors": [
            "a[href*='/order/review']",
            "#pap a:has-text('購入手続きする')",
            "a:has-text('購入手続きする')",
            "a:has-text('まとめて購入手続きする')",
            "a:has-text('単品で購入手続きする')",
            "#bndl a",
        ],
    },
    "trade_nav_id_mismatch": {
        "page_name": "取引ナビ账号不符",
        "path": "误开 contact.auctions.yahoo.co.jp/trade/top 或登录 ID 不是入札账号时，页面报「取引ナビにアクセスできません」",
        "looking_for": "错误文案「取引ナビにアクセスできません」/「異なるYahoo! JAPAN ID」",
        "selectors": [
            "text=取引ナビにアクセスできません",
            "text=異なるYahoo! JAPAN ID",
        ],
    },
    "checkout_address_form": {
        "page_name": "结算/地址页",
        "path": "店铺：購入手続きへ后的结算页；个人：/top 或 /trade/top 点購入手続きをする后的 buyer/payment，或直接进入 /buyer/edit 落札者情報",
        "looking_for": "收件地址区域（お届け先 / address2 / home_address2 / 落札者情報）",
        "selectors": [
            "#address",
            "#bidinfo",
            "h2:has-text('お届け先')",
            "h2:has-text('お届け先住所')",
            "h2:has-text('落札者情報')",
            "form[name='tradeForm']",
            "input[name='address2']",
            "input[name='home_address2']",
            "#mBoxConfBt",
        ],
    },
    "address_edit": {
        "page_name": "改地址",
        "path": "店铺 /order/review 的 #address 旁「変更」（h2 兄弟，不是 h2 内部）→ お届け先編集 input[name=address2] → #confirm「変更する」；个人仍是「編集する」",
        "looking_for": "地址编辑入口或室号输入框",
        "selectors": [
            "#address a:has-text('変更')",
            "#address a[data-cl-params*='_cl_link:address']",
            "header:has(h2:has-text('お届け先')) a:has-text('変更')",
            "a[data-cl-params*='_cl_link:address']",
            "h1:has-text('お届け先編集')",
            "#confirm a:has-text('変更する')",
            "a[data-cl-params*='_cl_link:update']",
            "#elAdressEdit",
            "#bidinfo a:has-text('編集する')",
            "a[data-cl-params*='_cl_link:edit']",
            "a:has-text('編集する')",
            "input[value='編集する']",
            "button:has-text('編集する')",
            "input[name='address2']",
            "input[name='home_address2']",
            "#home_address2",
            "[data-testid='home_address2']",
            "h1:has-text('氏名・住所情報')",
            "form[name='address'] button[type='submit']",
            "button:has-text('登録する')",
            "a:has-text('ご利用中のサービスに戻る')",
            "button:has-text('ご利用中のサービスに戻る')",
            "a:has-text('サービスに戻る')",
            "a:has-text('オークションに戻る')",
        ],
    },
    "shipping_radios": {
        "page_name": "配送方式",
        "path": "现网店铺 /order/review 多为 #dlvry「配送方法」展示 +「変更」，没有 radio；有 radio 时才选 input[name='shipMethodName']",
        "looking_for": "配送展示块 #dlvry / h2「配送方法」，或可选 radio",
        "selectors": [
            "#dlvry",
            "h2:has-text('配送方法')",
            "section:has(h2:has-text('配送方法'))",
            "#orderPrice",
            "a[data-cl-params*='_cl_link:dlvry']",
            "input[name='shipMethodName']",
        ],
    },
    "cvv_dummy_code": {
        "page_name": "信用卡 CVV",
        "path": "店铺结算页选择クレジットカード后 → 填写 CVV → 确认购买",
        "looking_for": "CVV 输入框 input[name='dummy_code']",
        "selectors": [
            "input[name='dummy_code']",
        ],
    },
    "pay_confirm_button": {
        "page_name": "最终确认",
        "path": "结算页填完地址/配送/支付 → 点確認する。店铺 /order/review 点完后若出现 #fconfirm「購入を確定しますか？」，再点「購入を確定する」。若跳到 login.yahoo.co.jp 且页上有「ログイン/新規取得/携帯電話番号」或「でログイン中です」，先取 .done= 回跳结算，不要当未登录停机；wallet 二次验证仍等人登完。不要把登录页当改版。",
        "looking_for": "最终确认/决定按钮，以及店铺二次确认「購入を確定する」",
        "selectors": [
            "#mBoxConfBt",
            "#confSubmitBtn",
            "#confirm a:has-text('確認する')",
            "a:has-text('確認し購入を進める')",
            "input[value='確認する']",
            "button:has-text('確認する')",
            "input[value='決定する']",
            "button:has-text('決定する')",
            "#fconfirm a:has-text('購入を確定する')",
            "a:has-text('購入を確定する')",
            "button:has-text('購入を確定する')",
            "a:has-text('上記に同意のうえ')",
            "button:has-text('上記に同意のうえ')",
        ],
    },
    "purchase_success_page": {
        "page_name": "购买完成页",
        "path": "点最終确认之后 → 个人 /buyer/payment/complete，店铺 /order/thank-you；随后打开订单详情取运费（店铺 /order/status，个人 /trade/top 点「情報」）",
        "looking_for": "购买成功页特征（URL complete/thank-you 或文案 購入が完了しました）",
        "selectors": [
            "h3:has-text('購入が完了しました')",
            "h1:has-text('完了')",
            "text=購入が完了しました",
            "text=購入完了",
            "text=購入が完了",
            "text=ご購入ありがとうございます",
            "#deal a:has-text('取引内容を確認する')",
            "#dtlinfo",
            "button:has-text('情報')",
            "h3:has-text('ストアへのお支払い金額')",
            "text=送料",
        ],
    },
    "wait_shipping_address": {
        "page_name": "待运费只改地址",
        "path": "点取引后进入 /buyer/edit「取引情報を入力してください」→ 改 address2 → #mBoxConfBt 決定する → #confSubmitBtn 確定する；卖家之后才报运费。已提交后的等待页是「送料連絡待ち」",
        "looking_for": "待运费页的地址输入框",
        "selectors": [
            "form[name='tradeForm']",
            "text=取引情報を入力してください",
            "text=出品者から送料の連絡があります",
            "input[name='home_address2']",
            "input[name='address2']",
            "a:has-text('編集する')",
            "#mBoxConfBt",
            "#confSubmitBtn",
        ],
    },
    "store_pickup": {
        "page_name": "仅到店自提",
        "path": "店铺结算页配送选项全部是店頭受取/来店/手渡し",
        "looking_for": "可追踪快递选项（当前页只有自提）",
        "selectors": [
            "input[name='shipMethodName']",
        ],
    },
}


class YahooUiWatchMixin:
    """约定控件找不到时：活 DOM 事故包 + 飞书。同一检查点每个 path 每轮只报一次。"""

    _HYDRATE_WAIT = (
        "main h1, #address, #pap, #dlvry, #__next h1, "
        "h1:has-text('取引ナビ'), h1:has-text('購入内容の確認'), "
        "text=ご確認ください"
    )

    async def _wait_live_dom(self, page: Page):
        if not page or page.is_closed():
            return
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        try:
            await page.locator(self._HYDRATE_WAIT).first.wait_for(state="visible", timeout=8000)
        except Exception:
            pass

    async def capture_live_incident(self, page: Page, checkpoint_id, ctx=None, extra="", order=None, notify=True):
        """等 SPA 水合后抓 outerHTML / __NEXT_DATA__ / 截图，并更新 LATEST 指针。"""
        spec = YAHOO_CHECKPOINTS.get(checkpoint_id) or {
            "page_name": checkpoint_id,
            "path": "",
            "looking_for": checkpoint_id,
            "selectors": [],
        }
        url = ""
        try:
            url = (page.url or "") if page and not page.is_closed() else ""
        except Exception:
            url = ""
        if order is None and ctx is not None:
            order = getattr(ctx, "order", None) or getattr(ctx, "order_info", None) or {}
        order = order or {}
        order_id = str(order.get("order_id") or "")
        product_id = str(order.get("product_id") or "")
        account_key = str(order.get("account_key") or "")
        stamp = int(time.time())
        safe_ckpt = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(checkpoint_id))
        folder_name = f"{stamp}_{order_id or 'na'}_{safe_ckpt}"
        pack_dir = os.path.join(self.BASE_DIR, "error_snapshots", "incidents", folder_name)
        os.makedirs(pack_dir, exist_ok=True)
        shot_path = os.path.join(pack_dir, "screenshot.png")
        html_path = os.path.join(pack_dir, "dom.outer.html")
        next_path = os.path.join(pack_dir, "next_data.json")
        meta_path = os.path.join(pack_dir, "meta.json")

        await self._wait_live_dom(page)

        outer_html = ""
        next_data = None
        hints = []
        main_text = ""
        if page and not page.is_closed():
            try:
                await page.screenshot(path=shot_path, full_page=True)
            except Exception as e:
                await self.log(f"事故包截图失败: {e}", "WARNING")
                shot_path = ""
            try:
                captured = await page.evaluate(
                    """() => {
                      const root = document.querySelector('main') || document.querySelector('#__next') || document.body;
                      const nodes = (root || document).querySelectorAll(
                        'a, button, input[type=submit], input[type=button], h1, h2, h3'
                      );
                      const out = [];
                      for (const el of nodes) {
                        const t = (el.innerText || el.value || '').trim().replace(/\\s+/g, ' ');
                        if (t && t.length <= 48) out.push(t);
                      }
                      const nd = document.getElementById('__NEXT_DATA__');
                      let nextData = null;
                      if (nd && nd.textContent) {
                        try { nextData = JSON.parse(nd.textContent); } catch (e) { nextData = null; }
                      }
                      return {
                        outer: document.documentElement ? document.documentElement.outerHTML : (document.body && document.body.outerHTML) || '',
                        hints: [...new Set(out)].slice(0, 50),
                        mainText: ((root && root.innerText) || '').slice(0, 4000),
                        nextData: nextData
                      };
                    }"""
                )
                outer_html = (captured or {}).get("outer") or ""
                hints = (captured or {}).get("hints") or []
                main_text = (captured or {}).get("mainText") or ""
                next_data = (captured or {}).get("nextData")
            except Exception as e:
                await self.log(f"事故包 DOM 提取失败: {e}", "WARNING")
                try:
                    outer_html = await page.content()
                except Exception:
                    outer_html = ""
            if outer_html:
                try:
                    with open(html_path, "w", encoding="utf-8") as f:
                        f.write(outer_html)
                except Exception as e:
                    await self.log(f"事故包 HTML 失败: {e}", "WARNING")
                    html_path = ""
            else:
                html_path = ""
            if next_data is not None:
                try:
                    with open(next_path, "w", encoding="utf-8") as f:
                        json.dump(next_data, f, ensure_ascii=False, indent=2)
                except Exception:
                    next_path = ""
            else:
                next_path = ""

        payload = {
            "checkpoint": checkpoint_id,
            "page_name": spec.get("page_name"),
            "path": spec.get("path"),
            "looking_for": spec.get("looking_for"),
            "old_selectors": spec.get("selectors") or [],
            "url": url,
            "order_id": order_id,
            "product_id": product_id,
            "account_key": account_key,
            "extra": extra or "",
            "visible_texts": hints,
            "main_text_preview": main_text[:1500],
            "screenshot": shot_path,
            "html": html_path,
            "next_data": next_path,
            "incident_dir": pack_dir,
            "replace_hint": "请在本仓库对 Cursor 说：修最新失败",
        }
        try:
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            await self.log(f"事故包 meta 失败: {e}", "WARNING")
            meta_path = ""
        latest_path = os.path.join(self.BASE_DIR, "error_snapshots", "LATEST")
        try:
            with open(latest_path, "w", encoding="utf-8") as f:
                f.write(pack_dir)
        except Exception as e:
            await self.log(f"写入 LATEST 失败: {e}", "WARNING")
        payload["meta"] = meta_path
        payload["latest"] = latest_path
        if notify:
            selector_lines = "\n".join(f"  - {s}" for s in (spec.get("selectors") or []))
            hint_preview = "、".join(hints[:12]) if hints else "(未能提取)"
            text = (
                "【雅虎页面改版】约定元素没找到，需要人工对照\n"
                f"检查点: {checkpoint_id}\n"
                f"页面: {spec.get('page_name')}\n"
                f"路径: {spec.get('path')}\n"
                f"找不到: {spec.get('looking_for')}\n"
                f"旧选择器:\n{selector_lines or '  (无)'}\n"
                f"当前URL: {url}\n"
                f"订单: {order_id or '-'} / 商品: {product_id or '-'}\n"
                f"{('说明: ' + extra + chr(10)) if extra else ''}"
                f"结算区可见按钮/标题: {hint_preview}\n"
                f"事故目录: {pack_dir}\n"
                f"截图: {shot_path or '(失败)'}\n"
                f"活DOM: {html_path or '(失败)'}\n"
                f"对照表: {meta_path or '(失败)'}\n"
                "请在本仓库对 Cursor 说：修最新失败\n"
                "（不要在群里回复 class，由 Cursor 读 DOM 修改选择器）"
            )
            await self.log(text, "ERROR")
            self.notify_feishu(text)
            await self.trigger_sound("attention")
        else:
            await self.log(f"事故包已保存: {pack_dir}", "ERROR")
        return payload

    async def report_missing_yahoo_ui(self, page: Page, checkpoint_id, ctx=None, extra="", order=None):
        url = ""
        try:
            url = (page.url or "") if page and not page.is_closed() else ""
        except Exception:
            url = ""
        path_key = urlparse(url).path if url else ""
        dedupe = f"{checkpoint_id}:{path_key}"
        reported = getattr(self, "ui_miss_reported", None)
        if reported is None:
            self.ui_miss_reported = set()
            reported = self.ui_miss_reported
        if dedupe in reported:
            await self.log(f"雅虎改版对照已报过，跳过重复: {dedupe}", "DEBUG")
            return None
        reported.add(dedupe)
        return await self.capture_live_incident(
            page, checkpoint_id, ctx=ctx, extra=extra, order=order, notify=True
        )
