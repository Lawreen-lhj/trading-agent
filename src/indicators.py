"""技术指标 —— 纯标准库实现，不依赖 pandas / TA-Lib。

原则：所有指标在这里算完，LLM 只读结果，不允许自己推算。

短线模式额外需要的指标（相对波段模式）：
  RSI6 / KDJ / BOLL          短线超买超卖与通道位置
  BIAS5 / BIAS10             偏离均线幅度 —— 判断"追高"的核心指标
  RET3 / RET5 / RET10        短周期涨幅，取代 RET20 当主因子
  ATR%                       波动空间，止损距离与仓位都由它推导
  CLOSE_POS                  收盘在当日振幅中的位置，尾盘强弱
  HIGH_PCT                   盘中最高涨幅，用于识别"冲高回落"
  UP_DAYS                    连续上涨天数，过热判据
"""
from __future__ import annotations

import math


def _sma(vals: list[float], n: int) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(vals)):
        if i + 1 < n:
            # 数据不足时用已有部分，避免开头全 None
            window = vals[: i + 1]
        else:
            window = vals[i + 1 - n: i + 1]
        out.append(sum(window) / len(window) if window else None)
    return out


def _ema(vals: list[float], n: int) -> list[float]:
    k = 2 / (n + 1)
    out: list[float] = []
    prev = None
    for v in vals:
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def _rsi(vals: list[float], n: int = 14) -> list[float]:
    if len(vals) < 2:
        return [50.0] * len(vals)
    gains, losses = [0.0], [0.0]
    for i in range(1, len(vals)):
        d = vals[i] - vals[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    # Wilder 平滑
    ag, al = gains[0], losses[0]
    out = [50.0]
    for i in range(1, len(vals)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
        out.append(100.0 if al == 0 else 100 - 100 / (1 + ag / al))
    return out


def _kdj(highs, lows, closes, n: int = 9):
    """KDJ(9,3,3)。短线看 J 值与 K/D 交叉。"""
    ks, ds, js = [], [], []
    k = d = 50.0
    for i in range(len(closes)):
        lo = min(lows[max(0, i - n + 1): i + 1])
        hi = max(highs[max(0, i - n + 1): i + 1])
        rsv = 50.0 if hi == lo else (closes[i] - lo) / (hi - lo) * 100.0
        k = (2 * k + rsv) / 3
        d = (2 * d + k) / 3
        ks.append(k)
        ds.append(d)
        js.append(3 * k - 2 * d)
    return ks, ds, js


def _boll(closes, n: int = 20, mult: float = 2.0):
    mid = _sma(closes, n)
    up, low = [], []
    for i in range(len(closes)):
        w = closes[max(0, i - n + 1): i + 1]
        sd = _stdev(w) if len(w) > 1 else 0.0
        m = mid[i] if mid[i] is not None else closes[i]
        up.append(m + mult * sd)
        low.append(m - mult * sd)
    return mid, up, low


def _stdev(vals: list[float]) -> float:
    if len(vals) < 2:
        return 0.0
    m = sum(vals) / len(vals)
    var = sum((v - m) ** 2 for v in vals) / (len(vals) - 1)
    return math.sqrt(var)


def limit_pct(code: str) -> float:
    """涨跌停幅度：创业板/科创板 20%，北交所 30%，主板 10%。"""
    c = str(code).zfill(6)
    if c.startswith(("688", "689")):
        return 20.0
    if c.startswith(("300", "301")):
        return 20.0
    if c.startswith(("8", "4", "9")):
        return 30.0
    return 10.0


def compute(bars: list[dict], code: str = "") -> list[dict]:
    """在日线列表上补齐指标列，返回新列表（不改原数据）。"""
    if not bars:
        return []
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    vols = [b["volume"] for b in bars]

    ma = {n: _sma(closes, n) for n in (5, 10, 20, 60)}
    rsi6 = _rsi(closes, 6)
    rsi14 = _rsi(closes, 14)
    ema12, ema26 = _ema(closes, 12), _ema(closes, 26)
    dif = [a - b for a, b in zip(ema12, ema26)]
    dea = _ema(dif, 9)
    hist = [(a - b) * 2 for a, b in zip(dif, dea)]
    kdj_k, kdj_d, kdj_j = _kdj(highs, lows, closes)
    boll_mid, boll_up, boll_low = _boll(closes)

    # ATR
    trs = []
    for i, b in enumerate(bars):
        prev_close = closes[i - 1] if i > 0 else b["close"]
        trs.append(max(b["high"] - b["low"], abs(b["high"] - prev_close), abs(b["low"] - prev_close)))
    atr14 = _ema(trs, 14)

    vol_ma5 = _sma(vols, 5)
    vol_ma10 = _sma(vols, 10)
    vol_ma20 = _sma(vols, 20)

    # 换手率均值：只用于算"相对自身"的换手强度。
    # 绝对换手率在不同市值之间不可比 —— 茅台 0.3% 是正常放量，
    # 小盘股 0.3% 是没人要。腾讯备源不提供换手率，全是 0 时这里退化为 None。
    tos = [b.get("turnover") or 0.0 for b in bars]
    to_ma20 = _sma(tos, 20) if sum(tos[-20:]) > 0 else [None] * len(bars)

    rets = [0.0] + [closes[i] / closes[i - 1] - 1 if closes[i - 1] else 0.0 for i in range(1, len(closes))]

    lp = limit_pct(code) if code else 10.0

    up_days = down_days = 0
    out = []
    for i, b in enumerate(bars):
        h10 = max(highs[max(0, i - 9): i + 1])
        l10 = min(lows[max(0, i - 9): i + 1])
        h20 = max(highs[max(0, i - 19): i + 1])
        l20 = min(lows[max(0, i - 19): i + 1])
        h60 = max(highs[max(0, i - 59): i + 1])
        l60 = min(lows[max(0, i - 59): i + 1])

        def _ret(n: int):
            return closes[i] / closes[i - n] - 1 if i >= n and closes[i - n] else None

        ret3, ret5, ret10, ret20, ret60 = _ret(3), _ret(5), _ret(10), _ret(20), _ret(60)
        vol20 = _stdev(rets[max(0, i - 19): i + 1]) * math.sqrt(252) if i >= 20 else None

        slope = slope10 = None
        if i >= 5 and ma[20][i - 5]:
            slope = ma[20][i] / ma[20][i - 5] - 1
        if i >= 3 and ma[10][i - 3]:
            slope10 = ma[10][i] / ma[10][i - 3] - 1

        # 连续涨跌天数
        if i == 0:
            up_days = down_days = 0
        elif closes[i] > closes[i - 1]:
            up_days, down_days = up_days + 1, 0
        elif closes[i] < closes[i - 1]:
            up_days, down_days = 0, down_days + 1
        else:
            up_days = down_days = 0

        prev_close = closes[i - 1] if i > 0 else b["close"]
        rng = b["high"] - b["low"]
        close_pos = (closes[i] - b["low"]) / rng if rng > 0 else 0.5
        high_pct = (b["high"] / prev_close - 1) * 100 if prev_close else None

        row = dict(b)
        row.update(
            {
                "ma5": ma[5][i], "ma10": ma[10][i], "ma20": ma[20][i], "ma60": ma[60][i],
                "rsi6": rsi6[i], "rsi14": rsi14[i],
                "dif": dif[i], "dea": dea[i], "macd": hist[i],
                "kdj_k": kdj_k[i], "kdj_d": kdj_d[i], "kdj_j": kdj_j[i],
                "boll_mid": boll_mid[i], "boll_up": boll_up[i], "boll_low": boll_low[i],
                "atr14": atr14[i],
                "atr_pct": (atr14[i] / closes[i] * 100) if closes[i] else None,
                "vol_ma5": vol_ma5[i], "vol_ma10": vol_ma10[i], "vol_ma20": vol_ma20[i],
                "vol_ratio": (vols[i] / vol_ma5[i]) if vol_ma5[i] else None,
                "vol_ratio10": (vols[i] / vol_ma10[i]) if vol_ma10[i] else None,
                "turnover_ma20": to_ma20[i],
                "turnover_ratio": (tos[i] / to_ma20[i]) if to_ma20[i] else None,
                "high10": h10, "low10": l10,
                "high20": h20, "low20": l20, "high60": h60, "low60": l60,
                "ret3": ret3, "ret5": ret5, "ret10": ret10, "ret20": ret20, "ret60": ret60,
                "vol20": vol20,
                "bias5": (closes[i] / ma[5][i] - 1) if ma[5][i] else None,
                "bias10": (closes[i] / ma[10][i] - 1) if ma[10][i] else None,
                "drawdown60": (closes[i] / h60 - 1) if h60 else None,
                "ma20_slope": slope,
                "ma10_slope3": slope10,
                "close_pos": close_pos,
                "high_pct": high_pct,
                "up_days": up_days,
                "down_days": down_days,
                "limit_pct": lp,
                "near_limit_up": (b.get("pct_chg") or 0) >= lp * 0.95,
            }
        )
        out.append(row)
    return out


def _r(v, nd: int = 4):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, nd)


def snapshot(bars: list[dict]) -> dict:
    """取最后一根的关键指标。"""
    if not bars:
        return {}
    last = bars[-1]
    prev = bars[-2] if len(bars) > 1 else last
    close, h60, l60 = last.get("close"), last.get("high60"), last.get("low60")
    pos = None
    if close and h60 and l60 and h60 > l60:
        pos = (close - l60) / (h60 - l60)

    # 布林带位置：0 = 下轨，1 = 上轨
    bu, bl = last.get("boll_up"), last.get("boll_low")
    boll_pos = None
    if close and bu and bl and bu > bl:
        boll_pos = (close - bl) / (bu - bl)

    keys = (
        "ma5", "ma10", "ma20", "ma60", "ma20_slope", "ma10_slope3",
        "rsi6", "rsi14", "dif", "dea", "macd",
        "kdj_k", "kdj_d", "kdj_j",
        "boll_mid", "boll_up", "boll_low",
        "atr14", "atr_pct",
        "vol_ratio", "vol_ratio10", "vol_ma5", "vol_ma10",
        "turnover_ma20", "turnover_ratio",
        "high10", "low10", "high20", "low20", "high60", "low60",
        "ret3", "ret5", "ret10", "ret20", "ret60", "vol20", "drawdown60",
        "bias5", "bias10", "close_pos", "high_pct", "up_days", "down_days",
        "turnover", "amplitude", "limit_pct",
    )
    snap = {
        "date": last.get("date"),
        "close": _r(close),
        "pct_chg": _r(last.get("pct_chg"), 2),
        "prev_close": _r(prev.get("close")),
        "open": _r(last.get("open")),
        "high": _r(last.get("high")),
        "low": _r(last.get("low")),
        "pos60": _r(pos),
        "boll_pos": _r(boll_pos),
        "near_limit_up": bool(last.get("near_limit_up")),
    }
    for k in keys:
        snap[k] = _r(last.get(k))
    return snap


def sparkline(values: list[float], width: int = 96, height: int = 28) -> str:
    """内联 SVG 迷你走势图。零 JS 依赖，不会因为脚本报错整页废掉。"""
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    step = width / (len(vals) - 1)
    pts = [
        f"{i * step:.1f},{height - (v - lo) / span * (height - 4) - 2:.1f}"
        for i, v in enumerate(vals)
    ]
    color = "#C0392B" if vals[-1] >= vals[0] else "#1E8449"  # 红涨绿跌
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'preserveAspectRatio="none" aria-hidden="true">'
        f'<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" '
        f'stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/></svg>'
    )
