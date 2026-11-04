"""市场情绪周期指标 —— 短线交易的环境判断层。

为什么情绪比指数重要：
  指数涨 1% 但只有 800 家上涨，短线是亏钱的行情。
  指数跌 0.5% 但涨停 80 家、连板高度 5 板，短线是赚钱的行情。
  情绪决定的是"能不能做"和"用什么策略做"，指数只决定仓位上限。

情绪周期五阶段（短线核心框架）：
  冰点 → 修复 → 回暖 → 高潮 → 退潮 → 冰点 ...

  冰点：涨停<30，跌停>50，连板高度≤2，昨涨停今日表现<0%
        → 空仓或极轻仓，只做确定性最高的超跌反弹
  退潮：涨停30-50，跌停20-50，连板高度2-3，昨涨停今日表现-2%~0%
        → 轻仓防守，不追高
  中性：涨停50-80，跌停<20，连板高度3-4，昨涨停今日表现0%~2%
        → 正常仓位，趋势波段为主
  回暖：涨停80-120，跌停<10，连板高度4-6，昨涨停今日表现2%~5%
        → 加仓，打板/半路策略开启
  高潮：涨停>120，跌停极少，连板高度≥6，昨涨停今日表现>5%
        → 警惕，准备止盈，不追最后一棒

关键指标：
  1. 涨停家数 / 跌停家数：题材活跃度与恐慌程度
  2. 连板高度：情绪天花板，高度越高说明资金接力意愿越强
  3. 昨涨停今日表现：打板策略的盈亏反馈，是情绪最敏感的指标
  4. 涨跌比 / 中位数涨幅：赚钱效应的体感
  5. 成交额：市场参与度，缩量说明观望情绪浓
  6. 北向资金：外资态度，连续流出时控制仓位
"""
from __future__ import annotations

import math
from typing import Optional


# ---------------- 情绪周期阶段 ----------------
class SentimentPhase:
    """情绪周期阶段枚举"""
    ICE = "冰点"           # 恐慌，空仓
    EBB = "退潮"           # 修复中，轻仓
    NEUTRAL = "中性"       # 正常，标准仓位
    WARM = "回暖"          # 向好，加仓
    CLIMAX = "高潮"        # 过热，警惕


# 阶段对应的仓位建议
PHASE_POSITION = {
    SentimentPhase.ICE: (0.0, 0.2),       # 0-20%
    SentimentPhase.EBB: (0.1, 0.4),       # 10-40%
    SentimentPhase.NEUTRAL: (0.3, 0.7),   # 30-70%
    SentimentPhase.WARM: (0.5, 0.9),      # 50-90%
    SentimentPhase.CLIMAX: (0.3, 0.6),    # 30-60%（高潮反而要降仓）
}

# 阶段对应的策略偏好
PHASE_STRATEGY = {
    SentimentPhase.ICE: ["oversold_bounce"],
    SentimentPhase.EBB: ["oversold_bounce", "trend_pullback"],
    SentimentPhase.NEUTRAL: ["trend_pullback", "board_hit"],
    SentimentPhase.WARM: ["board_hit", "trend_pullback", "event_driven"],
    SentimentPhase.CLIMAX: ["board_hit", "event_driven"],  # 高潮期打板但要快进快出
}


def _curve(v: float | None, pts: list[tuple[float, float]]) -> float:
    """分段线性映射（与 shortterm.py 保持一致）"""
    if v is None:
        return pts[0][1]
    if v <= pts[0][0]:
        return pts[0][1]
    if v >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= v <= x1:
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (v - x0) / (x1 - x0)
    return pts[-1][1]


# ---------------- 情绪指标计算 ----------------
def calc_sentiment_score(
    limit_up: int,
    limit_down: int,
    up_ratio: float,
    median_pct: float,
    max_board_height: int = 0,
    yesterday_limit_up_perf: float = 0.0,
    total_amount: float = 0.0,
    north_net_flow: float = 0.0,
) -> dict:
    """计算综合情绪分数（0-100）和阶段判定。

    参数：
      limit_up: 涨停家数
      limit_down: 跌停家数
      up_ratio: 上涨家数占比（0-1）
      median_pct: 全市场中位数涨幅（%）
      max_board_height: 最高连板数（如 5 表示有五连板）
      yesterday_limit_up_perf: 昨日涨停股今日平均涨幅（%）
      total_amount: 全市场成交额（亿元）
      north_net_flow: 北向资金净流入（亿元）

    返回：
      {
        "score": 0-100,
        "phase": "冰点/退潮/中性/回暖/高潮",
        "position_range": (min, max),
        "preferred_strategies": [...],
        "factors": {...},  # 各因子得分明细
        "signals": [...],  # 关键信号提示
      }
    """
    factors = {}
    signals = []

    # 1. 涨停家数得分（权重 25%）
    # 甜点区：60-120 家最活跃，>150 反而要警惕（过于亢奋）
    lu_score = _curve(limit_up, [
        (0, 0.0), (15, 0.15), (30, 0.35), (50, 0.55),
        (80, 0.80), (120, 1.0), (180, 0.85), (250, 0.6)
    ])
    factors["limit_up"] = {"value": limit_up, "score": round(lu_score * 100, 1), "weight": 0.25}

    # 2. 跌停家数得分（权重 15%，反向）
    # 跌停越多越恐慌，得分越低
    ld_score = _curve(limit_down, [
        (0, 1.0), (5, 0.9), (15, 0.7), (30, 0.45),
        (50, 0.25), (80, 0.1), (120, 0.0)
    ])
    factors["limit_down"] = {"value": limit_down, "score": round(ld_score * 100, 1), "weight": 0.15}

    # 3. 涨跌比得分（权重 15%）
    ur_score = _curve(up_ratio, [
        (0.15, 0.0), (0.30, 0.25), (0.45, 0.50),
        (0.55, 0.70), (0.65, 0.90), (0.80, 1.0)
    ])
    factors["up_ratio"] = {"value": round(up_ratio, 3), "score": round(ur_score * 100, 1), "weight": 0.15}

    # 4. 中位数涨幅得分（权重 10%）
    med_score = _curve(median_pct, [
        (-3.0, 0.0), (-1.5, 0.20), (-0.5, 0.40),
        (0.0, 0.55), (0.8, 0.80), (2.0, 1.0), (4.0, 0.85)
    ])
    factors["median_pct"] = {"value": round(median_pct, 2), "score": round(med_score * 100, 1), "weight": 0.10}

    # 5. 连板高度得分（权重 15%）
    # 高度越高说明资金接力意愿越强，但>8板要警惕（过于极端）
    bh_score = _curve(max_board_height, [
        (0, 0.0), (1, 0.2), (2, 0.35), (3, 0.55),
        (4, 0.75), (5, 0.90), (6, 1.0), (8, 0.85), (12, 0.6)
    ])
    factors["board_height"] = {"value": max_board_height, "score": round(bh_score * 100, 1), "weight": 0.15}

    # 6. 昨涨停今日表现（权重 15%）—— 打板策略的盈亏反馈
    # 这是情绪最敏感的指标：昨天打板的人今天赚不赚钱
    ylu_score = _curve(yesterday_limit_up_perf, [
        (-5.0, 0.0), (-2.0, 0.20), (0.0, 0.45),
        (2.0, 0.70), (4.0, 0.90), (6.0, 1.0), (10.0, 0.8)
    ])
    factors["yesterday_limit_up"] = {"value": round(yesterday_limit_up_perf, 2), "score": round(ylu_score * 100, 1), "weight": 0.15}

    # 7. 成交额得分（权重 5%）
    # 万亿以上为活跃，7000亿以下为缩量
    amt_score = _curve(total_amount, [
        (4000, 0.0), (6000, 0.3), (8000, 0.6),
        (10000, 0.85), (15000, 1.0), (25000, 0.8)
    ])
    factors["amount"] = {"value": round(total_amount, 0), "score": round(amt_score * 100, 1), "weight": 0.05}

    # 加权总分
    total = sum(f["score"] * f["weight"] for f in factors.values())
    total = round(max(0, min(100, total)), 1)

    # 北向资金作为修正项（不纳入主分，但影响信号）
    if north_net_flow < -50:
        signals.append(f"北向资金大幅流出 {north_net_flow:.0f} 亿，外资态度谨慎")
        total = max(0, total - 5)
    elif north_net_flow > 50:
        signals.append(f"北向资金大幅流入 {north_net_flow:.0f} 亿，外资看多")
        total = min(100, total + 3)

    # 阶段判定
    phase = _determine_phase(total, limit_up, limit_down, max_board_height, yesterday_limit_up_perf)

    # 关键信号
    if limit_up >= 100 and limit_down >= 30:
        signals.append("涨停与跌停同时偏多，市场分歧剧烈，注意板块轮动")
    if max_board_height >= 6:
        signals.append(f"连板高度达 {max_board_height} 板，情绪接近高潮，警惕退潮")
    if yesterday_limit_up_perf < -2:
        signals.append("昨涨停今日大幅低开，打板策略亏损，情绪退潮")
    if limit_up < 20 and limit_down > 50:
        signals.append("涨停极少、跌停众多，市场冰点，等待修复信号")

    return {
        "score": total,
        "phase": phase,
        "position_range": PHASE_POSITION.get(phase, (0.3, 0.7)),
        "preferred_strategies": PHASE_STRATEGY.get(phase, ["trend_pullback"]),
        "factors": factors,
        "signals": signals,
    }


def _determine_phase(
    score: float,
    limit_up: int,
    limit_down: int,
    max_board_height: int,
    yesterday_perf: float,
) -> str:
    """根据综合分数和关键指标判定情绪阶段。

    不只看分数，还要看结构：
    - 分数高但跌停也多 → 分歧，不是高潮
    - 分数低但连板高度高 → 局部热点，不是冰点
    """
    # 冰点：分数低 + 涨停少 + 跌停多
    if score < 30 and limit_up < 35 and limit_down > 40:
        return SentimentPhase.ICE

    # 高潮：分数高 + 涨停多 + 连板高 + 昨涨停表现好
    if score > 75 and limit_up > 100 and max_board_height >= 5 and yesterday_perf > 3:
        return SentimentPhase.CLIMAX

    # 退潮：分数中等偏低，或从高潮回落
    if score < 45 or (limit_down > 25 and yesterday_perf < 0):
        return SentimentPhase.EBB

    # 回暖：分数中高，涨停较多，连板有高度
    if score > 60 and limit_up > 70 and max_board_height >= 3:
        return SentimentPhase.WARM

    # 默认中性
    return SentimentPhase.NEUTRAL


# ---------------- 情绪趋势（多日对比） ----------------
def calc_sentiment_trend(history: list[dict]) -> dict:
    """根据近 N 日情绪数据判断趋势方向。

    history: [{"date": "2024-01-01", "score": 65, "phase": "中性", ...}, ...]
    按日期升序排列。

    返回：
      {
        "direction": "上升/下降/震荡",
        "momentum": 正数表示情绪在改善，负数表示恶化,
        "consecutive_days": 连续同方向天数,
        "signal": "情绪修复中/情绪恶化中/情绪高位震荡/..."
      }
    """
    if len(history) < 2:
        return {"direction": "未知", "momentum": 0, "consecutive_days": 0, "signal": "数据不足"}

    scores = [h.get("score", 50) for h in history]
    recent = scores[-5:] if len(scores) >= 5 else scores

    # 计算动量（近5日线性回归斜率）
    n = len(recent)
    x_mean = (n - 1) / 2
    y_mean = sum(recent) / n
    numerator = sum((i - x_mean) * (y - y_mean) for i, y in enumerate(recent))
    denominator = sum((i - x_mean) ** 2 for i in range(n))
    slope = numerator / denominator if denominator else 0

    # 连续同方向天数
    direction_days = 0
    if len(scores) >= 2:
        last_dir = 1 if scores[-1] > scores[-2] else (-1 if scores[-1] < scores[-2] else 0)
        for i in range(len(scores) - 1, 0, -1):
            d = 1 if scores[i] > scores[i - 1] else (-1 if scores[i] < scores[i - 1] else 0)
            if d == last_dir and last_dir != 0:
                direction_days += 1
            else:
                break

    # 方向判定
    if slope > 3:
        direction = "上升"
    elif slope < -3:
        direction = "下降"
    else:
        direction = "震荡"

    # 信号
    current_score = scores[-1]
    if current_score < 30 and slope > 0:
        signal = "冰点修复中，关注能否持续回暖"
    elif current_score < 30 and slope <= 0:
        signal = "情绪仍在冰点，等待止跌信号"
    elif current_score > 75 and slope < 0:
        signal = "高潮回落，注意退潮风险"
    elif current_score > 75 and slope >= 0:
        signal = "情绪高潮，警惕过热"
    elif 40 <= current_score <= 65:
        signal = "情绪中性，正常操作"
    else:
        signal = f"情绪{direction}中"

    return {
        "direction": direction,
        "momentum": round(slope, 2),
        "consecutive_days": direction_days,
        "signal": signal,
        "recent_scores": recent,
    }


# ---------------- 板块情绪 ----------------
def calc_sector_sentiment(sector_data: list[dict]) -> dict:
    """计算板块层面的情绪分布。

    sector_data: [{"name": "半导体", "pct_chg": 2.5, "limit_up_count": 5, ...}, ...]

    返回：
      {
        "hot_sectors": [...],      # 涨停家数最多的板块
        "strong_sectors": [...],   # 涨幅最大的板块
        "weak_sectors": [...],     # 跌幅最大的板块
        "rotation_signal": "...",  # 轮动信号
        "concentration": 0-1,      # 热度集中度（越高说明资金越集中）
      }
    """
    if not sector_data:
        return {}

    # 按涨停家数排序
    by_limit = sorted(sector_data, key=lambda x: x.get("limit_up_count", 0), reverse=True)
    hot = [s for s in by_limit if s.get("limit_up_count", 0) >= 3][:5]

    # 按涨幅排序
    by_pct = sorted(sector_data, key=lambda x: x.get("pct_chg", 0), reverse=True)
    strong = by_pct[:5]
    weak = by_pct[-5:][::-1]

    # 热度集中度：涨停家数的赫芬达尔指数
    total_limit = sum(s.get("limit_up_count", 0) for s in sector_data)
    if total_limit > 0:
        shares = [(s.get("limit_up_count", 0) / total_limit) ** 2 for s in sector_data]
        concentration = sum(shares)
    else:
        concentration = 0

    # 轮动信号
    if concentration > 0.3:
        rotation = "资金高度集中，主线明确，跟随热点"
    elif concentration > 0.15:
        rotation = "资金相对集中，有主线但分歧存在"
    else:
        rotation = "资金分散，板块轮动快，不宜追高"

    return {
        "hot_sectors": [{"name": s["name"], "limit_up": s.get("limit_up_count", 0), "pct_chg": s.get("pct_chg", 0)} for s in hot],
        "strong_sectors": [{"name": s["name"], "pct_chg": s.get("pct_chg", 0)} for s in strong],
        "weak_sectors": [{"name": s["name"], "pct_chg": s.get("pct_chg", 0)} for s in weak],
        "rotation_signal": rotation,
        "concentration": round(concentration, 3),
    }


# ---------------- 与现有 market_temp 的集成 ----------------
def enhance_market_temp(breadth: dict, extra: dict | None = None) -> dict:
    """增强现有的 market_temp，加入情绪周期指标。

    breadth: 现有的涨跌家数数据（来自 datasource.breadth()）
    extra: 额外数据 {
        "max_board_height": int,
        "yesterday_limit_up_perf": float,
        "north_net_flow": float,
        "sector_data": list,
        # 涨停口径修复（可选）：push2ex 封板池的精确家数。
        # 仅当数据源确为封板池（source=push2ex）时由调用方传入；
        # 传入后覆盖 breadth 的 ≥9.8% 快照粗估，不传则回退 breadth。
        "limit_up": int | None,
        "limit_down": int | None,
    }

    返回增强后的温度数据，兼容现有 shortterm.market_temp() 的输出格式。
    """
    from src.shortterm import market_temp as _base_temp

    extra = extra or {}

    # ── 涨停口径修复 ──────────────────────────────────────────────
    # breadth.limit_up 用「涨幅 ≥9.8%」粗估涨停，20cm 大涨日会把大涨但未封板的
    # 票也算进去（如 365 vs 真实封板 118），温度分/情绪分随之虚高，还会和脉搏页
    # （push2ex 封板池）数字打架。封板池可用时以其为准，不可用则回退 breadth。
    raw_lu = breadth.get("limit_up", 0) if breadth else 0
    raw_ld = breadth.get("limit_down", 0) if breadth else 0
    pool_lu = extra.get("limit_up")
    pool_ld = extra.get("limit_down")
    temp_breadth = breadth
    lu_source = "breadth_snapshot"
    lu_note = ""
    if isinstance(pool_lu, (int, float)) and pool_lu >= 0:
        temp_breadth = dict(breadth or {})
        temp_breadth["limit_up"] = int(pool_lu)
        if isinstance(pool_ld, (int, float)) and pool_ld >= 0:
            temp_breadth["limit_down"] = int(pool_ld)
        lu_source = "push2ex_pool"
        if int(pool_lu) != raw_lu:
            lu_note = (f"涨停口径以封板池为准（{int(pool_lu)} 家），"
                       f"快照粗估 {raw_lu} 家（≥9.8%）已弃用")

    base = _base_temp(temp_breadth)
    if not base:
        return {}

    # 计算增强情绪分
    sentiment = calc_sentiment_score(
        limit_up=base.get("limit_up", 0),
        limit_down=base.get("limit_down", 0),
        up_ratio=base.get("up_ratio", 0.5),
        median_pct=base.get("median_pct", 0),
        max_board_height=extra.get("max_board_height", 0),
        yesterday_limit_up_perf=extra.get("yesterday_limit_up_perf", 0),
        total_amount=(breadth.get("total_amount") or 0) / 1e8,  # 转为亿
        north_net_flow=extra.get("north_net_flow", 0),
    )

    # 板块情绪
    sector_sentiment = calc_sector_sentiment(extra.get("sector_data", []))

    # 合并输出
    enhanced = dict(base)
    enhanced.update({
        "sentiment_score": sentiment["score"],
        "sentiment_phase": sentiment["phase"],
        "position_range": sentiment["position_range"],
        "preferred_strategies": sentiment["preferred_strategies"],
        "sentiment_factors": sentiment["factors"],
        "sentiment_signals": sentiment["signals"],
        "sector_sentiment": sector_sentiment,
        # 涨停口径透明化：limit_up 已是最终采用值，raw/source/note 供追溯
        "limit_up_raw": raw_lu,
        "limit_down_raw": raw_ld,
        "limit_up_source": lu_source,
        "limit_up_note": lu_note,
    })

    return enhanced
