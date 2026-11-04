"""当日资金脉搏采集器 —— 让"今天市场在干嘛"在界面上实时可见。

为什么单独一个模块：WebUI 行情脉搏页与 idle 策略候选池都需要同一份
"板块主力榜 / 个股主力榜 / 北向 / 涨停炸板计数"数据。集中采集 + 60s 短缓存，
既保证页面刷新拿到的是一分钟级新鲜度，又把上游请求压到 ≤1 次/分钟/源，
避开东财限流（见 操作文档 §7.1）。

设计要点：
- 按需采集（不新起常驻进程）：server /api/pulse 触发 collect()，命中 60s 缓存
  直接返回；缓存过期才真正抓上游。
- 部分失败降级：任一源失败不阻塞其余源，errors 里记录，页面如实标注。
- 每次成功采集落一行 pulse_snapshots，供日内趋势 sparkline（北向/涨停家数）。
- stock_flow_pool() 把个股主力净流入榜转成策略候选池（见 run_sim_live 扩池）。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from . import datasource, extras

log = logging.getLogger(__name__)

# 采集结果缓存有效期（秒）。WebUI 60s 轮询 → 上游最多 1 次/分钟/源。
CACHE_TTL = 60

_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "data": None}


def _now_ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def collect(store=None, force: bool = False) -> dict:
    """采集当日资金脉搏。返回结构化 dict（含 errors 与 ts）。

    store: 可选 db.Store，传入则把本次快照落 pulse_snapshots。
    force: 忽略缓存强制重抓。
    """
    with _lock:
        if not force and _cache["data"] and (time.time() - _cache["ts"]) < CACHE_TTL:
            return _cache["data"]

        result = {
            "ts": _now_ts(),
            "date": _today(),
            "sector_industry": [],
            "sector_concept": [],
            "stock_in": [],
            "stock_out": [],
            "north": None,
            "zt": None,
            "indices": [],
            "errors": [],
        }

        # 板块主力榜（行业 + 概念），60s 级新鲜度
        for key, bt in (("sector_industry", "industry"), ("sector_concept", "concept")):
            try:
                rows = extras.sector_fund_flow(bt, "today", top_n=10, max_age_h=1 / 60)
                if rows:
                    result[key] = rows
                else:
                    result["errors"].append(f"{bt} 板块资金流为空")
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"{bt} 板块资金流失败 {type(exc).__name__}")

        # 个股主力净流入 / 净流出双榜
        for key, direction in (("stock_in", "in"), ("stock_out", "out")):
            try:
                rows = extras.stock_fund_flow(top_n=15, direction=direction, max_age_h=1 / 60)
                if rows:
                    result[key] = rows
                else:
                    result["errors"].append(f"个股资金流({direction})为空")
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"个股资金流({direction})失败 {type(exc).__name__}")

        # 北向资金
        try:
            result["north"] = extras.north_flow()
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"北向失败 {type(exc).__name__}")

        # 涨停 / 炸板 / 跌停计数 + 最高连板
        try:
            result["zt"] = extras.limit_up_sentiment()
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"涨停池失败 {type(exc).__name__}")

        # A 股指数实时行情（仪表盘顶部指数条用，避免只显示最新简报的昨收）
        try:
            rows, src = datasource.index_quotes()
            if rows:
                result["indices"] = rows
            else:
                result["errors"].append(f"指数行情为空（源={src}）")
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"指数行情失败 {type(exc).__name__}")

        _cache["ts"] = time.time()
        _cache["data"] = result

        if store is not None:
            _persist(store, result)
        return result


def _persist(store, result: dict) -> None:
    """把本次采集落一行日内快照（供趋势 sparkline）。失败不影响返回。"""
    try:
        zt = result.get("zt") or {}
        sec = (result.get("sector_industry") or [{}])[0]
        north = result.get("north") or {}
        store.save_pulse_snapshot(
            result["date"], result["ts"],
            {
                "north_net": north.get("total"),
                "zt_count": zt.get("zt_count"),
                "broken_count": zt.get("zb_count"),
                "ld_count": zt.get("dt_count"),
                "top_sector": sec.get("name"),
                "top_sector_net": sec.get("net_flow"),
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("pulse 快照落库失败：%s", exc)


def stock_flow_pool(top_n: int = 15) -> list[dict]:
    """个股主力净流入榜 → 策略候选池。

    返回 [{"code","name","pct_chg","net_flow","net_ratio"}...]；源失败返回 []，
    调用方（run_sim_live 扩池）应回退到仅自选池。
    """
    try:
        rows = extras.stock_fund_flow(top_n=top_n, direction="in", max_age_h=1 / 60) or []
    except Exception as exc:  # noqa: BLE001
        log.warning("候选池资金流获取失败：%s", exc)
        return []
    return [
        {
            "code": r["code"], "name": r["name"], "pct_chg": r["pct_chg"],
            "net_flow": r["net_flow"], "net_ratio": r["net_ratio"],
        }
        for r in rows
    ]
