@echo off
chcp 65001 >nul
title 小派语音合成服务
cd /d "%~dp0"

set PY=C:\Users\xuxin\.workbuddy\binaries\python\envs\default\Scripts\python.exe

echo ================================================
echo   小派语音合成服务（给树莓派用的 Windows SAPI）
echo   监听 192.168.137.1:8765
echo   关掉这个窗口就等于关掉服务
echo ================================================
echo.

"%PY%" tts_server.py

echo.
echo 服务已退出。如果上面有报错，截图发给助手。
pause
