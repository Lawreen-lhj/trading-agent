#!/bin/bash
# 交易日全天托管 · 定时任务管理
# 为什么需要你双击一次：从 WorkBuddy 的沙箱里调 launchctl 会被系统拒绝
# （Bootstrap failed: 5: Input/output error），而你自己的终端有权限。

cd "$(dirname "$0")" || exit 1

LABEL="com.lihaijun.tradingagent"
SRC="$(pwd)/${LABEL}.plist"
DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
UID_NUM="$(id -u)"
PY="$(pwd)/.venv/bin/python"

c_green=$'\033[32m'; c_red=$'\033[31m'; c_dim=$'\033[2m'; c_bold=$'\033[1m'; c_off=$'\033[0m'

echo
echo "${c_bold}A股交易 Agent · 全天托管${c_off}"
echo "${c_dim}工作日 09:00 自动启动 → 15:10 收尾并跑复盘；进程崩溃会自动重启${c_off}"
echo

if [ ! -x "$PY" ]; then
  echo "${c_red}找不到 Python 环境：$PY${c_off}"
  echo "请先在项目目录执行：python3 -m venv .venv && .venv/bin/pip install pyyaml"
  echo; read -r -p "按回车关闭…" _; exit 1
fi
if [ ! -f "$SRC" ]; then
  echo "${c_red}找不到配置文件：$SRC${c_off}"; echo; read -r -p "按回车关闭…" _; exit 1
fi

install_job() {
  mkdir -p "$HOME/Library/LaunchAgents"
  cp "$SRC" "$DST" || { echo "${c_red}复制 plist 失败${c_off}"; return 1; }
  launchctl bootout "gui/${UID_NUM}/${LABEL}" >/dev/null 2>&1
  if launchctl bootstrap "gui/${UID_NUM}" "$DST" 2>/tmp/launchctl_err.txt; then
    echo "${c_green}✔ 已安装${c_off}：每个工作日 09:00 自动启动"
    echo "${c_dim}  下次触发：$($PY - <<'PY'
from datetime import datetime, timedelta
d = datetime.now()
for i in range(1, 9):
    t = d + timedelta(days=i)
    if t.weekday() < 5:
        print(t.strftime("%Y-%m-%d（周") + "一二三四五六日"[t.weekday()] + "）09:00")
        break
PY
)${c_off}"
  else
    echo "${c_red}安装失败${c_off}：" $(cat /tmp/launchctl_err.txt)
    echo "${c_dim}若提示 Input/output error，可试试重启终端后重试，或在「系统设置 → 隐私与安全性 → 完全磁盘访问权限」里给终端授权。${c_off}"
    return 1
  fi
}

uninstall_job() {
  launchctl bootout "gui/${UID_NUM}/${LABEL}" >/dev/null 2>&1 && echo "${c_green}✔ 已卸载定时任务${c_off}" || echo "${c_dim}定时任务本来就没在运行${c_off}"
  rm -f "$DST" && echo "${c_dim}已删除 $DST${c_off}"
  echo "${c_dim}提示：已启动的托管进程不会被杀，需要停止请单独执行 pkill -f run_day.py${c_off}"
}

run_now() {
  if pgrep -f "run_day.py" >/dev/null 2>&1; then
    echo "${c_dim}已有托管进程在跑，先停掉再试跑${c_off}"
    pkill -f "run_day.py"; sleep 2
  fi
  echo "${c_green}启动全天托管（本次在前台运行，关掉窗口或 Ctrl-C 即停止）${c_off}"
  exec "$PY" run_day.py
}

show_status() {
  echo "${c_bold}定时任务${c_off}"
  if launchctl print "gui/${UID_NUM}/${LABEL}" >/dev/null 2>&1; then
    echo "  ${c_green}已注册${c_off}"
    launchctl print "gui/${UID_NUM}/${LABEL}" 2>/dev/null | grep -E "state = |last exit code" | sed 's/^/  /'
  else
    echo "  ${c_dim}未注册（第 1 项可安装）${c_off}"
  fi

  echo
  echo "${c_bold}托管进程${c_off}"
  if pgrep -f "run_day.py" >/dev/null 2>&1; then
    ps -o pid,etime,command -p "$(pgrep -f run_day.py | head -1)" | sed 's/^/  /'
  else
    echo "  ${c_dim}当前没有在跑${c_off}"
  fi
  if [ -f data/run/day_state.json ]; then
    "$PY" - <<'PY'
import json
d = json.load(open("data/run/day_state.json", encoding="utf-8"))
print(f"  心跳：{d.get('updated_at')} · 阶段 {d.get('phase')} · 时段 {d.get('session', {}).get('label')}")
for c in d.get("children", []):
    print(f"    - {c['name']}: {'运行中' if c['alive'] else '已停止'} pid={c['pid']} 重启{c['restarts']}次")
PY
  fi
  echo
  echo "${c_bold}日志${c_off}"
  echo "  汇总  data/logs/day-$(date +%Y-%m-%d).log"
  echo "  子进程 data/logs/{server,monitor,sim,brief,review}.log"
}

while true; do
  echo "  1) 安装定时任务（每个工作日 09:00 启动）"
  echo "  2) 立即启动一次（前台，验证用）"
  echo "  3) 查看运行状态"
  echo "  4) 卸载定时任务"
  echo "  5) 退出"
  echo
  read -r -p "选择 [1-5]：" choice
  echo
  case "$choice" in
    1) install_job ;;
    2) run_now ;;
    3) show_status ;;
    4) uninstall_job ;;
    5) exit 0 ;;
    *) echo "${c_dim}请输入 1-5${c_off}" ;;
  esac
  echo
  read -r -p "按回车继续…" _
  clear
done
