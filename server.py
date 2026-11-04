#!/usr/bin/env python3
"""本地 Web 控制台 —— 给交易 Agent 一个能点的界面。

    .venv/bin/python server.py            # 打开 http://127.0.0.1:8765
    .venv/bin/python server.py --port 9000 --no-open

只监听 127.0.0.1，不对外暴露。所有数据读写都落回 config.yaml / data/，
界面本身不持有任何状态，关掉就没了，不会出现"界面上改了但文件没改"的情况。
"""
from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import re
import subprocess
import threading
import traceback
import webbrowser
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import run_brief
from src import configstore, notify, pulse
from src.db import Store
from src.simulation.account import Account
from src.simulation.matcher import Matcher, RiskManager, market_session
from src.review.reviewer import ReviewCalculator, format_review_report
from src.strategies import list_strategies, select_strategies

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DATA = ROOT / "data"
CONFIG = ROOT / "config.yaml"
BACKUP = DATA / "backup"
BRIEFINGS = DATA / "briefings"
DB_PATH = DATA / "market.db"
SIM_DB = DATA / "simulation.db"
SIM_LIVE_STATE = DATA / "sim_live_state.json"

BRIEF_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}|demo)\.html$")

# 四策略并行模拟账户（account_id 即策略名），与 run_sim.py / run_sim_live.py 同源
SIM_STRATEGY_IDS = ["board_hit", "trend_pullback", "oversold_bounce", "event_driven"]
SIM_STRATEGY_CN = {
    "board_hit": "打板/半路",
    "trend_pullback": "趋势波段",
    "oversold_bounce": "超跌反弹",
    "event_driven": "事件驱动",
}


# ---------------------------------------------------------------- 运行管理
class _LogHandler(logging.Handler):
    def __init__(self, sink):
        super().__init__()
        self.sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.sink(record.getMessage())
        except Exception:  # 日志本身不能把主流程搞崩
            pass


class RunManager:
    """同一时刻只允许一次运行：并发跑会同时打同一个接口，容易被限流。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.running = False
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.result: dict | None = None
        self.log: deque[str] = deque(maxlen=300)

    def add(self, msg: str) -> None:
        self.log.append(f"{datetime.now():%H:%M:%S}  {msg}")

    def start(self, notify_enabled: bool) -> bool:
        with self._lock:
            if self.running:
                return False
            self.running = True
            self.result = None
            self.log.clear()
            self.started_at = datetime.now().strftime("%H:%M:%S")
            self.finished_at = None
        threading.Thread(target=self._work, args=(notify_enabled,), daemon=True).start()
        return True

    def _work(self, notify_enabled: bool) -> None:
        handler = _LogHandler(self.add)
        root_logger = logging.getLogger()
        old_level = root_logger.level
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.INFO)
        try:
            self.add("开始采集行情（主源东财，失败自动切腾讯）…")
            result = run_brief.run_once(CONFIG, notify_enabled=notify_enabled, quiet=True)
            self.result = result
            self.add(
                f"完成：{result['watch_count']} 只标的，"
                f"{result['plans_ready']} 个可执行计划，{result['alerts']} 条提醒"
            )
            if result.get("data_health"):
                self.add("数据缺口：" + "；".join(result["data_health"]))
        except Exception as exc:  # noqa: BLE001 —— 界面要把真实错误显示出来
            self.result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            self.add(f"失败：{type(exc).__name__}: {exc}")
            self.add(traceback.format_exc(limit=3).replace("\n", " | "))
        finally:
            root_logger.removeHandler(handler)
            root_logger.setLevel(old_level)
            with self._lock:
                self.running = False
                self.finished_at = datetime.now().strftime("%H:%M:%S")

    def status(self) -> dict:
        return {
            "running": self.running,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "log": list(self.log),
        }


RUNNER = RunManager()


# ---------------------------------------------------------------- 数据汇总
def _scan_briefings() -> tuple[Path | None, list[dict]]:
    if not BRIEFINGS.exists():
        return None, []
    items = []
    for p in BRIEFINGS.glob("*.html"):
        if not BRIEF_FILE_RE.match(p.name):
            continue
        j = p.with_suffix(".json")
        items.append(
            {
                "date": p.stem,
                "html": f"/briefings/{p.name}",
                "has_json": j.exists(),
                "size": p.stat().st_size,
                "mtime": datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M"),
                "is_demo": p.stem == "demo",
            }
        )
    # 真实日期简报按日期倒序在前，排版演示样例沉底
    items.sort(key=lambda it: (it["is_demo"], "" if it["is_demo"] else ""), reverse=False)
    dated = sorted((it for it in items if not it["is_demo"]),
                   key=lambda it: it["date"], reverse=True)
    demos = [it for it in items if it["is_demo"]]
    items = dated + demos

    latest = next(
        (BRIEFINGS / f"{it['date']}.json" for it in items
         if (BRIEFINGS / f"{it['date']}.json").exists()),
        None,
    )
    return latest, items


def _read_brief(path: Path | None) -> dict | None:
    if not path or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def collect_state() -> dict:
    cfg = configstore.load(CONFIG)
    latest, history = _scan_briefings()
    brief = _read_brief(latest)

    store = Store(DB_PATH)
    try:
        runs = store.runs(60)
        alerts = store.alerts_recent(300)
        plans = store.plans_recent(200)
        stats = store.plan_stats()
        counts = store.alert_counts()
    finally:
        store.close()

    return {
        "config": cfg,
        "brief": brief,
        "history": history,
        "runs": runs,
        "alerts": alerts,
        "plans": plans,
        "stats": stats,
        "alert_counts": counts,
        "meta": {
            "root": str(ROOT),
            "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "config_path": str(CONFIG),
        },
    }


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "TradingAgentUI/1.0"

    # ---------- 基础 ----------
    def log_message(self, fmt: str, *args) -> None:  # 静音默认访问日志
        return

    def _send(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(
            json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
            "application/json; charset=utf-8",
            code,
        )

    def _err(self, msg: str, code: int = 400, extra: dict | None = None) -> None:
        payload = {"ok": False, "error": msg}
        if extra:
            payload.update(extra)
        self._json(payload, code)

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"请求体不是合法 JSON：{exc}") from exc

    def _same_origin(self) -> bool:
        """挡住别的网页偷偷 POST 到本地端口（CSRF）。"""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host") or ""
        return origin.endswith(host) or "127.0.0.1" in origin or "localhost" in origin

    # ---------- GET ----------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/api/"):
                return self._api_get(path, parse_qs(parsed.query))
            if path.startswith("/briefings/"):
                return self._briefing_file(path.rsplit("/", 1)[-1])
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            return self._static(path.lstrip("/") or "index.html")
        except Exception as exc:  # noqa: BLE001
            self._err(f"服务器错误：{type(exc).__name__}: {exc}", 500)

    def _api_get(self, path: str, qs: dict) -> None:
        if path == "/api/state":
            return self._json({"ok": True, "data": collect_state()})
        if path == "/api/run/status":
            return self._json({"ok": True, "data": RUNNER.status()})
        if path == "/api/alerts":
            limit = int((qs.get("limit") or ["200"])[0])
            level = (qs.get("level") or [None])[0]
            day = (qs.get("date") or [None])[0]
            store = Store(DB_PATH)
            try:
                rows = store.alerts_recent(limit, level, day)
            finally:
                store.close()
            return self._json({"ok": True, "data": rows})
        if path == "/api/config":
            return self._json({"ok": True, "data": configstore.load(CONFIG)})
        if path == "/api/pulse":
            return self._pulse()
        if path == "/api/activity":
            limit = int((qs.get("limit") or ["60"])[0])
            day = (qs.get("date") or [None])[0]
            return self._activity(limit, day)
        if path == "/api/simulation/accounts":
            return self._sim_accounts()
        if path == "/api/simulation/compare":
            return self._sim_compare()
        if path == "/api/simulation/kline":
            code = (qs.get("code") or [None])[0]
            days = int((qs.get("days") or ["160"])[0])
            return self._sim_kline(code, days)
        if path == "/api/simulation/summary":
            return self._sim_summary((qs.get("account") or [None])[0])
        if path == "/api/simulation/positions":
            return self._sim_positions((qs.get("account") or [None])[0])
        if path == "/api/simulation/trades":
            limit = int((qs.get("limit") or ["50"])[0])
            return self._sim_trades(limit, (qs.get("account") or [None])[0])
        if path == "/api/simulation/performance":
            return self._sim_performance((qs.get("account") or [None])[0])
        if path == "/api/simulation/snapshots":
            limit = int((qs.get("limit") or ["60"])[0])
            return self._sim_snapshots(limit, (qs.get("account") or [None])[0])
        if path == "/api/review/latest":
            return self._review_latest()
        if path == "/api/review/history":
            limit = int((qs.get("limit") or ["30"])[0])
            return self._review_history(limit)
        if path == "/api/signals":
            day = (qs.get("date") or [None])[0]
            strategy = (qs.get("strategy") or [None])[0]
            return self._signals(day, strategy)
        if path == "/api/sentiment":
            limit = int((qs.get("limit") or ["60"])[0])
            return self._sentiment(limit)
        if path == "/api/monitor/alerts":
            limit = int((qs.get("limit") or ["100"])[0])
            level = (qs.get("level") or [None])[0]
            return self._monitor_alerts(limit, level)
        if path == "/api/strategies":
            return self._json({"ok": True, "data": list_strategies()})
        if path == "/api/screening":
            day = (qs.get("date") or [None])[0]
            return self._screening(day)
        return self._err("未知接口", 404)

    def _briefing_file(self, name: str) -> None:
        if not BRIEF_FILE_RE.match(name):
            return self._err("文件名不合法", 400)
        p = BRIEFINGS / name
        if not p.exists():
            return self._err("简报不存在", 404)
        self._send(p.read_bytes(), "text/html; charset=utf-8")

    def _static(self, rel: str) -> None:
        rel = rel.split("?")[0]
        target = (WEB / rel).resolve()
        try:
            target.relative_to(WEB.resolve())
        except ValueError:
            return self._err("路径越界", 403)
        if target.is_dir():
            target = target / "index.html"
        if not target.exists():
            return self._err(f"文件不存在：{rel}", 404)
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(target.read_bytes(), ctype)

    # ---------- POST ----------
    def do_POST(self) -> None:  # noqa: N802
        if not self._same_origin():
            return self._err("拒绝跨站请求", 403)
        path = urlparse(self.path).path
        try:
            body = self._body()
        except ValueError as exc:
            return self._err(str(exc))

        try:
            if path == "/api/config":
                return self._save_config(body)
            if path == "/api/run":
                return self._start_run(body)
            if path == "/api/notify/test":
                return self._test_notify()
            if path == "/api/reveal":
                return self._reveal(body)
            if path == "/api/simulation/buy":
                return self._sim_buy(body)
            if path == "/api/simulation/sell":
                return self._sim_sell(body)
            if path == "/api/review/run":
                return self._review_run(body)
        except configstore.ConfigError as exc:
            return self._err("配置校验未通过", 400, {"errors": exc.errors})
        except Exception as exc:  # noqa: BLE001
            return self._err(f"服务器错误：{type(exc).__name__}: {exc}", 500)
        return self._err("未知接口", 404)

    def _save_config(self, body: dict) -> None:
        current = configstore.load(CONFIG)
        patch = body.get("patch") if isinstance(body.get("patch"), dict) else body.get("config")
        if not isinstance(patch, dict):
            return self._err("缺少 patch 字段")
        merged = _deep_merge(current, patch)
        result = configstore.save(CONFIG, merged, BACKUP)
        self._json({"ok": True, "data": {"backup": result["backup"]}})

    def _start_run(self, body: dict) -> None:
        notify_enabled = bool(body.get("notify", True))
        if not RUNNER.start(notify_enabled):
            return self._err("已有任务在运行，请稍候", 409)
        self._json({"ok": True, "data": {"started": True}}, 202)

    def _test_notify(self) -> None:
        cfg = configstore.load(CONFIG)
        n = cfg.get("notify") or {}
        notify.macos_notify(n.get("title", "交易 Agent"), "通知测试成功：这是本地提醒。", n.get("sound"))
        self._json({"ok": True, "data": {"sent": True}})

    def _reveal(self, body: dict) -> None:
        target = str(body.get("target") or "data")
        mapping = {"data": DATA, "briefings": BRIEFINGS, "config": CONFIG.parent}
        path = mapping.get(target, DATA)
        subprocess.run(["open", str(path)], check=False)
        self._json({"ok": True, "data": {"path": str(path)}})

    # ---------- 模拟交易 ----------
    def _get_account(self, account: str = None) -> Account:
        SIM_DB.parent.mkdir(parents=True, exist_ok=True)
        cfg = configstore.load(CONFIG)
        sim_cfg = cfg.get("simulation", {})
        return Account(
            account_id=account or sim_cfg.get("account_id", "default"),
            initial_capital=sim_cfg.get("initial_capital", 100000),
            db_path=SIM_DB,
            fee_config=sim_cfg.get("fees"),
        )

    @staticmethod
    def _quote_prices(codes: list[str]) -> dict:
        """批量取实时价（腾讯源），返回 {code: price}；失败返回空 dict。"""
        codes = [c for c in dict.fromkeys(codes) if c]
        if not codes:
            return {}
        try:
            from src import tx
            out = {}
            for q in tx.quotes([tx.tx_code(c) for c in codes]):
                c = str(q.get("code", "")).zfill(6)
                if q.get("close"):
                    out[c] = q["close"]
            return out
        except Exception:  # noqa: BLE001
            return {}

    def _refresh_accounts(self, accounts: list[Account]) -> None:
        """给一组账户刷新持仓市值：优先实时价，退回最近快照价。

        DB 只存成本/数量，不存实时市值；不刷新的话 total_assets 会只剩现金。
        """
        codes = [c for a in accounts for c in a.positions]
        prices = self._quote_prices(codes)
        for a in accounts:
            if prices:
                a.update_market_prices(prices)
            # 实时价缺失的持仓，用最近一次快照里的现价兜底
            if a.daily_snapshots:
                last = a.daily_snapshots[-1]
                snap = {p.get("code"): p.get("current_price")
                        for p in last.get("positions", []) if p.get("current_price")}
                missing = {c: snap[c] for c in a.positions if c in snap and not a.positions[c].current_price}
                if missing:
                    a.update_market_prices(missing)

    def _sim_accounts(self) -> None:
        """四策略账户总览 + 当前市场时段 + 盘中自动循环心跳。"""
        cfg = configstore.load(CONFIG)
        capital = cfg.get("simulation", {}).get("initial_capital", 100000)
        accts = [self._get_account(aid) for aid in SIM_STRATEGY_IDS]
        self._refresh_accounts(accts)
        accounts = []
        for acct in accts:
            s = acct.get_summary()
            accounts.append({
                "account_id": acct.account_id,
                "cn": SIM_STRATEGY_CN.get(acct.account_id, acct.account_id),
                "initial_capital": s.get("initial_capital", capital),
                "total_assets": s.get("total_assets"),
                "cash": s.get("cash"),
                "position_value": s.get("position_value"),
                "position_ratio": s.get("position_ratio"),
                "total_pnl": s.get("total_pnl"),
                "total_return_pct": s.get("total_return_pct"),
                "position_count": s.get("position_count"),
                "trade_count": s.get("trade_count"),
            })
        live = {}
        try:
            live = json.loads(SIM_LIVE_STATE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            live = {}
        self._json({"ok": True, "data": {
            "session": market_session(),
            "accounts": accounts,
            "live": {
                "running": live.get("running", False),
                "updated_at": live.get("updated_at"),
                "bought_today": live.get("bought_today", False),
                "snapshot_done": live.get("snapshot_done", False),
            },
        }})

    def _pulse(self) -> None:
        """当日资金脉搏：板块主力榜/个股主力双榜/北向/涨停炸板计数 + 日内趋势。"""
        store = Store(DB_PATH)
        try:
            data = pulse.collect(store=store)
            today = datetime.now().strftime("%Y-%m-%d")
            data = {**data, "trend": store.pulse_snapshots_today(today, limit=120)}
        finally:
            store.close()
        return self._json({"ok": True, "data": data})

    def _activity(self, limit: int = 60, day: str = None) -> None:
        """系统活动流：run_sim_live 每轮落盘的 scan/skip/buy/sell/snapshot 等。"""
        store = Store(DB_PATH)
        try:
            rows = store.activity_recent(day, limit)
        finally:
            store.close()
        return self._json({"ok": True, "data": rows})

    def _sim_compare(self) -> None:
        """四策略横向对比：每个账户的概览 + 完整持仓 + 空仓原因（供对比视图一次取全）。"""
        cfg = configstore.load(CONFIG)
        capital = cfg.get("simulation", {}).get("initial_capital", 100000)
        accts = [self._get_account(aid) for aid in SIM_STRATEGY_IDS]
        self._refresh_accounts(accts)
        live = {}
        try:
            live = json.loads(SIM_LIVE_STATE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            live = {}
        notes = live.get("notes") or {}
        accounts = []
        for acct in accts:
            s = acct.get_summary()
            accounts.append({
                "account_id": acct.account_id,
                "cn": SIM_STRATEGY_CN.get(acct.account_id, acct.account_id),
                "initial_capital": s.get("initial_capital", capital),
                "total_assets": s.get("total_assets"),
                "cash": s.get("cash"),
                "position_value": s.get("position_value"),
                "position_ratio": s.get("position_ratio"),
                "total_pnl": s.get("total_pnl"),
                "unrealized_pnl": s.get("unrealized_pnl"),
                "total_return_pct": s.get("total_return_pct"),
                "position_count": s.get("position_count"),
                "trade_count": s.get("trade_count"),
                "positions": acct.get_positions(),
                "note": notes.get(acct.account_id, ""),
            })
        self._json({"ok": True, "data": {
            "session": market_session(),
            "accounts": accounts,
            "live": {
                "running": live.get("running", False),
                "updated_at": live.get("updated_at"),
                "bought_today": live.get("bought_today", False),
                "snapshot_done": live.get("snapshot_done", False),
            },
        }})

    def _sim_kline(self, code: str, days: int = 160) -> None:
        """某标的日K + 四策略账户在该标的上的全部买卖点（供 K 线图画点）。"""
        if not code:
            return self._json({"ok": False, "error": "缺少 code 参数"})
        code = str(code).zfill(6)
        candles, src_err = [], ""
        try:
            from src import tx
            raw = tx.kline(code, days=min(max(days, 60), 320))
            candles = [
                {"date": r.get("date"), "open": r.get("open"), "close": r.get("close"),
                 "high": r.get("high"), "low": r.get("low")}
                for r in raw
            ]
        except Exception as exc:  # noqa: BLE001
            src_err = f"日线源不可用：{type(exc).__name__} {str(exc)[:60]}"
        marks, name = [], ""
        for aid in SIM_STRATEGY_IDS:
            acct = self._get_account(aid)
            for t in acct.get_trades(500):
                if str(t.get("code", "")).zfill(6) != code:
                    continue
                if not name:
                    name = t.get("name", "")
                marks.append({
                    "date": t.get("trade_date"), "direction": t.get("direction"),
                    "price": t.get("price"), "quantity": t.get("quantity"),
                    "account": aid, "cn": SIM_STRATEGY_CN.get(aid, aid),
                    "reason": t.get("reason") or "",
                })
        for aid in SIM_STRATEGY_IDS:
            if name:
                break
            acct = self._get_account(aid)
            pos = acct.positions.get(code)
            if pos:
                name = pos.name
        marks.sort(key=lambda m: (m.get("date") or "", m.get("direction") or ""))
        self._json({"ok": True, "data": {
            "code": code, "name": name, "candles": candles,
            "marks": marks, "source_error": src_err,
        }})

    def _sim_summary(self, account: str = None) -> None:
        acct = self._get_account(account)
        self._refresh_accounts([acct])
        data = acct.get_summary()
        data["cn"] = SIM_STRATEGY_CN.get(acct.account_id, acct.account_id)
        data["session"] = market_session()
        self._json({"ok": True, "data": data})

    def _sim_positions(self, account: str = None) -> None:
        acct = self._get_account(account)
        self._refresh_accounts([acct])
        self._json({"ok": True, "data": acct.get_positions()})

    def _sim_trades(self, limit: int, account: str = None) -> None:
        acct = self._get_account(account)
        self._json({"ok": True, "data": acct.get_trades(limit)})

    def _sim_performance(self, account: str = None) -> None:
        acct = self._get_account(account)
        self._json({"ok": True, "data": acct.get_performance()})

    def _sim_snapshots(self, limit: int, account: str = None) -> None:
        acct = self._get_account(account)
        snaps = [
            {
                "date": s.get("date"), "total_assets": s.get("total_assets"),
                "cash": s.get("cash"), "position_value": s.get("position_value"),
                "position_ratio": s.get("position_ratio"), "total_pnl": s.get("total_pnl"),
                "total_return_pct": s.get("total_return_pct"),
                "position_count": s.get("position_count"),
            }
            for s in acct.daily_snapshots[-limit:]
        ]
        self._json({"ok": True, "data": snaps})

    def _sim_buy(self, body: dict) -> None:
        code = str(body.get("code", "")).zfill(6)
        name = body.get("name", code)
        price = float(body.get("price", 0))
        quantity = int(body.get("quantity", 0))
        strategy = body.get("strategy", "manual")

        if not code or not price or not quantity:
            return self._err("缺少必要参数: code, price, quantity")

        acct = self._get_account()
        # 风控检查
        cfg = configstore.load(CONFIG)
        risk_cfg = cfg.get("simulation", {}).get("risk", {})
        rm = RiskManager(acct, risk_cfg)
        ok, reason = rm.check_buy(code, price, quantity)
        if not ok:
            return self._err(f"风控拒绝: {reason}")

        result = acct.buy(code, name, price, quantity, strategy=strategy)
        if result["success"]:
            rm.on_trade_executed()
            acct.save()
        self._json({"ok": result["success"], "data": result})

    def _sim_sell(self, body: dict) -> None:
        code = str(body.get("code", "")).zfill(6)
        price = float(body.get("price", 0))
        quantity = int(body.get("quantity", 0))
        reason = body.get("reason", "manual")

        if not code or not price or not quantity:
            return self._err("缺少必要参数: code, price, quantity")

        acct = self._get_account()
        result = acct.sell(code, price, quantity, reason=reason)
        if result["success"]:
            acct.save()
        self._json({"ok": result["success"], "data": result})

    # ---------- 复盘 ----------
    def _review_latest(self) -> None:
        store = Store(DB_PATH)
        try:
            reviews = store.reviews_recent(1)
            if not reviews:
                return self._json({"ok": True, "data": None})
            detail = store.review_by_date(reviews[0]["date"])
            self._json({"ok": True, "data": detail})
        finally:
            store.close()

    def _review_history(self, limit: int) -> None:
        store = Store(DB_PATH)
        try:
            self._json({"ok": True, "data": store.reviews_recent(limit)})
        finally:
            store.close()

    def _review_run(self, body: dict) -> None:
        """触发复盘（异步）"""
        import run_review
        day = body.get("date")
        try:
            review = run_review.run_review(day)
            if review:
                self._json({"ok": True, "data": {"date": review.date, "score": review.overall_score}})
            else:
                self._err("复盘失败：缺少当日简报数据")
        except Exception as exc:
            self._err(f"复盘异常: {exc}", 500)

    # ---------- 策略信号 ----------
    def _signals(self, day: str | None, strategy: str | None) -> None:
        store = Store(DB_PATH)
        try:
            if not day:
                # 取最近有信号的日期
                runs = store.runs(5)
                day = runs[0]["run_date"] if runs else None
            if not day:
                return self._json({"ok": True, "data": []})
            signals = store.signals_by_date(day, strategy)
            self._json({"ok": True, "data": {"date": day, "signals": signals, "stats": store.signal_stats(strategy)}})
        finally:
            store.close()

    # ---------- 选股结果（含理由） ----------
    def _screening(self, day: str | None) -> None:
        store = Store(DB_PATH)
        try:
            dates = store.screening_dates(30)
            if not day:
                day = dates[0] if dates else None
            if not day:
                return self._json({"ok": True, "data": {"date": None, "dates": [], "rows": []}})
            rows = store.screening_with_detail(day)
            self._json({"ok": True, "data": {"date": day, "dates": dates, "rows": rows}})
        finally:
            store.close()

    # ---------- 情绪历史 ----------
    def _sentiment(self, limit: int) -> None:
        store = Store(DB_PATH)
        try:
            history = store.sentiment_history(limit)
            latest = history[0] if history else None
            self._json({"ok": True, "data": {"latest": latest, "history": history}})
        finally:
            store.close()

    # ---------- 监控告警 ----------
    def _monitor_alerts(self, limit: int, level: str | None) -> None:
        store = Store(DB_PATH)
        try:
            alerts = store.monitor_alerts_recent(limit, level)
            stats = store.monitor_alert_stats()
            self._json({"ok": True, "data": {"alerts": alerts, "stats": stats}})
        finally:
            store.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="交易 Agent 本地控制台")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"交易 Agent 控制台已启动：{url}")
    print("按 Ctrl+C 停止。")
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
