"""全市场选股发现引擎 —— 三层漏斗筛选。

为什么需要选股发现：
  现有系统依赖手动维护 watchlist，但短线机会稍纵即逝，
  今天的主线龙头可能昨天还不在你的自选里。
  选股引擎的职责是每天盘前从 5000+ 只股票里筛出 20-50 只候选，
  再交给评分模型排序，最终输出 3-5 只可执行标的。

三层漏斗设计：
  第一层：硬性过滤（排除不可交易标的）
    - ST/*ST：风险警示，流动性差
    - 上市<60日：新股无足够K线，波动无规律
    - 停牌/一字板：无法成交
    - 市值<30亿：容易被操纵，机构不参与
    - 日均成交<5000万：资金容量不足

  第二层：市场环境过滤（决定能不能做）
    - 情绪冰点时只保留超跌反弹候选
    - 情绪高潮时警惕追高，降低仓位上限
    - 板块轮动快时不追单一热点

  第三层：个股筛选（在可做的市场里选好票）
    - 量价关系：量比、换手率变化、涨时量/跌时量
    - 价格位置：距高点距离、均线排列、布林带位置
    - 形态识别：平台突破、缩量回踩、放量长阳、N字形态
    - 资金面：主力净流入、北向持仓变化（需额外数据源）

与现有系统的关系：
  screener 输出候选池 → shortterm.score() 评分排序 → shortterm.plan() 生成计划
  不替代现有评分模型，而是扩大它的输入范围。
"""
from __future__ import annotations

import math
from typing import Optional


# ---------------- 硬性过滤条件 ----------------
HARD_FILTERS = {
    "exclude_st": True,           # 排除 ST/*ST
    "min_list_days": 60,          # 上市天数下限
    "exclude_suspended": True,    # 排除停牌
    "exclude_one_word": True,     # 排除一字板（无法买入）
    "min_market_cap": 30e8,       # 最小市值 30 亿
    "min_avg_amount": 5000e4,     # 最小日均成交额 5000 万
    "exclude_new_high_60d": False, # 是否排除 60 日新高（默认不排除）
}


def hard_filter(
    stocks: list[dict],
    filters: dict | None = None,
) -> tuple[list[dict], dict]:
    """第一层：硬性过滤。

    stocks: 全市场股票列表，每项需包含：
      - code: 股票代码
      - name: 股票名称
      - close: 收盘价
      - pct_chg: 涨跌幅
      - market_cap: 总市值（元）
      - amount: 成交额（元）
      - turnover: 换手率
      - is_st: 是否 ST（可选，默认从名称判断）
      - list_date: 上市日期（可选）
      - is_suspended: 是否停牌（可选）
      - avg_amount_20d: 20 日均成交额（可选）

    filters: 过滤条件，默认使用 HARD_FILTERS

    返回：(通过过滤的股票列表, 过滤统计)
    """
    filters = filters or HARD_FILTERS
    passed = []
    stats = {
        "total": len(stocks),
        "excluded_st": 0,
        "excluded_new": 0,
        "excluded_suspended": 0,
        "excluded_one_word": 0,
        "excluded_small_cap": 0,
        "excluded_low_amount": 0,
        "passed": 0,
    }

    for s in stocks:
        code = str(s.get("code", "")).zfill(6)
        name = s.get("name", "")

        # ST 过滤
        if filters.get("exclude_st"):
            is_st = s.get("is_st") or ("ST" in name.upper())
            if is_st:
                stats["excluded_st"] += 1
                continue

        # 新股过滤（上市<60天）
        if filters.get("min_list_days", 0) > 0:
            list_days = s.get("list_days")
            if list_days is not None and list_days < filters["min_list_days"]:
                stats["excluded_new"] += 1
                continue

        # 停牌过滤
        if filters.get("exclude_suspended"):
            if s.get("is_suspended") or s.get("close") in (None, 0):
                stats["excluded_suspended"] += 1
                continue

        # 一字板过滤（开盘=收盘=最高=最低，且涨停）
        if filters.get("exclude_one_word"):
            o, c, h, l = s.get("open"), s.get("close"), s.get("high"), s.get("low")
            if o and c and h and l and o == c == h == l:
                pct = s.get("pct_chg", 0)
                if pct and abs(pct) > 9:  # 一字涨停或跌停
                    stats["excluded_one_word"] += 1
                    continue

        # 市值过滤
        min_cap = filters.get("min_market_cap", 0)
        if min_cap > 0:
            cap = s.get("market_cap") or 0
            if cap < min_cap:
                stats["excluded_small_cap"] += 1
                continue

        # 成交额过滤
        min_amt = filters.get("min_avg_amount", 0)
        if min_amt > 0:
            # 优先用 20 日均额，没有就用当日
            amt = s.get("avg_amount_20d") or s.get("amount") or 0
            if amt < min_amt:
                stats["excluded_low_amount"] += 1
                continue

        passed.append(s)

    stats["passed"] = len(passed)
    return passed, stats


# ---------------- 量价筛选 ----------------
def volume_price_filter(
    stocks: list[dict],
    min_vol_ratio: float = 1.0,
    max_vol_ratio: float = 5.0,
    min_turnover_ratio: float = 0.8,
    require_up_with_volume: bool = True,
) -> list[dict]:
    """量价关系筛选。

    短线核心逻辑：
    - 量比 1.2-2.5 为温和放量，>5 为异常（可能是出货）
    - 换手率相对自身 20 日均值 >1.5 倍说明资金关注度提升
    - 涨时要放量，跌时要缩量（量价配合）
    """
    passed = []
    for s in stocks:
        vr = s.get("vol_ratio")
        tr = s.get("turnover_ratio")  # 相对自身 20 日均换手
        pct = s.get("pct_chg", 0)

        # 量比过滤
        if vr is not None:
            if vr < min_vol_ratio or vr > max_vol_ratio:
                continue

        # 换手强度过滤
        if tr is not None and tr < min_turnover_ratio:
            continue

        # 量价配合：涨要放量
        if require_up_with_volume and pct and pct > 0:
            if vr is not None and vr < 1.0:
                continue  # 无量上涨，持续性差

        passed.append(s)

    return passed


# ---------------- 价格位置筛选 ----------------
def price_position_filter(
    stocks: list[dict],
    max_distance_to_high20: float = 0.20,  # 距 20 日高点不超过 20%
    min_distance_to_low20: float = 0.05,   # 距 20 日低点至少 5%（已脱离底部）
    require_above_ma5: bool = True,
    require_ma_alignment: bool = False,     # 是否要求多头排列
) -> list[dict]:
    """价格位置筛选。

    短线选股的位置要求：
    - 不能离前高太远（套牢盘压力大）
    - 不能还在底部趴着（没有启动迹象）
    - 最好站在 MA5 之上（短期强势）
    """
    passed = []
    for s in stocks:
        close = s.get("close")
        high20 = s.get("high20")
        low20 = s.get("low20")
        ma5 = s.get("ma5")
        ma10 = s.get("ma10")
        ma20 = s.get("ma20")

        if not close:
            continue

        # 距 20 日高点
        if high20 and high20 > 0:
            dist_high = (high20 - close) / high20
            if dist_high > max_distance_to_high20:
                continue

        # 距 20 日低点
        if low20 and low20 > 0:
            dist_low = (close - low20) / low20
            if dist_low < min_distance_to_low20:
                continue

        # 站上 MA5
        if require_above_ma5 and ma5 and close < ma5:
            continue

        # 多头排列
        if require_ma_alignment:
            if not (ma5 and ma10 and ma20 and ma5 > ma10 > ma20):
                continue

        passed.append(s)

    return passed


# ---------------- 形态识别 ----------------
def detect_patterns(snap: dict) -> list[str]:
    """识别短线常见形态。

    返回匹配到的形态名称列表。
    """
    patterns = []
    close = snap.get("close")
    open_ = snap.get("open")
    high = snap.get("high")
    low = snap.get("low")
    vol_ratio = snap.get("vol_ratio")
    ma5 = snap.get("ma5")
    ma10 = snap.get("ma10")
    high20 = snap.get("high20")
    low10 = snap.get("low10")
    ret5 = snap.get("ret5")
    amplitude = snap.get("amplitude")
    boll_pos = snap.get("boll_pos")

    if not close:
        return patterns

    # 1. 平台突破：近 5 日振幅小，今日放量突破
    if amplitude is not None and vol_ratio and high20:
        # 近 5 日振幅小（用 ATR% 近似）
        atr_pct = snap.get("atr_pct")
        if atr_pct and atr_pct < 3.0 and vol_ratio > 1.5:
            if close >= high20 * 0.98:  # 接近或突破 20 日高
                patterns.append("平台突破")

    # 2. 缩量回踩：回调至 MA5/MA10 附近，量比<0.8
    if vol_ratio and vol_ratio < 0.8 and ma5 and ma10:
        if ma5 > ma10:  # 多头排列才叫回踩
            dist_ma5 = abs(close - ma5) / ma5
            if dist_ma5 < 0.02:  # 贴近 MA5
                patterns.append("缩量回踩")

    # 3. 放量长阳：涨幅>5%，量比>2，实体占比>80%
    pct = snap.get("pct_chg", 0)
    if pct and pct > 5 and vol_ratio and vol_ratio > 2:
        if open_ and high and low and high > low:
            body = abs(close - open_)
            full_range = high - low
            if full_range > 0 and body / full_range > 0.8:
                patterns.append("放量长阳")

    # 4. N 字形态：上涨→回调→再上涨，回调不破前低
    # 简化判断：ret5>0 但 ret3<0（近 3 日回调），今日又涨
    ret3 = snap.get("ret3")
    if ret5 and ret3 and pct:
        if ret5 > 0.03 and ret3 < 0 and pct > 1:
            if low10 and low and low > low10 * 1.02:  # 不破前低
                patterns.append("N字形态")

    # 5. 尾盘拉升：收盘位置在日内高位（close_pos > 0.85）
    close_pos = snap.get("close_pos")
    if close_pos and close_pos > 0.85 and pct and pct > 2:
        patterns.append("尾盘强势")

    # 6. 突破布林上轨：boll_pos > 1.0 且放量
    if boll_pos and boll_pos > 1.0 and vol_ratio and vol_ratio > 1.5:
        patterns.append("突破布林上轨")

    return patterns


# ---------------- 综合选股 ----------------
def screen_stocks(
    all_stocks: list[dict],
    market_sentiment: dict | None = None,
    config: dict | None = None,
) -> dict:
    """三层漏斗综合选股。

    all_stocks: 全市场股票数据（需包含日线指标）
    market_sentiment: 市场情绪数据（来自 sentiment.calc_sentiment_score）
    config: 选股配置

    返回：
      {
        "candidates": [...],      # 候选股票列表（已排序）
        "stats": {...},           # 各层过滤统计
        "market_filter": {...},   # 市场环境过滤结果
        "patterns_found": {...},  # 形态识别结果
      }
    """
    config = config or {}
    sentiment = market_sentiment or {}
    phase = sentiment.get("phase", "中性")

    stats = {}
    candidates = list(all_stocks)

    # 第一层：硬性过滤
    candidates, hard_stats = hard_filter(candidates, config.get("hard_filters"))
    stats["hard_filter"] = hard_stats

    # 第二层：市场环境过滤
    # 冰点时只保留超跌反弹候选（跌幅大但结构未破）
    if phase == "冰点":
        candidates = [
            s for s in candidates
            if s.get("rsi6") and s["rsi6"] < 30  # 超卖
            and s.get("close") and s.get("ma20") and s["close"] > s["ma20"] * 0.9  # 结构未完全破
        ]
        stats["market_filter"] = {"phase": phase, "action": "只保留超跌反弹候选", "passed": len(candidates)}
    elif phase == "高潮":
        # 高潮时警惕追高，排除已经大涨的
        candidates = [
            s for s in candidates
            if not (s.get("ret5") and s["ret5"] > 0.25)  # 5 日涨幅不超过 25%
            and not (s.get("bias5") and s["bias5"] > 0.12)  # 偏离 MA5 不超过 12%
        ]
        stats["market_filter"] = {"phase": phase, "action": "排除过热标的", "passed": len(candidates)}
    else:
        stats["market_filter"] = {"phase": phase, "action": "正常筛选", "passed": len(candidates)}

    # 第三层：个股筛选
    # 3a. 量价筛选
    candidates = volume_price_filter(candidates)
    stats["volume_price"] = {"passed": len(candidates)}

    # 3b. 价格位置筛选
    require_alignment = phase in ("回暖", "高潮")  # 情绪好时要求更严格
    candidates = price_position_filter(candidates, require_ma_alignment=require_alignment)
    stats["price_position"] = {"passed": len(candidates)}

    # 3c. 形态识别（加分项，不过滤）
    patterns_found = {}
    for s in candidates:
        pats = detect_patterns(s)
        if pats:
            patterns_found[s.get("code")] = pats

    # 3d. 综合打分排序（使用现有 shortterm.score）
    from src.shortterm import score as st_score

    scored = []
    for s in candidates:
        try:
            total, label, parts = st_score(s)
            scored.append({
                **s,
                "screen_score": total,
                "screen_label": label,
                "screen_parts": parts,
                "patterns": patterns_found.get(s.get("code"), []),
            })
        except Exception:
            continue

    # 按评分排序
    scored.sort(key=lambda x: -x.get("screen_score", 0))

    # 取 Top N
    top_n = config.get("top_n", 30)
    final = scored[:top_n]

    return {
        "candidates": final,
        "stats": stats,
        "market_filter": stats.get("market_filter", {}),
        "patterns_found": patterns_found,
        "total_screened": len(all_stocks),
        "final_count": len(final),
    }


# ---------------- 选股结果格式化 ----------------
def format_screen_result(result: dict) -> str:
    """格式化选股结果为可读文本。"""
    lines = []
    lines.append(f"=== 选股结果 ===")
    lines.append(f"全市场扫描: {result['total_screened']} 只")
    lines.append(f"最终候选: {result['final_count']} 只")
    lines.append("")

    # 过滤统计
    stats = result.get("stats", {})
    hard = stats.get("hard_filter", {})
    lines.append(f"硬性过滤: ST {hard.get('excluded_st', 0)} | "
                 f"新股 {hard.get('excluded_new', 0)} | "
                 f"小市值 {hard.get('excluded_small_cap', 0)} | "
                 f"低成交 {hard.get('excluded_low_amount', 0)}")

    mf = stats.get("market_filter", {})
    lines.append(f"市场环境: {mf.get('phase', '未知')} - {mf.get('action', '')}")
    lines.append("")

    # 候选列表
    lines.append("代码     名称       评分   标签     形态")
    lines.append("-" * 50)
    for s in result.get("candidates", [])[:20]:
        code = s.get("code", "")
        name = s.get("name", "")[:6]
        score = s.get("screen_score", 0)
        label = s.get("screen_label", "")
        patterns = ",".join(s.get("patterns", [])) or "-"
        lines.append(f"{code}  {name:<8} {score:>3}   {label:<8} {patterns}")

    return "\n".join(lines)
