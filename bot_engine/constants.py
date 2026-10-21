# 与原 bot_engine.YahooAutoBot 类常量一致，集中方便改 CVV/卡/白名单。
DEFAULT_CVV = "059"
DEFAULT_CARD_ID = "23"
FORCE_SHIPPING_WARNING_SELLERS = ["82meTj3FgR5hnjCcmaDn6vbxatPWC"]
REMARK_WHITELIST_PATTERNS = [
    r"出价\d+日元失败：竞拍结束。",
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
]
# 出价账号 → Chrome 目录。ad 用原来的 user_data，ym 用 user_data_ym。QBT 固定开在 ad。
ACCOUNT_EMAILS = {
    "ad": ["adxez09955@yahoo.co.jp", "dxez09955@yahoo.co.jp"],
    "ym": ["ymzjw19369@yahoo.co.jp", "ymzwj19369@yahoo.co.jp"],
}
QBT_BROWSER_ACCOUNT = "ad"
# 可用环境变量 FEISHU_WEBHOOK 覆盖。当前仓库原先没有任何飞书调用。
FEISHU_WEBHOOK = (
    "https://open.feishu.cn/open-apis/bot/v2/hook/0698f195-67e8-4359-b3dc-cd947782d2b4"
)
BACKEND_URL = "https://www.qbt.jp/yii/web/index.php?r=shopAdmin/order/get-auction-not-buy-list"
EDI_TICKET_URL = "https://edi.jpgoodbuy.com/service.php?func=apiCreateWorkOrder"
EDI_TICKET_PC_MARK = "nananana"
# 可用环境变量 EDI_SIGN_SECRET 覆盖。
EDI_TICKET_SECRET = "sgfegerggdsfwewythhdjyujfgrgerdgref"
RISK_STATUS_SEARCH_URL = "https://www.qbt.jp/yii/web/index.php?r=shopAdmin/order/auction-order-list"
YAHOO_HOME_URL = "https://auctions.yahoo.co.jp/"
YAHOO_AUCTION_URL = "https://auctions.yahoo.co.jp/jp/auction/{product_id}"
# 已结束拍品页 #closedNotice「取引ナビ」的现网地址；缺 seller/buyer 参数时 Yahoo 会按登录态补全。
YAHOO_TRADE_NAV_URL = "https://contact.auctions.yahoo.co.jp/top?aid={product_id}"
# 旧版取引ナビ；点不到页面链接时的回退。店铺已落札切勿走这条，会 403。
YAHOO_TRADE_TOP_URL = "https://contact.auctions.yahoo.co.jp/trade/top?aid={product_id}"
# 店铺已落札后的取引ナビ；点「購入手続きする」进 /order/review。
YAHOO_STORE_TRADE_STATUS_URL = (
    "https://buy.auctions.yahoo.co.jp/order/status?auctionId={product_id}"
)
SUCCESS_STATUSES = ("PURCHASED", "TEST_SUCCESS", "WAIT_SHIPPING", "WAIT_SHIPPING_CONTACT")
