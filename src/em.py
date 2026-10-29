"""东方财富行情接口客户端（主源）。

为什么不用 akshare：它本身就是对下面这些 push2 接口的封装，却要拖进 pandas
等 ~40MB 依赖、冷启动 3 秒以上，且版本升级经常断。标准库实现三年后还能跑。

字段编码（东方财富内部编号）：
    f2 最新价  f3 涨跌幅  f4 涨跌额  f5 成交量(手)  f6 成交额(元)
    f8 换手率  f9 市盈率(动)  f10 量比  f12 代码  f13 市场  f14 名称
    f18 昨收   f20 总市值
"""
from __future__ import annotations

import time

from src.net import FetchError, get_json

UT = "bd1d9ddb04089700cf9c27f6f7426281"  # 东财前端固定 token，缺了会被拒
HOSTS = [
    "https://push2.eastmoney.com/api/qt",
    "https://82.push2.eastmoney.com/api/qt",
]
HOSTS_HIS = [
    "https://push2his.eastmoney.com/api/qt",
    "https://push2.eastmoney.com/api/qt",
]
REFERER = {"Referer": "https://quote.eastmoney.com/"}

FS_ALL_A = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"


def _get(path: str, his: bool = False) -> dict:
    """依次尝试多个镜像主机，全失败才抛错。"""
    hosts = HOSTS_HIS if his else HOSTS
    last = None
    for host in hosts:
        try:
            return get_json(host + path, headers=REFERER)
        except FetchError as exc:
            last = exc
    raise FetchError(str(last))


def secid(code: str) -> str:
    code = str(code).zfill(6)
    return f"1.{code}" if code[0] == "6" else f"0.{code}"


def _rows(data: dict) -> list[dict]:
    rows = data.get("diff") or []
    return list(rows.values()) if isinstance(rows, dict) else rows


def quotes(secids: list[str]) -> list[dict]:
    """批量报价，一次请求拿多个标的。"""
    fields = "f1,f2,f3,f4,f5,f6,f12,f13,f14,f18"
    d = _get(f"/ulist.np/get?fltt=2&secids={','.join(secids)}&fields={fields}")
    return [
        {
            "code": r.get("f12"),
            "market": r.get("f13"),
            "name": r.get("f14"),
            "close": _num(r.get("f2")),
            "pct_chg": _num(r.get("f3")),
            "change": _num(r.get("f4")),
            "volume": _num(r.get("f5")),
            "amount": _num(r.get("f6")),
            "prev_close": _num(r.get("f18")),
        }
        for r in _rows(d.get("data") or {})
    ]


def spot(codes: list[str]) -> list[dict]:
    """批量快照，含短线需要的换手率/量比/振幅/最高最低。

    单独开一个函数而不是改 quotes()：指数没有换手率和量比，
    混在一起会让调用方到处判空。
    """
    if not codes:
        raise FetchError("空代码列表")
    fields = "f2,f3,f4,f5,f6,f7,f8,f10,f12,f13,f14,f15,f16,f17,f18"
    secids = ",".join(secid(c) for c in codes)
    d = _get(f"/ulist.np/get?fltt=2&secids={secids}&fields={fields}")
    out = []
    for r in _rows(d.get("data") or {}):
        out.append(
            {
                "code": str(r.get("f12", "")).zfill(6),
                "name": r.get("f14"),
                "close": _num(r.get("f2")),
                "pct_chg": _num(r.get("f3")),
                "prev_close": _num(r.get("f18")),
                "open": _num(r.get("f17")),
                "high": _num(r.get("f15")),
                "low": _num(r.get("f16")),
                "volume": _num(r.get("f5")),
                "amount": _num(r.get("f6")),
                "amplitude": _num(r.get("f7")),
                "turnover": _num(r.get("f8")),
                "vol_ratio": _num(r.get("f10")),
            }
        )
    if not out:
        raise FetchError("快照解析为空")
    return out


def market_list(page_size: int = 100, max_pages: int = 80) -> list[dict]:
    """全市场 A 股快照（翻页收全）。

    坑：pz=6000 一次性请求时，镜像站会静默只回 100 行（= 涨幅榜首页），
    于是「全市场」变成 100 家全红、中位数 +9.96%，赚钱效应分虚高到 90——
    而指数其实在跌。必须按 data.total 翻页，并用返回条数反查是否收全。
    """
    fields = "f2,f3,f4,f5,f6,f8,f9,f10,f12,f13,f14,f18,f20"
    out: list[dict] = []
    seen: set[str] = set()
    total = 0
    for pn in range(1, max_pages + 1):
        try:
            d = _get(
                f"/clist/get?ut={UT}&pn={pn}&pz={page_size}&po=1&np=1&fltt=2&invt=2"
                f"&fid=f3&fs={FS_ALL_A}&fields={fields}"
            )
        except FetchError:
            if pn == 1:
                raise
            break  # 中途失败：保留已收到的部分，由调用方按样本量判断可信度
        data = d.get("data") or {}
        if not total:
            total = data.get("total") or 0
        rows = _rows(data)
        if not rows:
            break
        for r in rows:
            code = str(r.get("f12", "")).zfill(6)
            if code in seen:
                continue
            seen.add(code)
            out.append(
                {
                    "code": code,
                    "name": r.get("f14"),
                    "close": _num(r.get("f2")),
                    "pct_chg": _num(r.get("f3")),
                    "amount": _num(r.get("f6")),
                    "turnover": _num(r.get("f8")),
                    "pe": _num(r.get("f9")),
                    "vol_ratio": _num(r.get("f10")),
                    "market_cap": _num(r.get("f20")),
                }
            )
        if total and len(out) >= total:
            break
    if not out:
        raise FetchError("快照解析为空")
    return out


def sectors(kind: str = "industry") -> list[dict]:
    fs = "m:90+t:2" if kind == "industry" else "m:90+t:3"
    d = _get(
        f"/clist/get?ut={UT}&pn=1&pz=500&po=1&np=1&fltt=2&invt=2"
        f"&fid=f3&fs={fs}&fields=f3,f6,f12,f14"
    )
    return [
        {"code": r.get("f12"), "name": r.get("f14"),
         "pct_chg": _num(r.get("f3")), "amount": _num(r.get("f6"))}
        for r in _rows(d.get("data") or {})
    ]


def kline(code: str, days: int = 300, adjust: int = 1) -> list[dict]:
    """日线。adjust: 0 不复权 / 1 前复权 / 2 后复权。"""
    beg = time.strftime("%Y%m%d", time.localtime(time.time() - days * 86400))
    d = _get(
        f"/stock/kline/get?secid={secid(code)}&klt=101&fqt={adjust}"
        f"&beg={beg}&end=20500101"
        "&fields1=f1,f2,f3,f4,f5,f6"
        "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        his=True,
    )
    lines = (d.get("data") or {}).get("klines") or []
    out = []
    for line in lines:
        p = line.split(",")
        if len(p) < 11:
            continue
        out.append(
            {
                "date": p[0],
                "open": float(p[1]),
                "close": float(p[2]),
                "high": float(p[3]),
                "low": float(p[4]),
                "volume": float(p[5]),
                "amount": float(p[6]),
                "amplitude": float(p[7]),
                "pct_chg": float(p[8]),
                "change": float(p[9]),
                "turnover": float(p[10]),
            }
        )
    if not out:
        raise FetchError(f"{code} 日线为空")
    return out


def _num(v):
    if v is None or v in ("-", ""):
        return None
    try:
        return round(float(v), 4)
    except (TypeError, ValueError):
        return None
