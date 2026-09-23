# Restored from YahooAutoBot_v2.0.exe (Python 3.9 / PyInstaller, unencrypted)
import os
import sys

if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
    local_browsers = os.path.join(BASE_DIR, "browsers")
    system_browsers = os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright")
    if os.path.exists(local_browsers):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = local_browsers
    elif os.path.exists(system_browsers):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = system_browsers
    else:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = local_browsers
    BUNDLE_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    BUNDLE_DIR = BASE_DIR

import argparse
import asyncio
import csv
import io
import json
import urllib.parse
import urllib.request
from typing import List, Optional

import aiofiles
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from bot_engine import YahooAutoBot

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_path = os.path.join(BASE_DIR, "static")
if not os.path.exists(os.path.join(static_path, "index.html")):
    internal_static = os.path.join(BUNDLE_DIR, "static")
    if os.path.exists(os.path.join(internal_static, "index.html")):
        static_path = internal_static

print(f"Static path: {os.path.abspath(static_path)}")
os.makedirs(static_path, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_path), name="static")


@app.get("/")
async def read_index():
    return FileResponse(os.path.join(static_path, "index.html"))


class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        stale = []
        for connection in list(self.active_connections):
            try:
                await connection.send_text(message)
            except Exception:
                stale.append(connection)
        for connection in stale:
            self.disconnect(connection)


manager = ConnectionManager()
bot = YahooAutoBot(log_callback=manager.broadcast)
print("Browser: system Chrome Profile 14 (CDP)")


class BotConfig(BaseModel):
    headless: bool = False
    test_mode: bool = True
    target_order_id: Optional[str] = None


@app.post("/start")
async def start_bot(config: BotConfig):
    if bot.is_running:
        return {"status": "already running"}
    asyncio.create_task(bot.run_loop(config.headless, config.test_mode, config.target_order_id))
    return {"status": "started"}


@app.post("/stop")
async def stop_bot():
    await bot.stop()
    return {"status": "stopped"}


@app.get("/status")
async def get_status():
    return {"running": bot.is_running, "paused": bot.is_paused}


@app.get("/screenshot_orders")
async def get_screenshot_orders():
    return {"orders": list(bot.screenshot_pending_orders)}


@app.post("/launch_browser")
async def launch_browser():
    """Launch browser for manual login"""
    await bot.start_browser(headless=False, for_login=True)
    return {"status": "browser launched"}


@app.websocket("/ws/logs")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


class KeywordInput(BaseModel):
    keyword: str


@app.get("/risk/history")
async def get_risk_history():
    return {"history": bot.blocked_risk_orders}


@app.get("/risk/keywords")
async def get_risk_keywords():
    return {"keywords": bot.RISK_KEYWORDS}


@app.post("/risk/keywords")
async def add_risk_keyword(item: KeywordInput):
    await bot.add_risk_keyword(item.keyword)
    return {"status": "added", "keywords": bot.RISK_KEYWORDS}


@app.delete("/risk/keywords")
async def remove_risk_keyword(item: KeywordInput):
    await bot.remove_risk_keyword(item.keyword)
    return {"status": "removed", "keywords": bot.RISK_KEYWORDS}


class CheckStatusInput(BaseModel):
    order_id: str


class RiskCheckRequest(BaseModel):
    order_id: str


@app.post("/risk/delete")
async def delete_risk_order(request: RiskCheckRequest):
    success = bot.delete_risk_history_entry(request.order_id)
    if success:
        return {"success": True, "message": f"Order {request.order_id} deleted from risk history."}
    return {"success": False, "error": "Order ID not found in history."}


@app.post("/risk/check_status")
async def check_risk_status(item: CheckStatusInput):
    result = await bot.check_risk_order_status(item.order_id)
    return result


@app.get("/api/seller_messages")
async def get_seller_messages(date: Optional[str] = None):
    """获取留言列表，支持 date=YYYY-MM-DD 参数按日期筛选"""
    if date:
        messages = bot.load_seller_messages_by_date(date)
    else:
        messages = bot.seller_messages
    return {"messages": messages}


@app.get("/api/seller_messages/dates")
async def get_seller_message_dates():
    """获取所有有留言记录的日期列表"""
    return {"dates": bot.get_seller_message_dates()}


class MessageDeleteInput(BaseModel):
    order_id: str


@app.post("/api/seller_messages/delete")
async def delete_seller_message(item: MessageDeleteInput):
    success = bot.delete_seller_message(item.order_id)
    return {"success": success}


class BlacklistInput(BaseModel):
    seller_id: str
    note: str = ""


@app.get("/api/blacklist")
async def get_blacklist():
    return {"blacklist": bot.seller_blacklist}


@app.post("/api/blacklist/add")
async def add_to_blacklist(item: BlacklistInput):
    success = bot.add_to_blacklist(item.seller_id, item.note)
    return {"success": success}


@app.post("/api/blacklist/remove")
async def remove_from_blacklist(item: BlacklistInput):
    success = bot.remove_from_blacklist(item.seller_id)
    return {"success": success}


@app.get("/api/seller_messages/export")
async def export_seller_messages(date: Optional[str] = None):
    """导出留言为 CSV 文件（前端转 Excel）"""
    if date:
        messages = bot.load_seller_messages_by_date(date)
    else:
        messages = bot.seller_messages
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow(["时间", "订单号", "卖家类型", "发送者", "留言内容"])
    for item in messages:
        for msg in item.get("messages", []):
            writer.writerow(
                [
                    item.get("timestamp", ""),
                    item.get("order_id", ""),
                    item.get("seller_type", ""),
                    msg.get("sender", ""),
                    msg.get("text", "").replace("\n", " "),
                ]
            )
    output.seek(0)
    filename = f"seller_messages_{date or 'all'}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


class TranslateInput(BaseModel):
    text: str
    source: str = "ja"
    target: str = "zh-CN"


@app.post("/api/translate")
async def translate_text(item: TranslateInput):
    """使用 Google Translate 免费 API 翻译文本（日文→中文）"""
    try:
        encoded = urllib.parse.quote(item.text)
        url = (
            "https://translate.googleapis.com/translate_a/single?client=gtx&sl="
            f"{item.source}&tl={item.target}&dt=t&q={encoded}"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        translated = "".join(segment[0] for segment in result[0] if segment and segment[0])
        return {"translated": translated}
    except Exception as e:
        return {"translated": "", "error": str(e)}


@app.get("/api/logs")
async def list_logs():
    """List all log files in logs directory, sorted by date desc"""
    if not hasattr(bot, "LOGS_DIR") or not os.path.exists(bot.LOGS_DIR):
        return {"logs": []}
    try:
        files = [f for f in os.listdir(bot.LOGS_DIR) if f.endswith(".log")]
        files.sort(reverse=True)
        return {"logs": files}
    except Exception as e:
        return {"logs": [], "error": str(e)}


@app.get("/api/logs/{filename}")
async def get_log_content(filename: str):
    """Get content of a specific log file"""
    if not hasattr(bot, "LOGS_DIR"):
        return {"content": "Logs directory not configured."}
    filepath = os.path.join(bot.LOGS_DIR, filename)
    if not os.path.abspath(filepath).startswith(os.path.abspath(bot.LOGS_DIR)):
        return {"content": "Access denied."}
    if not os.path.exists(filepath):
        return {"content": "File not found."}
    try:
        async with aiofiles.open(filepath, "r", encoding="utf-8") as f:
            content = await f.read()
        return {"content": content}
    except Exception as e:
        return {"content": f"Error reading log: {e}"}


if __name__ == "__main__":
    from subprocess import check_call

    parser = argparse.ArgumentParser(description="Yahoo Auto Bot")
    parser.add_argument(
        "--install-browsers-only",
        action="store_true",
        help="Install Playwright browsers and exit",
    )
    args = parser.parse_args()
    if args.install_browsers_only:
        print("Installing Playwright browsers...")
        try:
            from playwright.__main__ import main as pw_main

            sys.argv = ["playwright", "install", "chromium"]
            try:
                pw_main()
            except SystemExit:
                pass
            print("Browser installation complete.")
        except Exception as e:
            print(f"Error installing browsers: {e}")
            sys.exit(1)
        sys.exit(0)

    uvicorn.run(app, host="0.0.0.0", port=8000)
