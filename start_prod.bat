@echo off
chcp 65001 >nul
cd /d "%~dp0"

call sync.bat
if errorlevel 1 (
    echo [start] 同步失败，仍使用当前本地代码启动。
)

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

echo [start] 安装/检查依赖...
"%PY%" -m pip install -r requirements.txt -q
"%PY%" -m playwright install chromium

echo [start] 启动 YahooAutoBot...
"%PY%" main.py
