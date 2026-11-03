"""盘中监控模块 —— 15秒级实时监控 + 异常检测。

为什么需要盘中监控：
  盘前简报只能给出"计划"，但市场是动态的。
  持仓股盘中破位、突发利好/利空、异动信号，都需要及时响应。
  没有盘中监控，短线交易等于"闭眼开车"。

监控维度：
  1. 持仓监控：破止损、破位、放量滞涨、时间止损临近
  2. 自选监控：突破买点、放量拉升、异常波动
  3. 市场监控：情绪突变、板块异动、涨跌停潮
  4. 事件监控：公告发布、突发新闻

监控频率：
  - 交易时段（9:30-11:30, 13:00-15:00）：15秒
  - 集合竞价（9:15-9:25, 14:57-15:00）：30秒
  - 盘前盘后：不监控

告警分级：
  P0：必须立即处理（破止损、涨跌停）
  P1：需要关注（放量破位、异动）
  P2：信息提示（板块轮动、事件）

告警去重：
  同一告警在冷却期内（默认30分钟）不重复推送。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, time as dtime, timedelta
from pathlib import Path
from typing import Optional, Callable

log = logging.getLogger(__name__)


@dataclass
class Alert:
    """告警"""
    alert_id: str
    level: str                     # P0/P1/P2
    category: str                  # position/watchlist/market/event
    rule: str                      # 触发的规则名
    code: str = ""
    name: str = ""
    message: str = ""
    data: dict = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    notified: bool = False
    acknowledged: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MonitorConfig:
    """监控配置"""
    interval_seconds: int = 15           # 主监控间隔
    auction_interval_seconds: int = 30   # 集合竞价间隔
    alert_cooldown_minutes: int = 30     # 告警冷却时间

    # 持仓监控阈值
    stop_loss_buffer_pct: float = 0.01   # 距止损1%时预警
    time_stop_warn_days: int = 4         # 持仓4天时提醒（默认5天止损）
    position_drop_pct: float = 0.05      # 单日跌幅>5%告警
    position_gain_pct: float = 0.08      # 单日涨幅>8%告警（提示止盈）

    # 自选监控阈值
    breakout_volume_ratio: float = 2.0   # 突破时量比>2
    watchlist_jump_pct: float = 0.03     # 3%异动

    # 市场监控阈值
    market_surge_limit_up: int = 30      # 5分钟内涨停新增30家
    market_crash_limit_down: int = 30    # 5分钟内跌停新增30家
    index_drop_pct: float = 0.02         # 指数跌幅>2%

    def to_dict(self) -> dict:
        return asdict(self)


class Monitor:
    """盘中监控器"""

    def __init__(
        self,
        config: MonitorConfig = None,
        alert_callback: Callable[[Alert], None] = None,
        state_path: str | Path = None,
    ):
        self.config = config or MonitorConfig()
        self.alert_callback = alert_callback
        self.state_path = Path(state_path) if state_path else None

        # 状态
        self.alerts: list[Alert] = []
        self.alert_cooldowns: dict[str, datetime] = {}  # rule_key -> last_alert_time
        self.last_snapshot: dict = {}                    # 上次快照（用于对比）
        self.market_history: list[dict] = []             # 市场数据历史（近1小时）
        self.running = False

        # 持仓和自选（从外部注入）
        self.positions: dict = {}     # code -> position_info
        self.watchlist: list[str] = []

        if self.state_path:
            self._load_state()

    # ---------------- 主循环 ----------------
    def start(self, data_fetcher: Callable[[], dict], duration_minutes: int = 240):
        """启动监控循环。

        data_fetcher: 数据获取函数，返回 {"stocks": {code: data}, "market": {...}}
        duration_minutes: 运行时长（分钟），默认4小时（覆盖全交易时段）
        """
        self.running = True
        start_time = datetime.now()
        end_time = start_time + timedelta(minutes=duration_minutes)

        log.info(f"监控启动，运行至 {end_time.strftime('%H:%M:%S')}")

        while self.running and datetime.now() < end_time:
            now = datetime.now()

            # 检查是否在监控时段
            if not self._should_monitor(now):
                time.sleep(30)
                continue

            # 获取数据
            try:
                data = data_fetcher()
            except Exception as exc:
                log.warning(f"数据获取失败: {exc}")
                time.sleep(self.config.interval_seconds)
                continue

            # 执行监控
            self._run_checks(data)

            # 保存状态
            if self.state_path:
                self._save_state()

            # 等待下次
            interval = self._get_interval(now)
            time.sleep(interval)

        self.running = False
        log.info("监控结束")

    def stop(self):
        """停止监控"""
        self.running = False

    def _should_monitor(self, now: datetime) -> bool:
        """判断当前时间是否需要监控"""
        if now.weekday() >= 5:
            return False  # 周末

        t = now.time()
        # 交易时段 + 集合竞价
        return (dtime(9, 15) <= t <= dtime(11, 30)) or (dtime(13, 0) <= t <= dtime(15, 0))

    def _get_interval(self, now: datetime) -> int:
        """根据当前时间返回监控间隔"""
        t = now.time()
        # 集合竞价时段间隔较长
        if dtime(9, 15) <= t <= dtime(9, 30) or dtime(14, 55) <= t <= dtime(15, 0):
            return self.config.auction_interval_seconds
        return self.config.interval_seconds

    # ---------------- 监控检查 ----------------
    def _run_checks(self, data: dict):
        """执行所有监控检查"""
        stocks = data.get("stocks", {})
        market = data.get("market", {})

        # 记录市场历史
        self.market_history.append({
            "time": datetime.now().isoformat(),
            "market": market,
        })
        # 只保留近1小时
        cutoff = (datetime.now() - timedelta(hours=1)).isoformat()
        self.market_history = [h for h in self.market_history if h["time"] > cutoff]

        # 1. 持仓监控
        self._check_positions(stocks)

        # 2. 自选监控
        self._check_watchlist(stocks)

        # 3. 市场监控
        self._check_market(market)

        # 更新快照
        self.last_snapshot = data

    def _check_positions(self, stocks: dict):
        """监控持仓"""
        for code, pos_info in self.positions.items():
            data = stocks.get(code)
            if not data:
                continue

            cost = pos_info.get("cost_price", 0)
            stop = pos_info.get("stop_price")
            quantity = pos_info.get("quantity", 0)
            hold_days = pos_info.get("hold_days", 0)
            name = pos_info.get("name", code)

            price = data.get("close") or data.get("price") or 0
            pct_chg = data.get("pct_chg", 0)
            vol_ratio = data.get("vol_ratio", 1)

            if not price:
                continue

            # P0: 破止损
            if stop and price <= stop:
                self._emit_alert(
                    level="P0",
                    category="position",
                    rule="stop_loss_broken",
                    code=code,
                    name=name,
                    message=f"{name} 跌破止损价 {stop:.2f}，当前 {price:.2f}，立即离场",
                    data={"price": price, "stop": stop, "pnl_pct": (price/cost-1)*100 if cost else 0},
                )

            # P0: 跌停
            if pct_chg <= -9.8:
                self._emit_alert(
                    level="P0",
                    category="position",
                    rule="limit_down",
                    code=code,
                    name=name,
                    message=f"{name} 跌停，注意流动性风险",
                    data={"pct_chg": pct_chg},
                )

            # P1: 接近止损
            if stop and price <= stop * (1 + self.config.stop_loss_buffer_pct):
                self._emit_alert(
                    level="P1",
                    category="position",
                    rule="near_stop_loss",
                    code=code,
                    name=name,
                    message=f"{name} 接近止损价（当前 {price:.2f}，止损 {stop:.2f}）",
                    data={"price": price, "stop": stop},
                )

            # P1: 单日大跌
            if pct_chg <= -self.config.position_drop_pct * 100:
                self._emit_alert(
                    level="P1",
                    category="position",
                    rule="daily_drop",
                    code=code,
                    name=name,
                    message=f"{name} 单日跌幅 {pct_chg:.2f}%",
                    data={"pct_chg": pct_chg},
                )

            # P1: 时间止损临近
            if hold_days >= self.config.time_stop_warn_days:
                self._emit_alert(
                    level="P1",
                    category="position",
                    rule="time_stop_warning",
                    code=code,
                    name=name,
                    message=f"{name} 已持仓 {hold_days} 天，接近时间止损",
                    data={"hold_days": hold_days},
                )

            # P2: 单日大涨（提示止盈）
            if pct_chg >= self.config.position_gain_pct * 100:
                self._emit_alert(
                    level="P2",
                    category="position",
                    rule="daily_gain",
                    code=code,
                    name=name,
                    message=f"{name} 单日涨幅 {pct_chg:.2f}%，考虑止盈",
                    data={"pct_chg": pct_chg},
                )

            # P1: 放量滞涨
            if vol_ratio > 2.0 and 0 <= pct_chg < 1.5:
                self._emit_alert(
                    level="P1",
                    category="position",
                    rule="volume_stall",
                    code=code,
                    name=name,
                    message=f"{name} 放量滞涨（量比 {vol_ratio:.1f}，涨幅 {pct_chg:.2f}%）",
                    data={"vol_ratio": vol_ratio, "pct_chg": pct_chg},
                )

    def _check_watchlist(self, stocks: dict):
        """监控自选股"""
        for code in self.watchlist:
            if code in self.positions:
                continue  # 持仓已在上面监控

            data = stocks.get(code)
            if not data:
                continue

            name = data.get("name", code)
            price = data.get("close") or data.get("price") or 0
            pct_chg = data.get("pct_chg", 0)
            vol_ratio = data.get("vol_ratio", 1)
            high20 = data.get("high20")

            if not price:
                continue

            # P1: 放量突破20日高
            if high20 and price >= high20 * 0.999 and vol_ratio >= self.config.breakout_volume_ratio:
                self._emit_alert(
                    level="P1",
                    category="watchlist",
                    rule="breakout",
                    code=code,
                    name=name,
                    message=f"{name} 放量突破20日高点（量比 {vol_ratio:.1f}）",
                    data={"price": price, "high20": high20, "vol_ratio": vol_ratio},
                )

            # P2: 大幅异动
            if abs(pct_chg) >= self.config.watchlist_jump_pct * 100:
                direction = "上涨" if pct_chg > 0 else "下跌"
                self._emit_alert(
                    level="P2",
                    category="watchlist",
                    rule="jump",
                    code=code,
                    name=name,
                    message=f"{name} {direction} {abs(pct_chg):.2f}%",
                    data={"pct_chg": pct_chg},
                )

    def _check_market(self, market: dict):
        """监控市场整体"""
        limit_up = market.get("limit_up", 0)
        limit_down = market.get("limit_down", 0)
        indices = market.get("indices", [])

        # P0: 涨停潮（5分钟内新增大量涨停）
        recent = self._get_recent_market(minutes=5)
        if recent:
            prev_limit_up = recent[0].get("market", {}).get("limit_up", limit_up)
            if limit_up - prev_limit_up >= self.config.market_surge_limit_up:
                self._emit_alert(
                    level="P0",
                    category="market",
                    rule="limit_up_surge",
                    message=f"涨停潮：5分钟内涨停从 {prev_limit_up} 增至 {limit_up}",
                    data={"limit_up": limit_up, "delta": limit_up - prev_limit_up},
                )

            prev_limit_down = recent[0].get("market", {}).get("limit_down", limit_down)
            if limit_down - prev_limit_down >= self.config.market_crash_limit_down:
                self._emit_alert(
                    level="P0",
                    category="market",
                    rule="limit_down_surge",
                    message=f"跌停潮：5分钟内跌停从 {prev_limit_down} 增至 {limit_down}",
                    data={"limit_down": limit_down, "delta": limit_down - prev_limit_down},
                )

        # P1: 指数大跌
        for idx in indices:
            pct = idx.get("pct_chg", 0)
            if pct <= -self.config.index_drop_pct * 100:
                self._emit_alert(
                    level="P1",
                    category="market",
                    rule="index_drop",
                    message=f"{idx.get('name', '')} 跌幅 {pct:.2f}%",
                    data={"index": idx.get("name"), "pct_chg": pct},
                )

    def _get_recent_market(self, minutes: int = 5) -> list[dict]:
        """获取近 N 分钟的市场数据"""
        cutoff = (datetime.now() - timedelta(minutes=minutes)).isoformat()
        return [h for h in self.market_history if h["time"] > cutoff]

    # ---------------- 告警 ----------------
    def _emit_alert(
        self,
        level: str,
        category: str,
        rule: str,
        message: str,
        code: str = "",
        name: str = "",
        data: dict = None,
    ):
        """发出告警（带去重）"""
        # 冷却检查
        cooldown_key = f"{category}:{rule}:{code}"
        last_time = self.alert_cooldowns.get(cooldown_key)
        if last_time:
            elapsed = (datetime.now() - last_time).total_seconds() / 60
            if elapsed < self.config.alert_cooldown_minutes:
                return  # 冷却期内，跳过

        # 创建告警
        alert_id = f"ALT_{datetime.now().strftime('%Y%m%d%H%M%S')}_{len(self.alerts)}"
        alert = Alert(
            alert_id=alert_id,
            level=level,
            category=category,
            rule=rule,
            code=code,
            name=name,
            message=message,
            data=data or {},
        )

        self.alerts.append(alert)
        self.alert_cooldowns[cooldown_key] = datetime.now()

        log.info(f"[{level}] {message}")

        # 触发回调
        if self.alert_callback:
            try:
                self.alert_callback(alert)
            except Exception as exc:
                log.warning(f"告警回调失败: {exc}")

    def get_alerts(
        self,
        level: str = None,
        category: str = None,
        limit: int = 100,
        unnotified_only: bool = False,
    ) -> list[dict]:
        """获取告警"""
        alerts = self.alerts
        if level:
            alerts = [a for a in alerts if a.level == level]
        if category:
            alerts = [a for a in alerts if a.category == category]
        if unnotified_only:
            alerts = [a for a in alerts if not a.notified]
        return [a.to_dict() for a in alerts[-limit:][::-1]]

    def mark_notified(self, alert_ids: list[str]):
        """标记告警已通知"""
        for alert in self.alerts:
            if alert.alert_id in alert_ids:
                alert.notified = True

    def clear_old_alerts(self, keep_hours: int = 24):
        """清理旧告警"""
        cutoff = datetime.now() - timedelta(hours=keep_hours)
        self.alerts = [
            a for a in self.alerts
            if datetime.fromisoformat(a.created_at) > cutoff
        ]

    # ---------------- 状态持久化 ----------------
    def _save_state(self):
        """保存监控状态"""
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            state = {
                "alerts": [a.to_dict() for a in self.alerts[-500:]],  # 只保留最近500条
                "cooldowns": {k: v.isoformat() for k, v in self.alert_cooldowns.items()},
                "updated_at": datetime.now().isoformat(),
            }
            self.state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            log.warning(f"监控状态保存失败: {exc}")

    def _load_state(self):
        """加载监控状态"""
        if not self.state_path or not self.state_path.exists():
            return
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            for a in state.get("alerts", []):
                alert = Alert(**{k: v for k, v in a.items() if k in Alert.__dataclass_fields__})
                self.alerts.append(alert)
            for k, v in state.get("cooldowns", {}).items():
                try:
                    self.alert_cooldowns[k] = datetime.fromisoformat(v)
                except ValueError:
                    pass
        except (OSError, json.JSONDecodeError) as exc:
            log.warning(f"监控状态加载失败: {exc}")

    # ---------------- 持仓/自选注入 ----------------
    def set_positions(self, positions: dict):
        """设置持仓（供监控使用）"""
        self.positions = positions

    def set_watchlist(self, codes: list[str]):
        """设置自选股"""
        self.watchlist = codes


# ---------------- 告警通知器 ----------------
class AlertNotifier:
    """告警通知器（支持多渠道）"""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.channels: list[Callable[[Alert], bool]] = []

        # 根据配置注册通知渠道
        if self.config.get("macos", True):
            self.channels.append(self._notify_macos)
        if self.config.get("log_file"):
            self.channels.append(self._notify_log)
        if self.config.get("webhook"):
            self.channels.append(self._notify_webhook)

    def notify(self, alert: Alert) -> bool:
        """发送告警通知"""
        success = False
        for channel in self.channels:
            try:
                if channel(alert):
                    success = True
            except Exception as exc:
                log.warning(f"通知渠道失败: {exc}")
        return success

    def _notify_macos(self, alert: Alert) -> bool:
        """macOS 系统通知"""
        try:
            import subprocess
            icon = {"P0": "🔴", "P1": "🟡", "P2": "🔵"}.get(alert.level, "⚪")
            title = f"{icon} {alert.level} - {alert.category}"
            message = alert.message[:200]  # macOS 通知有长度限制

            script = f'display notification "{message}" with title "{title}" sound name "Glass"'
            subprocess.run(["osascript", "-e", script], check=False, timeout=5)
            return True
        except Exception as exc:
            log.warning(f"macOS 通知失败: {exc}")
            return False

    def _notify_log(self, alert: Alert) -> bool:
        """写入日志文件"""
        try:
            log_path = Path(self.config["log_file"])
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(alert.to_dict(), ensure_ascii=False) + "\n")
            return True
        except OSError as exc:
            log.warning(f"日志写入失败: {exc}")
            return False

    def _notify_webhook(self, alert: Alert) -> bool:
        """Webhook 通知（钉钉/企业微信等）"""
        try:
            from src.net import get_json
            url = self.config["webhook"]
            payload = {
                "msgtype": "text",
                "text": {
                    "content": f"[{alert.level}] {alert.message}"
                }
            }
            # 简化实现，实际需要签名
            import urllib.request
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=5)
            return True
        except Exception as exc:
            log.warning(f"Webhook 通知失败: {exc}")
            return False
