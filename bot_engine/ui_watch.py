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
    "store_buy_now": {
        "page_name": "店铺拍品页",
        "path": "拍品页 → 点「購入手続きへ」→ 店铺结算页",
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
    "checkout_address_form": {
        "page_name": "结算/地址页",
        "path": "店铺：購入手続きへ后的结算页；个人：/top 或 /trade/top 点購入手続きをする后的 buyer/payment 或落札者情報",
        "looking_for": "收件地址区域（お届け先 / address2 / home_address2 / 落札者情報）",
        "selectors": [
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
            "header:has(h2:has-text('お届け先')) a:has-text('変更')",
            "a[data-cl-params*='_cl_link:address']",
            "h1:has-text('お届け先編集')",
            "#confirm a:has-text('変更する')",
            "a[data-cl-params*='_cl_link:update']",
            "#elAdressEdit",
            "a:has-text('編集する')",
            "input[value='編集する']",
            "button:has-text('編集する')",
            "input[name='address2']",
            "input[name='home_address2']",
        ],
    },
    "shipping_radios": {
        "page_name": "配送方式",
        "path": "店铺结算页 → 选择配送 radio（优先ゆうパケット等可追踪）",
        "looking_for": "配送方式选项 input[name='shipMethodName']",
        "selectors": [
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
        "path": "结算页填完地址/配送/支付 → 点確認する/確認し購入を進める → 店铺弹 #fconfirm「購入を確定する」；旧版个人待运费是 #mBoxConfBt 再弹 #confSubmitBtn",
        "looking_for": "最终确认/决定按钮",
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
        ],
    },
    "purchase_success_page": {
        "page_name": "购买完成页",
        "path": "点最终确认之后 → thank-you / 「購入完了」/「購入が完了しました」",
        "looking_for": "购买成功页特征（URL thank-you 或文案 購入完了）",
        "selectors": [
            "text=購入完了",
            "text=購入が完了",
            "text=ご購入ありがとうございます",
        ],
    },
    "wait_shipping_address": {
        "page_name": "待运费只改地址",
        "path": "取引ナビ出现「送料連絡待ち」→ 只改 home_address2/address2 → 正式模式点 #mBoxConfBt 和 #confSubmitBtn",
        "looking_for": "待运费页的地址输入框",
        "selectors": [
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
    """约定控件找不到时：截图 + HTML + 对照 JSON + 飞书。同一检查点每个 path 每轮只报一次。"""

    async def report_missing_yahoo_ui(self, page: Page, checkpoint_id, ctx=None, extra="", order=None):
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

        if order is None and ctx is not None:
            order = getattr(ctx, "order", None) or getattr(ctx, "order_info", None) or {}
        order = order or {}
        order_id = str(order.get("order_id") or "")
        product_id = str(order.get("product_id") or "")

        snapshot_dir = os.path.join(self.BASE_DIR, "error_snapshots", "ui_miss")
        os.makedirs(snapshot_dir, exist_ok=True)
        stamp = int(time.time())
        base_name = f"{checkpoint_id}_{order_id or 'na'}_{stamp}"
        shot_path = os.path.join(snapshot_dir, base_name + ".png")
        html_path = os.path.join(snapshot_dir, base_name + ".html")
        json_path = os.path.join(snapshot_dir, base_name + ".json")

        if page and not page.is_closed():
            try:
                await page.screenshot(path=shot_path, full_page=True)
            except Exception as e:
                await self.log(f"改版对照截图失败: {e}", "WARNING")
                shot_path = ""
            try:
                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(await page.content())
            except Exception as e:
                await self.log(f"改版对照 HTML 失败: {e}", "WARNING")
                html_path = ""

        hints = []
        try:
            if page and not page.is_closed():
                hints = await page.evaluate(
                    """() => {
                      const out = [];
                      const nodes = document.querySelectorAll(
                        'a, button, input[type=submit], input[type=button], h1, h2, h3'
                      );
                      for (const el of nodes) {
                        const t = (el.innerText || el.value || '').trim().replace(/\\s+/g, ' ');
                        if (t && t.length <= 48) out.push(t);
                      }
                      return [...new Set(out)].slice(0, 50);
                    }"""
                )
        except Exception:
            hints = []

        payload = {
            "checkpoint": checkpoint_id,
            "page_name": spec.get("page_name"),
            "path": spec.get("path"),
            "looking_for": spec.get("looking_for"),
            "old_selectors": spec.get("selectors") or [],
            "url": url,
            "order_id": order_id,
            "product_id": product_id,
            "extra": extra or "",
            "visible_texts": hints,
            "screenshot": shot_path,
            "html": html_path,
            "replace_hint": "请对照截图/源码，告诉我新页面上对应控件的 class、id 或可见文本，我直接替换 old_selectors。",
        }
        try:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except Exception as e:
            await self.log(f"改版对照 JSON 失败: {e}", "WARNING")
            json_path = ""

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
            f"本页可见按钮/标题: {hint_preview}\n"
            f"截图: {shot_path or '(失败)'}\n"
            f"源码: {html_path or '(失败)'}\n"
            f"对照表: {json_path or '(失败)'}\n"
            "请回复：新页面上对应的 class / id / 按钮文字，我直接替换。"
        )
        await self.log(text, "ERROR")
        self.notify_feishu(text)
        await self.trigger_sound("attention")
        return payload
