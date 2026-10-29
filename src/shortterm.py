"""短线评分、交易计划与短线信号 —— 全部确定性规则。

和波段模式的根本区别（不是调阈值，是换逻辑）：

1. **不追高是硬约束，不是提示。** 短线最贵的错误是买在情绪最高点，
   所以"过热"在模型里是**负分项**，能把一只形态很好的票直接从"可跟"打成"观望"。
   波段模型里没有这个维度，因为波段容忍更高偏离。

2. **动量用短周期。** RET20 对短线太钝（20 天前的信息已经反映完了），
   主因子改成 RET5 / RET10，并用"甜点区"曲线：涨 3%~12% 最优，
   涨超 20% 反而降分——这是和"越强越好"的直觉相反的刻意设计。

3. **必须给得出出场价。** 只看多空的评分没法执行。这里每只票都推导
   买点区间 / 止损 / 两档目标 / 盈亏比 / 建议股数，且盈亏比不达标就不给计划。

4. **时间止损。** 短线拿着不动就是错。max_hold_days 之外强制离场，
   这条在信号里会顶成 P0。
"""
from __future__ import annotations

import math


def _clip(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _curve(v: float | None, pts: list[tuple[float, float]]) -> float:
    """分段线性映射。pts 按 x 递增，越界取端点值。

    用它代替"线性 + 硬截断"，是为了表达"甜点区"这种非单调关系：
    比如量比 1.2-2.2 最好，缩量和失控放量都要降分。
    """
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


# ---------------- 评分 ----------------
def score(snap: dict) -> tuple[int, str, dict]:
    """0-100 短线分 + 标签 + 分项拆解（含负分项「过热」）。

    五个正分维度 + 一个负分维度：
      动量 28    RET5 甜点区 3%~12%，RET10 定方向
      量价 24    量比甜点 1.1~2.2，换手 2%~12%，涨放量/跌缩量
      形态 22    距 20 日高点距离、站上 MA5、BOLL 通道位置、尾盘强弱
      结构 16    MA5>MA10>MA20 多头排列、MA10 上行
      空间 10    ATR% 甜点 2.5%~6%（太低没短线空间，太高风险大）
      过热 −     BIAS5 偏离、RSI6、连续涨幅、贴布林上轨
    """
    close = snap.get("close")
    ma5, ma10, ma20 = snap.get("ma5"), snap.get("ma10"), snap.get("ma20")
    ret5, ret10 = snap.get("ret5"), snap.get("ret10")
    vr = snap.get("vol_ratio")
    turnover = snap.get("turnover")
    pct = snap.get("pct_chg")
    high20 = snap.get("high20")
    boll_pos = snap.get("boll_pos")
    close_pos = snap.get("close_pos")
    atr_pct = snap.get("atr_pct")
    bias5 = snap.get("bias5")
    rsi6 = snap.get("rsi6")
    up_days = snap.get("up_days")
    slope10 = snap.get("ma10_slope3")

    # --- 动量 28：甜点区 + 方向确认 ---
    m_curve = _curve(
        (ret5 or 0) * 100,
        [(-12, 0.05), (-5, 0.22), (0, 0.40), (3, 0.72), (6, 0.95),
         (12, 1.00), (18, 0.62), (25, 0.30), (40, 0.12)],
    )
    momentum = 28 * m_curve
    if ret10 is not None and ret10 < 0:
        momentum *= 0.82  # 5 日反弹但 10 日仍向下：多为反抽，不给满
    elif ret10 is not None and ret10 > 0:
        momentum *= 1.0
    momentum = round(momentum, 1)

    # --- 量价 24 ---
    vr_score = _curve(
        vr,
        [(0.4, 0.0), (0.7, 0.25), (0.9, 0.55), (1.15, 0.88), (1.6, 1.0),
         (2.6, 0.85), (3.6, 0.5), (5.0, 0.2)],
    )
    # 换手强度优先用"相对自身 20 日均换手"，没有才退回绝对值。
    # 绝对换手率跨市值不可比：茅台 0.9% 已经是明显放量，小盘股 0.9% 是没人玩。
    to_ratio = snap.get("turnover_ratio")
    if to_ratio:
        to_score = _curve(
            to_ratio,
            [(0.4, 0.05), (0.7, 0.3), (1.0, 0.6), (1.6, 0.95), (2.5, 1.0),
             (4.0, 0.6), (6.0, 0.2)],
        )
    else:
        to_score = _curve(
            turnover,
            [(0.3, 0.05), (1.0, 0.3), (2.0, 0.72), (4.0, 1.0), (9.0, 0.9),
             (15.0, 0.6), (25.0, 0.25)],
        )
    volprice = 18 * vr_score + 6 * to_score
    # 量价配合：涨要放量，跌要缩量。反向就扣。
    if pct is not None and vr is not None:
        if pct > 0 and vr < 0.85:
            volprice *= 0.78          # 无量上涨，持续性差
        elif pct < -1 and vr > 1.5:
            volprice *= 0.55          # 放量下跌
        elif pct > 0 and vr >= 1.2:
            volprice *= 1.08          # 量价齐升，小幅加成
    volprice = round(min(volprice, 24), 1)

    # --- 形态 22 ---
    p = 0.0
    if close and high20:
        gap = (close / high20 - 1) * 100  # 距 20 日高点的百分比
        # 贴着前高（-3%~0）最好：蓄势待突破；突破后（0~3%）次之；
        # 离得远说明还在箱体里爬，-8% 以下没戏。
        p += 10 * _curve(gap, [(-15, 0.0), (-8, 0.15), (-5, 0.45), (-3, 0.85),
                               (0, 1.0), (4, 0.72), (10, 0.35)])
    if close and ma5:
        p += 6 * _curve((close / ma5 - 1) * 100,
                        [(-6, 0.0), (-2, 0.25), (0, 0.75), (2, 1.0), (8, 0.55)])
    if boll_pos is not None:
        # 0.5-0.85 为强势未超买；>1 触上轨算过热，这里只降不加
        p += 4 * _curve(boll_pos, [(0.2, 0.0), (0.45, 0.4), (0.65, 0.9),
                                   (0.85, 1.0), (1.0, 0.55)])
    if close_pos is not None:
        p += 2 * _curve(close_pos, [(0.1, 0.0), (0.4, 0.4), (0.7, 1.0)])
    pattern = round(min(p, 22), 1)

    # --- 结构 16 ---
    s = 0.0
    if ma5 and ma10 and ma20:
        if ma5 > ma10 > ma20:
            s += 9
        elif ma5 > ma10:
            s += 5
        elif ma10 > ma20:
            s += 2
    if slope10 is not None and slope10 > 0:
        s += 7 * min(1.0, slope10 / 0.03)
    structure = round(min(s, 16), 1)

    # --- 空间 10 ---
    room = round(10 * _curve(atr_pct, [(0.8, 0.0), (1.8, 0.35), (2.5, 0.9),
                                      (3.5, 1.0), (6.0, 0.8), (9.0, 0.35), (13, 0.0)]), 1)

    # --- 过热（负分）---
    heat = 0.0
    heat_notes: list[str] = []
    if bias5 is not None and bias5 * 100 > 6:
        pen = min(14.0, (bias5 * 100 - 6) / 4 * 12)
        heat -= pen
        heat_notes.append(f"偏离5日线 {bias5 * 100:.1f}%")
    if rsi6 is not None and rsi6 > 85:
        heat -= min(12.0, (rsi6 - 85) / 15 * 10)
        heat_notes.append(f"RSI6 {rsi6:.0f}")
    if ret5 is not None and ret5 * 100 > 18:
        heat -= min(10.0, (ret5 * 100 - 18) / 12 * 8)
        heat_notes.append(f"5日涨 {ret5 * 100:.1f}%")
    if up_days and up_days >= 5:
        heat -= 5
        heat_notes.append(f"连涨 {int(up_days)} 天")
    if boll_pos is not None and boll_pos > 0.98 and (pct or 0) > 0:
        heat -= 4
        heat_notes.append("贴上轨")
    heat = round(max(heat, -34.0), 1)

    total = int(round(momentum + volprice + pattern + structure + room + heat))
    total = max(0, min(100, total))

    if total >= 72:
        label = "可跟"
    elif total >= 62:
        label = "待突破"
    elif total >= 48:
        label = "观察"
    elif total >= 34:
        label = "偏弱"
    else:
        label = "回避"
    if heat <= -14:
        label += "·过热"

    return total, label, {
        "动量": momentum,
        "量价": volprice,
        "形态": pattern,
        "结构": structure,
        "空间": room,
        "过热": heat,
        "过热说明": "、".join(heat_notes),
    }


# ---------------- 交易计划 ----------------
def plan(snap: dict, cfg: dict, score: int | None = None) -> dict:
    """从指标推导可执行的交易计划：买点区间 / 止损 / 目标 / 盈亏比 / 股数。

    支持不了就直接说不支持，并给出原因。宁可空着，也不给一个算不出盈亏比的"建议"。

    score 传入后会做一次一致性校验：评分没过观察线的票不给计划。
    否则会出现"评分 32 分（回避）但 ready=True"这种自相矛盾的输出，
    这在第一版里真实发生过。
    """
    rk = cfg.get("risk") or {}
    capital = float(rk.get("capital") or 100000)
    risk_pct = float(rk.get("single_risk_pct") or 1.0)
    atr_mult = float(rk.get("atr_stop_mult") or 1.5)
    min_rr = float(rk.get("min_rr") or 1.8)
    max_hold = int(rk.get("max_hold_days") or 5)
    min_score = float(rk.get("min_score") or 48)

    close = snap.get("close")
    ma5, ma10 = snap.get("ma5"), snap.get("ma10")
    atr = snap.get("atr14")
    high20, high60 = snap.get("high20"), snap.get("high60")

    out: dict = {
        "ready": False, "why": "", "buy_lo": None, "buy_hi": None,
        "stop": None, "target1": None, "target2": None,
        "rr": None, "risk_pct": None, "shares": None, "amount": None,
        "hold_days": max_hold,
    }
    if not close or not atr or not ma5 or not ma10:
        out["why"] = "指标不全，无法定价"
        return out

    # 结构否决：跌破 10 日线，短线已经走坏，不接
    if close < ma10:
        out["why"] = f"收盘 {close:.2f} 低于 MA10 {ma10:.2f}，结构已破，等收复再看"
        return out
    if close < ma5 * 0.97:
        out["why"] = "刚跌破 5 日线，方向未明"
        return out
    if score is not None and score < min_score:
        out["why"] = f"评分 {score} 未达观察线 {min_score:.0f}，形态不具备短线胜率"
        return out

    # 买点区间：偏离 5 日线过远就等回踩，否则次日小幅低吸
    dev = close / ma5 - 1
    if dev > 0.05:
        buy_lo, buy_hi = ma5 * 1.00, ma5 * 1.02
        out["why"] = f"现价偏离 5 日线 {dev * 100:.1f}%，等回踩 MA5 再进"
    else:
        buy_lo = max(ma5 * 0.985, close - 0.6 * atr)
        buy_hi = close + 0.25 * atr

    buy_mid = (buy_lo + buy_hi) / 2

    # 止损：结构位下方留一点缓冲，和 ATR 止损取**更宽**的那个。
    #
    # 这里第一版写错了方向：取"更紧"的止损，结果招行算出 1.85% 止损，
    # 不到 1 个 ATR，日内正常波动就打掉了。止损必须宽于噪音，
    # 否则你会以极高的频率被扫出局——这是短线最典型的执行错误。
    # 宽度的代价是仓位变小，这是应该付出的代价。
    stop_struct = ma10 - 0.4 * atr
    stop_atr = close - atr_mult * atr
    stop = min(stop_struct, stop_atr)
    stop = max(stop, buy_mid * 0.94)   # 上限：止损不超过 6%，超过说明这票不适合短线
    stop = min(stop, buy_mid * 0.985)  # 下限：至少留 1.5% 距离
    if stop >= buy_mid:
        out["why"] = "止损位高于买点，参数冲突"
        return out

    risk = buy_mid - stop

    # 阻力位：只有收盘还在前高下方时，前高才算阻力。
    # 已经创新高还拿自己的高点当阻力，算出来的盈亏比必然是 0（第一版就是这个 bug）。
    resistance = None
    res_name = ""
    if high20 and close < high20 * 0.995:
        resistance, res_name = high20, "20 日高"
    elif high60 and close < high60 * 0.995:
        resistance, res_name = high60, "60 日高"

    t1_by_rr = buy_mid + 2.0 * risk
    t1 = min(resistance, t1_by_rr) if resistance and resistance > buy_mid else t1_by_rr
    t2 = buy_mid + 3.0 * risk
    rr = (t1 - buy_mid) / risk if risk else 0

    out.update({
        "buy_lo": round(buy_lo, 2), "buy_hi": round(buy_hi, 2),
        "stop": round(stop, 2),
        "target1": round(t1, 2), "target2": round(t2, 2),
        "rr": round(rr, 2),
        "risk_pct": round(risk / buy_mid * 100, 2),
        "hold_days": max_hold,
        "resistance": res_name or "创新高（无明确阻力）",
    })

    if rr < min_rr:
        if resistance:
            out["why"] = (out["why"] + "；" if out["why"] else "") + \
                f"上方 {res_name} {resistance:.2f} 太近，盈亏比只有 {rr:.1f}，不划算"
        else:
            out["why"] = (out["why"] + "；" if out["why"] else "") + \
                f"盈亏比只有 {rr:.1f}，低于 {min_rr}"
        out["ready"] = False
        return out

    # 仓位：单笔风险 1% 反推，再限制单票不超过总资金 30%
    max_loss = capital * risk_pct / 100
    shares = math.floor(max_loss / risk / 100) * 100
    cap_shares = math.floor(capital * 0.30 / buy_mid / 100) * 100
    shares = int(min(shares, cap_shares))
    if shares < 100:
        out["why"] = f"按 {risk_pct}% 单笔风险算不足 1 手（需 {risk:.2f} 元/股风险）"
        out["ready"] = False
        return out

    out["shares"] = shares
    out["amount"] = round(shares * buy_mid, 0)
    out["ready"] = True
    if not out["why"]:
        out["why"] = f"结构未破，上方{out['resistance']}"
    return out


# ---------------- 短线信号 ----------------
def signals(snap: dict, cfg: dict, cost: float | None = None,
            opened_days: int | None = None) -> list[dict]:
    """短线异常识别。返回 [{level, rule, message}]。

    和波段版的差别：这里一半规则是**卖出触发**（跌破 MA10、放量滞涨、冲高回落、
    时间止损），因为短线亏钱的真实原因是不会走，而不是不会买。
    """
    th = cfg["thresholds"]
    rk = cfg.get("risk") or {}
    max_hold = int(rk.get("max_hold_days") or 5)
    out: list[dict] = []

    close = snap.get("close")
    pct = snap.get("pct_chg")
    vr = snap.get("vol_ratio")
    ma5, ma10 = snap.get("ma5"), snap.get("ma10")
    bias5 = snap.get("bias5")
    rsi6 = snap.get("rsi6")
    up_days = snap.get("up_days")
    high_pct = snap.get("high_pct")
    limit = snap.get("limit_pct") or 10.0
    boll_pos = snap.get("boll_pos")

    def add(level, rule, msg):
        out.append({"level": level, "rule": rule, "message": msg})

    # ---- P0：必须今天处理 ----
    if snap.get("near_limit_up") and (pct or 0) > 0:
        add("P0", "涨停", f"涨幅 {pct:.2f}%，触及 {limit:.0f}% 涨停板，次日面临兑现压力")
    if pct is not None and pct <= -(limit * 0.95):
        add("P0", "跌停", f"跌幅 {pct:.2f}%，接近跌停，注意流动性与次日跳空")

    # ---- 卖出触发（P1）----
    if close and ma10 and close < ma10 and (vr or 0) >= 1.3:
        add("P1", "放量破MA10", f"收盘 {close:.2f} 跌破 MA10 {ma10:.2f}，量比 {vr:.1f}，短线减仓信号")
    elif close and ma10 and close < ma10:
        add("P1", "跌破MA10", f"收盘 {close:.2f} 跌破 MA10 {ma10:.2f}，短线结构转弱")

    if vr and vr >= th.get("volume_ratio_stall", 2.2) and pct is not None and 0 <= pct < 1.5:
        add("P1", "放量滞涨", f"量比 {vr:.1f} 但只涨 {pct:.2f}%，放量不涨多为出货")

    if high_pct is not None and pct is not None:
        if high_pct >= th.get("fade_gain", 4.0) and pct < 1.0:
            add("P1", "冲高回落", f"盘中最高 +{high_pct:.2f}%，收于 {pct:+.2f}%，上方抛压重")

    # ---- 过热（P1/P2）----
    if bias5 is not None and bias5 * 100 >= th.get("bias5_hot", 10.0):
        add("P1", "偏离过大", f"偏离 5 日线 {bias5 * 100:.1f}%，追高随时回踩")
    if rsi6 is not None and rsi6 >= th.get("rsi6_hot", 88):
        add("P1", "RSI6过热", f"RSI6 = {rsi6:.0f}，短期严重超买")
    if up_days and up_days >= th.get("up_days_hot", 5):
        add("P1", "连涨过多", f"已连涨 {int(up_days)} 天，注意兑现")
    if boll_pos is not None and boll_pos > 1.0 and (pct or 0) > 0:
        add("P2", "触布林上轨", f"价格突破布林上轨（位置 {boll_pos:.2f}）")

    # ---- 买入观察（P2，不是买入建议）----
    # 必须 MA5 在 MA10 之上、且收盘仍守住 MA10 才叫"回踩"。
    # 均线空头排列时价格贴着 MA5，那是暴跌后的弱反弹；刚跌破 MA10 的也不是回踩。
    # 这条在第一版里误报过（东方财富跌破 MA10 却报了低吸窗口）。
    if (close and ma5 and ma10 and vr and ma5 > ma10 and close >= ma10
            and 0.975 <= close / ma5 <= 1.02 and vr <= 0.85):
        add("P2", "缩量回踩MA5", f"缩量回踩 5 日线（量比 {vr:.1f}），是短线低吸窗口，需看次日能否收回")
    if rsi6 is not None and rsi6 <= 15:
        add("P2", "RSI6超跌", f"RSI6 = {rsi6:.0f}，短期超跌，可能有反抽")

    # ---- 常规异动 ----
    if pct is not None:
        if abs(pct) >= th.get("pct_change_p0", 7.0):
            add("P0", "涨跌幅超阈值", f"单日{'涨' if pct > 0 else '跌'} {abs(pct):.2f}%")
        elif abs(pct) >= th.get("pct_change_p1", 4.0):
            add("P1", "涨跌幅异动", f"单日{'涨' if pct > 0 else '跌'} {abs(pct):.2f}%")

    if vr and vr >= th.get("volume_ratio_p1", 1.8):
        add("P1", "放量", f"成交量为 5 日均量的 {vr:.1f} 倍")

    if close and snap.get("high20") and close >= snap["high20"] * 0.999 and (vr or 0) >= 1.3:
        add("P1", "放量突破20日高", f"创 20 日新高，量比 {vr:.1f}，突破有效需次日确认")

    # 60 日回撤只在结构还没破时才有额外信息量；已经跌破 MA10 的情况上面报过了，
    # 重复报两条同样是噪音。
    dd = snap.get("drawdown60")
    if (dd is not None and dd <= -th.get("drawdown_from_high_p1", 0.20)
            and close and ma10 and close >= ma10):
        add("P2", "高位回撤", f"距 60 日最高回撤 {abs(dd) * 100:.1f}%，趋势级别尚未修复")

    # ---- 持仓规则（有成本才算）----
    if cost and close:
        pnl = close / cost - 1
        if pnl <= -0.07:
            add("P0", "触及止损区", f"相对成本 {pnl * 100:.1f}%，已超短线 7% 止损线，按纪律离场")
        elif pnl <= -0.04:
            add("P1", "亏损扩大", f"相对成本 {pnl * 100:.1f}%，接近止损区")
        if opened_days is not None and opened_days >= max_hold:
            add("P0", "时间止损", f"已持有 {opened_days} 个交易日（上限 {max_hold}），未走出预期即离场")
    return out


# ---------------- 短线市场温度 ----------------
def market_temp(breadth: dict) -> dict:
    """短线赚不赚钱，看的是涨停家数和赚钱效应，不是指数涨跌。

    指数涨 1% 但只有 800 家上涨，短线是亏钱的行情——这种情况必须能识别出来。
    """
    if not breadth or not breadth.get("total_count"):
        return {}
    up = breadth.get("up", 0)
    down = breadth.get("down", 0)
    lu = breadth.get("limit_up", 0)
    ld = breadth.get("limit_down", 0)
    med = breadth.get("median_pct") or 0.0
    up_ratio = up / max(1, breadth.get("total_count", 1))

    # 赚钱效应打分：涨停数（题材活跃度）+ 上涨占比 + 中位数（体感）
    s = 0.0
    s += _curve(lu, [(0, 0), (20, 0.25), (40, 0.55), (60, 0.8), (85, 1.0), (150, 0.9)])
    s += _curve(up_ratio, [(0.2, 0), (0.35, 0.25), (0.5, 0.6), (0.62, 0.9), (0.75, 1.0)])
    s += _curve(med, [(-2.0, 0), (-0.8, 0.25), (0, 0.6), (0.8, 0.95), (1.8, 1.0)])
    score_ = s / 3 * 100

    if score_ >= 68 and ld <= 12:
        tag, tone = "赚钱效应强", "up"
    elif score_ >= 50:
        tag, tone = "赚钱效应中性", "neutral"
    else:
        tag, tone = "赚钱效应弱", "down"

    warn = ""
    if breadth.get("sample_suspect"):
        tag, tone = "数据不可用", "neutral"
        warn = f"样本仅 {breadth.get('total_count')} 家，接口被截断，温度分不可信"
    elif lu >= 60 and ld >= 20:
        warn = "涨停与跌停同时偏多，分歧剧烈"
    elif lu <= 15:
        warn = "涨停家数偏少，题材熄火"

    return {
        "score": round(score_, 0),
        "tag": tag,
        "tone": tone,
        "limit_up": lu,
        "limit_down": ld,
        "up_ratio": round(up_ratio, 3),
        "median_pct": med,
        "warn": warn,
    }
