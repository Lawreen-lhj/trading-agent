#!/usr/bin/env python3
"""离线预览：用合成数据渲染一版简报，验证排版。

不依赖 akshare / pandas / yaml，只用标准库 + src/briefing.py。
用途：改样式时快速迭代，不用等真实数据；也是日报模板的回归测试。

    .venv/bin/python preview_demo.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src import briefing  # noqa: E402


def fake_spark(n: int = 30, drift: float = 1.0, seed: int = 7) -> str:
    vals, v = [], 100.0
    for i in range(n):
        v += drift * (0.6 if (i + seed) % 3 else -0.9)
        vals.append(v)
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    pts = [
        f"{i * (96 / (n - 1)):.1f},{28 - (x - lo) / span * 24 - 2:.1f}"
        for i, x in enumerate(vals)
    ]
    color = "#C0392B" if vals[-1] >= vals[0] else "#1E8449"
    return (
        '<svg width="96" height="28" viewBox="0 0 96 28" preserveAspectRatio="none">'
        f'<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" '
        'stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/></svg>'
    )


DEMO = {
    "date": "2026-10-27",
    "generated_at": "2026-10-27 21:40",
    "market": {
        "state": {"state": "分化", "tone": "neutral", "up_ratio": 0.482, "sh_pct": 0.31},
        "indices": [
            {"code": "000001", "name": "上证指数", "close": 3421.55, "pct_chg": 0.31, "amount": 4.12e11},
            {"code": "399001", "name": "深证成指", "close": 10882.4, "pct_chg": -0.18, "amount": 5.31e11},
            {"code": "399006", "name": "创业板指", "close": 2214.7, "pct_chg": -0.62, "amount": 2.18e11},
            {"code": "000688", "name": "科创50", "close": 1012.3, "pct_chg": 1.05, "amount": 6.2e10},
            {"code": "000300", "name": "沪深300", "close": 3987.6, "pct_chg": 0.12, "amount": 2.97e11},
        ],
        "breadth": {
            "up": 2415, "down": 2578, "flat": 118, "limit_up": 47, "limit_down": 12,
            "median_pct": -0.08, "total_amount": 9.43e11, "total_count": 5111,
            "top_amount": [
                {"code": "300750", "name": "宁德时代", "amount": 8.9e9, "pct_chg": 2.14},
                {"code": "600519", "name": "贵州茅台", "amount": 6.2e9, "pct_chg": -0.75},
                {"code": "002594", "name": "比亚迪", "amount": 5.7e9, "pct_chg": 1.32},
                {"code": "601899", "name": "紫金矿业", "amount": 4.8e9, "pct_chg": 3.41},
                {"code": "688981", "name": "中芯国际", "amount": 4.1e9, "pct_chg": 4.62},
            ],
        },
        "sectors": {
            "top": [
                {"name": "半导体", "pct_chg": 3.42}, {"name": "有色金属", "pct_chg": 2.88},
                {"name": "电池", "pct_chg": 2.15}, {"name": "通信设备", "pct_chg": 1.74},
                {"name": "电力", "pct_chg": 1.21},
            ],
            "bottom": [
                {"name": "房地产", "pct_chg": -2.31}, {"name": "银行", "pct_chg": -1.62},
                {"name": "保险", "pct_chg": -1.18}, {"name": "白酒", "pct_chg": -0.94},
                {"name": "煤炭", "pct_chg": -0.71},
            ],
        },
    },
    "global": {
        "us": {"ok": True, "data": [
            {"name": "纳斯达克", "close": 18234.5, "pct_chg": 0.86, "date": "2026-10-26"},
            {"name": "标普500", "close": 5782.1, "pct_chg": 0.42, "date": "2026-10-26"},
            {"name": "道琼斯", "close": 42180.3, "pct_chg": -0.11, "date": "2026-10-26"},
        ]},
        "hk": {"ok": True, "data": [
            {"name": "恒生指数", "close": 20115.6, "pct_chg": 0.74, "date": "最新"},
            {"name": "恒生科技", "close": 4412.8, "pct_chg": 1.32, "date": "最新"},
        ]},
        "crypto": {"ok": True, "data": [
            {"name": "BTC", "close": 68240.0, "pct_chg": 1.85},
            {"name": "ETH", "close": 2518.4, "pct_chg": 2.31},
        ]},
    },
    "watch": [
        {"code": "688981", "name": "中芯国际", "tag": "半导体", "score": 82, "label": "强势",
         "snap": {"close": 98.42, "pct_chg": 4.62, "rsi14": 68.2},
         "spark": fake_spark(drift=1.2),
         "alerts": [{"level": "P1", "rule": "放量", "message": "成交量为 5 日均量的 2.3 倍"},
                    {"level": "P1", "rule": "创20日新高", "message": "收盘创 20 日新高"}]},
        {"code": "601899", "name": "紫金矿业", "tag": "有色", "score": 76, "label": "强势",
         "snap": {"close": 19.87, "pct_chg": 3.41, "rsi14": 64.1},
         "spark": fake_spark(drift=0.9, seed=2),
         "alerts": [{"level": "P1", "rule": "创20日新高", "message": "收盘创 20 日新高"}]},
        {"code": "300750", "name": "宁德时代", "tag": "新能源", "score": 68, "label": "偏强",
         "snap": {"close": 246.8, "pct_chg": 2.14, "rsi14": 58.7},
         "spark": fake_spark(drift=0.6, seed=5), "alerts": []},
        {"code": "002594", "name": "比亚迪", "tag": "新能源车", "score": 61, "label": "偏强",
         "snap": {"close": 268.4, "pct_chg": 1.32, "rsi14": 55.2},
         "spark": fake_spark(drift=0.4, seed=9), "alerts": []},
        {"code": "300059", "name": "东方财富", "tag": "券商", "score": 52, "label": "中性",
         "snap": {"close": 15.62, "pct_chg": -0.82, "rsi14": 48.3},
         "spark": fake_spark(drift=-0.2, seed=3),
         "alerts": [{"level": "P2", "rule": "RSI超卖", "message": "RSI14 = 48.3，短期超跌"}]},
        {"code": "600519", "name": "贵州茅台", "tag": "白酒", "score": 44, "label": "中性",
         "snap": {"close": 1482.0, "pct_chg": -0.75, "rsi14": 45.6},
         "spark": fake_spark(drift=-0.5, seed=4), "alerts": []},
        {"code": "600036", "name": "招商银行", "tag": "银行", "score": 38, "label": "偏弱",
         "snap": {"close": 36.44, "pct_chg": -1.62, "rsi14": 38.2},
         "spark": fake_spark(drift=-0.8, seed=6), "alerts": []},
        {"code": "601318", "name": "中国平安", "tag": "保险", "score": 31, "label": "偏弱",
         "snap": {"close": 48.12, "pct_chg": -2.31, "rsi14": 32.4},
         "spark": fake_spark(drift=-1.1, seed=8),
         "alerts": [{"level": "P1", "rule": "高位回撤", "message": "距 60 日最高回撤 16.2%"}]},
        {"code": "002475", "name": "立讯精密", "tag": "消费电子", "score": 27, "label": "走弱",
         "snap": {"close": 31.05, "pct_chg": -5.42, "rsi14": 28.9},
         "spark": fake_spark(drift=-1.4, seed=1),
         "alerts": [{"level": "P0", "rule": "涨跌幅超阈值", "message": "单日跌 5.42%，超过 5.0% 阈值"},
                    {"level": "P2", "rule": "RSI超卖", "message": "RSI14 = 28.9，短期超跌"}]},
    ],
    "holdings": [
        {"code": "300750", "name": "宁德时代", "qty": 200, "cost": 220.0,
         "price": 246.8, "pnl_pct": 12.18, "pnl_amt": 5360.0, "market_value": 49360.0},
        {"code": "600519", "name": "贵州茅台", "qty": 100, "cost": 1560.0,
         "price": 1482.0, "pnl_pct": -5.0, "pnl_amt": -7800.0, "market_value": 148200.0},
    ],
    "alerts_all": [],
    "data_health": ["美股指数取新浪源，收盘时间与 A 股不同步，已标注"],
}

CFG = {
    "meta": {
        "name": "A股每日简报",
        "source_primary": "东方财富（经 AKShare 接口）",
        "source_secondary": "新浪财经",
    }
}


def main() -> int:
    demo = dict(DEMO)
    demo["alerts_all"] = [
        {**a, "code": r["code"], "name": r["name"]}
        for r in demo["watch"] for a in r["alerts"]
    ]
    demo["summary"] = [
        "全市场 <b>2415</b> 家上涨、<b>2578</b> 家下跌，涨跌中位数 <b>-0.08%</b>，"
        "成交额 <b>9430 亿</b>，市场状态判定为 <b>分化</b>。",
        "资金主线在 <b>半导体(+3.4%)、有色金属(+2.9%)、电池(+2.2%)</b>；"
        "最弱的是 房地产(-2.3%)、银行(-1.6%)、保险(-1.2%)。",
        "自选池里 <b>中芯国际</b> 最强（82 分，强势），<b>立讯精密</b> 最弱（27 分，走弱）。",
        "<b>P0 提醒 1 条</b>：立讯精密（涨跌幅超阈值） —— 需要优先处理。",
        "隔夜外围：纳斯达克+0.86%、标普500+0.42%、道琼斯-0.11%。",
    ]
    html = briefing.render(demo, CFG)
    out = ROOT / "data" / "briefings" / "demo.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
