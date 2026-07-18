@echo off
chcp 65001 >nul
cd /d "%~dp0\..\.."
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-server.ps1"
echo.
echo 安装程序已经结束。请查看上方结果。
pause

