#!/usr/bin/env python3
"""全市场选股运行器 —— 两段漏斗。

为什么需要它：
  screener.screen_stocks() 需要每只票的完整日线指标（均线/RSI/ATR/20日高低点），
  而给全市场 5000+ 只逐只拉日线在东财限流下要几小时，不现实。
  所以分两段：
    第一段（快照初筛）：用全市场快照的轻量字段（市值/成交额/换手/量比/涨跌幅）
                        做硬过滤 + 预排序，收窄到 top_n 只（默认 30）。
    第二段（日线精算）：只给这批候选拉日线、算全指标，跑完整三层漏斗 + 短线评分，
                        产出最终排序，并可选写回自选池。

数据源降级策略（东财各子域 WAF 独立，常被分别限流）：
  选股域：em.market_list()（全市场，push2）→ 失败则用 push2ex 涨停池+炸板池（短线最强域）
  日线：  ds.daily()（em push2his → 腾讯备源）；探测到 push2his 被限时直接走腾讯，省去无谓重试

用法：
    python3 run_screen.py                 # 选股，打印候选，不落库不改配置
    python3 run_screen.py --top 30        # 第二段精算的候选数（默认 30）
    python3 run_screen.py --pick 10       # 最终选入自选池的数量（默认 10）
    python3 run_screen.py --write         # 把 Top pick 写回 config.yaml watchlist（自动备份）
    python3 run_screen.py --replace       # 写回时替换整个自选池（默认是合并去重）
    python3 run_screen.py --save          # 结果落库 screening_results 表

注意：本脚本只做选股分析，不存在任何下单代码路径。
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src import configstore, datasource as ds, em, indicators, tx  # noqa: E402
from src.db import Store  # noqa: E402
from src.extras import broken_board_pool, collect_sentiment_data, limit_up_pool  # noqa: E402
from src.screener import hard_filter, screen_stocks  # noqa: E402
from src.shortterm import plan as st_plan  # noqa: E402
from src.shortterm import score as st_score  # noqa: E402

log = logging.getLogger("screen")

CONFIG_PATH = ROOT / "config.yaml"
BACKUP_DIR = ROOT / "data" / "backup"

# 探测到东财日线主源被限流后置 True，第二段直接走腾讯，省去每只票无谓的 em 重试
_EM_KLINE_BLOCKED = False


# ═══════════════════════════════════════════════════════════════════════
# 第一段：构建选股域（全市场快照，降级到涨停池）
# ═══════════════════════════════════════════════════════════════════════

def _normalize_snapshot(row: dict) -> dict:
    """把不同来源的快照统一成 screener 需要的字段口径。"""
    return {
        "code": str(row.get("code", "")).zfill(6),
        "name": row.get("name", ""),
        "close": row.get("close"),
        "pct_chg": row.get("pct_chg"),
        "amount": row.get("amount"),
        "turnover": row.get("turnover"),
        "vol_ratio": row.get("vol_ratio"),
        "market_cap": row.get("market_cap"),
    }


def build_universe() -> tuple[list[dict], str]:
    """构建选股域。返回 (股票快照列表, 数据源说明)。

    优先全市场快照（push2 clist）；被限流则降级到 push2ex 涨停池+炸板池——
    对短线而言，今日涨停/炸板池本就是最强的候选域。
    """
    # 主路径：全市场快照
    try:
        rows = em.market_list(page_size=100, max_pages=80)
        if rows and len(rows) >= 3000:
            uni = [_normalize_snapshot(r) for r in rows]
            return uni, f"全市场快照（东财 push2，{len(uni)} 只）"
        log.warning("全市场快照样本不足（%d 只），可能被截断", len(rows) if rows else 0)
    except Exception as exc:
        log.warning("全市场快照失败（%s），降级到涨停板池", exc)

    # 降级路径：push2ex 涨停池 + 炸板池（短线最强域）
    uni: list[dict] = []
    seen: set[str] = set()
    try:
        for s in limit_up_pool():
            code = str(s.get("code", "")).zfill(6)
            if not code or code in seen:
                continue
            seen.add(code)
            uni.append({
                "code": code,
                "name": s.get("name", ""),
                "close": s.get("price"),
                "pct_chg": s.get("pct"),
                "amount": s.get("amount"),
                "turnover": s.get("turnover"),
                "vol_ratio": None,
                "market_cap": s.get("float_cap"),
                # 涨停池特有，供预排序加成
                "limit_days": s.get("limit_days", 1),
                "seal_fund": s.get("seal_fund"),
                "break_times": s.get("break_times", 0),
                "industry": s.get("industry", ""),
            })
        for s in broken_board_pool():
            code = str(s.get("code", "")).zfill(6)
            if not code or code in seen:
                continue
            seen.add(code)
            uni.append({
                "code": code,
                "name": s.get("name", ""),
                "close": s.get("price"),
                "pct_chg": s.get("pct"),
                "amount": None,
                "turnover": s.get("turnover"),
                "vol_ratio": None,
                "market_cap": None,
                "limit_days": 0,  # 炸板，未封住
                "break_times": s.get("break_times", 0),
                "industry": s.get("industry", ""),
            })
    except Exception as exc:
        log.error("涨停板池也失败（%s），无可用选股域", exc)
        return [], "无（全市场与涨停池均不可用）"

    return uni, f"涨停板池+炸板池（东财 push2ex，{len(uni)} 只，全市场快照被限流降级）"


# ═══════════════════════════════════════════════════════════════════════
# 第一段：快照级预排序，收窄到 top_n
# ═══════════════════════════════════════════════════════════════════════

def _prerank_score(s: dict) -> float:
    """用快照可得字段给短线潜力打一个粗分，用于第一段收窄。

    没有日线指标，只能用量价/市值/涨跌幅这些轻量字段近似：
      - 涨幅甜点区 2%~7% 最优（涨停池另有连板加成）；追高(>9)或下跌不给分
      - 量比 1.2~3.0 温和放量最优
      - 成交额越大流动性越好（对数缩放）
      - 换手 3%~15% 活跃区间
      - 涨停池：连板数加成、封单加成、炸板次数扣分
    """
    score = 0.0
    pct = s.get("pct_chg")
    if pct is not None:
        if 2 <= pct <= 7:
            score += 30
        elif 0 <= pct < 2:
            score += 12
        elif 7 < pct <= 9:
            score += 18
        # 涨停(>9)不额外加分，避免追高；下跌不给分

    vr = s.get("vol_ratio")
    if vr is not None and 1.2 <= vr <= 3.0:
        score += 15
    elif vr is not None and 1.0 <= vr < 1.2:
        score += 6

    amt = s.get("amount")
    if amt:
        # 成交额对数缩放：1亿→~8分，10亿→~16分，封顶 20
        import math
        score += min(20, max(0, (math.log10(amt) - 7) * 8))

    to = s.get("turnover")
    if to is not None and 3 <= to <= 15:
        score += 10
    elif to is not None and 1 <= to < 3:
        score += 4

    # 涨停池特有加成
    ld = s.get("limit_days")
    if ld:
        score += min(20, ld * 6)  # 连板越高越强，封顶 20
    seal = s.get("seal_fund")
    if seal and seal > 0:
        score += 8
    bt = s.get("break_times")
    if bt:
        score -= bt * 4  # 炸板次数多说明封板不稳

    return round(score, 2)


def stage1_narrow(universe: list[dict], top_n: int, cfg: dict) -> tuple[list[dict], dict]:
    """第一段：硬过滤 + 预排序，收窄到 top_n 只。"""
    passed, hard_stats = hard_filter(universe, (cfg.get("screener") or {}).get("hard_filters"))
    for s in passed:
        s["prerank"] = _prerank_score(s)
    passed.sort(key=lambda x: -x.get("prerank", 0))
    narrowed = passed[:top_n]
    stats = {
        "universe": len(universe),
        "hard_passed": hard_stats.get("passed", 0),
        "hard_filter": hard_stats,
        "narrowed": len(narrowed),
    }
    return narrowed, stats


# ═══════════════════════════════════════════════════════════════════════
# 第二段：为候选拉日线、算全指标
# ═══════════════════════════════════════════════════════════════════════

def _probe_em_kline() -> bool:
    """探测东财日线主源是否被限流。被限流则第二段直接走腾讯。"""
    global _EM_KLINE_BLOCKED
    try:
        em.kline("600519", 5)
        _EM_KLINE_BLOCKED = False
    except Exception:
        _EM_KLINE_BLOCKED = True
        log.warning("东财日线主源(push2his)被限流，第二段改用腾讯备源")
    return _EM_KLINE_BLOCKED


def _fetch_daily(code: str) -> tuple[list[dict] | None, str]:
    """拉日线。东财被限时直接走腾讯，省去无谓重试。"""
    if _EM_KLINE_BLOCKED:
        try:
            return tx.kline(code, 320), "tencent"
        except Exception:
            return None, "none"
    bars, src = ds.daily(code)
    return bars, src


def stage2_enrich(candidates: list[dict]) -> list[dict]:
    """第二段：为每只候选拉日线、算全指标，合并成完整快照。"""
    enriched = []
    total = len(candidates)
    for i, c in enumerate(candidates, 1):
        code = c["code"]
        bars, src = _fetch_daily(code)
        if not bars or len(bars) < 30:
            log.info("[%d/%d] %s 日线不足，跳过", i, total, code)
            continue
        try:
            df = indicators.compute(bars, code=code)
            snap = indicators.snapshot(df)
        except Exception as exc:
            log.info("[%d/%d] %s 指标计算失败：%s", i, total, code, exc)
            continue
        # 合并：快照指标为主，补上第一段已有的市值/换手/涨停池字段
        merged = {**snap, **{k: v for k, v in c.items() if v is not None and k not in snap}}
        merged["code"] = code
        merged["name"] = c.get("name") or snap.get("name") or code
        merged["daily_src"] = src
        enriched.append(merged)
        if i % 10 == 0:
            log.info("第二段进度 %d/%d", i, total)
    return enriched


# ═══════════════════════════════════════════════════════════════════════
# 最终筛选与输出
# ═══════════════════════════════════════════════════════════════════════

def run_screen(cfg: dict, top_n: int = 30) -> dict:
    """完整跑一次两段漏斗选股。"""
    t_start = time.time()

    # 情绪数据（驱动第二层市场环境过滤）
    sentiment = {}
    try:
        extras = collect_sentiment_data()
        sentiment = {
            "phase": _phase_from_extras(extras),
            "max_board_height": extras.get("max_board_height", 0),
        }
    except Exception as exc:
        log.warning("情绪采集失败，用中性阶段：%s", exc)
        sentiment = {"phase": "中性"}

    # 第一段
    universe, uni_src = build_universe()
    if not universe:
        return {"ok": False, "error": "无可用选股域（全市场与涨停池均被限流）", "sentiment": sentiment}
    narrowed, s1_stats = stage1_narrow(universe, top_n, cfg)
    log.info("第一段：%s → 硬过滤 %d → 收窄 %d 只",
             uni_src, s1_stats["hard_passed"], s1_stats["narrowed"])

    # 探测日线主源
    _probe_em_kline()

    # 第二段
    enriched = stage2_enrich(narrowed)
    log.info("第二段：%d 只候选取到完整指标", len(enriched))
    if not enriched:
        return {"ok": False, "error": "候选日线全部获取失败", "sentiment": sentiment,
                "universe_source": uni_src, "stage1": s1_stats}

    # 完整三层漏斗 + 评分（screen_stocks 内部会再跑一遍 hard_filter/量价/位置/形态/评分）
    result = screen_stocks(enriched, sentiment, cfg.get("screener") or {})

    # 为最终候选补交易计划 + 入选理由
    candidates = result.get("candidates", [])
    for c in candidates:
        try:
            total, label, _ = st_score(c)
            c["plan"] = st_plan(c, cfg, score=total)
        except Exception:
            c["plan"] = {}
        c["reasons"] = build_reasons(c)

    result["ok"] = True
    result["universe_source"] = uni_src
    result["sentiment"] = sentiment
    result["stage1"] = s1_stats
    result["enriched_count"] = len(enriched)
    result["elapsed"] = round(time.time() - t_start, 1)
    return result


def _phase_from_extras(extras: dict) -> str:
    """从 collect_sentiment_data 结果粗判情绪阶段（供 screener 第二层用）。"""
    stats = extras.get("limit_up_stats") or {}
    zt = stats.get("limit_up_count", 0)
    dt = stats.get("limit_down_count", 0)
    height = extras.get("max_board_height", 0)
    if zt < 35 and dt > 40:
        return "冰点"
    if zt > 100 and height >= 5:
        return "高潮"
    if zt > 80 and height >= 4:
        return "回暖"
    if dt > 25:
        return "退潮"
    return "中性"


# ═══════════════════════════════════════════════════════════════════════
# 选股理由：把评分拆解 + 指标证据翻译成可读文字
# ═══════════════════════════════════════════════════════════════════════

def build_reasons(c: dict) -> list[str]:
    """为单只候选生成可读的入选理由（每条都带具体数字，便于人工核对）。

    理由来源：
      - shortterm.score 的分项拆解（screen_parts：动量/量价/形态/结构/空间/过热）
      - 快照里的关键指标证据（RET5/量比/换手/距20日高/均线排列/布林/ATR/连板封单等）
    不说"形态好""量价配合"这类无法核对的空话，每个判断都给出触发它的数值。
    """
    parts = c.get("screen_parts") or {}
    reasons: list[str] = []

    def emit(dim: str, got, full: int, bits: list[str]):
        if got is None:
            return
        body = "，".join(b for b in bits if b)
        reasons.append(f"{dim} {got:.0f}/{full}" + (f"：{body}" if body else ""))

    # 动量 28
    ret5, ret10 = c.get("ret5"), c.get("ret10")
    bits = []
    if ret5 is not None:
        r5 = ret5 * 100
        zone = "甜点区(3~12%)" if 3 <= r5 <= 12 else ("偏热(>12%)" if r5 > 12 else "偏弱(<3%)")
        bits.append(f"RET5 {r5:+.1f}% {zone}")
    if ret10 is not None:
        bits.append(f"RET10 {ret10 * 100:+.1f}% {'方向向上' if ret10 > 0 else '方向仍向下(多为反抽)'}")
    emit("动量", parts.get("动量"), 28, bits)

    # 量价 24
    vr, to, tor = c.get("vol_ratio"), c.get("turnover"), c.get("turnover_ratio")
    pct = c.get("pct_chg")
    bits = []
    if vr is not None:
        bits.append(f"量比 {vr:.2f} " + ("温和放量" if 1.1 <= vr <= 2.2 else ("缩量" if vr < 1 else "放量偏大")))
    if tor is not None:
        bits.append(f"换手为自身20日均 {tor:.1f} 倍")
    elif to is not None:
        bits.append(f"换手 {to:.1f}%")
    if pct is not None and vr is not None:
        if pct > 0 and vr >= 1.2:
            bits.append("涨时放量(量价齐升)")
        elif pct > 0 and vr < 0.85:
            bits.append("无量上涨(持续性存疑)")
        elif pct < -1 and vr > 1.5:
            bits.append("放量下跌(警惕)")
    emit("量价", parts.get("量价"), 24, bits)

    # 形态 22
    close, high20, ma5 = c.get("close"), c.get("high20"), c.get("ma5")
    bits = []
    if close and high20:
        gap = (close / high20 - 1) * 100
        if gap > 0:
            gap_txt = "已突破前高"
        elif gap >= -3:
            gap_txt = "贴近20日高·蓄势"
        else:
            gap_txt = "仍在箱体内"
        bits.append(f"距20日高 {gap:+.1f}% {gap_txt}")
    if close and ma5:
        bits.append("站上MA5" if close >= ma5 else "跌破MA5")
    bp = c.get("boll_pos")
    if bp is not None:
        if bp > 0.98:
            bp_txt = "贴上轨偏热"
        elif bp > 0.85:
            bp_txt = "偏上轨(偏热)"
        elif bp >= 0.5:
            bp_txt = "强势未超买"
        else:
            bp_txt = "中低位"
        bits.append(f"布林位置 {bp:.2f} {bp_txt}")
    cp = c.get("close_pos")
    if cp is not None:
        bits.append(f"尾盘收在日内 {cp:.2f} 分位" + ("(强)" if cp > 0.7 else ""))
    pats = c.get("patterns") or []
    if pats:
        bits.append("识别形态 " + "、".join(pats))
    emit("形态", parts.get("形态"), 22, bits)

    # 结构 16
    ma10, ma20 = c.get("ma10"), c.get("ma20")
    bits = []
    if ma5 and ma10 and ma20:
        if ma5 > ma10 > ma20:
            bits.append("MA5>MA10>MA20 多头排列")
        elif ma5 > ma10:
            bits.append("MA5>MA10 短期转强")
        elif ma10 > ma20:
            bits.append("仅 MA10>MA20")
        else:
            bits.append("均线空头排列")
    sl = c.get("ma10_slope3")
    if sl is not None:
        bits.append(f"MA10 三日斜率 {sl * 100:+.1f}% {'上行' if sl > 0 else '下行'}")
    emit("结构", parts.get("结构"), 16, bits)

    # 空间 10
    atr = c.get("atr_pct")
    bits = []
    if atr is not None:
        bits.append(f"ATR% {atr:.1f}% " + ("甜点区(2.5~6%)" if 2.5 <= atr <= 6 else ("波动偏小" if atr < 2.5 else "波动偏大")))
    emit("空间", parts.get("空间"), 10, bits)

    # 过热（负分）
    heat = parts.get("过热")
    if heat is not None:
        note = parts.get("过热说明") or ""
        if heat < 0:
            reasons.append(f"过热 {heat:.0f}(扣分)：{note or '存在过热信号'}")
        else:
            reasons.append("过热 0：无过热信号，非追高买入")

    # 板池背景（仅当候选来自涨停/炸板池，带连板/封单/行业）
    ld, seal, bt, ind = c.get("limit_days"), c.get("seal_fund"), c.get("break_times"), c.get("industry")
    bits = []
    if ld:
        bits.append(f"{ld} 连板")
    if seal:
        bits.append(f"封单 {seal / 1e8:.2f} 亿")
    if bt:
        bits.append(f"炸板 {bt} 次(封板不稳)")
    if ind:
        bits.append(f"行业 {ind}")
    if bits:
        reasons.append("板池背景：" + "，".join(bits))

    return reasons


def format_report(result: dict, pick: int) -> str:
    """格式化选股报告为可读文本。"""
    lines = []
    lines.append("=" * 64)
    lines.append("A股短线选股报告")
    lines.append("=" * 64)
    if not result.get("ok"):
        lines.append(f"选股失败：{result.get('error')}")
        return "\n".join(lines)

    sent = result.get("sentiment", {})
    lines.append(f"情绪阶段：{sent.get('phase', '未知')}    连板高度：{sent.get('max_board_height', 0)}")
    lines.append(f"选股域：{result.get('universe_source')}")
    s1 = result.get("stage1", {})
    hf = s1.get("hard_filter", {})
    lines.append(
        f"第一段：域 {s1.get('universe', 0)} 只 → 硬过滤后 {s1.get('hard_passed', 0)} 只 "
        f"→ 收窄 {s1.get('narrowed', 0)} 只"
    )
    lines.append(
        f"  硬过滤排除：ST {hf.get('excluded_st', 0)} | 新股 {hf.get('excluded_new', 0)} | "
        f"小市值 {hf.get('excluded_small_cap', 0)} | 低成交 {hf.get('excluded_low_amount', 0)}"
    )
    lines.append(f"第二段：{result.get('enriched_count', 0)} 只取到完整日线指标")
    lines.append(f"耗时：{result.get('elapsed', 0)}s")
    lines.append("")

    cands = result.get("candidates", [])
    lines.append(f"最终候选（Top {min(pick, len(cands))} / 共 {len(cands)} 只）：")
    for i, c in enumerate(cands[:pick], 1):
        code = c.get("code", "")
        name = c.get("name", "") or code
        score = c.get("screen_score", 0)
        label = c.get("screen_label", "")
        close = c.get("close")
        pct = c.get("pct_chg")
        close_txt = f"{close:.2f}" if close else "-"
        pct_txt = f"{pct:+.1f}%" if pct is not None else "-"
        lines.append("=" * 64)
        lines.append(f"#{i}  {code} {name}    评分 {score}（{label}）    现价 {close_txt}（{pct_txt}）")
        # 入选理由（逐条带数字）
        for r in c.get("reasons", []):
            lines.append(f"    · {r}")
        # 交易计划
        plan = c.get("plan") or {}
        if plan.get("ready"):
            lines.append(
                f"    → 计划：买 {plan['buy_lo']:.2f}-{plan['buy_hi']:.2f}  "
                f"止损 {plan['stop']:.2f}  盈亏比 {plan['rr']:.1f}  建议 {plan['shares']} 股"
            )
        else:
            lines.append(f"    → 暂无计划：{plan.get('why', '不满足条件')}")
    lines.append("=" * 64)
    return "\n".join(lines)


def write_to_watchlist(result: dict, pick: int, replace: bool) -> dict:
    """把 Top pick 候选写回 config.yaml 的 watchlist（经 configstore 校验+备份+原子写）。"""
    cands = result.get("candidates", [])[:pick]
    if not cands:
        return {"ok": False, "error": "无候选可写入"}

    cfg = configstore.load(CONFIG_PATH)
    existing = [] if replace else list(cfg.get("watchlist") or [])
    existing_codes = {str(w.get("code", "")).zfill(6) for w in existing}

    added = []
    for c in cands:
        code = str(c.get("code", "")).zfill(6)
        if code in existing_codes:
            continue
        existing_codes.add(code)
        existing.append({
            "code": code,
            "name": c.get("name", code),
            "tag": (c.get("industry", "") or "选股")[:12],
        })
        added.append(f"{c.get('name', code)}({code})")

    # 上限 60（configstore 校验规则），超出按顺序截断
    if len(existing) > 60:
        existing = existing[:60]

    cfg["watchlist"] = existing
    saved = configstore.save(CONFIG_PATH, cfg, BACKUP_DIR)
    return {"ok": True, "added": added, "total": len(existing), "backup": saved.get("backup")}


def save_to_db(result: dict, day: str) -> int:
    """把最终候选落库 screening_results 表。"""
    cands = result.get("candidates", [])
    records = []
    for c in cands:
        records.append({
            "code": c.get("code"),
            "name": c.get("name"),
            "layer1_pass": True,
            "layer2_pass": True,
            "layer3_pass": True,
            "patterns": c.get("patterns", []),
            "score": c.get("screen_score"),
            "screen_label": c.get("screen_label"),
            "close": c.get("close"),
            "pct_chg": c.get("pct_chg"),
            "plan": c.get("plan"),
            "reasons": c.get("reasons", []),
        })
    store = Store(ROOT / "data" / "market.db")
    try:
        store.save_screening(day, records)
    finally:
        store.close()
    return len(records)


def main() -> int:
    ap = argparse.ArgumentParser(description="A股短线选股（两段漏斗）")
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--top", type=int, default=30, help="第二段精算的候选数（默认30）")
    ap.add_argument("--pick", type=int, default=10, help="最终选入自选池的数量（默认10）")
    ap.add_argument("--write", action="store_true", help="把 Top pick 写回 config.yaml watchlist")
    ap.add_argument("--replace", action="store_true", help="写回时替换整个自选池（默认合并去重）")
    ap.add_argument("--save", action="store_true", help="结果落库 screening_results 表")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = configstore.load(args.config)
    day = datetime.now().strftime("%Y-%m-%d")

    result = run_screen(cfg, top_n=args.top)
    print(format_report(result, args.pick))

    if not result.get("ok"):
        return 1

    if args.save:
        n = save_to_db(result, day)
        print(f"\n已落库 screening_results：{n} 条（{day}）")

    if args.write:
        wr = write_to_watchlist(result, args.pick, args.replace)
        if wr.get("ok"):
            mode = "替换" if args.replace else "合并"
            print(f"\n已{mode}写回自选池（备份：{wr['backup']}）")
            print(f"  新增 {len(wr['added'])} 只：{'、'.join(wr['added'])}")
            print(f"  自选池现共 {wr['total']} 只")
        else:
            print(f"\n写回自选池失败：{wr.get('error')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
