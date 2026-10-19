"""飞书群机器人。当前项目原先没有飞书模块，统一发到新 webhook。"""
import json
import os
import urllib.error
import urllib.request

from .constants import FEISHU_WEBHOOK


def _webhook():
    return (os.environ.get("FEISHU_WEBHOOK") or FEISHU_WEBHOOK or "").strip()


def send_feishu_text(text, timeout=8):
    """同步发送纯文本。失败只返回 False，不抛给下单主流程。"""
    url = _webhook()
    if not url or not (text or "").strip():
        return False
    body = json.dumps(
        {"msg_type": "text", "content": {"text": str(text)[:8000]}},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json; charset=utf-8"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False
