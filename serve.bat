@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv not found. Run: python -m venv .venv
  pause
  exit /b 1
)
".venv\Scripts\python.exe" launch.py
rem 窗口留 2 秒让提示看得见，然后自己关掉，服务留在后台
timeout /t 2 >nul
