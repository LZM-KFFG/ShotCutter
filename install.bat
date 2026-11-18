@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================
echo   ShotCutter 首次安装（建虚拟环境 + 装依赖）
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 没找到 python，请先安装 Python 3.12 或 3.13
  echo        安装时记得勾选 "Add python.exe to PATH"
  pause
  exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo 检测到 Python %PYVER%
echo.

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] 创建虚拟环境 .venv ...
  python -m venv .venv
  if errorlevel 1 (
    echo [错误] 创建虚拟环境失败
    pause
    exit /b 1
  )
) else (
  echo [1/3] 虚拟环境已存在，跳过
)

echo [2/3] 安装依赖（首次约 3~8 分钟，含 GPU 包约 1.2GB）...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo [错误] 依赖安装失败，检查网络后重试
  pause
  exit /b 1
)

echo [3/3] 检查 ffmpeg ...
if exist ".venv\Scripts\ffmpeg.exe" (
  echo       .venv\Scripts\ffmpeg.exe 就绪
) else (
  where ffmpeg >nul 2>nul
  if errorlevel 1 (
    echo [提示] 没有找到 ffmpeg。把 ffmpeg.exe 放进 .venv\Scripts\，
    echo        或者安装 ffmpeg 并加入 PATH，然后再启动。
  ) else (
    echo       PATH 中的 ffmpeg 就绪
  )
)

echo.
echo 安装完成，双击「start.bat」即可使用（默认打开 http://127.0.0.1:8777）
pause
