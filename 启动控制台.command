#!/bin/bash
# 双击启动交易 Agent 控制台（macOS）
cd "$(dirname "$0")" || exit 1

PORT=8765

# 已经在跑就直接开浏览器，不重复启动
if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:${PORT}/api/state"; then
  echo "控制台已在运行，打开浏览器…"
  open "http://127.0.0.1:${PORT}"
  sleep 1
  exit 0
fi

# 首次使用：自动建虚拟环境
if [ ! -x ".venv/bin/python" ]; then
  echo "首次启动，正在创建虚拟环境（约 30 秒）…"
  python3 -m venv .venv || { echo "创建失败：请确认已安装 python3"; read -r -p "按回车关闭"; exit 1; }
  .venv/bin/pip install -q pyyaml -i https://pypi.tuna.tsinghua.edu.cn/simple \
    || { echo "依赖安装失败"; read -r -p "按回车关闭"; exit 1; }
fi

echo "启动中… 关闭本窗口即停止服务。"
exec .venv/bin/python server.py --port "${PORT}"
