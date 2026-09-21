import datetime
import os
import time

import aiofiles
from playwright.async_api import Page


class LoggingMixin:
    """WebSocket + 文件日志、前端提示音、出错快照。"""

    async def log(self, message, level="INFO"):
        """Send log to frontend via WebSocket and save to file"""
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        log_msg = f"[{timestamp}] [{level}] {message}"
        print(log_msg)
        if self.log_callback:
            try:
                await self.log_callback(log_msg)
            except Exception:
                pass
        try:
            date_str = datetime.datetime.now().strftime("%Y-%m-%d")
            log_file = os.path.join(self.LOGS_DIR, f"{date_str}.log")
            async with aiofiles.open(log_file, mode="a", encoding="utf-8") as f:
                await f.write(log_msg + "\n")
        except Exception as e:
            print(f"File logging failed: {e}")

    async def trigger_sound(self, sound_type="error"):
        """Send sound command to frontend. Types: success, error, attention"""
        if self.log_callback:
            await self.log_callback(f"[[SOUND:{sound_type}]]")

    async def save_error_snapshot(self, page: Page, tag="error"):
        """Save HTML and Screenshot for debugging"""
        try:
            timestamp = int(time.time())
            snapshot_dir = os.path.join(self.BASE_DIR, "error_snapshots")
            os.makedirs(snapshot_dir, exist_ok=True)
            base_name = f"error_{tag}_{timestamp}"
            shot_path = os.path.join(snapshot_dir, base_name + ".png")
            try:
                await page.screenshot(path=shot_path, full_page=True)
            except Exception as e:
                await self.log(f"Snapshot Screenshot Failed: {e}", "WARNING")
            html_path = os.path.join(snapshot_dir, base_name + ".html")
            try:
                content = await page.content()
                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(content)
            except Exception as e:
                await self.log(f"Snapshot HTML Failed: {e}", "WARNING")
            await self.log(f"SNAPSHOT SAVED: {base_name} (in error_snapshots/)", "ERROR")
            await self.trigger_sound("error")
        except Exception as e:
            await self.log(f"Snapshot Logic Failed: {e}", "ERROR")
