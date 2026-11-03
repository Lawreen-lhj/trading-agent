#!/usr/bin/env python3
"""交易复盘入口 —— 收盘后运行，生成四维复盘报告。

用法：
    .venv/bin/python run_review.py                    # 复盘今天
    .venv/bin/python run_review.py --date 2024-01-15  # 复盘指定日期
    .venv/bin/python run_review.py --week             # 生成本周汇总

复盘依赖的数据：
    data/briefings/YYYY-MM-DD.json   盘前简报（计划）
    data/market.db                   历史交易、信号、情绪
    data/simulation.db               模拟交易记录（如有）

输出：
    data/reviews/YYYY-MM-DD.json     结构化复盘数据
    data/reviews/YYYY-MM-DD.md       可读复盘报告
    data/market.db → reviews 表      落库
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src.db import Store
from src.review.reviewer import (
    DailyReview,
    ReviewCalculator,
    format_review_report,
    save_review,
)

log = logging.getLogger(__name__)

DB_PATH = ROOT / "data" / "market.db"
BRIEFINGS = ROOT / "data" / "briefings"
REVIEWS_DIR = ROOT / "data" / "reviews"


def load_config() -> dict:
    cfg_path = ROOT / "config.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_brief(day: str) -> dict | None:
    """加载盘前简报 JSON"""
    p = BRIEFINGS / f"{day}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _build_post_data(brief: dict, store: Store, day: str) -> dict:
    """从数据库和简报构建"收盘后数据"。

    复盘需要对比"盘前预判"和"实际收盘"。
    实际收盘数据来自：
    1. watch_rows 表中的 close/pct_chg（简报生成时已写入）
    2. sentiment_history 表中的情绪数据
    3. 模拟交易账户的当日交易记录
    """
    post = {
        "market": {},
        "stocks": {},
        "signals": {},
        "position": 0,
    }

    # 市场收盘数据：从简报中提取（简报是收盘后生成的，所以数据就是收盘数据）
    market = brief.get("market", {})
    temp = market.get("temp", {})
    post["market"]["temp"] = temp
    post["market"]["sectors"] = market.get("sectors", {})

    # 个股收盘数据
    for row in brief.get("watch", []):
        snap = row.get("snap", {})
        post["stocks"][row["code"]] = {
            "close": snap.get("close"),
            "pct_chg": snap.get("pct_chg", 0),
            "high": snap.get("high"),
            "low": snap.get("low"),
        }

    # 策略信号
    signals = store.signals_by_date(day)
    for s in signals:
        strategy = s.get("strategy", "unknown")
        post["signals"].setdefault(strategy, []).append(s)

    # 持仓比例（从模拟账户获取）
    sim_db = ROOT / "data" / "simulation.db"
    if sim_db.exists():
        try:
            from src.simulation.account import Account
            acct = Account(db_path=sim_db)
            post["position"] = acct.position_ratio
        except Exception as exc:
            log.warning("模拟账户加载失败: %s", exc)

    return post


def _load_trades(day: str) -> list[dict]:
    """加载当日模拟交易记录"""
    sim_db = ROOT / "data" / "simulation.db"
    if not sim_db.exists():
        return []
    try:
        from src.simulation.account import Account
        acct = Account(db_path=sim_db)
        return [t for t in acct.trade_history if t.get("trade_date") == day]
    except Exception:
        return []


def _load_holdings(brief: dict) -> list[dict]:
    """从简报中提取持仓数据"""
    return brief.get("holdings", [])


def _load_strategy_history(store: Store, strategy: str, days: int = 60) -> list[dict]:
    """加载策略的历史交易（用于计算累计统计）"""
    # 从 signals 表中提取历史信号，与交易记录交叉
    # 简化实现：返回空列表，实际应从 sim_trades 中按 strategy 筛选
    return []


def run_review(day: str = None, config: dict = None) -> DailyReview | None:
    """执行单日复盘"""
    day = day or date.today().isoformat()
    config = config or load_config()

    # 加载盘前简报
    brief = _load_brief(day)
    if not brief:
        log.error("未找到 %s 的简报数据，无法复盘", day)
        return None

    store = Store(DB_PATH)
    calculator = ReviewCalculator()

    try:
        # 构建收盘后数据
        post_data = _build_post_data(brief, store, day)

        # 加载交易和持仓
        trades = _load_trades(day)
        holdings = _load_holdings(brief)

        # 加载策略历史
        strategy_history = {}
        for name in ["board_hit", "trend_pullback", "oversold_bounce", "event_driven"]:
            strategy_history[name] = _load_strategy_history(store, name)

        # 生成复盘
        review = calculator.generate_daily_review(
            date_str=day,
            pre_brief=brief,
            post_data=post_data,
            trades=trades,
            holdings=holdings,
            strategy_history=strategy_history,
        )

        # 保存
        REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
        save_review(review, REVIEWS_DIR / day)

        # 落库
        store.save_review(asdict(review))

        log.info("复盘完成: %s, 综合评分 %.1f", day, review.overall_score)
        return review

    finally:
        store.close()


def run_weekly_review(end_day: str = None) -> str:
    """生成周复盘汇总"""
    end_day = end_day or date.today().isoformat()
    end_date = date.fromisoformat(end_day)

    # 回溯 5 个交易日
    days = []
    cur = end_date
    while len(days) < 5:
        if cur.weekday() < 5:
            days.append(cur.isoformat())
        cur -= timedelta(days=1)

    store = Store(DB_PATH)
    try:
        reviews = []
        for d in days:
            r = store.review_by_date(d)
            if r:
                reviews.append(r)

        if not reviews:
            return "本周无复盘数据。"

        # 汇总
        lines = [f"# 周复盘汇总 ({days[-1]} ~ {days[0]})", ""]

        avg_score = sum(r["overall_score"] for r in reviews) / len(reviews)
        lines.append(f"**平均综合评分**: {avg_score:.1f}/100")
        lines.append(f"**复盘天数**: {len(reviews)}")
        lines.append("")

        # 每日概览
        lines.append("## 每日概览")
        lines.append("| 日期 | 评分 | 关键洞察 |")
        lines.append("|------|------|----------|")
        for r in reviews:
            insight = r["key_insights"][0][:30] if r.get("key_insights") else "-"
            lines.append(f"| {r['date']} | {r['overall_score']:.0f} | {insight} |")
        lines.append("")

        # 策略表现汇总
        lines.append("## 策略表现")
        strategy_agg = {}
        for r in reviews:
            for s in r.get("strategies", []):
                name = s.get("strategy_name", "")
                if name not in strategy_agg:
                    strategy_agg[name] = {"trades": 0, "pnl": 0, "suggestions": []}
                strategy_agg[name]["trades"] += s.get("signals_taken", 0)
                strategy_agg[name]["pnl"] += s.get("total_pnl", 0)
                strategy_agg[name]["suggestions"].append(s.get("suggestion", ""))

        for name, data in strategy_agg.items():
            latest_suggestion = data["suggestions"][-1] if data["suggestions"] else "-"
            lines.append(f"- **{name}**: {data['trades']}笔交易, 盈亏{data['pnl']:.0f}元, 建议: {latest_suggestion}")
        lines.append("")

        # 行为改进追踪
        lines.append("## 行为改进")
        all_actions = set()
        for r in reviews:
            for a in r.get("action_items", []):
                all_actions.add(a)
        for a in sorted(all_actions):
            lines.append(f"- [ ] {a}")

        report = "\n".join(lines)

        # 保存
        REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
        out_path = REVIEWS_DIR / f"weekly_{days[0]}.md"
        out_path.write_text(report, encoding="utf-8")

        return report

    finally:
        store.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="交易复盘")
    ap.add_argument("--date", default=None, help="复盘日期 (YYYY-MM-DD)")
    ap.add_argument("--week", action="store_true", help="生成周汇总")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    if args.week:
        report = run_weekly_review(args.date)
        print(report)
    else:
        review = run_review(args.date)
        if review:
            print(format_review_report(review))
        else:
            print("复盘失败：缺少数据")
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
