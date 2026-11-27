# 与原 bot_engine.YahooAutoBot 类常量一致，集中方便改 CVV/卡/白名单。
import os
DEFAULT_CVV = "059"
DEFAULT_CARD_ID = "23"
FORCE_SHIPPING_WARNING_SELLERS = ["82meTj3FgR5hnjCcmaDn6vbxatPWC"]
REMARK_WHITELIST_PATTERNS = [
    r"出价\d+日元失败：竞拍结束。",
    "下单使用优惠券减免金额",
    # QBT 系统回写：锁定优惠券没用上、已释放。不是「不要买」。
    "本次支付未使用已锁定优惠券",
    "adxez09955@yahoo.co.jp",
    "dxez09955@yahoo.co.jp",
    "ymzjw19369@yahoo.co.jp",
    "ymzwj19369@yahoo.co.jp",
]
DEFAULT_RISK_KEYWORDS = [
    "直接引取",
    "来店引取",
    "西濃運輸",
    "会社名義",
    "個人宅配達不可",
    "個人様名義",
    "西濃",
    # 西濃運輸的品牌名；拍品/结算常只写这个，不含「西濃」二字。
    "カンガルー便",
]
# 出价账号 → Chrome 目录。ad 用原来的 user_data，ym 用 user_data_ym。QBT 固定开在 ad。
ACCOUNT_EMAILS = {
    "ad": ["adxez09955@yahoo.co.jp", "dxez09955@yahoo.co.jp"],
    "ym": ["ymzjw19369@yahoo.co.jp", "ymzwj19369@yahoo.co.jp"],
}
# 独立 Chrome 的 CDP 端口。重启 main 时只断开 Playwright，不关浏览器。
CHROME_CDP_PORTS = {
    "ad": 9222,
    "ym": 9223,
}
# 监控页 / 看门狗：Chrome 掉线后自动拉起。
CHROME_WATCHDOG_SECONDS = 8
CHROME_RESTART_COOLDOWN = 45
QBT_BROWSER_ACCOUNT = "ad"
# 可用环境变量 FEISHU_WEBHOOK / FEISHU_APP_ID / FEISHU_APP_SECRET 覆盖。
FEISHU_WEBHOOK = (
    os.environ.get("FEISHU_WEBHOOK")
    or "https://open.feishu.cn/open-apis/bot/v2/hook/0698f195-67e8-4359-b3dc-cd947782d2b4"
)
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID") or "cli_aaf6bb056339dcbd"
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET") or "3EEGI9iEUti7cbbtm6ZGWbscZZoLCSzR"
FEISHU_IMAGE_HOST = (
    "https://softbank.jpgoodbuy.com/common_upload.php?act=post_document_flow"
)
FEISHU_IMAGE_FOLDER = "/upload/daiGouImage"
BACKEND_URL = "https://www.qbt.jp/yii/web/index.php?r=shopAdmin/order/get-auction-not-buy-list"
QBT_AUCTION_BUY_URL = (
    "https://www.qbt.jp/yii/web/index.php?r=shopAdmin/order/auction-buy&id={order_id}"
)
EDI_TICKET_URL = "https://edi.jpgoodbuy.com/service.php?func=apiCreateWorkOrder"
EDI_TICKET_PC_MARK = "nananana"
# 可用环境变量 EDI_SIGN_SECRET 覆盖。
EDI_TICKET_SECRET = "sgfegerggdsfwewythhdjyujfgrgerdgref"
RISK_STATUS_SEARCH_URL = "https://www.qbt.jp/yii/web/index.php?r=shopAdmin/order/auction-order-list"
YAHOO_HOME_URL = "https://auctions.yahoo.co.jp/"
YAHOO_AUCTION_URL = "https://auctions.yahoo.co.jp/jp/auction/{product_id}"
# 新版个人取引ナビ（Next /top）。不少待运费/已提交取引情報的单在这里是系统错误页，不要当首选。
YAHOO_TRADE_NAV_URL = "https://contact.auctions.yahoo.co.jp/top?aid={product_id}"
# 个人旧版取引ナビ。待运费复查、送料連絡待ち、情報 Tab 走这条。店铺已落札切勿走这条，会 403。
YAHOO_TRADE_TOP_URL = "https://contact.auctions.yahoo.co.jp/trade/top?aid={product_id}"
# 店铺已落札后的取引ナビ；点「購入手続きする」进 /order/review。
YAHOO_STORE_TRADE_STATUS_URL = (
    "https://buy.auctions.yahoo.co.jp/order/status?auctionId={product_id}"
)
SUCCESS_STATUSES = ("PURCHASED", "TEST_SUCCESS", "WAIT_SHIPPING", "WAIT_SHIPPING_CONTACT")
# 这些结论没有必要留一堆雅虎页给人工看；留着会把 Chrome 撑崩。
CLOSE_TABS_STATUSES = SUCCESS_STATUSES + (
    "ADDRESS_FAIL",
    "SKIPPED_RISK",
    "SKIPPED_NO_PURCHASE_DATA",
    "SKIPPED_TRADE_CANCELLED",
    "YAHOO_ID_MISMATCH",
    "YAHOO_REAUTH",
    "PURCHASE_UNCONFIRMED",
    "WRONG_YAHOO_TAB",
    "DETAIL_PAGE_ERROR",
)
# 每个 Chrome 闲时最多留这么多标签（列表+首页+当前页）。超过就扫掉旧拍品/购买页。
MAX_CHROME_PAGES = 8
# 雅虎跳转/水合上限。wait_for_url 等匹配到会立刻返回，不会真的空等 5 分钟。
WAIT_HANG_MS = 5 * 60 * 1000
# 单次可见点击；隐藏节点不要用 Playwright 默认 30 秒空等。
WAIT_CLICK_MS = 8000
# 雅虎「システムでエラーが発生しました」：模拟人工 F5，每次后等 30 秒看它自己恢复，最多 3 次。
YAHOO_SYSERR_MAX_TRIES = 3
YAHOO_SYSERR_WAIT_SECONDS = 30
# 同一用户+同一卖家暂时只有一单时，等第二单进未购买；从首次出现在未购买起算。
BUNDLE_HOLD_SECONDS = 5 * 60
