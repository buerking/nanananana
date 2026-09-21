"""Yahoo 拍卖自动下单引擎。

`from bot_engine import YahooAutoBot` 仍可用。模块按阶段拆分，方便打断点：

- bot.py            启动循环
- qbt.py            阶段1 扫单 / 阶段4 回填
- orders.py         阶段2 详情页 → 打开雅虎
- yahoo_purchase.py 阶段3 下单入口 / 同捆 / 确认支付
- yahoo_checkout.py 结算：地址、配送、信用卡、CVV
"""

from .bot import YahooAutoBot
from .exceptions import BotStopped

__all__ = ["YahooAutoBot", "BotStopped"]
