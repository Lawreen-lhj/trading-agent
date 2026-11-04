"""扩展数据源 —— 情绪周期和事件驱动策略需要的精确市场数据。

V2 重写：用东财涨停板池（push2ex）、同花顺北向资金、东财板块资金流、
东财事件驱动数据中心替代原来的近似推断逻辑。

设计原则（不变）：
  - 所有函数"能拿到最好，拿不到返回 None/空"，不阻塞主流程
  - 纯 stdlib，不依赖 pandas/requests
  - 东财请求走 src.net.em_get_json()（≥1.2s 限流防封）
  - 非东财请求走 src.net.get_json()（0.35s 间隔）

数据源端点（参考 github.com/simonlin1212/a-stock-data V3.10）：
  §8.1  push2ex 涨停/炸板/跌停/昨涨停池 — 精确连板数、封板资金、炸板次数
  §8.3  打板情绪速算 — 炸板率、最高连板、梯队分布
  §3.2  同花顺 hsgtApi — 北向分钟流向（不封IP）
  §3.8  东财 push2/clist — 板块资金流向（行业/概念 × 今日/5日/10日）
  §3.5  东财 datacenter — 龙虎榜席位
  §14   东财 datacenter — 事件驱动（业绩预告/增减持/回购/调研）
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from src.net import FetchError, get_json, get_text, em_get_json

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
EXTRAS_CACHE = ROOT / "data" / "cache" / "extras"
EXTRAS_CACHE.mkdir(parents=True, exist_ok=True)

# 东财涨停板池 ut 令牌（公开值，a-stock-data 项目同款）
ZTB_UT = "7eea3edcaed734bea9cbfc24409ed989"

# 同花顺北向资金 headers
HSGT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/117.0.0.0 Safari/537.36",
    "Host": "data.hexin.cn",
    "Referer": "https://data.hexin.cn/",
}

# 东财数据中心统一 URL
DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


# ─── 缓存基础设施 ──────────────────────────────────────────────────────

def _cache_get(name: str, max_age_h: float = 6.0):
    """带时效的缓存读取"""
    f = EXTRAS_CACHE / f"{name}.json"
    if not f.exists():
        return None
    try:
        obj = json.loads(f.read_text(encoding="utf-8"))
        ts = float(obj.get("ts") or 0)
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None
    if (time.time() - ts) / 3600 > max_age_h:
        return None
    return obj.get("data")


def _cache_set(name: str, data):
    """缓存写入"""
    try:
        (EXTRAS_CACHE / f"{name}.json").write_text(
            json.dumps({"ts": time.time(), "asof": time.strftime("%Y-%m-%d %H:%M"), "data": data},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("extras 缓存写入失败 %s: %s", name, exc)


def _today_str(fmt: str = "%Y%m%d") -> str:
    return date.today().strftime(fmt)


def _num(v):
    if v is None or v in ("-", ""):
        return None
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None


# ═══════════════════════════════════════════════════════════════════════
# §8.1 涨停板池（push2ex）— 精确数据，替代原来从快照推断的方式
# ═══════════════════════════════════════════════════════════════════════

def _fmt_zt_time(t) -> str:
    """涨停板时间整数 → HH:MM:SS（92500 → 09:25:00）"""
    s = str(t).zfill(6)
    return f"{s[0:2]}:{s[2:4]}:{s[4:6]}"


def _em_zt_api(endpoint: str, sort: str, trade_date: str) -> list[dict]:
    """东财涨停板行情中心通用请求。
    endpoint: getTopicZTPool / getTopicZBPool / getTopicDTPool / getYesterdayZTPool
    """
    url = f"https://push2ex.eastmoney.com/{endpoint}"
    params = {
        "ut": ZTB_UT, "dpt": "wz.ztzt",
        "Pageindex": 0, "pagesize": 10000,
        "sort": sort, "date": trade_date,
    }
    try:
        d = em_get_json(url, params=params)
        return (d.get("data") or {}).get("pool") or []
    except FetchError as exc:
        log.warning("涨停板池 %s 请求失败: %s", endpoint, exc)
        return []


def limit_up_pool(trade_date: str = None) -> list[dict]:
    """涨停池。返回每只: code/name/price/pct/amount/float_cap/turnover/
    limit_days(连板数)/first_seal/last_seal/seal_fund(封板资金,元)/
    break_times(炸板次数)/industry/zt_stat(N天M板)

    这是情绪计算的核心数据源：精确连板数、封板资金、行业归属。
    """
    trade_date = trade_date or _today_str()
    cached = _cache_get(f"zt_pool_{trade_date}", max_age_h=0.5)
    if cached:
        return cached

    out = []
    for p in _em_zt_api("getTopicZTPool", "fbt:asc", trade_date):
        out.append({
            "code": p.get("c", ""), "name": p.get("n", ""),
            "price": (p.get("p") or 0) / 1000,
            "pct": round(p.get("zdp") or 0, 2),
            "amount": p.get("amount"), "float_cap": p.get("ltsz"),
            "turnover": round(p.get("hs") or 0, 2),
            "limit_days": p.get("lbc") or 1,
            "first_seal": _fmt_zt_time(p.get("fbt", 0)),
            "last_seal": _fmt_zt_time(p.get("lbt", 0)),
            "seal_fund": p.get("fund") or 0,
            "break_times": p.get("zbc") or 0,
            "industry": p.get("hybk", ""),
            "zt_stat": f'{(p.get("zttj") or {}).get("days", "?")}天{(p.get("zttj") or {}).get("ct", "?")}板',
        })
    if out:
        _cache_set(f"zt_pool_{trade_date}", out)
    return out


def broken_board_pool(trade_date: str = None) -> list[dict]:
    """炸板池（触及涨停后开板）。用于计算炸板率。"""
    trade_date = trade_date or _today_str()
    cached = _cache_get(f"zb_pool_{trade_date}", max_age_h=0.5)
    if cached:
        return cached

    out = []
    for p in _em_zt_api("getTopicZBPool", "fbt:asc", trade_date):
        out.append({
            "code": p.get("c", ""), "name": p.get("n", ""),
            "price": (p.get("p") or 0) / 1000,
            "limit_price": (p.get("ztp") or 0) / 1000,
            "pct": round(p.get("zdp") or 0, 2),
            "turnover": round(p.get("hs") or 0, 2),
            "first_seal": _fmt_zt_time(p.get("fbt", 0)),
            "break_times": p.get("zbc") or 0,
            "amplitude": round(p.get("zf") or 0, 2),
            "industry": p.get("hybk", ""),
        })
    if out:
        _cache_set(f"zb_pool_{trade_date}", out)
    return out


def limit_down_pool(trade_date: str = None) -> list[dict]:
    """跌停池。"""
    trade_date = trade_date or _today_str()
    cached = _cache_get(f"dt_pool_{trade_date}", max_age_h=0.5)
    if cached:
        return cached

    out = []
    for p in _em_zt_api("getTopicDTPool", "fund:asc", trade_date):
        out.append({
            "code": p.get("c", ""), "name": p.get("n", ""),
            "price": (p.get("p") or 0) / 1000,
            "pct": round(p.get("zdp") or 0, 2),
            "turnover": round(p.get("hs") or 0, 2),
            "seal_fund": p.get("fund") or 0,
            "dt_days": p.get("days") or 1,
            "open_times": p.get("oc") or 0,
            "industry": p.get("hybk", ""),
        })
    if out:
        _cache_set(f"dt_pool_{trade_date}", out)
    return out


def yesterday_limit_up_pool(trade_date: str = None) -> list[dict]:
    """昨日涨停池（含今日表现）。用于计算晋级率和赚钱效应。

    返回每只: code/name/price/pct(今日涨幅)/turnover/amplitude/
    y_first_seal(昨封板时间)/y_limit_days(昨连板)/industry
    """
    trade_date = trade_date or _today_str()
    cached = _cache_get(f"yzt_pool_{trade_date}", max_age_h=0.5)
    if cached:
        return cached

    out = []
    for p in _em_zt_api("getYesterdayZTPool", "zs:desc", trade_date):
        out.append({
            "code": p.get("c", ""), "name": p.get("n", ""),
            "price": (p.get("p") or 0) / 1000,
            "pct": round(p.get("zdp") or 0, 2),
            "turnover": round(p.get("hs") or 0, 2),
            "amplitude": round(p.get("zf") or 0, 2),
            "speed": round(p.get("zs") or 0, 2),
            "y_first_seal": _fmt_zt_time(p.get("yfbt", 0)),
            "y_limit_days": p.get("ylbc") or 1,
            "industry": p.get("hybk", ""),
        })
    if out:
        _cache_set(f"yzt_pool_{trade_date}", out)
    return out


# ═══════════════════════════════════════════════════════════════════════
# §8.3 打板情绪速算 — 炸板率 / 连板高度 / 连板梯队
# ═══════════════════════════════════════════════════════════════════════

def limit_up_sentiment(trade_date: str = None) -> dict:
    """打板情绪温度计：连板梯队 + 炸板率 + 涨跌停对比 + 晋级率。

    返回：{
      "date": str,
      "zt_count": 涨停家数,
      "zb_count": 炸板家数,
      "dt_count": 跌停家数,
      "break_rate": 炸板率(%),
      "max_height": 最高连板数,
      "ladder": {板数: 家数} 连板梯队,
      "promotion_rate": 晋级率(昨涨停今仍涨停/昨涨停总数),
      "yesterday_perf": 昨涨停今日平均涨幅,
      "top_boards": [最高连板的几只股票],
    }
    """
    trade_date = trade_date or _today_str()
    cached = _cache_get(f"zt_sentiment_{trade_date}", max_age_h=0.3)
    if cached:
        return cached

    zt = limit_up_pool(trade_date)
    zb = broken_board_pool(trade_date)
    dt = limit_down_pool(trade_date)
    yzt = yesterday_limit_up_pool(trade_date)

    # 连板梯队
    ladder: dict[int, int] = {}
    for s in zt:
        d = s.get("limit_days", 1)
        ladder[d] = ladder.get(d, 0) + 1

    zt_n, zb_n = len(zt), len(zb)
    break_rate = round(zb_n / (zt_n + zb_n) * 100, 1) if (zt_n + zb_n) else 0

    # 最高连板 + 梯队龙头
    max_height = max((s["limit_days"] for s in zt), default=0)
    top_boards = sorted([s for s in zt if s["limit_days"] >= max(2, max_height - 1)],
                        key=lambda x: -x["limit_days"])[:5]

    # 晋级率：昨涨停今日仍涨停的比例
    promotion_rate = 0.0
    yesterday_perf = 0.0
    if yzt:
        today_zt_codes = {s["code"] for s in zt}
        promoted = sum(1 for s in yzt if s["code"] in today_zt_codes)
        promotion_rate = round(promoted / len(yzt), 3)
        pcts = [s["pct"] for s in yzt if s.get("pct") is not None]
        yesterday_perf = round(sum(pcts) / len(pcts), 2) if pcts else 0.0

    result = {
        "date": trade_date,
        "zt_count": zt_n,
        "zb_count": zb_n,
        "dt_count": len(dt),
        "break_rate": break_rate,
        "max_height": max_height,
        "ladder": dict(sorted(ladder.items())),
        "promotion_rate": promotion_rate,
        "yesterday_perf": yesterday_perf,
        "top_boards": [{"code": s["code"], "name": s["name"],
                        "height": s["limit_days"], "industry": s["industry"]}
                       for s in top_boards],
    }
    if zt_n > 0:
        _cache_set(f"zt_sentiment_{trade_date}", result)
    return result


# ═══════════════════════════════════════════════════════════════════════
# §3.2 北向资金 — 同花顺 hsgtApi（不封IP）+ 东财 kamt 备源
# ═══════════════════════════════════════════════════════════════════════

def north_flow() -> dict | None:
    """北向资金当日净流入（亿元）。

    主源：同花顺 data.hexin.cn hsgtApi（分钟级，不封IP）
    备源：东财 push2 kamt（字段经常返 None，2024-08后收紧披露）

    返回：{"hgt": 沪股通, "sgt": 深股通, "total": 合计, "asof": 时间}
    """
    cached = _cache_get("north_flow", max_age_h=0.1)
    if cached:
        return cached

    # 主源：同花顺
    result = _north_flow_ths()
    if result:
        _cache_set("north_flow", result)
        return result

    # 备源：东财 kamt
    result = _north_flow_em()
    if result:
        _cache_set("north_flow", result)
        return result

    return None


def _north_flow_ths() -> dict | None:
    """同花顺北向分钟流向（取最后一个有效数据点作为当日累计）。"""
    try:
        url = "https://data.hexin.cn/market/hsgtApi/method/dayChart/"
        d = get_json(url, headers=HSGT_HEADERS)
        times = d.get("time") or []
        hgt = d.get("hgt") or []
        sgt = d.get("sgt") or []

        if not times or not hgt:
            return None

        # 取最后一个非 None 值（累计净流入，单位亿元）
        last_hgt = None
        last_sgt = None
        last_time = ""
        for i in range(len(times) - 1, -1, -1):
            if last_hgt is None and i < len(hgt) and hgt[i] is not None:
                last_hgt = float(hgt[i])
            if last_sgt is None and i < len(sgt) and sgt[i] is not None:
                last_sgt = float(sgt[i])
            if last_hgt is not None:
                last_time = times[i] if i < len(times) else ""
                break

        if last_hgt is None:
            return None

        return {
            "hgt": round(last_hgt, 2),
            "sgt": round(last_sgt, 2) if last_sgt is not None else None,
            "total": round(last_hgt + (last_sgt or 0), 2),
            "asof": f"{date.today().isoformat()} {last_time}",
            "source": "ths",
        }
    except (FetchError, KeyError, TypeError, ValueError) as exc:
        log.debug("同花顺北向失败: %s", exc)
        return None


def _north_flow_em() -> dict | None:
    """东财 kamt 北向资金（备源）。"""
    try:
        url = "https://push2.eastmoney.com/api/qt/kamt/get"
        params = {"fields1": "f1,f2,f3,f4", "fields2": "f51,f52,f53,f54,f55,f56"}
        d = em_get_json(url, params=params)
        data = d.get("data") or {}

        hgt = _num(data.get("hgt"))
        sgt = _num(data.get("sgt"))
        if hgt is None and sgt is None:
            return None

        return {
            "hgt": round((hgt or 0) / 10000, 2),
            "sgt": round((sgt or 0) / 10000, 2),
            "total": round(((hgt or 0) + (sgt or 0)) / 10000, 2),
            "asof": time.strftime("%Y-%m-%d %H:%M"),
            "source": "em",
        }
    except (FetchError, KeyError, TypeError) as exc:
        log.debug("东财北向失败: %s", exc)
        return None


def north_flow_history(days: int = 5) -> list[dict] | None:
    """北向资金近 N 日历史。优先读本地缓存（逐日积累），不足时从东财 kamt.kline 补充。"""
    cached = _cache_get("north_history", max_age_h=12.0)
    if cached and len(cached) >= days:
        return cached[:days]

    try:
        url = "https://push2his.eastmoney.com/api/qt/kamt.kline/get"
        params = {"fields1": "f1,f3", "fields2": "f51,f52", "klt": "101", "lmt": str(days)}
        d = em_get_json(url, params=params)
        klines = ((d.get("data") or {}).get("hk2sh") or [])

        result = []
        for line in klines[-days:]:
            parts = line.split(",")
            if len(parts) >= 2:
                result.append({"date": parts[0], "net_flow": _num(parts[1]) or 0})
        if result:
            _cache_set("north_history", result)
        return result
    except (FetchError, KeyError, TypeError) as exc:
        log.warning("北向历史获取失败: %s", exc)
        return None


# ═══════════════════════════════════════════════════════════════════════
# §3.8 板块资金流向（行业/概念/地域 × 今日/5日/10日）
# ═══════════════════════════════════════════════════════════════════════

# 东财 clist 板块类型映射
_BOARD_TYPES = {"industry": "m:90+t:2", "concept": "m:90+t:3", "region": "m:90+t:1"}
# 周期 → 资金流字段前缀
_PERIOD_FIELDS = {
    "today": {"net": "f62", "pct": "f184", "xl": "f66", "da": "f72", "zh": "f78", "xiao": "f84"},
    "5d": {"net": "f164", "pct": "f165", "xl": "f166", "da": "f167", "zh": "f168", "xiao": "f169"},
    "10d": {"net": "f174", "pct": "f175", "xl": "f176", "da": "f177", "zh": "f178", "xiao": "f179"},
}


def sector_fund_flow(board_type: str = "industry", period: str = "today",
                     top_n: int = 10, max_age_h: float = 0.5) -> list[dict] | None:
    """板块资金流向。

    board_type: "industry"(行业) / "concept"(概念) / "region"(地域)
    period: "today" / "5d" / "10d"
    top_n: 返回前 N 个（按主力净流入降序）
    max_age_h: 缓存有效期（小时）。默认 0.5；盘中实时脉搏页传更小值（如 1/60）。

    返回：[{"name": 板块名, "pct_chg": 涨跌幅, "net_flow": 主力净流入(元),
            "net_pct": 净占比(%), "lead_stock": 领涨股}, ...]
    """
    if board_type not in _BOARD_TYPES:
        return None
    if period not in _PERIOD_FIELDS:
        return None

    cached = _cache_get(f"sector_flow_{board_type}_{period}", max_age_h=max_age_h)
    if cached:
        return cached[:top_n]

    fields = _PERIOD_FIELDS[period]
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1", "pz": "500", "po": "1",  # po=1 降序
            "np": "1", "fltt": "2", "invt": "2",
            "fid": fields["net"],  # 按主力净流入排序
            "fs": _BOARD_TYPES[board_type],
            "fields": f"f12,f14,f3,{fields['net']},{fields['pct']},{fields['xl']},{fields['da']},{fields['zh']},{fields['xiao']},f128,f136",
        }
        d = em_get_json(url, params=params)
        items = ((d.get("data") or {}).get("diff") or [])

        result = []
        for it in items[:top_n]:
            result.append({
                "code": it.get("f12", ""),
                "name": it.get("f14", ""),
                "pct_chg": _num(it.get("f3")),
                "net_flow": _num(it.get(fields["net"])),
                "net_pct": _num(it.get(fields["pct"])),
                "super_large": _num(it.get(fields["xl"])),
                "large": _num(it.get(fields["da"])),
                "medium": _num(it.get(fields["zh"])),
                "small": _num(it.get(fields["xiao"])),
                "lead_stock": it.get("f128", ""),
                "lead_pct": _num(it.get("f136")),
            })
        if result:
            _cache_set(f"sector_flow_{board_type}_{period}", result)
        return result
    except (FetchError, KeyError, TypeError) as exc:
        log.warning("板块资金流获取失败: %s", exc)
        return None


# ═══════════════════════════════════════════════════════════════════════
# §3.4b 个股主力资金流排名（东财 push2 clist，fid=f62）
# ═══════════════════════════════════════════════════════════════════════

# 沪深A股（沪主板/深主板/创业板/科创板）
_STOCK_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"


def stock_fund_flow(top_n: int = 20, direction: str = "in",
                    max_age_h: float = 1 / 60) -> list[dict] | None:
    """个股主力净流入/净流出排名（当日累计）。

    direction: "in"=主力净流入降序 / "out"=主力净流出升序（即流出榜）
    max_age_h: 缓存有效期（小时），默认 1 分钟。

    返回：[{"code","name","price","pct_chg","net_flow"(主力净流入元),
            "super_large"(超大单净), "large"(大单净), "medium"(中单净),
            "small"(小单净), "net_ratio"(主力净占比%)}...]；失败返回 None。
    """
    cached = _cache_get(f"stock_flow_{direction}", max_age_h=max_age_h)
    if cached:
        return cached[:top_n]
    try:
        url = "https://push2.eastmoney.com/api/qt/clist/get"
        params = {
            "pn": "1", "pz": str(max(top_n, 1)),
            "po": "1" if direction == "in" else "0",
            "np": "1", "fltt": "2", "invt": "2",
            "fid": "f62",           # 主力净流入
            "fs": _STOCK_FS,
            "fields": "f12,f14,f2,f3,f62,f66,f72,f78,f84,f184",
        }
        d = em_get_json(url, params=params)
        items = ((d.get("data") or {}).get("diff") or [])
        result = []
        for it in items:
            result.append({
                "code": str(it.get("f12", "")).zfill(6),
                "name": it.get("f14", ""),
                "price": _num(it.get("f2")),
                "pct_chg": _num(it.get("f3")),
                "net_flow": _num(it.get("f62")),
                "super_large": _num(it.get("f66")),
                "large": _num(it.get("f72")),
                "medium": _num(it.get("f78")),
                "small": _num(it.get("f84")),
                "net_ratio": _num(it.get("f184")),
            })
        if result:
            _cache_set(f"stock_flow_{direction}", result)
        return result
    except (FetchError, KeyError, TypeError) as exc:
        log.warning("个股资金流获取失败: %s", exc)
        return None


# ═══════════════════════════════════════════════════════════════════════
# §3.5 龙虎榜（东财 datacenter）
# ═══════════════════════════════════════════════════════════════════════

def dragon_tiger(code: str = None, days: int = 30) -> list[dict]:
    """龙虎榜记录。code=None 时取近5日全市场。

    返回：[{"date", "code", "name", "reason", "close", "pct",
            "buy_total", "sell_total", "net_buy", "turnover"}, ...]
    """
    cached = _cache_get(f"dragon_tiger_{code or 'all'}", max_age_h=6.0)
    if cached:
        return cached

    try:
        filter_str = ""
        if code:
            code = code.zfill(6)
            filter_str = f'(SECURITY_CODE="{code}")'

        params = {
            "reportName": "RPT_DAILYBILLBOARD_DETAILSNEW",
            "columns": "ALL",
            "filter": filter_str,
            "pageNumber": "1", "pageSize": "50",
            "sortColumns": "TRADE_DATE", "sortTypes": "-1",
            "source": "WEB", "client": "WEB",
        }
        d = em_get_json(DATACENTER_URL, params=params)
        rows = ((d.get("result") or {}).get("data") or [])

        result = []
        for r in rows:
            result.append({
                "date": (r.get("TRADE_DATE") or "")[:10],
                "code": r.get("SECURITY_CODE", ""),
                "name": r.get("SECURITY_NAME_ABBR", ""),
                "reason": r.get("EXPLANATION", ""),
                "close": _num(r.get("CLOSE_PRICE")),
                "pct": _num(r.get("CHANGE_RATE")),
                "buy_total": _num(r.get("BILLBOARD_BUY_AMT")),
                "sell_total": _num(r.get("BILLBOARD_SELL_AMT")),
                "net_buy": _num(r.get("BILLBOARD_NET_AMT")),
                "turnover": _num(r.get("TURNOVERRATE")),
            })
        if result:
            _cache_set(f"dragon_tiger_{code or 'all'}", result)
        return result
    except (FetchError, KeyError, TypeError) as exc:
        log.warning("龙虎榜获取失败: %s", exc)
        return []


# ═══════════════════════════════════════════════════════════════════════
# §14 事件驱动（东财 datacenter）
# ═══════════════════════════════════════════════════════════════════════

def _datacenter_query(report_name: str, filter_str: str = "",
                      sort_columns: str = "", sort_types: str = "-1",
                      page_size: int = 100) -> list[dict]:
    """东财数据中心统一查询。"""
    params = {
        "reportName": report_name, "columns": "ALL",
        "filter": filter_str, "pageNumber": "1",
        "pageSize": str(page_size),
        "sortColumns": sort_columns, "sortTypes": sort_types,
        "source": "WEB", "client": "WEB",
    }
    d = em_get_json(DATACENTER_URL, params=params)
    return ((d.get("result") or {}).get("data") or [])


def earnings_forecast(code: str = None, limit: int = 50) -> list[dict]:
    """业绩预告。code=None 时取最新全市场预告。

    返回：[{"code", "name", "notice_date", "report_date", "type"(预增/预减/扭亏/续亏...),
            "net_profit_lower", "net_profit_upper", "change_pct_lower", "change_pct_upper"}, ...]
    """
    cached = _cache_get(f"earnings_{code or 'all'}", max_age_h=4.0)
    if cached:
        return cached[:limit]

    try:
        filter_str = f'(SECURITY_CODE="{code.zfill(6)}")' if code else ""
        rows = _datacenter_query(
            "RPT_PUBLIC_OP_NEWPREDICT",
            filter_str=filter_str,
            sort_columns="NOTICE_DATE", sort_types="-1",
            page_size=min(limit, 500),
        )
        result = []
        for r in rows[:limit]:
            result.append({
                "code": r.get("SECURITY_CODE", ""),
                "name": r.get("SECURITY_NAME_ABBR", ""),
                "notice_date": (r.get("NOTICE_DATE") or "")[:10],
                "report_date": (r.get("REPORT_DATE") or "")[:10],
                "indicator": r.get("PREDICT_FINANCE", ""),  # 预告指标（归母净利润/扣非/营收）
                "type": r.get("PREDICT_TYPE", ""),          # 预增/预减/扭亏/续亏
                "net_profit_lower": _num(r.get("PREDICT_AMT_LOWER")),
                "net_profit_upper": _num(r.get("PREDICT_AMT_UPPER")),
                "change_pct_lower": _num(r.get("ADD_AMP_LOWER")),
                "change_pct_upper": _num(r.get("ADD_AMP_UPPER")),
                "prior_year": _num(r.get("PREYEAR_SAME_PERIOD")),
                "reason": r.get("CHANGE_REASON_EXPLAIN", ""),
            })
        if result:
            _cache_set(f"earnings_{code or 'all'}", result)
        return result
    except (FetchError, KeyError, TypeError) as exc:
        log.warning("业绩预告获取失败: %s", exc)
        return []


def holder_trades(code: str = None, direction: str = None, limit: int = 50) -> list[dict]:
    """股东增减持。direction: "增持"/"减持"/None(全部)

    返回：[{"code", "name", "holder", "direction", "change_shares"(万股,带符号),
            "change_pct", "price", "notice_date"}, ...]
    """
    cached = _cache_get(f"holder_{code or 'all'}_{direction or 'all'}", max_age_h=6.0)
    if cached:
        return cached[:limit]

    try:
        parts = []
        if code:
            parts.append(f'(SECURITY_CODE="{code.zfill(6)}")')
        if direction:
            parts.append(f'(DIRECTION="{direction}")')
        filter_str = "".join(parts)

        rows = _datacenter_query(
            "RPT_SHARE_HOLDER_INCREASE",
            filter_str=filter_str,
            sort_columns="NOTICE_DATE", sort_types="-1",
            page_size=min(limit, 500),
        )
        result = []
        for r in rows[:limit]:
            result.append({
                "code": r.get("SECURITY_CODE", ""),
                "name": r.get("SECURITY_NAME_ABBR", ""),
                "holder": r.get("HOLDER_NAME", ""),
                "direction": r.get("DIRECTION", ""),
                "change_shares": _num(r.get("CHANGE_NUM_SYMBOL")),  # 万股，带符号
                "change_pct": _num(r.get("CHANGE_RATE")),
                "price": _num(r.get("TRADE_AVERAGE_PRICE") or r.get("CLOSE_PRICE")),
                "notice_date": (r.get("NOTICE_DATE") or "")[:10],
                "end_date": (r.get("END_DATE") or "")[:10],
            })
        if result:
            _cache_set(f"holder_{code or 'all'}_{direction or 'all'}", result)
        return result
    except (FetchError, KeyError, TypeError) as exc:
        log.warning("股东增减持获取失败: %s", exc)
        return []


def share_buyback(code: str = None, limit: int = 30) -> list[dict]:
    """股票回购（报表 RPTA_WEB_GETHGLIST_NEW）。

    返回：[{"code", "name", "progress_code", "amount_lower"(元), "amount_upper"(元),
            "shares_lower", "shares_upper", "notice_date"}, ...]
    """
    cached = _cache_get(f"buyback_{code or 'all'}", max_age_h=12.0)
    if cached:
        return cached[:limit]

    try:
        filter_str = f'(DIM_SCODE="{code.zfill(6)}")' if code else ""
        rows = _datacenter_query(
            "RPTA_WEB_GETHGLIST_NEW",
            filter_str=filter_str,
            sort_columns="UPD,DIM_SCODE,REPURCODE", sort_types="-1,1,1",
            page_size=min(limit, 200),
        )
        result = []
        for r in rows[:limit]:
            result.append({
                "code": r.get("DIM_SCODE", ""),
                "name": r.get("SECURITYSHORTNAME", ""),
                "progress_code": r.get("REPURPROGRESS"),
                "amount_lower": _num(r.get("REPURAMOUNTLOWER")),
                "amount_upper": _num(r.get("REPURAMOUNTLIMIT")),
                "shares_lower": _num(r.get("REPURNUMLOWER")),
                "shares_upper": _num(r.get("REPURNUMCAP")),
                "price_cap": _num(r.get("REPURPRICECAP")),
                "notice_date": (r.get("UPD") or "")[:10],
            })
        if result:
            _cache_set(f"buyback_{code or 'all'}", result)
        return result
    except (FetchError, KeyError, TypeError) as exc:
        log.warning("回购获取失败: %s", exc)
        return []


def institution_survey(code: str = None, limit: int = 30) -> list[dict]:
    """机构调研。

    返回：[{"code", "name", "survey_date", "org_count", "survey_type"}, ...]
    """
    cached = _cache_get(f"survey_{code or 'all'}", max_age_h=12.0)
    if cached:
        return cached[:limit]

    try:
        filter_str = f'(SECURITY_CODE="{code.zfill(6)}")' if code else ""
        rows = _datacenter_query(
            "RPT_ORG_SURVEYNEW",
            filter_str=filter_str,
            sort_columns="NOTICE_DATE", sort_types="-1",
            page_size=min(limit, 200),
        )
        result = []
        for r in rows[:limit]:
            result.append({
                "code": r.get("SECURITY_CODE", ""),
                "name": r.get("SECURITY_NAME_ABBR", ""),
                "survey_date": (r.get("START_DATE") or "")[:10],
                "org_count": r.get("ORG_NUMS") or 0,
                "survey_type": r.get("SURVEY_TYPE", ""),
                "notice_date": (r.get("NOTICE_DATE") or "")[:10],
            })
        if result:
            _cache_set(f"survey_{code or 'all'}", result)
        return result
    except (FetchError, KeyError, TypeError) as exc:
        log.warning("机构调研获取失败: %s", exc)
        return []


# ═══════════════════════════════════════════════════════════════════════
# 涨停统计（兼容旧接口，内部改为从涨停池获取精确数据）
# ═══════════════════════════════════════════════════════════════════════

def calc_limit_up_stats(market_rows: list[dict] = None) -> dict:
    """涨停统计。优先从涨停板池获取精确数据，失败时退回快照推断。

    返回：{
      "limit_up_count", "limit_down_count", "touch_limit_up_count",
      "broken_board_count", "broken_board_rate", "max_board_height",
      "source": "push2ex" | "snapshot"
    }
    """
    # 优先用精确数据
    try:
        zt = limit_up_pool()
        zb = broken_board_pool()
        dt = limit_down_pool()
        if zt:
            max_h = max((s["limit_days"] for s in zt), default=0)
            return {
                "limit_up_count": len(zt),
                "limit_down_count": len(dt),
                "touch_limit_up_count": len(zt) + len(zb),
                "broken_board_count": len(zb),
                "broken_board_rate": round(len(zb) / max(1, len(zt) + len(zb)), 3),
                "max_board_height": max_h,
                "source": "push2ex",
            }
    except Exception as exc:
        log.debug("涨停池不可用，退回快照推断: %s", exc)

    # 退回：从全市场快照推断（兼容旧逻辑）
    if not market_rows:
        return {}

    limit_up = limit_down = touch_limit_up = broken_board = 0
    for r in market_rows:
        pct = r.get("pct_chg")
        code = str(r.get("code", "")).zfill(6)
        if pct is None:
            continue
        if code.startswith(("300", "301", "688", "689")):
            limit = 20.0
        elif code.startswith(("8", "4", "9")):
            limit = 30.0
        else:
            limit = 10.0

        if pct >= limit * 0.98:
            limit_up += 1
            touch_limit_up += 1
        elif pct >= limit * 0.90:
            touch_limit_up += 1
            broken_board += 1
        elif pct <= -limit * 0.98:
            limit_down += 1

    return {
        "limit_up_count": limit_up,
        "limit_down_count": limit_down,
        "touch_limit_up_count": touch_limit_up,
        "broken_board_count": broken_board,
        "broken_board_rate": round(broken_board / max(1, touch_limit_up), 3),
        "max_board_height": 0,  # 快照无法得到连板数
        "source": "snapshot",
    }


def calc_sector_limit_up(market_rows: list[dict] = None, sector_map: dict = None) -> dict:
    """每个板块的涨停家数。优先从涨停池的 industry 字段统计。"""
    # 优先用涨停池精确数据
    try:
        zt = limit_up_pool()
        if zt:
            sector_count: dict[str, int] = {}
            for s in zt:
                ind = s.get("industry") or "未知"
                sector_count[ind] = sector_count.get(ind, 0) + 1
            return dict(sorted(sector_count.items(), key=lambda x: -x[1]))
    except Exception:
        pass

    # 退回快照推断
    if not market_rows:
        return {}
    sector_count = {}
    for r in market_rows:
        pct = r.get("pct_chg")
        if pct is None:
            continue
        code = str(r.get("code", "")).zfill(6)
        if code.startswith(("300", "301", "688", "689")):
            limit = 20.0
        elif code.startswith(("8", "4", "9")):
            limit = 30.0
        else:
            limit = 10.0
        if pct < limit * 0.98:
            continue
        industry = None
        if sector_map:
            industry = sector_map.get(code)
        if not industry:
            industry = r.get("industry") or r.get("sector") or "未知"
        sector_count[industry] = sector_count.get(industry, 0) + 1
    return dict(sorted(sector_count.items(), key=lambda x: -x[1]))


# ═══════════════════════════════════════════════════════════════════════
# 综合情绪数据采集（主入口，run_brief.py 调用）
# ═══════════════════════════════════════════════════════════════════════

def collect_sentiment_data(market_rows: list[dict] = None) -> dict:
    """综合采集情绪周期需要的所有数据。

    V2 重写：直接从涨停板池获取精确数据，不再依赖 market_rows 推断。
    market_rows 保留为降级参数（涨停池不可用时退回快照推断）。

    返回：{
      "north_flow": {...},
      "limit_up_stats": {...},
      "max_board_height": int,
      "board_ladder": [...],
      "yesterday_limit_up_perf": {...},
      "sector_limit_up": {...},
      "sector_fund_flow": [...],
      "break_rate": float,
      "promotion_rate": float,
      "collected_at": str,
    }
    """
    result = {"collected_at": time.strftime("%Y-%m-%d %H:%M:%S")}

    # 1. 打板情绪（一次请求拿到涨停/炸板/跌停/昨涨停四池）
    try:
        zt_sent = limit_up_sentiment()
        result["max_board_height"] = zt_sent.get("max_height", 0)
        result["board_ladder"] = zt_sent.get("top_boards", [])
        result["break_rate"] = zt_sent.get("break_rate", 0)
        result["promotion_rate"] = zt_sent.get("promotion_rate", 0)
        result["yesterday_limit_up_perf"] = {
            "avg_pct": zt_sent.get("yesterday_perf", 0),
            "count": zt_sent.get("zt_count", 0),
        }
        result["limit_up_stats"] = {
            "limit_up_count": zt_sent.get("zt_count", 0),
            "limit_down_count": zt_sent.get("dt_count", 0),
            "broken_board_count": zt_sent.get("zb_count", 0),
            "broken_board_rate": round(zt_sent.get("break_rate", 0) / 100, 3),
            "max_board_height": zt_sent.get("max_height", 0),
            "ladder": zt_sent.get("ladder", {}),
            "source": "push2ex",
        }
    except Exception as exc:
        log.warning("打板情绪采集失败，退回快照: %s", exc)
        result["limit_up_stats"] = calc_limit_up_stats(market_rows)
        result["max_board_height"] = result["limit_up_stats"].get("max_board_height", 0)
        result["board_ladder"] = []
        result["break_rate"] = 0
        result["promotion_rate"] = 0
        result["yesterday_limit_up_perf"] = {}

    # 2. 北向资金（同花顺主源）
    result["north_flow"] = north_flow() or {}

    # 3. 板块涨停分布
    result["sector_limit_up"] = calc_sector_limit_up(market_rows)

    # 4. 板块资金流向（行业今日 Top5）
    try:
        flows = sector_fund_flow("industry", "today", top_n=5)
        result["sector_fund_flow"] = flows or []
    except Exception:
        result["sector_fund_flow"] = []

    return result


# ═══════════════════════════════════════════════════════════════════════
# 涨停历史存档（供次日"昨涨停今表现"对比，兼容旧接口）
# ═══════════════════════════════════════════════════════════════════════

def save_yesterday_limit_ups(codes: list[str], day: str = None):
    """保存当日涨停股票代码，供次日使用。"""
    day = day or date.today().isoformat()
    try:
        f = EXTRAS_CACHE / "limit_ups_history.json"
        history = {}
        if f.exists():
            try:
                history = json.loads(f.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                history = {}
        history[day] = codes
        keys = sorted(history.keys())[-30:]
        history = {k: history[k] for k in keys}
        f.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        log.warning("涨停历史保存失败: %s", exc)


def load_yesterday_limit_ups(day: str = None) -> list[str]:
    """加载昨日涨停股票代码列表"""
    day = day or (date.today() - timedelta(days=1)).isoformat()
    try:
        f = EXTRAS_CACHE / "limit_ups_history.json"
        if not f.exists():
            return []
        history = json.loads(f.read_text(encoding="utf-8"))
        return history.get(day, [])
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("涨停历史加载失败: %s", exc)
        return []
