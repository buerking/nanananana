"""轻量 Pipeline / Gate。

约定：
- Gate / Step 返回 None → 放行
- 返回 dict（含 status）→ 提前结束本单
"""
from dataclasses import dataclass, field
from typing import Any, Optional


def deny(status, **extra):
    payload = {"status": status}
    payload.update(extra)
    return payload


@dataclass
class OrderContext:
    order: dict
    test_mode: bool = True
    backend_page: Any = None
    detail_page: Any = None
    yahoo_page: Any = None
    is_new_tab: bool = False
    bound_items: list = field(default_factory=list)
    backend_total: int = 0
    order_info: dict = field(default_factory=dict)
    is_store: bool = False
    is_personal: bool = False
    is_wait_for_shipping: bool = False
    early_cod: bool = False
    shipping_cost: int = 0
    yahoo_total: int = 0
    body_text: str = ""
    result: Optional[dict] = None

    @property
    def order_id(self):
        return str(self.order.get("order_id", "00000"))

    @property
    def suffix(self):
        return f"{self.order_id[-5:]}室"

    @property
    def need_track(self):
        opt = str(self.order.get("option_info") or "")
        return "可追踪" in opt or "快递单号" in opt

    def sync_order_info(self):
        info = dict(self.order)
        info["bound_items"] = self.bound_items
        info["backend_total"] = self.backend_total
        self.order_info = info
        return info


async def run_chain(steps, ctx):
    """按序执行 Gate 或 Pipeline 步骤。第一个非 None 结果即结束。"""
    for step in steps:
        result = await step(ctx)
        if result:
            ctx.result = result
            return result
    return None
