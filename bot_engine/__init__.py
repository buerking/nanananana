"""Yahoo 拍卖自动下单引擎。

`from bot_engine import YahooAutoBot` 仍可用。

分层（加规则时优先改对应层）：
- gates.py            门卫：黑名单 / 备注 / 敏感词 / 同捆弹窗 / 登录
- yahoo_purchase.py   主流程 Pipeline：进购买页 → 结算 → 确认支付
- yahoo_checkout.py   结算步骤：地址、配送、信用卡、CVV
- orders.py           打开详情/雅虎页，再交给主流程
- qbt.py              扫单、回填
- flow.py             OrderContext + run_chain
"""

from .bot import YahooAutoBot
from .exceptions import BotStopped

__all__ = ["YahooAutoBot", "BotStopped"]
