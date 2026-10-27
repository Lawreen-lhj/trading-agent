"""打分与信号识别 —— 全部确定性规则，可复现、可回测。

分数只用于排序和分层，不代表预期收益。任何"要不要买"的判断必须由人来下。
"""
from __future__ import annotations


def _clip(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _lin(v: float | None, lo: float, hi: float) -> float:
    """把 v 从 [lo, hi] 线性映射到 [0, 1]，越界截断。"""
    if v is None:
        return 0.0
    return _clip((v - lo) / (hi - lo))


def score(snap: dict) -> tuple[int, str, dict]:
    """0-100 综合分 + 标签 + 分项拆解。

    四维加权，全部落在 0-100：
      趋势 45 分：站上 MA20 / 多头排列 / MA20 上行（跌破 MA20 但守住 MA60 给部分分）
      动量 25 分：20 日涨幅，[-15%, +15%] 线性映射
      量能 15 分：量比，[0.7, 1.6] 线性映射
      位置 15 分：在 60 日区间的相对位置

    为什么用线性映射而不是硬阈值：硬阈值会让一批票同时压在 0 分或满分，
    排序失去区分度。这里刻意留出中间地带，让"偏弱但没崩"和"彻底走坏"能分开。
    """
    close = snap.get("close")
    ma5, ma20, ma60 = snap.get("ma5"), snap.get("ma20"), snap.get("ma60")
    slope = snap.get("ma20_slope")
    ret20 = snap.get("ret20")
    vol_ratio = snap.get("vol_ratio")
    pos = snap.get("pos60")

    # --- 趋势 45 ---
    t = 0.0
    if close and ma20:
        t += 18 if close > ma20 else 0
    if close and ma60 and close <= (ma20 or 0) and close > ma60:
        t += 7  # 跌破短期均线但仍在长期趋势上
    if ma5 and ma20 and ma5 > ma20:
        t += 12
    if ma20 and ma60 and ma20 > ma60:
        t += 6
    if slope:
        t += 8 * min(1.0, max(0.0, slope / 0.02))
    trend = round(min(t, 45), 1)

    momentum = round(25 * _lin(ret20, -0.15, 0.15), 1)
    volume = round(15 * _lin(vol_ratio, 0.7, 1.6), 1)
    position = round(15 * _clip(pos), 1) if pos is not None else 0.0

    total = int(round(trend + momentum + volume + position))
    total = max(0, min(100, total))

    if total >= 75:
        label = "强势"
    elif total >= 60:
        label = "偏强"
    elif total >= 45:
        label = "中性"
    elif total >= 30:
        label = "偏弱"
    else:
        label = "走弱"

    return total, label, {
        "趋势": trend,
        "动量": momentum,
        "量能": volume,
        "位置": position,
    }


def signals(snap: dict, cfg: dict, cost: float | None = None) -> list[dict]:
    """识别需要人看的异常。返回 [{level, rule, message}]。"""
    th = cfg["thresholds"]
    out: list[dict] = []
    close = snap.get("close")
    pct = snap.get("pct_chg")
    vr = snap.get("vol_ratio")
    rsi = snap.get("rsi14")
    dd = snap.get("drawdown60")

    if pct is not None:
        if abs(pct) >= th["pct_change_p0"]:
            out.append(
                {
                    "level": "P0",
                    "rule": "涨跌幅超阈值",
                    "message": f"单日{'涨' if pct > 0 else '跌'} {abs(pct):.2f}%，超过 {th['pct_change_p0']}% 阈值",
                }
            )
        elif abs(pct) >= th["pct_change_p1"]:
            out.append(
                {
                    "level": "P1",
                    "rule": "涨跌幅异动",
                    "message": f"单日{'涨' if pct > 0 else '跌'} {abs(pct):.2f}%",
                }
            )

    if vr and vr >= th["volume_ratio_p1"]:
        out.append(
            {
                "level": "P1",
                "rule": "放量",
                "message": f"成交量为 5 日均量的 {vr:.1f} 倍",
            }
        )

    if close and snap.get("high20") and close >= snap["high20"] * 0.999:
        out.append({"level": "P1", "rule": "创20日新高", "message": "收盘创 20 日新高"})
    if close and snap.get("low20") and close <= snap["low20"] * 1.001:
        out.append({"level": "P1", "rule": "创20日新低", "message": "收盘创 20 日新低"})

    if dd is not None and dd <= -th["drawdown_from_high_p1"]:
        out.append(
            {
                "level": "P1",
                "rule": "高位回撤",
                "message": f"距 60 日最高回撤 {abs(dd) * 100:.1f}%",
            }
        )

    if rsi is not None:
        if rsi >= th["rsi_overbought"]:
            out.append({"level": "P2", "rule": "RSI超买", "message": f"RSI14 = {rsi:.1f}，短期过热"})
        elif rsi <= th["rsi_oversold"]:
            out.append({"level": "P2", "rule": "RSI超卖", "message": f"RSI14 = {rsi:.1f}，短期超跌"})

    if cost and close:
        loss = close / cost - 1
        if loss <= -0.08:
            out.append(
                {
                    "level": "P0",
                    "rule": "持仓亏损超8%",
                    "message": f"相对成本浮亏 {abs(loss) * 100:.1f}%，需检查止损纪律",
                }
            )
    return out


def market_state(breadth: dict, index_quotes: list[dict]) -> dict:
    """市场状态判定：涨跌家数比为主，指数为辅。

    涨跌家数拿不到时（数据源限流），退化成纯指数口径，并明确标注"仅指数"，
    不要假装有全市场宽度数据。只做描述，不做预测。
    """
    idx = next((x for x in index_quotes if x["code"] == "000001"), None)
    sh_pct = idx.get("pct_chg") if idx else None

    if not breadth or not breadth.get("total_count"):
        if sh_pct is None:
            return {}
        if sh_pct >= 1.0:
            state, tone = "指数偏强", "up"
        elif sh_pct >= 0.2:
            state, tone = "指数微涨", "up"
        elif sh_pct <= -1.0:
            state, tone = "指数偏弱", "down"
        elif sh_pct <= -0.2:
            state, tone = "指数微跌", "down"
        else:
            state, tone = "指数平盘", "neutral"
        return {"state": f"{state}（仅指数口径）", "tone": tone,
                "up_ratio": None, "sh_pct": sh_pct}

    up = breadth.get("up", 0)
    total = breadth.get("total_count") or 1
    ratio = up / total

    if ratio >= 0.65:
        state, tone = "普涨", "up"
    elif ratio >= 0.55:
        state, tone = "偏强", "up"
    elif ratio >= 0.45:
        state, tone = "分化", "neutral"
    elif ratio >= 0.35:
        state, tone = "偏弱", "down"
    else:
        state, tone = "普跌", "down"

    if sh_pct is not None:
        if sh_pct >= 1.0 and tone == "neutral":
            state += "（指数强于个股）"
        elif sh_pct <= -1.0 and tone == "neutral":
            state += "（指数弱于个股）"

    return {
        "state": state,
        "tone": tone,
        "up_ratio": round(ratio, 3),
        "sh_pct": sh_pct,
    }
