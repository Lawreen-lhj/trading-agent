#!/usr/bin/env python3
"""盘中定时自动循环模拟交易 —— 结合开盘/收盘时间自动买卖。

和 run_sim.py 的区别：
  run_sim.py 是「跑一遍」的一次性建仓器（盘后运行＝以收盘价模拟建仓）。
  run_sim_live.py 是「常驻盘中循环」：交易日 09:30 自动开始，
    ① 开盘后按各策略逻辑用实时价建仓（当日只建一次）
    ② 盘中每隔 N 秒轮询实时价，对 T+1 可卖持仓评估止损/止盈/炸板/到期并卖出
    ③ 临近收盘做最终到期处理并写当日快照，15:00 后自动收尾退出

时段判断统一走 src.simulation.matcher.market_session()（与 server / WebUI 同源）。

为什么盘中卖出用 account.sell 而不是 Matcher：
  Matcher 的市价卖单以「当日开盘价」成交，那是给盘后回测用的口径。
  盘中实时触发止损/止盈应当以「当前价」成交，所以直接调 account.sell(current_price)，
  并自行做跌停封死不可卖的守卫。买入仍走 Matcher（用腾讯实时 OHLC 撮合，口径一致）。

T+1 的真实体现：
  当日买入的仓位 frozen、available=0，当天不会被卖出引擎处理；
  次日 on_new_day 解冻后 available>0，才进入止损/止盈/到期评估。完全符合 A 股规则。

用法：
    python3 run_sim_live.py                 # 交易日盘中常驻循环（到 15:00 收尾）
    python3 run_sim_live.py --interval 30   # 盘中轮询间隔（秒，默认 60）
    python3 run_sim_live.py --duration 120  # 只运行 120 分钟
    python3 run_sim_live.py --once          # 只跑一轮（建仓+卖出评估+快照）后退出，便于验证
    python3 run_sim_live.py --once --force  # 非交易时段也强制跑一轮（用最新可得行情）

注意：纯本地模拟，不存在任何真实下单代码路径。
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from datetime import datetime, date, time as dtime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src import configstore  # noqa: E402
from src.simulation.account import Account  # noqa: E402
from src.simulation.matcher import (  # noqa: E402
    Matcher, RiskManager, market_session, calc_limit_prices,
)
from src.strategies import get_strategy, select_strategies  # noqa: E402

# 复用一次性运行器里的采集/构建逻辑，避免重复实现
import run_sim  # noqa: E402
from run_sim import (  # noqa: E402
    STRATEGY_IDS, STRATEGY_CN, KLINE_DEPENDENT, SIM_DB,
    fetch_quotes, fetch_board_pool, probe_kline, compute_sentiment,
    fetch_events, build_stocks, _order_type_for,
)

log = logging.getLogger("sim_live")

CONFIG_PATH = ROOT / "config.yaml"
STATE_PATH = ROOT / "data" / "sim_live_state.json"


# ═══════════════════════════════════════════════════════════════════════
# 状态文件（供 WebUI 展示自动循环是否在跑、最后活动时间）
# ═══════════════════════════════════════════════════════════════════════

def write_state(payload: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": datetime.now().isoformat(), **payload}
    try:
        STATE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("状态写入失败: %s", exc)


def read_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


# ═══════════════════════════════════════════════════════════════════════
# 建仓（开盘后当日一次）
# ═══════════════════════════════════════════════════════════════════════

# 每个策略账户最近一轮建仓轮的结论（空仓原因/建仓笔数），随心跳落盘供 WebUI 对比视图展示
LAST_PER_NOTE: dict = {}


def buy_pass(accounts: dict, risks: dict, cfg: dict, capital: float,
             kline_ok: bool, events: list) -> dict:
    """用实时价扫描并为每个策略账户建仓。返回活动日志字典。"""
    watchlist = cfg.get("watchlist", []) or []
    codes = [str(w.get("code", "")).zfill(6) for w in watchlist]
    # 已持仓的代码也要取实时价（供后续卖出评估）
    held = {c for a in accounts.values() for c in a.positions}
    all_codes = list(dict.fromkeys(codes + list(held)))

    quotes = fetch_quotes(all_codes)
    pool = fetch_board_pool()
    sentiment = compute_sentiment()
    market = {"sentiment": sentiment}
    stocks, market_data = build_stocks(watchlist, quotes, pool, kline_ok)

    phase = sentiment.get("phase", "中性")
    activity = {
        "time": datetime.now().strftime("%H:%M:%S"),
        "phase": phase,
        "score": sentiment.get("score"),
        "notes": [], "buys": [], "quotes_codes": len(quotes),
        "per_note": {},
    }
    for d in sentiment.get("_degraded", []):
        activity["notes"].append(f"数据降级：{d}")

    if not stocks:
        activity["notes"].append("未取到自选池实时报价，本轮不建仓")
        for name in STRATEGY_IDS:
            activity["per_note"][name] = "空仓·未取到自选池实时报价"
        LAST_PER_NOTE.update(activity["per_note"])
        return activity

    select_strategies(phase, sentiment.get("score", 50))

    for name in STRATEGY_IDS:
        account = accounts[name]
        strategy = get_strategy(name, _strat_cfg(cfg, name, capital))
        if strategy is None:
            activity["notes"].append(f"{STRATEGY_CN[name]}：策略未注册")
            activity["per_note"][name] = "空仓·策略未注册"
            continue
        if not strategy.is_suitable(phase):
            activity["notes"].append(
                f"{STRATEGY_CN[name]}：空仓·相位不适配（当前「{phase}」，适合 {strategy.suitable_phases}）")
            activity["per_note"][name] = f"空仓·相位不适配（当前「{phase}」）"
            continue
        if name in KLINE_DEPENDENT and not kline_ok:
            activity["notes"].append(
                f"{STRATEGY_CN[name]}：空仓·K线指标源被限流，无法计算 {'/'.join(KLINE_DEPENDENT[name])}")
            activity["per_note"][name] = f"空仓·K线源限流，无法算 {'/'.join(KLINE_DEPENDENT[name])}"
            continue
        if name == "event_driven":
            if events:
                strategy.add_events(events)
            else:
                activity["notes"].append(f"{STRATEGY_CN[name]}：空仓·自选池内无 48h 新鲜利好事件")
                activity["per_note"][name] = "空仓·自选池内无 48h 新鲜利好"
                continue

        try:
            signals = strategy.scan(stocks, market)
        except Exception as exc:  # noqa: BLE001
            activity["notes"].append(f"{STRATEGY_CN[name]}：scan 异常 {type(exc).__name__} {str(exc)[:60]}")
            activity["per_note"][name] = f"空仓·scan 异常 {type(exc).__name__}"
            continue

        if not signals:
            activity["notes"].append(f"{STRATEGY_CN[name]}：空仓·未选出符合条件标的")
            activity["per_note"][name] = "空仓·未选出符合条件标的"
            continue

        matcher = Matcher(account, strict_time_check=False)
        risk = risks[name]
        submitted = 0
        for sig in signals:
            shares = int(sig.shares or 0)
            if shares < 100:
                continue
            ok, why = risk.check_buy(sig.code, sig.price, shares)
            if not ok:
                activity["notes"].append(f"{STRATEGY_CN[name]}·{sig.name}：风控拒绝 {why}")
                continue
            otype, oprice = _order_type_for(sig)
            plan = {
                "buy_lo": sig.buy_lo, "buy_hi": sig.buy_hi, "stop": sig.stop,
                "target1": sig.target1, "target2": sig.target2, "rr": sig.rr,
                "hold_days": sig.hold_days, "reason": sig.reason,
                "is_limit_up": bool(sig.extra.get("is_limit_up")),
            }
            matcher.submit_order(sig.code, sig.name, "buy", shares, oprice,
                                 order_type=otype, strategy=name, plan=plan)
            submitted += 1

        fills = matcher.match_orders(market_data)
        for f in fills:
            risk.on_trade_executed()
            activity["buys"].append({
                "account": name, "code": f.get("code"), "name": f.get("name"),
                "price": f.get("price"), "quantity": f.get("quantity"),
            })
        for o in matcher.order_history:
            if o.status.value in ("rejected", "expired") and o.side.value == "buy":
                activity["notes"].append(
                    f"{STRATEGY_CN[name]}·{o.name}：未成交 {o.reason or o.status.value}")
        if submitted and not fills:
            activity["notes"].append(f"{STRATEGY_CN[name]}：{submitted} 笔挂单均未成交（价格未触达/涨停封死）")
            activity["per_note"][name] = f"空仓·{submitted} 笔挂单未成交"
        elif fills:
            activity["per_note"][name] = f"建仓 {len(fills)} 笔"

    # 刷新持仓市值并落库
    prices = {c: market_data[c]["close"] for c in market_data if market_data.get(c, {}).get("close")}
    for account in accounts.values():
        account.update_market_prices(prices)
        account.save()

    LAST_PER_NOTE.update(activity["per_note"])
    return activity


def _strat_cfg(cfg: dict, name: str, capital: float) -> dict:
    strat_cfg = dict((cfg.get("strategies", {}) or {}).get(name, {}) or {})
    strat_cfg["capital"] = capital
    strat_cfg["single_risk_pct"] = cfg.get("risk", {}).get("single_risk_pct", 1.0)
    return strat_cfg


# ═══════════════════════════════════════════════════════════════════════
# 卖出评估（盘中每轮，仅处理 T+1 可卖持仓）
# ═══════════════════════════════════════════════════════════════════════

def _hold_days(open_date: str) -> int:
    try:
        d0 = date.fromisoformat(open_date)
    except (ValueError, TypeError):
        return 0
    d1 = date.today()
    if d1 <= d0:
        return 0
    n, cur = 0, d0
    while cur < d1:
        cur = date.fromordinal(cur.toordinal() + 1)
        if cur.weekday() < 5:
            n += 1
    return n


def evaluate_exit(pos, quote: dict, near_close: bool) -> tuple[bool, str]:
    """根据持仓计划 + 实时价判断是否卖出。返回 (should_sell, reason)。"""
    price = quote.get("close")
    prev_close = quote.get("prev_close")
    if not price or not prev_close:
        return False, ""

    plan = pos.original_plan or {}
    stop = plan.get("stop")
    target1 = plan.get("target1")
    hold_days_plan = int(plan.get("hold_days") or 5)
    is_board = pos.strategy == "board_hit"

    _up, limit_down = calc_limit_prices(prev_close, pos.code)
    if price <= limit_down:
        return False, "跌停封死，无法卖出"

    # 1) 止损优先
    if stop and price <= stop:
        return True, f"止损触发（现价 {price} ≤ 止损 {stop}）"

    # 2) 打板炸板：曾涨停建仓，开板且已翻绿 → 离场
    if is_board and plan.get("is_limit_up") and price <= pos.cost_price:
        return True, f"打板炸板离场（现价 {price} ≤ 成本 {round(pos.cost_price, 2)}）"

    # 3) 止盈：达到第一目标
    if target1 and price >= target1:
        return True, f"止盈触发（现价 {price} ≥ 目标 {target1}）"

    # 4) 到期：临近收盘且持仓达到计划天数
    if near_close and _hold_days(pos.open_date) >= hold_days_plan:
        return True, f"持仓到期（{_hold_days(pos.open_date)} 日 ≥ 计划 {hold_days_plan} 日）"

    return False, ""


def sell_pass(accounts: dict, quotes: dict, near_close: bool) -> list:
    """对所有账户的 T+1 可卖持仓评估并执行卖出。返回成交列表。"""
    executed = []
    for name, account in accounts.items():
        for code, pos in list(account.positions.items()):
            if pos.available <= 0:
                continue  # T+1 冻结，今日不可卖
            quote = quotes.get(code) or {}
            should, reason = evaluate_exit(pos, quote, near_close)
            if not should:
                continue
            price = quote.get("close")
            qty = (pos.available // 100) * 100
            if qty < 100:
                continue
            res = account.sell(code, price, qty, reason=reason)
            if res["success"]:
                t = res["trade"]
                executed.append({
                    "account": name, "code": code, "name": pos.name,
                    "price": t["price"], "quantity": qty, "reason": reason,
                    "realized_pnl": t.get("realized_pnl"),
                })
                log.info("[%s] 卖出 %s %s %d股 @%.2f —— %s",
                         STRATEGY_CN.get(name, name), code, pos.name, qty, t["price"], reason)
        account.save()
    return executed


# ═══════════════════════════════════════════════════════════════════════
# 快照与收尾
# ═══════════════════════════════════════════════════════════════════════

def finalize(accounts: dict, quotes: dict):
    prices = {c: q.get("close") for c, q in quotes.items() if q.get("close")}
    for account in accounts.values():
        if prices:
            account.update_market_prices(prices)
        account.take_snapshot()
        account.save()


# ═══════════════════════════════════════════════════════════════════════
# 主循环
# ═══════════════════════════════════════════════════════════════════════

def run_live(interval: int, duration: int = None, once: bool = False, force: bool = False):
    cfg = configstore.load(CONFIG_PATH)
    sim_cfg = cfg.get("simulation", {})
    capital = sim_cfg.get("initial_capital", 100000)
    fee_cfg = sim_cfg.get("fees", {})
    risk_cfg = sim_cfg.get("risk", {})

    watchlist = cfg.get("watchlist", []) or []
    if not watchlist:
        print("自选池为空，请先用 run_screen.py --write 或手动填充 config.yaml watchlist。")
        return 1
    codes = [str(w.get("code", "")).zfill(6) for w in watchlist]

    # 载入四个策略账户（沿用已有持仓/现金；不存在则新建）
    accounts = {
        name: Account(account_id=name, initial_capital=capital, db_path=SIM_DB, fee_config=fee_cfg)
        for name in STRATEGY_IDS
    }
    risks = {name: RiskManager(accounts[name], risk_cfg) for name in STRATEGY_IDS}

    today = date.today().isoformat()
    for account in accounts.values():
        account.on_new_day(today)   # T+1 解冻
        account.save()
    for risk in risks.values():
        risk.on_new_day()

    held = sum(len(a.positions) for a in accounts.values())
    print(f"盘中自动循环模拟交易启动 · {today}")
    print(f"  账户：{', '.join(STRATEGY_CN[n] + '(' + n + ')' for n in STRATEGY_IDS)}")
    print(f"  各账户本金 {capital:,.0f}，当前合计持仓 {held} 只（含昨日结转）")
    print(f"  轮询间隔 {interval} 秒")

    kline_ok = probe_kline(codes[0])
    if not kline_ok:
        print("  ⚠ 日线源被限流：trend_pullback / oversold_bounce 本轮无法评估")

    events = []
    try:
        events = [e for e in fetch_events(codes)
                  if (datetime.now() - e.publish_time).total_seconds() / 3600 <= 48]
        print(f"  自选池 48h 新鲜利好事件：{len(events)} 条")
    except Exception as exc:  # noqa: BLE001
        log.info("事件采集失败：%s", exc)

    running = {"flag": True}

    def handle_signal(signum, frame):
        log.info("收到退出信号，收尾…")
        running["flag"] = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    start = datetime.now()
    end = start + timedelta(minutes=duration) if duration else None

    bought_today = False
    snapshot_done = False

    def _finalize_once():
        nonlocal snapshot_done
        if snapshot_done:
            return
        q = fetch_quotes(list({c for a in accounts.values() for c in a.positions} | set(codes)))
        finalize(accounts, q)
        snapshot_done = True
        log.info("已写当日快照")

    while running["flag"]:
        now = datetime.now()
        sess = market_session(now)

        # 强制/一次性模式：忽略时段
        if once or force:
            if not bought_today:
                act = buy_pass(accounts, risks, cfg, capital, kline_ok, events)
                bought_today = True
                _report_buy(act)
            q = fetch_quotes(list({c for a in accounts.values() for c in a.positions} | set(codes)))
            sells = sell_pass(accounts, q, near_close=True)
            _report_sell(sells)
            for a in accounts.values():
                a.update_market_prices({c: qq.get("close") for c, qq in q.items() if qq.get("close")})
            _finalize_once()
            _write_heartbeat(accounts, sess, bought_today, snapshot_done, running=False)
            _print_summary(accounts)
            return 0

        # 非交易日：直接退出
        if not sess["is_trading_day"]:
            print("今日非交易日，无需运行。")
            _write_heartbeat(accounts, sess, bought_today, snapshot_done, running=False)
            return 0

        # 收盘后：收尾退出
        if sess["key"] == "closed":
            _finalize_once()
            _write_heartbeat(accounts, sess, bought_today, snapshot_done, running=False)
            _print_summary(accounts)
            print("已收盘，自动循环结束。")
            return 0

        # 盘前/午休：等待
        if not sess["in_session"]:
            _write_heartbeat(accounts, sess, bought_today, snapshot_done, running=True)
            log.info("当前时段 %s（%s），等待…", sess["label"], sess["time"])
            time.sleep(min(interval, 30))
            if end and datetime.now() >= end:
                break
            continue

        # 交易时段内
        if not bought_today:
            log.info("开盘建仓（%s）…", sess["time"])
            act = buy_pass(accounts, risks, cfg, capital, kline_ok, events)
            bought_today = True
            _report_buy(act)

        # 盘中卖出评估
        held_codes = {c for a in accounts.values() for c in a.positions}
        q = fetch_quotes(list(held_codes | set(codes)))
        near_close = sess["key"] == "closing_auction" or now.time() >= dtime(14, 50)
        sells = sell_pass(accounts, q, near_close=near_close)
        if sells:
            _report_sell(sells)

        for a in accounts.values():
            a.update_market_prices({c: qq.get("close") for c, qq in q.items() if qq.get("close")})

        # 临近收盘写快照（只写一次）
        if near_close:
            _finalize_once()

        _write_heartbeat(accounts, sess, bought_today, snapshot_done, running=True)
        time.sleep(interval)
        if end and datetime.now() >= end:
            break

    # 循环结束（duration 到点或收到信号）
    _finalize_once()
    _write_heartbeat(accounts, market_session(), bought_today, snapshot_done, running=False)
    _print_summary(accounts)
    return 0


# ═══════════════════════════════════════════════════════════════════════
# 输出与心跳
# ═══════════════════════════════════════════════════════════════════════

def _report_buy(act: dict):
    print(f"\n[{act['time']}] 建仓轮 · 情绪 {act['phase']}（{act['score']} 分）· 取到 {act['quotes_codes']} 只报价")
    if act["buys"]:
        for b in act["buys"]:
            print(f"  ✓ 买入 [{STRATEGY_CN.get(b['account'])}] {b['code']} {b['name']} "
                  f"{b['quantity']} 股 @ {b['price']}")
    else:
        print("  本轮无成交")
    for n in act["notes"]:
        print(f"  · {n}")


def _report_sell(sells: list):
    for s in sells:
        pnl = s.get("realized_pnl")
        pnl_s = f" 盈亏 {pnl:+,.2f}" if isinstance(pnl, (int, float)) else ""
        print(f"  ✗ 卖出 [{STRATEGY_CN.get(s['account'])}] {s['code']} {s['name']} "
              f"{s['quantity']} 股 @ {s['price']}{pnl_s} —— {s['reason']}")


def _write_heartbeat(accounts: dict, sess: dict, bought: bool, snapped: bool, running: bool):
    per = {}
    for name, a in accounts.items():
        s = a.get_summary()
        per[name] = {
            "cn": STRATEGY_CN.get(name, name),
            "total_assets": s["total_assets"], "cash": s["cash"],
            "position_value": s["position_value"], "position_ratio": s["position_ratio"],
            "total_pnl": s["total_pnl"], "total_return_pct": s["total_return_pct"],
            "position_count": s["position_count"],
        }
    write_state({
        "running": running, "session": sess, "bought_today": bought,
        "snapshot_done": snapped, "accounts": per,
        "notes": dict(LAST_PER_NOTE),
    })


def _print_summary(accounts: dict):
    line = "═" * 72
    print(f"\n{line}\n  各策略账户当日收束\n{line}")
    for name in STRATEGY_IDS:
        s = accounts[name].get_summary()
        print(f"【{STRATEGY_CN[name]} · {name}】总资产 {s['total_assets']:,.2f}  "
              f"现金 {s['cash']:,.2f}  仓位 {s['position_ratio']*100:.1f}%  "
              f"盈亏 {s['total_pnl']:+,.2f}（{s['total_return_pct']:+.2f}%）  "
              f"持仓 {s['position_count']} 只")
    print(line + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="盘中定时自动循环模拟交易")
    ap.add_argument("--interval", type=int, default=60, help="盘中轮询间隔（秒，默认 60）")
    ap.add_argument("--duration", type=int, default=None, help="最长运行分钟数（默认到收盘）")
    ap.add_argument("--once", action="store_true", help="只跑一轮后退出（验证用）")
    ap.add_argument("--force", action="store_true", help="配合 --once，非交易时段也强制跑")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S",
    )
    return run_live(args.interval, args.duration, args.once, args.force)


if __name__ == "__main__":
    sys.exit(main())
