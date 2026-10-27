"""统一数据层：主源东方财富 → 备源腾讯，带本地缓存。

设计要点：
1. **故障切换**：主源挂了自动走备源，并在简报里注明用了哪个源。
2. **当日缓存**：日线按「代码 + 交易日」缓存到磁盘。反复跑调试不会重复打接口，
   这也是避免被限流最有效的办法。
3. **降级诚实**：拿不到就返回 None，由上层写进"数据缺口"，绝不编数。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date
from pathlib import Path

from src import em, tx
from src.net import FetchError

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / "cache" / "kline"

INDEX_SECIDS = {
    "1.000001": ("000001", "上证指数", "sh000001"),
    "0.399001": ("399001", "深证成指", "sz399001"),
    "0.399006": ("399006", "创业板指", "sz399006"),
    "1.000688": ("000688", "科创50", "sh000688"),
    "1.000300": ("000300", "沪深300", "sh000300"),
}
INDEX_ORDER = ["000001", "399001", "399006", "000688", "000300"]

US_SECIDS = {"100.NDX": "纳斯达克", "100.SPX": "标普500", "100.DJI": "道琼斯"}
US_TX = ["usIXIC", "usINX", "usDJI"]
HK_SECIDS = {"100.HSI": "恒生指数", "124.HSTECH": "恒生科技"}
HK_TX = ["hkHSI", "hkHSTECH"]


# ---------------- 指数 ----------------
def index_quotes() -> tuple[list[dict] | None, str]:
    try:
        rows = em.quotes(list(INDEX_SECIDS))
        if rows:
            out = []
            for r in rows:
                key = f"{r['market']}.{r['code']}"
                code, label, _ = INDEX_SECIDS.get(key, (r["code"], r["name"], ""))
                out.append({"code": code, "name": label, "close": r["close"],
                            "pct_chg": r["pct_chg"], "amount": r["amount"]})
            out.sort(key=lambda x: INDEX_ORDER.index(x["code"]) if x["code"] in INDEX_ORDER else 99)
            return out, "eastmoney"
    except FetchError as exc:
        log.warning("指数主源失败，切备源: %s", exc)

    try:
        rows = tx.quotes([v[2] for v in INDEX_SECIDS.values()])
        by_code = {r["code"]: r for r in rows}
        out = []
        for code, label, tcode in INDEX_SECIDS.values():
            r = by_code.get(tcode[2:]) or next(
                (x for x in rows if x["name"] == label), None
            )
            if r:
                out.append({"code": code, "name": label, "close": r["close"],
                            "pct_chg": r["pct_chg"], "amount": r["amount"]})
        out.sort(key=lambda x: INDEX_ORDER.index(x["code"]) if x["code"] in INDEX_ORDER else 99)
        return (out, "tencent") if out else (None, "none")
    except FetchError as exc:
        log.warning("指数备源也失败: %s", exc)
        return None, "none"


def global_quotes(kind: str) -> tuple[list[dict] | None, str]:
    """kind: 'us' | 'hk'"""
    mapping, tx_codes = (US_SECIDS, US_TX) if kind == "us" else (HK_SECIDS, HK_TX)
    try:
        rows = em.quotes(list(mapping))
        if rows:
            return [
                {"name": mapping.get(f"{r['market']}.{r['code']}", r["name"]),
                 "close": r["close"], "pct_chg": r["pct_chg"]}
                for r in rows
            ], "eastmoney"
    except FetchError as exc:
        log.warning("%s 主源失败: %s", kind, exc)

    try:
        rows = tx.quotes(tx_codes)
        return [{"name": r["name"], "close": r["close"], "pct_chg": r["pct_chg"]} for r in rows], "tencent"
    except FetchError as exc:
        log.warning("%s 备源失败: %s", kind, exc)
        return None, "none"


# ---------------- 个股快照（短线指标补齐） ----------------
def spot(codes: list[str]) -> dict[str, dict]:
    """批量快照 → {code: quote}。用于补日线里缺的换手率/量比。

    腾讯日线不带换手率，走备源时这一项会是 0，短线评分里"量价"维度就废了一半，
    所以单独拉一次快照补上。失败就返回空，上层把该项当缺失，不猜。
    """
    codes = [str(c).zfill(6) for c in codes]
    if not codes:
        return {}
    try:
        rows = em.spot(codes)
        if rows:
            return {r["code"]: r for r in rows}
    except FetchError as exc:
        log.warning("快照主源失败，切备源: %s", exc)
    try:
        rows = tx.quotes([tx.tx_code(c) for c in codes])
        return {
            str(r["code"]).zfill(6)[-6:]: r for r in rows if r.get("code")
        }
    except FetchError as exc:
        log.warning("快照备源也失败: %s", exc)
        return {}


# ---------------- 当日缓存（市场级数据） ----------------
# 涨跌家数与行业板块只有东财提供，而东财会随机限流。
# 缓存的不是"能凑合用"，是"上次真实拿到的数据 + 明确时点"，
# 比拿不到就整段缺失强，但不能当实时数据用，所以带上 stale 标记由上层声明。
SNAP_DIR = ROOT / "data" / "cache" / "market"


def _cache_read(name: str, max_age_h: float = 20.0) -> dict | None:
    f = SNAP_DIR / f"{name}.json"
    if not f.exists():
        return None
    try:
        obj = json.loads(f.read_text(encoding="utf-8"))
        ts = float(obj.get("ts") or 0)
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None
    if (time.time() - ts) / 3600 > max_age_h:
        return None
    return obj


def _cache_write(name: str, data: dict) -> None:
    try:
        SNAP_DIR.mkdir(parents=True, exist_ok=True)
        (SNAP_DIR / f"{name}.json").write_text(
            json.dumps({"ts": time.time(), "asof": time.strftime("%Y-%m-%d %H:%M"), "data": data},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("市场缓存写入失败 %s: %s", name, exc)


# ---------------- 全市场 ----------------
def breadth() -> dict | None:
    """涨跌家数、成交额、中位数。仅东方财富提供，失败时回落到当日缓存。"""
    try:
        rows = em.market_list()
    except FetchError as exc:
        log.warning("全市场快照失败: %s", exc)
        cached = _cache_read("breadth")
        if cached:
            d = dict(cached.get("data") or {})
            d["stale_asof"] = cached.get("asof")
            return d
        return None

    valid = [r for r in rows if r["pct_chg"] is not None]
    if not valid:
        return None

    pcts = sorted(r["pct_chg"] for r in valid)
    n = len(pcts)
    median = pcts[n // 2] if n % 2 else round((pcts[n // 2 - 1] + pcts[n // 2]) / 2, 2)
    top_amount = sorted(rows, key=lambda r: -(r["amount"] or 0))[:8]
    out = {
        "up": sum(1 for p in pcts if p > 0),
        "down": sum(1 for p in pcts if p < 0),
        "flat": sum(1 for p in pcts if p == 0),
        "limit_up": sum(1 for p in pcts if p >= 9.8),
        "limit_down": sum(1 for p in pcts if p <= -9.8),
        "median_pct": median,
        "total_amount": sum(r["amount"] or 0 for r in rows),
        "total_count": n,
        "top_amount": [
            {"code": r["code"], "name": r["name"], "amount": r["amount"], "pct_chg": r["pct_chg"]}
            for r in top_amount
        ],
    }
    _cache_write("breadth", out)
    return out


def sector_rank() -> dict | None:
    try:
        rows = [r for r in em.sectors("industry") if r["pct_chg"] is not None]
    except FetchError as exc:
        log.warning("行业板块失败: %s", exc)
        cached = _cache_read("sectors")
        if cached:
            d = dict(cached.get("data") or {})
            d["stale_asof"] = cached.get("asof")
            return d
        return None
    if not rows:
        return None
    rows.sort(key=lambda r: -r["pct_chg"])
    out = {
        "top": [{"name": r["name"], "pct_chg": r["pct_chg"]} for r in rows[:5]],
        "bottom": [{"name": r["name"], "pct_chg": r["pct_chg"]} for r in rows[-5:][::-1]],
    }
    _cache_write("sectors", out)
    return out


# ---------------- 加密（仅观测） ----------------
def crypto(symbols: list[str]) -> list[dict] | None:
    """加密报价，多源兜底。仅作风险偏好观测，不接任何交易能力。

    国内对虚拟货币交易炒作有明确禁止性规定，本模块只读公开行情，
    不要求任何交易所密钥，也不提供下单接口。
    """
    for provider in (_okx, _binance):
        data = provider(symbols)
        if data:
            return data
    return None


def _http_json(url: str) -> dict:
    from src.net import get_json

    return get_json(url)


def _okx(symbols: list[str]) -> list[dict] | None:
    out = []
    for sym in symbols:
        try:
            d = _http_json(f"https://www.okx.com/api/v5/market/ticker?instId={sym}-USDT")
            item = (d.get("data") or [{}])[0]
            last, open24 = float(item["last"]), float(item["open24h"])
            out.append({"name": sym, "close": round(last, 2),
                        "pct_chg": round((last / open24 - 1) * 100, 2) if open24 else None})
        except Exception:  # noqa: BLE001
            return None
    return out


def _binance(symbols: list[str]) -> list[dict] | None:
    out = []
    for sym in symbols:
        try:
            d = _http_json(f"https://api.binance.com/api/v3/ticker/24hr?symbol={sym}USDT")
            out.append({"name": sym, "close": round(float(d["lastPrice"]), 2),
                        "pct_chg": round(float(d["priceChangePercent"]), 2)})
        except Exception:  # noqa: BLE001
            return None
    return out


# ---------------- 日线（带缓存） ----------------
def daily(code: str, days: int = 320) -> tuple[list[dict] | None, str]:
    code = str(code).zfill(6)
    today = date.today().isoformat()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"{code}.json"

    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if cached.get("fetched_on") == today and cached.get("bars"):
                return cached["bars"], f"{cached.get('source')}(缓存)"
        except (json.JSONDecodeError, OSError):
            pass

    bars, source = None, "none"
    try:
        bars, source = em.kline(code, days=days), "eastmoney"
    except FetchError as exc:
        log.warning("%s 主源日线失败: %s", code, exc)
        try:
            bars, source = tx.kline(code, days=days), "tencent"
        except FetchError as exc2:
            log.warning("%s 备源日线也失败: %s", code, exc2)
            return None, "none"

    try:
        cache_file.write_text(
            json.dumps({"fetched_on": today, "source": source, "bars": bars}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("缓存写入失败 %s: %s", code, exc)
    return bars, source
