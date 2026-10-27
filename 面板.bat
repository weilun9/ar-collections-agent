@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 面板启动中，浏览器会自动打开…按 Ctrl+C 停止。
python ar.py dashboard
pause
