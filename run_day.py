#!/usr/bin/env python3
"""交易日全天托管：一条命令把整套系统跑一整天，中途挂了自动拉起。

为什么需要它：`run_monitor.py` / `run_sim_live.py` 各自只会常驻到收盘，
`server.py` 更是不会自己退出。要「9:00 自动开始、一整天不挂」，
缺的是一个负责「按顺序拉起 + 看住子进程 + 收盘收尾」的托管层。

做四件事：
  1. 盘前：若今天还没有简报，先跑 run_brief.py（已有则跳过，避免重复打接口）
  2. 盘中：并行拉起 Web 控制台 / 盘中监控 / 模拟盘自动循环，任何一个意外退出就重启（指数退避，连续失败则放弃该子进程）
  3. 收盘：到 stop_at（默认 15:10）后按顺序收尾，再跑 run_review.py 生成复盘
  4. 全程：写 data/run/day_state.json 心跳，异常/重启原因写 data/logs/day-*.log

交易日判断直接复用 src.simulation.matcher.market_session()，与全项目同源
（注意：它只按星期判断，不含法定节假日日历——节假日会照常启动，属已知口径）。

用法：
  python run_day.py                 # 正常托管，15:10 收尾
  python run_day.py --dry-run       # 只打印今天会怎么做，不启动任何进程
  python run_day.py --force         # 忽略交易日/日期检查（测试用）
  python run_day.py --stop-at 15:05 # 自定义收尾时间
  python run_day.py --no-ui         # 不起 Web 控制台（例如已经在别的窗口开着）
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOGDIR = ROOT / "data" / "logs"
RUNDIR = ROOT / "data" / "run"
BRIEF_DIR = ROOT / "data" / "briefings"

UI_PORT = 8765
TICK_SECONDS = 20          # 巡检间隔
MAX_RESTARTS = 8           # 单个子进程最多重启次数
RESTART_BASE = 20          # 重启退避基数（秒），按失败次数递增，上限 120
RESTART_CAP = 120

# 这些时段才启动盘中监控与模拟盘（盘前启动它们只会「无事可做」直接退出）
TRADING_KEYS = {"morning", "lunch", "afternoon", "closing_auction"}

_STOP = {"flag": False}


# ---------------------------------------------------------------- 基础设施
class Log:
    """同时写文件与 stderr，launchd 的 out/err 日志里也能看到。"""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = path.open("a", encoding="utf-8", buffering=1)
        self.path = path

    def __call__(self, msg: str) -> None:
        line = f"[{datetime.now():%H:%M:%S}] {msg}"
        print(line, file=sys.stderr, flush=True)
        self.fh.write(line + "\n")

    def close(self) -> None:
        try:
            self.fh.close()
        except Exception:
            pass


def python_exe() -> str:
    """优先用项目 venv 的解释器，保证子进程能 import yaml。"""
    venv = ROOT / ".venv" / "bin" / "python"
    if venv.exists():
        return str(venv)
    return sys.executable


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex((host, port)) == 0


def session_now() -> dict:
    """当前时段，取项目统一口径；导入失败时退化为只看星期。"""
    try:
        sys.path.insert(0, str(ROOT))
        from src.simulation.matcher import market_session

        return market_session(datetime.now())
    except Exception:
        d = datetime.now()
        return {
            "key": "unknown",
            "label": "未知",
            "is_trading_day": d.weekday() < 5,
            "time": d.strftime("%H:%M:%S"),
        }


def parse_hhmm(text: str) -> dtime:
    hh, mm = text.split(":")
    return dtime(int(hh), int(mm))


# ---------------------------------------------------------------- 子进程
class Child:
    def __init__(self, name: str, cmd: list[str], log: Log, deadline: dtime,
                 deferred: bool = False):
        self.name = name
        self.cmd = cmd
        self.log = log
        self.deadline = deadline
        self.deferred = deferred      # True=等进入交易时段再启动（监控/模拟）
        self.proc: subprocess.Popen | None = None
        self.restarts = 0
        self.gave_up = False
        self.last_start = 0.0
        self.next_retry_at = 0.0
        self.ever_started = False

    def start(self) -> None:
        logf = (LOGDIR / f"{self.name}.log").open("ab")
        self.proc = subprocess.Popen(
            self.cmd,
            cwd=str(ROOT),
            stdout=logf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,   # 独立进程组，收尾时可以整组杀掉
        )
        self.last_start = time.time()
        self.ever_started = True
        self.log(f"{self.name} 已启动 pid={self.proc.pid}")

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def uptime(self) -> float:
        return time.time() - self.last_start if self.last_start else 0.0

    def stop(self, timeout: float = 10.0) -> None:
        if not self.alive():
            return
        assert self.proc is not None
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except Exception:
            try:
                self.proc.terminate()
            except Exception:
                return
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except Exception:
                pass
        self.log(f"{self.name} 已停止")

    def check(self, allow_restart: bool, in_session: bool) -> None:
        """巡检：按退出码决定要不要拉起，别把「正常收工」当成崩溃。"""
        if self.gave_up or not self.ever_started or self.alive():
            return

        code = self.proc.returncode if self.proc else None
        up = self.uptime()
        # 退出码 0 且跑了足够久 = 它自己认为该结束了（例如已收盘、监控窗口用完）
        # 只有「非 0」或「启动不到 60 秒就退」才按崩溃处理
        crash_like = code != 0 or (up < 60 and in_session)

        if not allow_restart:
            self.log(f"{self.name} 退出 code={code}（已到收尾时间，不再重启）")
            self.gave_up = True
            return

        if not crash_like:
            self.log(f"{self.name} 正常退出 code={code}（运行 {up:.0f}s），不再重启")
            self.gave_up = True
            return

        if self.restarts >= MAX_RESTARTS:
            self.log(f"{self.name} 异常退出已达 {self.restarts} 次，放弃重启（code={code}）")
            self.gave_up = True
            return

        wait = min(RESTART_CAP, RESTART_BASE * (self.restarts + 1))
        if time.time() < self.next_retry_at:
            return
        self.restarts += 1
        self.next_retry_at = time.time() + wait
        self.log(f"{self.name} 异常退出 code={code}，{wait}s 后重启（第 {self.restarts} 次）")

        # 分片等待：收尾时间或停止信号到达时不要卡在整段 sleep 里
        end = time.time() + wait
        while time.time() < end:
            if _STOP["flag"] or datetime.now().time() >= self.deadline:
                return
            time.sleep(1)
        if not _STOP["flag"] and datetime.now().time() < self.deadline:
            self.start()


def kill_stale(log: Log) -> None:
    """清理上一次托管残留的监控/模拟进程，避免两套同时交易。

    只清理 monitor / sim（重复交易的风险在这里）；
    Web 服务交给端口占用检查处理，不擅自杀掉用户手动开着的实例。
    """
    state = RUNDIR / "day_state.json"
    if not state.exists():
        return
    try:
        data = json.loads(state.read_text(encoding="utf-8"))
    except Exception:
        return

    markers = {"monitor": "run_monitor.py", "sim": "run_sim_live.py"}
    for item in data.get("children") or []:
        name, pid = item.get("name"), item.get("pid")
        marker = markers.get(name)
        if not marker or not pid:
            continue
        try:
            out = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
        except Exception:
            continue
        if marker not in out:
            continue
        log(f"清理上次残留的 {name} pid={pid}")
        try:
            os.kill(int(pid), signal.SIGTERM)
        except Exception:
            pass


# ---------------------------------------------------------------- 一次性任务
def run_once(log: Log, name: str, cmd: list[str], timeout: int) -> int:
    log(f"{name} 开始：{' '.join(cmd[1:])}")
    logf = (LOGDIR / f"{name}.log").open("ab")
    try:
        proc = subprocess.run(
            cmd, cwd=str(ROOT), stdout=logf, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, timeout=timeout,
        )
        log(f"{name} 结束 code={proc.returncode}")
        return proc.returncode
    except subprocess.TimeoutExpired:
        log(f"{name} 超时（{timeout}s），放弃")
        return -1
    except Exception as exc:  # noqa: BLE001
        log(f"{name} 失败：{type(exc).__name__} {exc}")
        return -1
    finally:
        logf.close()


def write_state(log: Log, children: list[Child], extra: dict) -> None:
    RUNDIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "supervisor_pid": os.getpid(),
        "session": session_now(),
        "children": [
            {
                "name": c.name,
                "pid": c.proc.pid if c.proc else None,
                "alive": c.alive(),
                "restarts": c.restarts,
                "gave_up": c.gave_up,
            }
            for c in children
        ],
        **extra,
    }
    try:
        (RUNDIR / "day_state.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        log(f"写心跳失败：{exc}")


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser(description="交易日全天托管（简报→服务/监控/模拟→复盘）")
    ap.add_argument("--stop-at", default="15:10", help="收尾时间 HH:MM，默认 15:10")
    ap.add_argument("--no-ui", action="store_true", help="不启动 Web 控制台")
    ap.add_argument("--no-monitor", action="store_true", help="不启动盘中监控")
    ap.add_argument("--no-sim", action="store_true", help="不启动模拟盘自动循环")
    ap.add_argument("--no-brief", action="store_true", help="不跑盘前简报")
    ap.add_argument("--no-review", action="store_true", help="收盘后不跑复盘")
    ap.add_argument("--force", action="store_true", help="忽略交易日与时间检查（测试用）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不启动任何进程")
    args = ap.parse_args()

    LOGDIR.mkdir(parents=True, exist_ok=True)
    RUNDIR.mkdir(parents=True, exist_ok=True)
    log = Log(LOGDIR / f"day-{datetime.now():%Y-%m-%d}.log")

    stop_at = parse_hhmm(args.stop_at)
    sess = session_now()
    today = datetime.now().strftime("%Y-%m-%d")
    py = python_exe()

    log("=" * 56)
    log(f"托管启动 force={args.force} stop_at={args.stop_at} session={sess.get('label')} py={py}")

    # --- 前置检查 ---
    if not args.force and not sess.get("is_trading_day", False):
        log("今天不是交易日（按星期口径），退出。")
        log.close()
        return 0
    if not args.force and datetime.now().time() >= stop_at:
        log(f"已过收尾时间 {args.stop_at}，无需启动。")
        log.close()
        return 0

    # 单实例保护：上一个还没退干净时不要再来一个，否则会重复下单/重复请求接口
    lock_path = RUNDIR / "day.lock"
    lock_fh = lock_path.open("w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fh.write(str(os.getpid()))
        lock_fh.flush()
    except BlockingIOError:
        log("已有托管进程在运行（day.lock 被占用），本次退出。")
        log.close()
        return 0

    plan = {
        "brief": not args.no_brief,
        "ui": not args.no_ui,
        "monitor": not args.no_monitor,
        "sim": not args.no_sim,
        "review": not args.no_review,
    }
    if args.dry_run:
        log(f"计划：{plan}")
        log(f"子进程命令：{[[py, 'server.py'], [py, 'run_monitor.py'], [py, 'run_sim_live.py']]}")
        log.close()
        return 0

    def on_signal(signum, _frame):
        _STOP["flag"] = True
        log(f"收到信号 {signum}，准备收尾")

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    # --- 1. 盘前简报（当天已有则跳过） ---
    brief_json = BRIEF_DIR / f"{today}.json"
    if plan["brief"]:
        if brief_json.exists():
            log(f"今日简报已存在（{brief_json.name}），跳过")
        else:
            run_once(log, "brief", [py, "run_brief.py", "--no-notify"], timeout=900)
    else:
        log("按要求跳过盘前简报")

    # --- 2. 拉起常驻子进程 ---
    # 先清残留，否则可能出现两套 monitor/sim 同时跑、重复记一笔交易
    kill_stale(log)

    children: list[Child] = []
    if plan["ui"]:
        if port_in_use(UI_PORT):
            log(f"端口 {UI_PORT} 已被占用（可能已有控制台在跑），复用而不新起")
        else:
            children.append(Child("server", [py, "server.py", "--port", str(UI_PORT), "--no-open"],
                                  log, stop_at))
    if plan["monitor"]:
        children.append(Child("monitor", [py, "run_monitor.py"], log, stop_at, deferred=True))
    if plan["sim"]:
        children.append(Child("sim", [py, "run_sim_live.py"], log, stop_at, deferred=True))

    # Web 服务立刻起（9:00 就要能打开看盘前简报）；监控与模拟等进入交易时段再起，
    # 否则它们在盘前会「无事可做直接正常退出」，白白消耗重启次数。
    for c in children:
        if not c.deferred:
            c.start()
            time.sleep(1.5)

    write_state(log, children, {"phase": "running", "stop_at": args.stop_at})

    # --- 3. 看住子进程直到收尾时间 ---
    exit_code = 0
    try:
        while not _STOP["flag"]:
            if datetime.now().time() >= stop_at:
                log(f"到达收尾时间 {args.stop_at}")
                break

            sess = session_now()
            in_session = sess.get("key") in TRADING_KEYS

            for c in children:
                if not c.ever_started:
                    # 延迟启动的子进程：等真正开盘（含午休/尾盘），--force 时不等待
                    if c.deferred and not (in_session or args.force):
                        continue
                    c.start()
                    time.sleep(1.5)   # 错开启动，避免同时打行情接口被限流
                else:
                    c.check(allow_restart=True, in_session=in_session)

            write_state(log, children, {"phase": "running", "stop_at": args.stop_at})

            for _ in range(TICK_SECONDS):
                if _STOP["flag"] or datetime.now().time() >= stop_at:
                    break
                time.sleep(1)
    except Exception as exc:  # noqa: BLE001
        log(f"巡检异常，进入收尾：{type(exc).__name__} {exc}")
        exit_code = 1
    finally:
        write_state(log, children, {"phase": "stopping", "stop_at": args.stop_at})
        # 先停监控和模拟（避免收尾期间继续交易），最后停服务
        for c in sorted(children, key=lambda x: (x.name == "server", x.name)):
            c.stop()

    # --- 4. 盘后复盘 ---
    if plan["review"] and not _STOP["flag"]:
        run_once(log, "review", [py, "run_review.py"], timeout=900)

    write_state(log, children, {"phase": "done", "stop_at": args.stop_at})
    log("托管结束")
    log.close()
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()
    except Exception:
        pass
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
