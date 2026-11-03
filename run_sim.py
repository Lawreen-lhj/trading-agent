#!/usr/bin/env python3
"""多策略并行模拟交易运行器。

做什么：
  为每个交易策略开一个独立账户（各 10 万本金），从自选池里按各策略自己的逻辑选股，
  生成信号 → 过风控 → 撮合成交 → 写快照 → 打印每个策略的持仓/现金/成交/拒单理由。

为什么每个策略单独一个账户：
  用户要对比几种打法在同一批自选股、同一段行情下的表现。
  共用一个账户会让仓位互相挤占、盈亏混在一起，没法归因。
  所以 board_hit / trend_pullback / oversold_bounce / event_driven 各开一个 account_id，
  并行独立跑，互不影响。

撮合口径（重要，模拟撮合的固有近似）：
  A 股 T+1，今天买的明天才能卖。本运行器用「当日 OHLC + 收盘价」做简化撮合：
    - 打板单(board)：当日封住涨停(收盘 >= 涨停价*0.998)才成交，成交价 = 涨停价
    - 限价单(limit)：委托价触达当日价格区间才成交；已涨停的票非打板单会被拒("涨停无法买入")
  盘后运行时，等价于「以今日收盘模拟建仓，次一交易日可卖」。

数据源现状（东财各子域 WAF 独立，常被分别限流）：
  push2ex 涨停板池 —— 可用：连板数/封单/流通市值/行业/炸板次数，驱动 board_hit 与情绪
  腾讯实时报价     —— 可用：OHLC/收盘/涨跌幅/量比/市值，喂给撮合引擎
  datacenter 事件  —— 可用：业绩预告/增持/回购，驱动 event_driven
  push2his 日线    —— 常被限流(HTTP 501)：ma/ret20/bias5/rsi6/atr14 无法计算，
                      此时 trend_pullback / oversold_bounce 如实空仓并说明原因，绝不编造成交。

用法：
    python3 run_sim.py                 # 跑一遍，建仓并落库 data/simulation.db
    python3 run_sim.py --capital 100000# 每个策略账户的本金（默认 10 万）
    python3 run_sim.py --reset         # 先清空这几个策略账户的历史再重跑
    python3 run_sim.py --no-events     # 跳过事件源采集（更快，event_driven 直接空仓）
    python3 run_sim.py --dry-run       # 只演算不落库

注意：纯本地模拟，不存在任何真实下单代码路径。
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src import configstore, datasource as ds, extras, indicators, tx  # noqa: E402
from src.sentiment import calc_sentiment_score  # noqa: E402
from src.simulation.account import Account  # noqa: E402
from src.simulation.matcher import Matcher, RiskManager  # noqa: E402
from src.strategies import get_strategy, select_strategies  # noqa: E402
from src.strategies.event_driven import Event  # noqa: E402

log = logging.getLogger("sim")

CONFIG_PATH = ROOT / "config.yaml"
SIM_DB = ROOT / "data" / "simulation.db"

# 四个策略账户（account_id 即策略名）
STRATEGY_IDS = ["board_hit", "trend_pullback", "oversold_bounce", "event_driven"]

# 依赖日线指标的策略：K线源被限流时无法诚实评估，直接空仓并说明
KLINE_DEPENDENT = {
    "trend_pullback": ["ma5/ma10/ma20", "ret20", "bias5", "rsi6", "atr14", "close_pos", "high20"],
    "oversold_bounce": ["ma5..ma60", "ret20", "bias5", "rsi6", "drawdown60", "close_pos", "atr14"],
}

STRATEGY_CN = {
    "board_hit": "打板/半路",
    "trend_pullback": "趋势波段",
    "oversold_bounce": "超跌反弹",
    "event_driven": "事件驱动",
}


# ═══════════════════════════════════════════════════════════════════════
# 数据采集
# ═══════════════════════════════════════════════════════════════════════

def fetch_quotes(codes: list[str]) -> dict[str, dict]:
    """腾讯实时报价（含 OHLC），返回 {code: quote}。"""
    tcodes = [tx.tx_code(c) for c in codes]
    out: dict[str, dict] = {}
    try:
        for q in tx.quotes(tcodes):
            code = str(q.get("code", "")).zfill(6)
            out[code] = q
    except Exception as exc:  # noqa: BLE001
        log.warning("腾讯报价失败: %s", exc)
    return out


def fetch_board_pool() -> dict[str, dict]:
    """push2ex 涨停板池，返回 {code: pool_row}，附带同板块涨停家数。"""
    try:
        pool = extras.limit_up_pool()
    except Exception as exc:  # noqa: BLE001
        log.warning("涨停板池失败: %s", exc)
        return {}
    by_code = {str(p["code"]).zfill(6): p for p in pool}
    # 同板块涨停家数（板块效应）
    ind_count: dict[str, int] = {}
    for p in pool:
        ind = p.get("industry") or "未知"
        ind_count[ind] = ind_count.get(ind, 0) + 1
    for p in by_code.values():
        p["sector_limit_count"] = ind_count.get(p.get("industry") or "未知", 0)
    return by_code


def probe_kline(code: str) -> bool:
    """探测日线源是否可用（东财 push2his 常被限流，退回腾讯 web.ifzq）。"""
    try:
        rows, _src = ds.daily(code, days=60)
        if rows:
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        if tx.kline(code, days=60):
            return True
    except Exception as exc:  # noqa: BLE001
        log.info("日线源不可用（%s %s）", type(exc).__name__, str(exc)[:60])
    return False


def compute_sentiment() -> dict:
    """综合情绪：push2ex 涨停池（可靠）+ breadth（常降级）+ 北向。

    breadth 全市场快照经常被限流（down=0/成交额偏小），此时涨跌比/中位数/成交额
    三个因子按「中性市场」估计，并在 degraded 里标注，避免用残缺数据得出极端结论。
    """
    zt = {}
    try:
        zt = extras.limit_up_sentiment()
    except Exception as exc:  # noqa: BLE001
        log.warning("打板情绪采集失败: %s", exc)

    limit_up = int(zt.get("zt_count", 0) or 0)
    limit_down = int(zt.get("dt_count", 0) or 0)
    max_height = int(zt.get("max_height", 0) or 0)
    y_perf = float(zt.get("yesterday_perf", 0) or 0)

    degraded = []
    up_ratio, median_pct, total_amount = 0.5, 0.0, 8000.0  # 中性默认
    try:
        b = ds.breadth() or {}
        up = int(b.get("up", 0) or 0)
        down = int(b.get("down", 0) or 0)
        amt = float(b.get("total_amount", 0) or 0) / 1e8
        # breadth 完整才采信（down>0 且成交额达到全天量级）
        if up > 0 and down > 0 and amt > 5000:
            up_ratio = round(up / (up + down), 3)
            median_pct = float(b.get("median_pct", 0) or 0)
            total_amount = amt
        else:
            degraded.append("全市场涨跌 breadth 被限流（up_ratio/median_pct/成交额按中性估计）")
    except Exception:  # noqa: BLE001
        degraded.append("breadth 不可用（涨跌比/中位数/成交额按中性估计）")

    north = 0.0
    try:
        nf = extras.north_flow() or {}
        north = float(nf.get("total", 0) or 0)
    except Exception:  # noqa: BLE001
        pass

    sent = calc_sentiment_score(
        limit_up=limit_up,
        limit_down=limit_down,
        up_ratio=up_ratio,
        median_pct=median_pct,
        max_board_height=max_height,
        yesterday_limit_up_perf=y_perf,
        total_amount=total_amount,
        north_net_flow=north,
    )
    sent["_raw"] = {
        "limit_up": limit_up, "limit_down": limit_down, "max_height": max_height,
        "yesterday_perf": y_perf, "break_rate": zt.get("break_rate", 0),
        "promotion_rate": zt.get("promotion_rate", 0), "north": north,
    }
    sent["_degraded"] = degraded
    return sent


def fetch_events(codes: list[str]) -> list[Event]:
    """从 datacenter 取全市场最新事件，过滤到自选池代码（3 次请求，避免逐票打接口）。"""
    code_set = {str(c).zfill(6) for c in codes}
    events: list[Event] = []

    def _mk(etype, code, name, title, day, positive=True, impact="medium"):
        code = str(code).zfill(6)
        if code not in code_set:
            return None
        try:
            pt = datetime.fromisoformat(day) if day else datetime.now() - timedelta(days=999)
        except ValueError:
            pt = datetime.now() - timedelta(days=999)
        return Event(
            event_type=etype, code=code, name=name, title=title, content=title[:200],
            publish_time=pt, source="eastmoney-datacenter", impact_level=impact,
            is_positive=positive,
        )

    try:
        for r in extras.earnings_forecast(None, limit=300):
            t = r.get("type", "")
            positive = t in ("预增", "扭亏", "略增", "续盈")
            if not positive:
                continue
            e = _mk("earnings_surprise", r.get("code"), r.get("name"),
                    f'业绩{t}：{(r.get("indicator") or "")}变动{r.get("change_pct_lower")}%',
                    r.get("notice_date"), True, "high")
            if e:
                events.append(e)
    except Exception as exc:  # noqa: BLE001
        log.info("业绩预告采集失败: %s", exc)

    try:
        for r in extras.holder_trades(None, direction="增持", limit=300):
            e = _mk("insider_buy", r.get("code"), r.get("name"),
                    f'股东增持：{r.get("holder","")} 增持{r.get("change_pct") or ""}%',
                    r.get("notice_date"), True, "medium")
            if e:
                events.append(e)
    except Exception as exc:  # noqa: BLE001
        log.info("股东增持采集失败: %s", exc)

    try:
        for r in extras.share_buyback(None, limit=200):
            e = _mk("buyback", r.get("code"), r.get("name"),
                    f'股票回购：{r.get("progress_code","")}',
                    r.get("notice_date"), True, "medium")
            if e:
                events.append(e)
    except Exception as exc:  # noqa: BLE001
        log.info("回购采集失败: %s", exc)

    return events


# ═══════════════════════════════════════════════════════════════════════
# 构建策略输入
# ═══════════════════════════════════════════════════════════════════════

def build_stocks(watchlist: list[dict], quotes: dict, pool: dict,
                 kline_ok: bool) -> tuple[list[dict], dict[str, dict]]:
    """把自选池 + 报价 + 涨停池合并成策略 scan 需要的 stocks 列表。

    返回 (stocks, market_data)；market_data 供撮合引擎用（含 OHLC + prev_close）。
    """
    stocks: list[dict] = []
    market_data: dict[str, dict] = {}

    for w in watchlist:
        code = str(w.get("code", "")).zfill(6)
        q = quotes.get(code)
        if not q:
            continue
        p = pool.get(code, {})

        close = q.get("close")
        prev_close = q.get("prev_close")
        # 涨跌停幅度（创业板/科创板 20%，北交所 30%，主板 10%）
        if code.startswith(("300", "301", "688", "689")):
            limit_pct = 20.0
        elif code.startswith(("8", "4", "9")):
            limit_pct = 30.0
        else:
            limit_pct = 10.0

        s = {
            "code": code,
            "name": q.get("name") or w.get("name", ""),
            "close": close,
            "prev_close": prev_close,
            "open": q.get("open"),
            "high": q.get("high"),
            "low": q.get("low"),
            "pct_chg": q.get("pct_chg"),
            "volume": q.get("volume"),
            "amount": q.get("amount"),
            "turnover": q.get("turnover"),
            "vol_ratio": q.get("vol_ratio"),
            "market_cap": q.get("market_cap"),
            "limit_pct": limit_pct,
            # 涨停池精确字段
            "float_market_cap": p.get("float_cap"),
            "board_height": p.get("limit_days", 1),
            "seal_amount": p.get("seal_fund", 0),
            "seal_ratio": round((p.get("seal_fund") or 0) / p["float_cap"], 4) if p.get("float_cap") else 0,
            "sector_limit_count": p.get("sector_limit_count", 0),
            "break_times": p.get("break_times", 0),
            "industry": p.get("industry") or w.get("tag", ""),
            "in_limit_pool": code in pool,
        }

        # K线指标增强（仅在日线源可用时；trend_pullback/oversold_bounce 依赖这些字段）
        if kline_ok:
            try:
                bars, _src = ds.daily(code, days=120)
                if not bars:
                    bars = tx.kline(code, days=120)
                if bars and len(bars) >= 30:
                    snap = indicators.snapshot(indicators.compute(bars, code=code))
                    for k, v in snap.items():
                        if v is not None and k not in s:
                            s[k] = v
                time.sleep(0.4)
            except Exception as exc:  # noqa: BLE001
                log.info("%s 指标增强失败：%s", code, str(exc)[:60])

        stocks.append(s)

        if close and prev_close:
            market_data[code] = {
                "open": q.get("open"), "high": q.get("high"), "low": q.get("low"),
                "close": close, "prev_close": prev_close,
            }

    return stocks, market_data


# ═══════════════════════════════════════════════════════════════════════
# 单策略执行
# ═══════════════════════════════════════════════════════════════════════

def _order_type_for(sig) -> tuple[str, float]:
    """按信号决定委托类型和委托价。"""
    if sig.strategy == "board_hit" and sig.extra.get("is_limit_up"):
        return "board", round(sig.price, 2)      # 打板：涨停价挂单
    return "limit", round(sig.buy_hi or sig.price, 2)  # 其余：限价（买入区间上沿）


def run_strategy(name: str, stocks: list[dict], market: dict, market_data: dict,
                 cfg: dict, capital: float, events: list[Event],
                 kline_ok: bool, dry_run: bool) -> dict:
    """跑单个策略：开户 → scan → 风控 → 撮合 → 快照。返回结果字典。"""
    sim_cfg = cfg.get("simulation", {})
    fee_cfg = sim_cfg.get("fees", {})
    risk_cfg = sim_cfg.get("risk", {})
    db_path = None if dry_run else SIM_DB

    account = Account(account_id=name, initial_capital=capital,
                      db_path=db_path, fee_config=fee_cfg)
    result = {
        "strategy": name, "cn": STRATEGY_CN.get(name, name),
        "account_id": name, "note": "", "signals": [], "fills": [],
        "rejects": [], "positions": [], "summary": {},
    }

    strat_cfg = dict((cfg.get("strategies", {}) or {}).get(name, {}) or {})
    strat_cfg["capital"] = capital
    strat_cfg["single_risk_pct"] = cfg.get("risk", {}).get("single_risk_pct", 1.0)
    strategy = get_strategy(name, strat_cfg)
    if strategy is None:
        result["note"] = "策略未注册"
        result["summary"] = account.get_summary()
        return result

    phase = market.get("sentiment", {}).get("phase", "中性")

    # 1) 相位适配
    if not strategy.is_suitable(phase):
        result["note"] = (f"空仓·相位不适配：当前情绪「{phase}」，"
                          f"本策略适合 {strategy.suitable_phases}")
        if not dry_run:
            account.take_snapshot(); account.save()
        result["summary"] = account.get_summary()
        return result

    # 2) 日线依赖 + K线源限流 → 无法诚实评估
    if name in KLINE_DEPENDENT and not kline_ok:
        result["note"] = ("空仓·K线指标源被限流(HTTP 501)，无法计算 "
                          + "/".join(KLINE_DEPENDENT[name]) + "，本日不评估该策略")
        if not dry_run:
            account.take_snapshot(); account.save()
        result["summary"] = account.get_summary()
        return result

    # 3) 事件驱动：注入事件池
    if name == "event_driven":
        if events:
            strategy.add_events(events)
        else:
            result["note"] = "空仓·自选池内无 48h 内的新鲜利好事件"
            if not dry_run:
                account.take_snapshot(); account.save()
            result["summary"] = account.get_summary()
            return result

    # 4) 扫描信号
    try:
        signals = strategy.scan(stocks, market)
    except Exception as exc:  # noqa: BLE001
        result["note"] = f"scan 异常：{type(exc).__name__} {str(exc)[:80]}"
        result["summary"] = account.get_summary()
        return result

    for sig in signals:
        result["signals"].append({
            "code": sig.code, "name": sig.name, "price": sig.price,
            "strength": round(sig.strength, 3), "rr": sig.rr, "shares": sig.shares,
            "stop": sig.stop, "target1": sig.target1, "reason": sig.reason,
        })

    if not signals:
        if not result["note"]:
            result["note"] = "空仓·本策略在当前自选池未选出符合条件的标的"
        if not dry_run:
            account.take_snapshot(); account.save()
        result["summary"] = account.get_summary()
        return result

    # 5) 风控 + 下单 + 撮合
    matcher = Matcher(account, strict_time_check=False)
    risk = RiskManager(account, risk_cfg)
    account.on_new_day()  # T+1 解冻（新账户无持仓，安全）

    for sig in signals:
        shares = int(sig.shares or 0)
        if shares < 100:
            result["rejects"].append({"code": sig.code, "name": sig.name,
                                      "reason": f"建议股数 {shares} 不足 1 手，跳过"})
            continue
        ok, why = risk.check_buy(sig.code, sig.price, shares)
        if not ok:
            result["rejects"].append({"code": sig.code, "name": sig.name, "reason": f"风控拒绝：{why}"})
            continue
        otype, oprice = _order_type_for(sig)
        plan = {
            "buy_lo": sig.buy_lo, "buy_hi": sig.buy_hi, "stop": sig.stop,
            "target1": sig.target1, "target2": sig.target2, "rr": sig.rr,
            "hold_days": sig.hold_days, "reason": sig.reason,
        }
        matcher.submit_order(sig.code, sig.name, "buy", shares, oprice,
                             order_type=otype, strategy=name, plan=plan)

    fills = matcher.match_orders(market_data)
    for f in fills:
        risk.on_trade_executed()
        result["fills"].append({
            "code": f.get("code"), "name": f.get("name"), "price": f.get("price"),
            "quantity": f.get("quantity"), "amount": f.get("amount"),
            "fees": f.get("fees"), "strategy": f.get("strategy"),
        })
    # 未成交的挂单记为拒单/失效
    for o in matcher.order_history:
        if o.status.value in ("rejected", "expired") and o.side.value == "buy":
            result["rejects"].append({"code": o.code, "name": o.name,
                                      "reason": f"撮合未成交：{o.reason or o.status.value}"})

    # 用收盘价刷新持仓市值并快照
    prices = {c: market_data[c]["close"] for c in market_data}
    account.update_market_prices(prices)
    if not dry_run:
        account.take_snapshot()
        account.save()

    result["positions"] = account.get_positions()
    result["summary"] = account.get_summary()
    if not result["note"]:
        result["note"] = f"建仓 {len(result['fills'])} 只（打板/限价撮合成交）"
    return result


# ═══════════════════════════════════════════════════════════════════════
# 报表
# ═══════════════════════════════════════════════════════════════════════

def _money(v) -> str:
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return "-"


def print_report(sentiment: dict, results: list[dict], capital: float):
    line = "═" * 72
    raw = sentiment.get("_raw", {})
    print(f"\n{line}\n  多策略模拟交易  ·  {date.today().isoformat()}  ·  每策略本金 {_money(capital)}\n{line}")
    print(f"  市场情绪：{sentiment.get('phase')}（{sentiment.get('score')} 分）  "
          f"涨停 {raw.get('limit_up')} / 跌停 {raw.get('limit_down')}  "
          f"最高 {raw.get('max_height')} 板  炸板率 {raw.get('break_rate')}%")
    print(f"  昨涨停今表现 {raw.get('yesterday_perf')}%  晋级率 {raw.get('promotion_rate')}  "
          f"北向 {raw.get('north')} 亿")
    print(f"  推荐策略组合：{sentiment.get('preferred_strategies')}")
    for d in sentiment.get("_degraded", []):
        print(f"  ⚠ 数据降级：{d}")
    print(line)

    for r in results:
        s = r["summary"]
        print(f"\n【{r['cn']} · {r['strategy']}】  账户 {r['account_id']}")
        print(f"  总资产 {_money(s.get('total_assets'))}  现金 {_money(s.get('cash'))}  "
              f"持仓市值 {_money(s.get('position_value'))}  仓位 "
              f"{(s.get('position_ratio') or 0)*100:.1f}%  盈亏 {_money(s.get('total_pnl'))}")
        if r["note"]:
            print(f"  说明：{r['note']}")

        if r["signals"]:
            print(f"  信号（{len(r['signals'])}）：")
            for sig in r["signals"]:
                print(f"    · {sig['code']} {sig['name']}  强度 {sig['strength']}  "
                      f"盈亏比 {sig['rr']}  建议 {sig['shares']} 股  止损 {sig['stop']}  —— {sig['reason']}")

        if r["fills"]:
            print(f"  成交（{len(r['fills'])}）：")
            for f in r["fills"]:
                print(f"    ✓ {f['code']} {f['name']}  {f['quantity']} 股 @ {f['price']}  "
                      f"金额 {_money(f['amount'])}  费用 {_money(f['fees'])}")

        if r["positions"]:
            print(f"  持仓（{len(r['positions'])}）：")
            for p in r["positions"]:
                print(f"    • {p['code']} {p['name']}  {p['quantity']} 股  成本 {p['cost_price']}  "
                      f"市值 {_money(p['market_value'])}  可卖 {p['available']}（T+1 冻结 {p['frozen']}）  "
                      f"策略 {p['strategy']}")

        if r["rejects"]:
            print(f"  拒单/未成交（{len(r['rejects'])}）：")
            seen = set()
            for rj in r["rejects"]:
                key = (rj["code"], rj["reason"])
                if key in seen:
                    continue
                seen.add(key)
                print(f"    ✗ {rj['code']} {rj['name']}  —— {rj['reason']}")

    print(f"\n{line}")
    deployed = sum(len(r["fills"]) for r in results)
    print(f"  合计建仓 {deployed} 笔；未部署资金的策略账户保持现金（符合各自策略纪律）。")
    print(f"  账户与成交已落库：{SIM_DB}\n{line}\n")


# ═══════════════════════════════════════════════════════════════════════
# 主流程
# ═══════════════════════════════════════════════════════════════════════

def reset_accounts(ids: list[str]):
    if not SIM_DB.exists():
        return
    conn = sqlite3.connect(SIM_DB)
    for t in ("sim_accounts", "sim_positions", "sim_trades", "sim_snapshots"):
        conn.execute(f"DELETE FROM {t} WHERE account_id IN ({','.join('?'*len(ids))})", ids)
    conn.commit()
    conn.close()


def main():
    ap = argparse.ArgumentParser(description="多策略并行模拟交易")
    ap.add_argument("--capital", type=float, default=None, help="每策略账户本金（默认取 config.simulation.initial_capital 或 10 万）")
    ap.add_argument("--reset", action="store_true", help="先清空这几个策略账户的历史再重跑")
    ap.add_argument("--no-events", action="store_true", help="跳过事件源采集")
    ap.add_argument("--dry-run", action="store_true", help="只演算不落库")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    cfg = configstore.load(CONFIG_PATH)
    capital = args.capital or cfg.get("simulation", {}).get("initial_capital", 100000)

    watchlist = cfg.get("watchlist", []) or []
    if not watchlist:
        print("自选池为空，请先用 run_screen.py --write 或手动填充 config.yaml watchlist。")
        return 1
    codes = [str(w.get("code", "")).zfill(6) for w in watchlist]

    if args.reset and not args.dry_run:
        reset_accounts(STRATEGY_IDS)
        print(f"已清空策略账户历史：{', '.join(STRATEGY_IDS)}")

    print("采集行情与情绪…")
    quotes = fetch_quotes(codes)
    pool = fetch_board_pool()
    kline_ok = probe_kline(codes[0])
    sentiment = compute_sentiment()
    market = {"sentiment": sentiment}

    events: list[Event] = []
    if not args.no_events:
        print("采集事件源（业绩预告/增持/回购）…")
        events = [e for e in fetch_events(codes)
                  if (datetime.now() - e.publish_time).total_seconds() / 3600 <= 48]
        print(f"  自选池内 48h 新鲜利好事件：{len(events)} 条")

    stocks, market_data = build_stocks(watchlist, quotes, pool, kline_ok)
    if not stocks:
        print("未能获取自选池报价（数据源可能被限流），请稍后重试。")
        return 1

    # 依据情绪相位确定要跑哪些策略（仍为每个策略开户，不适配的账户如实空仓）
    select_strategies(sentiment.get("phase", "中性"), sentiment.get("score", 50))

    results = []
    for name in STRATEGY_IDS:
        results.append(run_strategy(
            name, stocks, market, market_data, cfg, capital,
            events, kline_ok, args.dry_run,
        ))

    print_report(sentiment, results, capital)
    return 0


if __name__ == "__main__":
    sys.exit(main())
