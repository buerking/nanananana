# 与原 bot_engine.YahooAutoBot 类常量一致，集中方便改 CVV/卡/白名单。
DEFAULT_CVV = "059"
DEFAULT_CARD_ID = "23"
FORCE_SHIPPING_WARNING_SELLERS = ["82meTj3FgR5hnjCcmaDn6vbxatPWC"]
REMARK_WHITELIST_PATTERNS = [
    r"出价\d+日元失败：竞拍结束。",
    "dxez09955@yahoo.co.jp",
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
BACKEND_URL = "https://www.qbt.jp/yii/web/index.php?r=shopAdmin/order/get-auction-not-buy-list"
RISK_STATUS_SEARCH_URL = "https://www.qbt.jp/yii/web/index.php?r=shopAdmin/order/auction-order-list"
YAHOO_HOME_URL = "https://auctions.yahoo.co.jp/"
SUCCESS_STATUSES = ("PURCHASED", "TEST_SUCCESS", "WAIT_SHIPPING", "WAIT_SHIPPING_CONTACT")
