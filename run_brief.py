#!/usr/bin/env python3
"""A 股每日简报 —— 入口。

style=short（默认）：短线模式，输出「盘前交易计划」，每只票给出买点区间/止损/目标/盈亏比/股数。
style=swing        ：波段模式，输出强弱排序。

用法：
    .venv/bin/python run_brief.py              # 采集 + 生成 HTML + 本地通知
    .venv/bin/python run_brief.py --no-notify  # 不弹通知
    .venv/bin/python run_brief.py --quiet      # 只写文件不打印

输出：
    data/briefings/YYYY-MM-DD.html   给人看的简报
    data/briefings/YYYY-MM-DD.json   给 agent 读的结构化数据
    data/market.db                   历史留痕，供后续复盘

注意：本脚本只做分析与计划，不存在任何下单代码路径。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src import briefing, datasource as ds, indicators, notify, scoring, shortterm  # noqa: E402
from src.db import Store  # noqa: E402


def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _held_days(opened: str | None) -> int | None:
    """持有交易日数（粗算：只数工作日，不含节假日）。用于时间止损。"""
    if not opened:
        return None
    try:
        d0 = date.fromisoformat(str(opened))
    except ValueError:
        return None
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


def build_brief(cfg: dict) -> dict:
    now = datetime.now()
    day = now.strftime("%Y-%m-%d")
    style = (cfg.get("meta") or {}).get("style", "short")
    health: list[str] = []   # 硬缺口：数据真没有，结论可信度下降
    notes: list[str] = []    # 提示：走了备用源，数据仍然可用

    # ---------- 1. A 股市场层 ----------
    indices, idx_src = ds.index_quotes()
    breadth = ds.breadth()
    sectors = ds.sector_rank()

    if not indices:
        indices = []
        health.append("指数行情缺失（主备源均不可用）")
    elif idx_src != "eastmoney":
        notes.append(f"指数行情走备源（{idx_src}）")
    if not breadth:
        health.append("全市场涨跌家数与成交额缺失")
    elif breadth.get("stale_asof"):
        notes.append(f"涨跌家数用缓存（时点 {breadth['stale_asof']}，东财当前不可用）")
    if not sectors:
        health.append("行业板块数据缺失")
    elif sectors.get("stale_asof"):
        notes.append(f"行业板块用缓存（时点 {sectors['stale_asof']}）")

    state = scoring.market_state(breadth or {}, indices)
    temp = shortterm.market_temp(breadth) if breadth else {}

    # ---------- 2. 外围观测层 ----------
    us, us_src = ds.global_quotes("us")
    hk, hk_src = ds.global_quotes("hk")
    crypto = ds.crypto(cfg.get("global_watch", {}).get("crypto", []))
    gl = {
        "us": {"ok": bool(us), "data": us or []},
        "hk": {"ok": bool(hk), "data": hk or []},
        "crypto": {"ok": bool(crypto), "data": crypto or []},
    }
    if not us:
        health.append("美股指数缺失")
    elif us_src != "eastmoney":
        notes.append(f"美股指数走备源（{us_src}）")
    if not hk:
        health.append("港股指数缺失")
    elif hk_src != "eastmoney":
        notes.append(f"港股指数走备源（{hk_src}）")
    if not crypto:
        notes.append("加密行情未取到（境外接口通常需要特殊网络环境，属正常）")

    # ---------- 3. 自选池 ----------
    wl = cfg.get("watchlist", [])
    spots = ds.spot([item["code"] for item in wl]) if wl else {}

    watch_rows = []
    fallback_codes: list[str] = []
    for item in wl:
        code, name = str(item["code"]).zfill(6), item.get("name", item["code"])
        bars, src = ds.daily(code)
        if not bars:
            health.append(f"{name}({code}) 日线缺失")
            continue
        if src.startswith("tencent"):
            fallback_codes.append(name)

        df = indicators.compute(bars, code=code)
        snap = indicators.snapshot(df)

        # 日线缺换手率（腾讯备源没有）时，用快照补齐
        q = spots.get(code) or {}
        for k in ("turnover", "vol_ratio", "amplitude"):
            if not snap.get(k) and q.get(k):
                snap[k] = q[k]

        if style == "short":
            total, label, parts = shortterm.score(snap)
            rows_alerts = shortterm.signals(snap, cfg)
            trade_plan = shortterm.plan(snap, cfg, score=total)
        else:
            total, label, parts = scoring.score(snap)
            rows_alerts = scoring.signals(snap, cfg)
            trade_plan = None

        watch_rows.append(
            {
                "code": code,
                "name": name,
                "tag": item.get("tag", ""),
                "snap": snap,
                "score": total,
                "label": label,
                "parts": parts,
                "plan": trade_plan,
                "spark": indicators.sparkline([b["close"] for b in df[-30:]]),
                "alerts": rows_alerts,
            }
        )

    # ---------- 4. 持仓（含短线止损与时间止损）----------
    holdings = []
    snap_by_code = {r["code"]: r["snap"] for r in watch_rows}
    plan_by_code = {r["code"]: r.get("plan") for r in watch_rows}
    for h in cfg.get("holdings") or []:
        code = str(h["code"]).zfill(6)
        snap = snap_by_code.get(code)
        if not snap or not snap.get("close"):
            continue
        price, cost, qty = snap["close"], float(h["cost"]), float(h["qty"])
        held = _held_days(h.get("opened"))
        p = plan_by_code.get(code) or {}
        # 持仓的止损位：有计划就用计划止损；结构已破没有计划时，退回成本止损 7%，
        # 与 signals 里触发 P0 的那条线保持一致。留空是最没用的，等于没有规则。
        stop, basis = p.get("stop"), "计划止损"
        if not stop:
            stop, basis = round(cost * 0.93, 2), "成本-7%"
        holdings.append(
            {
                "code": code,
                "name": h.get("name", code),
                "qty": int(qty) if qty == int(qty) else qty,
                "cost": cost,
                "price": price,
                "pnl_pct": round((price / cost - 1) * 100, 2),
                "pnl_amt": round((price - cost) * qty, 2),
                "market_value": round(price * qty, 2),
                "held_days": held,
                "stop": stop,
                "stop_basis": basis,
                "stop_broken": bool(stop and price <= stop),
                "stop_dist_pct": round((stop / price - 1) * 100, 2) if stop else None,
            }
        )
        # 持仓的止损/时间止损信号由 signals 统一处理，这里补充成本口径
        if style == "short":
            extra = shortterm.signals(snap, cfg, cost=cost, opened_days=held)
            for row in watch_rows:
                if row["code"] == code:
                    row["alerts"] = extra
                    row["is_holding"] = True

    # ---------- 5. 汇总提醒 ----------
    if fallback_codes:
        notes.append(f"{len(fallback_codes)} 只个股日线走备源（{fallback_codes[0]} 等）")

    alerts_all = []
    for row in watch_rows:
        for a in row["alerts"]:
            alerts_all.append({**a, "code": row["code"], "name": row["name"]})

    if len(watch_rows) < len(wl):
        health.append(f"自选池 {len(wl)} 只中 {len(watch_rows)} 只取到数据")

    brief = {
        "date": day,
        "generated_at": now.strftime("%Y-%m-%d %H:%M"),
        "style": style,
        "market": {
            "state": state,
            "temp": temp,
            "indices": indices,
            "breadth": breadth,
            "sectors": sectors,
        },
        "global": gl,
        "watch": watch_rows,
        "holdings": holdings,
        "alerts_all": alerts_all,
        "data_health": health,
        "data_notes": notes,
        "risk": cfg.get("risk") or {},
    }
    brief["summary"] = build_summary(brief, cfg)
    return brief


def build_summary(brief: dict, cfg: dict) -> list[str]:
    """首屏结论：全部由确定性规则生成，不调用 LLM。

    短线模式下刻意把「可执行计划」放在第一位：如果只有强弱排序而给不出价，
    这份简报对短线就没有用。
    """
    out: list[str] = []
    m = brief["market"]
    b = m.get("breadth") or {}
    state = m.get("state") or {}
    temp = m.get("temp") or {}
    short = brief.get("style") == "short"

    # 1) 市场温度（短线第一眼该看的）
    if temp:
        line = (
            f"涨停 <b>{temp.get('limit_up', 0)}</b> 家、跌停 "
            f"<b>{temp.get('limit_down', 0)}</b> 家，上涨占比 "
            f"<b>{temp.get('up_ratio', 0) * 100:.0f}%</b>，涨跌中位数 "
            f"<b>{temp.get('median_pct', 0):+.2f}%</b>，"
            f"短线 <b>{temp.get('tag', '')}</b>（{temp.get('score', 0):.0f}/100）。"
        )
        if temp.get("warn"):
            line += f"注意：{temp['warn']}。"
        out.append(line)
    elif b:
        out.append(
            f"全市场 <b>{b.get('up', 0)}</b> 家上涨、<b>{b.get('down', 0)}</b> 家下跌，"
            f"涨跌中位数 <b>{b.get('median_pct', 0):+.2f}%</b>，"
            f"成交额 <b>{briefing._amt(b.get('total_amount'))}</b>，"
            f"市场状态判定为 <b>{state.get('state', '未知')}</b>。"
        )

    sectors = m.get("sectors") or {}
    if sectors.get("top"):
        lead = "、".join(f"{s['name']}({s['pct_chg']:+.1f}%)" for s in sectors["top"][:3])
        lag = "、".join(f"{s['name']}({s['pct_chg']:+.1f}%)" for s in sectors["bottom"][:3])
        out.append(f"资金主线在 <b>{lead}</b>；最弱的是 {lag}。")

    # 2) 可执行计划
    if short and brief["watch"]:
        ready = [r for r in brief["watch"] if (r.get("plan") or {}).get("ready")]
        ready.sort(key=lambda r: -r["score"])
        if ready:
            r0 = ready[0]
            p = r0["plan"]
            out.append(
                f"<b>今日可执行 {len(ready)} 只</b>，首选 <b>{r0['name']}</b>（{r0['score']} 分）："
                f"买点 <b>{p['buy_lo']:.2f}–{p['buy_hi']:.2f}</b>，"
                f"止损 <b>{p['stop']:.2f}</b>（{p['risk_pct']:.1f}%），"
                f"目标 <b>{p['target1']:.2f}</b>，盈亏比 {p['rr']:.1f}，"
                f"约 {p['shares']} 股（{p['amount'] / 10000:.1f} 万元）。"
            )
            if r0.get("parts", {}).get("过热说明"):
                out.append(
                    f"{r0['name']} 同时存在过热特征（{r0['parts']['过热说明']}），"
                    "计划以回踩为主，不要开盘直接追。"
                )
        else:
            out.append(
                "<b>今日没有满足盈亏比的标的</b>：不是没有强势票，而是上方空间不够，"
                "这种日子空仓也是决策。"
            )

    # 3) 风险提示
    hot = [r for r in brief["watch"] if (r.get("parts") or {}).get("过热", 0) <= -12]
    broken = [r for r in brief["watch"] if r["snap"].get("ma10") and r["snap"].get("close")
              and r["snap"]["close"] < r["snap"]["ma10"]]
    if short and (hot or broken):
        bits = []
        if hot:
            bits.append("过热 " + "、".join(r["name"] for r in hot[:3]))
        if broken:
            bits.append("跌破 MA10 " + "、".join(r["name"] for r in broken[:3]))
        out.append(f"<b>回避区</b>：{'；'.join(bits)}。")

    # 4) 提醒
    p0 = [a for a in brief["alerts_all"] if a["level"] == "P0"]
    p1 = [a for a in brief["alerts_all"] if a["level"] == "P1"]

    def _uniq_name(items, limit=3):
        seen, out_ = set(), []
        for a in items:
            if a["name"] in seen:
                continue
            seen.add(a["name"])
            out_.append(f"{a['name']}（{a['rule']}）")
            if len(out_) >= limit:
                break
        return "、".join(out_)

    if p0:
        out.append(f"<b>P0 提醒 {len(p0)} 条</b>：{_uniq_name(p0)} —— 需要优先处理。")
    elif p1:
        out.append(f"P1 提醒 {len(p1)} 条：{_uniq_name(p1)}，无需立即动作但要盯住。")

    # 5) 外围
    us = (brief["global"].get("us") or {})
    if us.get("ok") and us.get("data"):
        txt = "、".join(f"{i['name']}{i['pct_chg']:+.2f}%" for i in us["data"][:3])
        out.append(f"隔夜外围：{txt}。")

    if brief.get("data_health"):
        out.append(
            f"<b>数据缺口提示</b>：{len(brief['data_health'])} 项采集失败，"
            "见页脚，相关结论可信度下降。"
        )
    return out


def run_once(
    config_path: str | Path = ROOT / "config.yaml",
    notify_enabled: bool = True,
    quiet: bool = True,
) -> dict:
    """完整跑一次：采集 → 渲染 → 落库 → 通知。返回摘要 dict。

    Web UI 和命令行都走这里，避免两条路径的行为分叉。
    """
    cfg = load_config(Path(config_path))
    brief = build_brief(cfg)

    out_dir = ROOT / "data" / "briefings"
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / f"{brief['date']}.html"
    json_path = out_dir / f"{brief['date']}.json"

    html_path.write_text(briefing.render(brief, cfg), encoding="utf-8")
    json_path.write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")

    store = Store(ROOT / "data" / "market.db")
    try:
        store.save_run(brief)
    finally:
        store.close()

    n = cfg.get("notify", {}) or {}
    if notify_enabled and n.get("macos"):
        p0 = [a for a in brief["alerts_all"] if a["level"] == "P0"]
        if p0 or not n.get("only_on_p0"):
            head, msg = notify.build_message(brief)
            notify.macos_notify(n.get("title", "A股简报"), f"{head}｜{msg}", n.get("sound"))

    ready = [r for r in brief["watch"] if (r.get("plan") or {}).get("ready")]
    result = {
        "ok": True,
        "style": brief.get("style"),
        "date": brief["date"],
        "html": str(html_path),
        "json": str(json_path),
        "watch_count": len(brief["watch"]),
        "plans_ready": len(ready),
        "alerts": len(brief["alerts_all"]),
        "p0": len([a for a in brief["alerts_all"] if a["level"] == "P0"]),
        "data_health": brief["data_health"],
    }
    if not quiet:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="A 股每日简报")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    run_once(args.config, notify_enabled=not args.no_notify, quiet=args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
