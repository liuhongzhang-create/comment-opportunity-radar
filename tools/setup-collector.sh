#!/usr/bin/env bash
# 准备抖音抓取所需的运行环境（Playwright + 真实 Chrome）。
#
# 分析功能本身只需要 Python 标准库；只有「抖音抓取」需要这些额外依赖，
# 所以它们装在项目内的 .venv 里，不污染系统 Python。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
PYTHON_BIN="${PYTHON:-python3}"

echo "▶ 项目目录：$ROOT"

if [ ! -d "$VENV" ]; then
  echo "▶ 创建虚拟环境 .venv"
  "$PYTHON_BIN" -m venv "$VENV"
fi

echo "▶ 安装 Playwright"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet playwright

echo "▶ 安装 Chromium（若系统已有 Chrome，抓取会优先使用 Chrome）"
if [ -n "${PLAYWRIGHT_DOWNLOAD_HOST:-}" ]; then
  "$VENV/bin/playwright" install chromium
else
  # 国内网络直连 Playwright CDN 常常很慢，默认走 npmmirror 镜像。
  PLAYWRIGHT_DOWNLOAD_HOST="${PLAYWRIGHT_DOWNLOAD_HOST:-https://cdn.npmmirror.com/binaries/playwright}" \
    "$VENV/bin/playwright" install chromium
fi

echo
echo "✔ 完成。抓取相关命令请用这个解释器："
echo "    $VENV/bin/python tools/radar-collect.py doctor"
echo
echo "  网页界面里的「抖音抓取」需要从同一个虚拟环境启动服务："
echo "    $VENV/bin/python app.py"
