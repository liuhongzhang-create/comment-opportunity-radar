#!/usr/bin/env bash
# Run the whole test suite. No API key and no network access are required:
# the end-to-end tests spin up a contract-accurate fake upstream.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
status=0

echo "▶ 前端纯函数测试 (node --test)"
if command -v node >/dev/null 2>&1; then
  node --test tests/web_core.test.cjs || status=1
else
  echo "  跳过：未找到 node（前端测试无法运行）"
fi

echo
echo "▶ 后端单元、契约与端到端测试 (unittest)"
"$PYTHON" -m unittest discover -s tests -v || status=1

exit "$status"
