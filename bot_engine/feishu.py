"""飞书群机器人：文字 webhook + 企业应用上传图片。失败不抛给下单主流程。"""
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .constants import (
    FEISHU_APP_ID,
    FEISHU_APP_SECRET,
    FEISHU_IMAGE_FOLDER,
    FEISHU_IMAGE_HOST,
    FEISHU_WEBHOOK,
)

_TOKEN_CACHE = {"token": "", "exp": 0.0}


def _webhook():
    return (os.environ.get("FEISHU_WEBHOOK") or FEISHU_WEBHOOK or "").strip()


def _app_id():
    return (os.environ.get("FEISHU_APP_ID") or FEISHU_APP_ID or "").strip()


def _app_secret():
    return (os.environ.get("FEISHU_APP_SECRET") or FEISHU_APP_SECRET or "").strip()


def _post_bytes(url, data, headers, timeout):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        status = getattr(resp, "status", 200)
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {"raw": raw}
    return status, payload


def _post_json(url, payload, headers=None, timeout=12):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hdrs = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        hdrs.update(headers)
    return _post_bytes(url, body, hdrs, timeout)


def _multipart_body(fields, files):
    boundary = "----YabBoundary" + uuid.uuid4().hex
    chunks = []
    for key, value in (fields or {}).items():
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    for key, (filename, data, ctype) in (files or {}).items():
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'
                f"Content-Type: {ctype}\r\n\r\n"
            ).encode("utf-8")
        )
        chunks.append(data)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return boundary, b"".join(chunks)


def send_feishu_text(text, timeout=8, at_all=False):
    """同步发送纯文本。失败只返回 False。"""
    url = _webhook()
    if not url or not (text or "").strip():
        return False
    content = str(text).strip()
    if at_all and "<at user_id=" not in content:
        content = '<at user_id="all">所有人</at>\n' + content
    try:
        status, _ = _post_json(
            url, {"msg_type": "text", "content": {"text": content[:8000]}}, timeout=timeout
        )
        return 200 <= status < 300
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def _tenant_access_token(timeout=8):
    now = time.time()
    if _TOKEN_CACHE["token"] and _TOKEN_CACHE["exp"] > now + 60:
        return _TOKEN_CACHE["token"]
    app_id = _app_id()
    app_secret = _app_secret()
    if not app_id or not app_secret:
        return ""
    try:
        status, payload = _post_json(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            {"app_id": app_id, "app_secret": app_secret},
            timeout=timeout,
        )
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return ""
    if status >= 400 or int(payload.get("code") or 1) != 0:
        return ""
    token = str(payload.get("tenant_access_token") or "").strip()
    expire = float(payload.get("expire") or 7200)
    if token:
        _TOKEN_CACHE["token"] = token
        _TOKEN_CACHE["exp"] = now + max(60.0, expire - 120.0)
    return token


def _upload_image_key(png_bytes, timeout=20):
    token = _tenant_access_token()
    if not token or not png_bytes:
        return ""
    boundary, body = _multipart_body(
        {"image_type": "message"},
        {"image": ("receipt.png", png_bytes, "image/png")},
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    try:
        status, payload = _post_bytes(
            "https://open.feishu.cn/open-apis/im/v1/images",
            body,
            headers,
            timeout,
        )
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return ""
    if status >= 400 or int(payload.get("code") or 1) != 0:
        return ""
    data = payload.get("data") or {}
    return str(data.get("image_key") or "").strip()


def _send_feishu_image(image_key, timeout=8):
    url = _webhook()
    if not url or not image_key:
        return False
    try:
        status, _ = _post_json(
            url,
            {"msg_type": "image", "content": {"image_key": image_key}},
            timeout=timeout,
        )
        return 200 <= status < 300
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def _upload_image_host(png_bytes, timeout=20):
    if not png_bytes:
        return ""
    name = f"{uuid.uuid4().hex}.png"
    b64 = base64.b64encode(png_bytes).decode("ascii")
    body = urllib.parse.urlencode(
        {
            "folder": FEISHU_IMAGE_FOLDER,
            "name": name,
            "flow": b64,
        }
    ).encode("utf-8")
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    try:
        status, payload = _post_bytes(FEISHU_IMAGE_HOST, body, headers, timeout)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return ""
    if status >= 400:
        return ""
    if not payload.get("Success"):
        return ""
    data = payload.get("Data")
    if isinstance(data, str) and data.strip():
        return data.strip()
    if isinstance(data, dict):
        return str(data.get("url") or data.get("path") or "").strip()
    return ""


def _read_png(image_path):
    path = (image_path or "").strip()
    if not path or not os.path.isfile(path):
        return b""
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return b""


def send_feishu_alert(text, timeout=12, at_all=False, image_path=""):
    """先发文字（可 @所有人），再发图片。上传 image_key 失败则改发图床链接。"""
    png = _read_png(image_path)
    note = str(text or "").strip()
    ok_text = send_feishu_text(note, timeout=timeout, at_all=at_all) if note else False
    if not png:
        return ok_text
    image_key = _upload_image_key(png, timeout=max(timeout, 20))
    if image_key and _send_feishu_image(image_key, timeout=timeout):
        return True
    hosted = _upload_image_host(png, timeout=max(timeout, 20))
    if hosted:
        extra = f"打码截图：{hosted}"
        send_feishu_text(
            f"{note}\n{extra}" if note else extra,
            timeout=timeout,
            at_all=False if ok_text else at_all,
        )
        return True
    return ok_text
