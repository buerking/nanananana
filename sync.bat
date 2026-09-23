@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [sync] 从 GitHub 拉取最新代码...
git fetch origin
if errorlevel 1 (
    echo [sync] fetch 失败：检查网络、Git 登录或 Deploy Key。
    exit /b 1
)

git pull --ff-only origin main
if errorlevel 1 (
    echo [sync] 快进合并失败：生产机上可能有本地改动，请先处理后再同步。
    exit /b 1
)

echo [sync] 同步完成。
git log -1 --oneline
exit /b 0
