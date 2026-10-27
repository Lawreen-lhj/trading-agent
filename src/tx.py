"""腾讯行情接口（备源）。

用途只有一个：东方财富被限流或抽风时顶上，保证简报不会因为单一数据源挂掉而缺失。
接口是明文的 GBK 文本，字段用 ~ 分隔，位置固定但没有官方文档，
所以解析全部用带默认值的下标访问，缺字段就返回 None 而不是抛错。
"""
from __future__ import annotations

import time

from src.net import FetchError, get_json, get_text

REFERER = {"Referer": "https://gu.qq.com/"}


def tx_code(code: str, market: str = "cn") -> str:
    """统一代码 → 腾讯代码。"""
    code = str(code)
    if market == "us":
        return f"us{code}"
    if market == "hk":
        return f"hk{code}"
    code = code.zfill(6)
    return ("sh" if code[0] == "6" else "sz") + code


def quotes(codes: list[str]) -> list[dict]:
    """批量报价。codes 已是腾讯格式（如 sh600519 / usIXIC / hkHSI）。"""
    if not codes:
        raise FetchError("空代码列表")
    text = get_text(f"https://qt.gtimg.cn/q={','.join(codes)}", encoding="gbk", headers=REFERER)
    out = []
    for line in text.strip().split("\n"):
        if "=" not in line or "~" not in line:
            continue
        f = line.split("~")
        if len(f) < 33:
            continue
        out.append(
            {
                "code": _s(f, 2),
                "name": _s(f, 1),
                "close": _n(f, 3),
                "prev_close": _n(f, 4),
                "change": _n(f, 31),
                "pct_chg": _n(f, 32),
                "volume": _n(f, 36),
                "amount": _n(f, 37, scale=1e4) if _n(f, 37) is not None else None,
                "turnover": _n(f, 38),
                "pe": _n(f, 39),
                "market_cap": _n(f, 45, scale=1e8) if _n(f, 45) is not None else None,
                "vol_ratio": _n(f, 49),
            }
        )
    if not out:
        raise FetchError("腾讯报价解析为空")
    return out


def kline(code: str, days: int = 320, market: str = "cn") -> list[dict]:
    """日线，统一成与东方财富一致的字段。A 股自动取前复权。"""
    tcode = tx_code(code, market)
    adjust = "qfq" if market == "cn" else ""
    param = f"{tcode},day,,,{days},{adjust}"
    d = get_json(
        f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={param}", headers=REFERER
    )
    node = (d.get("data") or {}).get(tcode) or {}
    rows = node.get("qfqday") or node.get("day") or []
    if not rows:
        raise FetchError(f"{code} 腾讯日线为空")

    out = []
    prev_close = None
    for r in rows:
        if len(r) < 6:
            continue
        try:
            o, c, h, lo, v = (float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
        except (TypeError, ValueError):
            continue
        pct = round((c / prev_close - 1) * 100, 2) if prev_close else 0.0
        out.append(
            {
                "date": str(r[0]),
                "open": o,
                "close": c,
                "high": h,
                "low": lo,
                "volume": v,
                "amount": float(r[6]) if len(r) > 6 and _isnum(r[6]) else 0.0,
                "amplitude": round((h - lo) / prev_close * 100, 2) if prev_close else 0.0,
                "pct_chg": pct,
                "change": round(c - prev_close, 2) if prev_close else 0.0,
                "turnover": 0.0,
            }
        )
        prev_close = c
    if len(out) < 20:
        raise FetchError(f"{code} 腾讯日线不足（{len(out)}）")
    return out


def _s(f: list[str], i: int) -> str | None:
    return f[i].strip() if i < len(f) and f[i].strip() else None


def _n(f: list[str], i: int, scale: float = 1.0) -> float | None:
    if i >= len(f):
        return None
    v = f[i].strip()
    if not v or v in ("-", "0.00"):
        return None if v != "0.00" else 0.0
    try:
        return round(float(v) * scale, 4)
    except ValueError:
        return None


def _isnum(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False
