#!/usr/bin/env bash
# 本机启动脚本：检查 ffmpeg、建虚拟环境、装依赖、启动服务。
# 兼容 Windows Git Bash 与 Linux/macOS。
set -euo pipefail
cd "$(dirname "$0")"

echo "============================================"
echo "  YouTube 视频中文化工具 - 本机启动"
echo "============================================"

command -v ffmpeg >/dev/null 2>&1 \
  || echo "[警告] 未找到 ffmpeg，烧录/配音功能将不可用（字幕下载仍可用）。"

if [ ! -d ".venv" ]; then
  echo "首次运行：创建虚拟环境 .venv ..."
  python -m venv .venv
fi

# Windows 的 venv 在 Scripts/，类 Unix 在 bin/
if [ -f ".venv/Scripts/activate" ]; then
  source ".venv/Scripts/activate"
else
  source ".venv/bin/activate"
fi

echo "安装/更新依赖 ..."
python -m pip install --upgrade pip >/dev/null
pip install -r requirements.txt

if [ -z "${DEEPSEEK_API_KEY:-}" ] && [ ! -f ".env" ]; then
  echo ""
  echo "[提示] 未设置 DEEPSEEK_API_KEY，翻译/配音将不可用（原文字幕仍可用）。"
  echo "       推荐: 复制 .env.example 为 .env 并填入 DEEPSEEK_API_KEY=你的key"
  echo "       或临时设置:  export DEEPSEEK_API_KEY=你的key"
  echo ""
fi

echo "启动服务，请在浏览器打开: http://127.0.0.1:8000"
python -m app.server
