#!/usr/bin/env python3
"""盘中监控入口 —— 交易时段启动，实时监控持仓与自选。

用法：
    .venv/bin/python run_monitor.py                    # 启动监控（到收盘自动停止）
    .venv/bin/python run_monitor.py --duration 120     # 只监控 120 分钟
    .venv/bin/python run_monitor.py --test             # 测试模式（不等待交易时段）

监控流程：
    1. 加载配置和持仓
    2. 初始化 Monitor + AlertNotifier
    3. 每 15 秒拉取实时行情
    4. 检查止损/异动/突破等条件
    5. 触发告警 → 通知 + 落库

依赖：
    data/market.db       持仓和自选来自最近一次简报
    data/simulation.db   模拟账户持仓（如有）
    config.yaml          监控阈值配置
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from datetime import date, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src import datasource as ds
from src.db import Store
from src.monitor.watcher import Alert, AlertNotifier, Monitor, MonitorConfig

log = logging.getLogger(__name__)

DB_PATH = ROOT / "data" / "market.db"
BRIEFINGS = ROOT / "data" / "briefings"
MONITOR_STATE = ROOT / "data" / "monitor_state.json"


def load_config() -> dict:
    cfg_path = ROOT / "config.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _build_monitor_config(cfg: dict) -> MonitorConfig:
    """从 config.yaml 构建 MonitorConfig"""
    mon = cfg.get("monitor", {})
    thresholds = mon.get("thresholds", {})

    return MonitorConfig(
        interval_seconds=mon.get("interval_seconds", 15),
        auction_interval_seconds=mon.get("auction_interval_seconds", 30),
        alert_cooldown_minutes=mon.get("alert_cooldown_minutes", 30),
        stop_loss_buffer_pct=thresholds.get("stop_loss_buffer_pct", 0.01),
        time_stop_warn_days=thresholds.get("time_stop_warn_days", 4),
        position_drop_pct=thresholds.get("position_drop_pct", 0.05),
        position_gain_pct=thresholds.get("position_gain_pct", 0.08),
        breakout_volume_ratio=thresholds.get("breakout_volume_ratio", 2.0),
        watchlist_jump_pct=thresholds.get("watchlist_jump_pct", 0.03),
        market_surge_limit_up=thresholds.get("market_surge_limit_up", 30),
        market_crash_limit_down=thresholds.get("market_crash_limit_down", 30),
        index_drop_pct=thresholds.get("index_drop_pct", 0.02),
    )


def _load_positions(cfg: dict) -> dict:
    """加载持仓信息供监控使用。

    优先从模拟账户加载，其次从 config.yaml 的 holdings 加载。
    """
    positions = {}

    # 1. 模拟账户持仓
    sim_db = ROOT / "data" / "simulation.db"
    if sim_db.exists():
        try:
            from src.simulation.account import Account
            acct = Account(db_path=sim_db)
            for code, pos in acct.positions.items():
                positions[code] = {
                    "name": pos.name,
                    "cost_price": pos.cost_price,
                    "quantity": pos.quantity,
                    "stop_price": (pos.original_plan or {}).get("stop"),
                    "hold_days": _calc_hold_days(pos.open_date),
                    "strategy": pos.strategy,
                }
        except Exception as exc:
            log.warning("模拟账户加载失败: %s", exc)

    # 2. config.yaml 中的 holdings（真实持仓手动记录）
    for h in cfg.get("holdings") or []:
        code = str(h["code"]).zfill(6)
        if code not in positions:
            positions[code] = {
                "name": h.get("name", code),
                "cost_price": float(h.get("cost", 0)),
                "quantity": int(h.get("qty", 0)),
                "stop_price": h.get("stop"),
                "hold_days": _calc_hold_days(h.get("opened")),
                "strategy": "manual",
            }

    # 3. 从最新简报中补充止损价
    latest_brief = _load_latest_brief()
    if latest_brief:
        for row in latest_brief.get("watch", []):
            code = row.get("code")
            if code in positions and not positions[code].get("stop_price"):
                plan = row.get("plan") or {}
                if plan.get("stop"):
                    positions[code]["stop_price"] = plan["stop"]

    return positions


def _load_watchlist(cfg: dict) -> list[str]:
    """加载自选股代码列表"""
    return [str(item["code"]).zfill(6) for item in cfg.get("watchlist", [])]


def _load_latest_brief() -> dict | None:
    """加载最新简报"""
    if not BRIEFINGS.exists():
        return None
    jsons = sorted(BRIEFINGS.glob("*.json"), reverse=True)
    for p in jsons[:5]:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
    return None


def _calc_hold_days(opened: str | None) -> int:
    """粗算持仓工作日数"""
    if not opened:
        return 0
    try:
        d0 = date.fromisoformat(str(opened))
    except (ValueError, TypeError):
        return 0
    d1 = date.today()
    if d1 <= d0:
        return 0
    n = 0
    cur = d0
    while cur < d1:
        cur = date.fromordinal(cur.toordinal() + 1)
        if cur.weekday() < 5:
            n += 1
    return n


def _make_data_fetcher(watchlist: list[str], positions: dict):
    """创建数据获取函数（供 Monitor.start 使用）"""
    all_codes = list(set(watchlist + list(positions.keys())))

    def fetcher() -> dict:
        """拉取实时行情"""
        result = {"stocks": {}, "market": {}}

        # 个股实时数据
        spots = ds.spot(all_codes)
        for code, data in (spots or {}).items():
            result["stocks"][code] = data

        # 市场整体数据
        breadth = ds.breadth()
        if breadth:
            result["market"] = {
                "limit_up": breadth.get("limit_up", 0),
                "limit_down": breadth.get("limit_down", 0),
                "up_ratio": breadth.get("up_ratio", 0.5),
                "indices": [],
            }

        # 指数
        indices, _ = ds.index_quotes()
        if indices:
            result["market"]["indices"] = indices

        return result

    return fetcher


def _save_alerts_to_db(alerts: list[dict]):
    """将告警落库"""
    if not alerts:
        return
    try:
        store = Store(DB_PATH)
        store.save_monitor_alerts(date.today().isoformat(), alerts)
        store.close()
    except Exception as exc:
        log.warning("告警落库失败: %s", exc)


def run_monitor(duration: int = None, test_mode: bool = False):
    """启动盘中监控"""
    cfg = load_config()
    mon_cfg = _build_monitor_config(cfg)

    # 加载持仓和自选
    positions = _load_positions(cfg)
    watchlist = _load_watchlist(cfg)

    log.info("持仓 %d 只，自选 %d 只", len(positions), len(watchlist))

    # 初始化通知器
    notify_cfg = cfg.get("monitor", {}).get("notify", {})
    if notify_cfg.get("log_file"):
        notify_cfg["log_file"] = str(ROOT / notify_cfg["log_file"])
    notifier = AlertNotifier(notify_cfg)

    # 告警回调：通知 + 落库
    pending_alerts = []

    def on_alert(alert: Alert):
        notifier.notify(alert)
        pending_alerts.append(alert.to_dict())
        # 每 10 条落库一次
        if len(pending_alerts) >= 10:
            _save_alerts_to_db(pending_alerts)
            pending_alerts.clear()

    # 初始化监控器
    monitor = Monitor(
        config=mon_cfg,
        alert_callback=on_alert,
        state_path=MONITOR_STATE,
    )
    monitor.set_positions(positions)
    monitor.set_watchlist(watchlist)

    # 数据获取器
    fetcher = _make_data_fetcher(watchlist, positions)

    # 优雅退出
    def handle_signal(signum, frame):
        log.info("收到退出信号，停止监控…")
        monitor.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # 计算运行时长
    if duration:
        run_minutes = duration
    elif test_mode:
        run_minutes = 5
    else:
        # 默认运行到 15:05（收盘后 5 分钟）
        now = datetime.now()
        close_time = now.replace(hour=15, minute=5, second=0)
        if now >= close_time:
            log.info("已收盘，无需监控")
            return
        run_minutes = int((close_time - now).total_seconds() / 60) + 1

    print(f"盘中监控启动")
    print(f"  持仓: {', '.join(f'{v['name']}({k})' for k, v in positions.items()) or '无'}")
    print(f"  自选: {len(watchlist)} 只")
    print(f"  间隔: {mon_cfg.interval_seconds}秒")
    print(f"  时长: {run_minutes}分钟")
    print(f"  按 Ctrl+C 停止")
    print()

    if test_mode:
        # 测试模式：跳过时间检查，立即执行一轮
        log.info("测试模式：执行一轮检查")
        data = fetcher()
        monitor._run_checks(data)
        alerts = monitor.get_alerts()
        print(f"检查完成，产生 {len(alerts)} 条告警")
        for a in alerts[:10]:
            print(f"  [{a['level']}] {a['message']}")
        _save_alerts_to_db(alerts)
    else:
        monitor.start(fetcher, duration_minutes=run_minutes)

    # 最终落库
    remaining = monitor.get_alerts(unnotified_only=False)
    _save_alerts_to_db(remaining)
    log.info("监控结束，共 %d 条告警", len(monitor.alerts))


def main() -> int:
    ap = argparse.ArgumentParser(description="盘中监控")
    ap.add_argument("--duration", type=int, default=None, help="监控时长（分钟）")
    ap.add_argument("--test", action="store_true", help="测试模式（不等交易时段）")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    run_monitor(duration=args.duration, test_mode=args.test)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
