@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================
echo   YouTube 视频中文化工具 - 本机启动
echo ============================================

where ffmpeg >nul 2>&1
if errorlevel 1 (
  echo [警告] 未在 PATH 找到 ffmpeg，烧录/配音功能将不可用（字幕下载仍可用）。
)

if not exist ".venv" (
  echo 首次运行：创建虚拟环境 .venv ...
  python -m venv .venv
)
call ".venv\Scripts\activate.bat"

echo 安装/更新依赖 ...
python -m pip install --upgrade pip >nul
pip install -r requirements.txt

if "%DEEPSEEK_API_KEY%"=="" (
  echo.
  echo [提示] 未设置 DEEPSEEK_API_KEY，翻译/配音将不可用（原文字幕仍可用）。
  echo        推荐: 复制 .env.example 为 .env 并在其中填入 DEEPSEEK_API_KEY=你的key
  echo        或临时设置:  set DEEPSEEK_API_KEY=你的key
  echo.
)

echo 启动服务，请在浏览器打开: http://127.0.0.1:8000
python -m app.server

endlocal
