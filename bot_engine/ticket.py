"""EDI 创建代购工单 apiCreateWorkOrder。失败不阻断飞书。"""
import asyncio
import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from .constants import (
    EDI_TICKET_PC_MARK,
    EDI_TICKET_SECRET,
    EDI_TICKET_URL,
)


def _secret():
    return (os.environ.get("EDI_SIGN_SECRET") or EDI_TICKET_SECRET or "").strip()


def edi_sign(params, secret):
    items = [
        (str(k), str(v))
        for k, v in params.items()
        if k != "Sign" and v is not None and str(v) != ""
    ]
    items.sort(key=lambda kv: kv[0])
    raw = "&".join(f"{k}={v}" for k, v in items) + f"&key={secret}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


def _post_once(order_id, storage_code, messages, send_message=1):
    secret = _secret()
    if not secret:
        return False, "未配置 EDI 签名密钥"
    title = (
        f"尊敬的用户【{storage_code}】您的代购订单【{order_id}】有问题，需要您手动确认"
    )
    lines = [
        f"入库码: {storage_code}",
        f"订单ID: {order_id}",
        "",
        f"订单 {order_id} 处理过程中发现问题:",
        "",
    ]
    for m in messages or []:
        lines.append(f"对应的订单链接：{m}")
    content = "\n".join(lines)
    body = {
        "DataId": str(order_id),
        "StorageCode": str(storage_code),
        "TicketType": "purchase-order",
        "Title": title,
        "Content": content,
        "SendMessage": str(int(send_message)),
        "PcMark": EDI_TICKET_PC_MARK,
    }
    body["Sign"] = edi_sign(body, secret)
    encoded = urllib.parse.urlencode(body).encode("utf-8")
    req = urllib.request.Request(
        EDI_TICKET_URL,
        data=encoded,
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        status = getattr(resp, "status", 200)
    if not (200 <= status < 300):
        return False, f"HTTP {status} {raw[:200]}"
    try:
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        return True, raw[:200]
    success = data.get("Success")
    if success is True or str(success).lower() == "true":
        return True, ""
    return False, str(data.get("Message") or raw[:200] or "Success=false")


async def create_work_order(order_id, storage_code, messages, send_message=1):
    """创建工单。只传入库码 StorageCode，不传数字 ZyUserId。"""
    oid = str(order_id or "").strip()
    code = str(storage_code or "").strip()
    if not oid or not code:
        return False, "缺少订单号或入库码"
    last_err = ""
    for attempt in range(3):
        try:
            ok, err = await asyncio.to_thread(
                _post_once, oid, code, list(messages or []), send_message
            )
            if ok:
                return True, ""
            last_err = err or "创建失败"
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = str(e)
        if attempt < 2:
            await asyncio.sleep(5 * (2 ** attempt))
    return False, last_err
