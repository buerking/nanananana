---
name: yahoo-autobot-incident-fix
description: >-
  Reads the latest YahooAutoBot live-DOM incident pack and patches checkout
  selectors or entry logic. Use when the user says 修最新失败, 飞书改版, 对照事故包,
  or pastes a Feishu 「雅虎页面改版」 alert / error_snapshots path.
---

# 修最新失败（活 DOM 事故包）

管理员在飞书看到改版告警后，在本仓库对 Cursor 说「修最新失败」。不要等人在群里回 class。

## 必读文件

1. `error_snapshots/LATEST`（纯文本，内容是事故目录的绝对路径）
2. 该目录下的 `meta.json`
3. `dom.outer.html`（水合后的 `document.documentElement.outerHTML`）
4. 若存在 `next_data.json`，一并读
5. 对照 `bot_engine/ui_watch.py` 的 `YAHOO_CHECKPOINTS`，以及 `bot_engine/yahoo_purchase.py` / `yahoo_checkout.py` 里现有定位器

工作区根一般是 `YahooAutoBot_v2.0/source`。`BASE_DIR` 就是这里。若 `LATEST` 不存在，取 `error_snapshots/incidents/` 里修改时间最新的文件夹。

## 怎么修

- 只改选择器、等待条件、店铺/个人入口判断。
- 用活 DOM 里真实出现的 `id`、可见按钮文字、`data-cl-params`、`href` 片段。哈希 class（`sc-xxxx`）不要当唯一依据。
- 若 `dom.outer.html` 仍是骨架屏（几乎没有 `h1`/`#address`/`#pap`/`購入`），**停下来**请人补一帧活页，不要猜。
- 结算区文本在 `meta.json` 的 `visible_texts` / `main_text_preview`，不要用页头广告链接当按钮。

## 禁止

- 不 `git commit` / 不 push，除非用户明确要求。
- 不改 CVV、卡号、飞书 webhook、账号密码。
- 不替用户点真实购买。
- 不扩大到无关重构。

## 修完后

用中文说明：检查点、页面 URL、改了哪个定位器、依据 DOM 哪段文字。提醒再跑对应订单验证。
