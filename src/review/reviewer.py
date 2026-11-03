"""交易复盘系统 —— 四维复盘模型。

为什么需要复盘：
  交易不是"做了就完了"，而是一个"计划→执行→复盘→改进"的闭环。
  没有复盘的交易，同样的错误会反复犯。
  复盘的目的不是"自我批评"，而是"找到可改进的点"。

四维复盘模型：
  维度1: 市场复盘 —— 我判断对了市场情绪吗？
    - 盘前情绪预判 vs 实际收盘情绪
    - 仓位决策是否合理
    - 板块判断是否准确

  维度2: 个股复盘 —— 我选对了吗？
    - 评分高的票是否真的涨
    - 买入逻辑是否兑现
    - 卖点执行是否到位

  维度3: 策略复盘 —— 策略还有效吗？
    - 各策略胜率、盈亏比、期望值
    - 策略是否衰减
    - 参数是否需要调整

  维度4: 行为复盘 —— 我按计划执行了吗？
    - 计划执行率
    - 冲动交易次数
    - 止损执行率
    - 情绪化操作

复盘闭环：
  记录 → 分析 → 决策 → 执行 → 验证 → 记录 ...

  复盘的产出不是"报告"，而是"改进动作"：
  - 某策略连续负期望 → 暂停该策略
  - 评分与收益相关性低 → 调整因子权重
  - 止损执行率<100% → 强制自动止损
  - 冲动交易>0 → 增加下单确认环节
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from pathlib import Path
from typing import Optional


# ---------------- 数据结构 ----------------
@dataclass
class MarketReview:
    """维度1: 市场复盘"""
    date: str
    predicted_sentiment: float      # 盘前预判情绪分 (0-100)
    actual_sentiment: float         # 实际收盘情绪分
    predicted_phase: str            # 预判情绪阶段
    actual_phase: str               # 实际情绪阶段
    sentiment_accuracy: float       # 情绪判断准确率 (0-1)

    predicted_strong_sectors: list[str]   # 预判强势板块
    actual_strong_sectors: list[str]      # 实际强势板块
    sector_hit_rate: float                # 板块命中率

    position_decision: float        # 实际仓位 (0-1)
    position_suggestion: float      # 建议仓位
    position_reasonable: bool       # 仓位是否合理

    market_signals: list[str]       # 市场关键信号
    lessons: list[str]              # 经验教训


@dataclass
class StockReview:
    """维度2: 个股复盘"""
    date: str
    code: str
    name: str

    # 盘前评估
    pre_score: int                  # 盘前评分
    pre_label: str                  # 盘前标签
    pre_plan: dict                  # 盘前计划

    # 实际表现
    actual_return: float            # 当日涨幅
    actual_high: float              # 最高价
    actual_low: float               # 最低价
    actual_close: float             # 收盘价

    # 交易执行
    traded: bool                    # 是否实际交易
    trade_action: str               # "buy" / "sell" / "hold" / "none"
    entry_price: Optional[float]    # 买入价
    exit_price: Optional[float]     # 卖出价
    pnl: Optional[float]            # 盈亏
    pnl_pct: Optional[float]        # 盈亏比例

    # 评估
    score_vs_return: float          # 评分与收益的相关性贡献
    plan_executed: bool             # 是否按计划执行
    deviation_reason: str           # 偏离计划的原因

    lessons: list[str]              # 经验教训


@dataclass
class StrategyReview:
    """维度3: 策略复盘"""
    date: str
    strategy_name: str

    # 当日表现
    signals_generated: int          # 生成信号数
    signals_taken: int              # 实际执行数
    win_count: int                  # 盈利笔数
    loss_count: int                 # 亏损笔数
    total_pnl: float                # 总盈亏

    # 累计统计
    total_trades: int               # 累计交易笔数
    win_rate: float                 # 胜率
    avg_profit: float               # 平均盈利
    avg_loss: float                 # 平均亏损
    profit_loss_ratio: float        # 盈亏比
    expectancy: float               # 期望值 = 胜率*平均盈利 - 败率*平均亏损
    max_drawdown: float             # 最大回撤

    # 趋势分析
    recent_20d_expectancy: float    # 近20日期望值
    recent_60d_expectancy: float    # 近60日期望值
    is_decaying: bool               # 是否衰减

    # 建议
    suggestion: str                 # "continue" / "reduce" / "pause" / "optimize"
    reason: str


@dataclass
class BehaviorReview:
    """维度4: 行为复盘"""
    date: str

    # 计划执行
    plan_execution_rate: float      # 计划执行率 (0-1)
    planned_trades: int             # 计划交易数
    executed_trades: int            # 实际执行数
    skipped_trades: int             # 跳过交易数

    # 冲动交易
    impulse_trades: int             # 冲动交易次数（非计划内）
    impulse_pnl: float              # 冲动交易盈亏

    # 止损执行
    stop_loss_triggered: int        # 触发止损次数
    stop_loss_executed: int         # 执行止损次数
    stop_loss_compliance: float     # 止损执行率

    # 持仓纪律
    avg_hold_days: float            # 平均持仓天数
    over_hold_count: int            # 超时持仓次数
    max_hold_days_allowed: int      # 允许的最大持仓天数

    # 情绪化操作
    chase_high_count: int           # 追高次数
    panic_sell_count: int           # 恐慌卖出次数
    revenge_trade_count: int        # 报复性交易次数

    # 综合评分
    discipline_score: float         # 纪律性评分 (0-100)
    main_issues: list[str]          # 主要问题
    improvements: list[str]         # 改进建议


@dataclass
class DailyReview:
    """每日复盘汇总"""
    date: str
    market: MarketReview
    stocks: list[StockReview]
    strategies: list[StrategyReview]
    behavior: BehaviorReview

    # 综合评估
    overall_score: float            # 综合评分 (0-100)
    key_insights: list[str]         # 关键洞察
    action_items: list[str]         # 待办事项

    # 元数据
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())


# ---------------- 复盘计算器 ----------------
class ReviewCalculator:
    """复盘指标计算器"""

    def __init__(self, db_path: str | Path = None):
        self.db_path = db_path
        self.history: list[DailyReview] = []

    def calc_market_review(
        self,
        date_str: str,
        pre_brief: dict,
        post_data: dict,
    ) -> MarketReview:
        """计算市场复盘"""
        # 盘前预判
        pre_temp = pre_brief.get("market", {}).get("temp", {})
        pre_sentiment = pre_temp.get("sentiment_score", pre_temp.get("score", 50))
        pre_phase = pre_temp.get("sentiment_phase", pre_temp.get("tag", "中性"))

        # 实际收盘
        actual_temp = post_data.get("market", {}).get("temp", {})
        actual_sentiment = actual_temp.get("sentiment_score", actual_temp.get("score", 50))
        actual_phase = actual_temp.get("sentiment_phase", actual_temp.get("tag", "中性"))

        # 情绪判断准确率
        sentiment_diff = abs(pre_sentiment - actual_sentiment)
        sentiment_accuracy = max(0, 1 - sentiment_diff / 50)  # 50分误差为0准确率

        # 板块判断
        pre_sectors = [s["name"] for s in pre_brief.get("market", {}).get("sectors", {}).get("top", [])]
        actual_sectors = [s["name"] for s in post_data.get("market", {}).get("sectors", {}).get("top", [])]
        hits = len(set(pre_sectors) & set(actual_sectors))
        sector_hit_rate = hits / max(1, len(pre_sectors))

        # 仓位评估
        position = post_data.get("position", 0)
        suggested = pre_brief.get("position_suggestion", 0.5)
        position_reasonable = abs(position - suggested) < 0.2

        return MarketReview(
            date=date_str,
            predicted_sentiment=pre_sentiment,
            actual_sentiment=actual_sentiment,
            predicted_phase=pre_phase,
            actual_phase=actual_phase,
            sentiment_accuracy=round(sentiment_accuracy, 2),
            predicted_strong_sectors=pre_sectors,
            actual_strong_sectors=actual_sectors,
            sector_hit_rate=round(sector_hit_rate, 2),
            position_decision=position,
            position_suggestion=suggested,
            position_reasonable=position_reasonable,
            market_signals=post_data.get("signals", []),
            lessons=[],
        )

    def calc_stock_review(
        self,
        date_str: str,
        pre_watch: dict,
        actual_data: dict,
        trades: list[dict],
    ) -> StockReview:
        """计算个股复盘"""
        code = pre_watch.get("code", "")
        name = pre_watch.get("name", "")

        # 盘前评估
        pre_score = pre_watch.get("score", 0)
        pre_label = pre_watch.get("label", "")
        pre_plan = pre_watch.get("plan", {})

        # 实际表现
        actual_return = actual_data.get("pct_chg", 0) / 100
        actual_high = actual_data.get("high", 0)
        actual_low = actual_data.get("low", 0)
        actual_close = actual_data.get("close", 0)

        # 交易执行
        trade = next((t for t in trades if t.get("code") == code), None)
        traded = trade is not None
        trade_action = trade.get("action", "none") if trade else "none"
        entry_price = trade.get("price") if trade else None
        exit_price = trade.get("exit_price") if trade else None
        pnl = trade.get("pnl") if trade else None
        pnl_pct = trade.get("pnl_pct") if trade else None

        # 计划执行评估
        plan_executed = False
        deviation_reason = ""
        if pre_plan.get("ready") and traded:
            # 检查是否在计划区间内买入
            buy_lo = pre_plan.get("buy_lo", 0)
            buy_hi = pre_plan.get("buy_hi", 0)
            if entry_price and buy_lo <= entry_price <= buy_hi:
                plan_executed = True
            else:
                deviation_reason = f"买入价{entry_price}不在计划区间{buy_lo}-{buy_hi}"
        elif pre_plan.get("ready") and not traded:
            deviation_reason = "有计划但未执行"
        elif not pre_plan.get("ready") and traded:
            deviation_reason = "无计划但执行了交易（冲动交易）"

        return StockReview(
            date=date_str,
            code=code,
            name=name,
            pre_score=pre_score,
            pre_label=pre_label,
            pre_plan=pre_plan,
            actual_return=round(actual_return, 4),
            actual_high=actual_high,
            actual_low=actual_low,
            actual_close=actual_close,
            traded=traded,
            trade_action=trade_action,
            entry_price=entry_price,
            exit_price=exit_price,
            pnl=pnl,
            pnl_pct=pnl_pct,
            score_vs_return=pre_score * actual_return,  # 简化计算
            plan_executed=plan_executed,
            deviation_reason=deviation_reason,
            lessons=[],
        )

    def calc_strategy_review(
        self,
        date_str: str,
        strategy_name: str,
        signals: list[dict],
        trades: list[dict],
        history: list[dict] = None,
    ) -> StrategyReview:
        """计算策略复盘"""
        history = history or []

        # 当日统计
        strategy_trades = [t for t in trades if t.get("strategy") == strategy_name]
        signals_generated = len(signals)
        signals_taken = len(strategy_trades)

        win_trades = [t for t in strategy_trades if t.get("pnl", 0) > 0]
        loss_trades = [t for t in strategy_trades if t.get("pnl", 0) <= 0]
        win_count = len(win_trades)
        loss_count = len(loss_trades)
        total_pnl = sum(t.get("pnl", 0) for t in strategy_trades)

        # 累计统计（含历史）
        all_trades = history + strategy_trades
        total_trades = len(all_trades)
        all_wins = [t for t in all_trades if t.get("pnl", 0) > 0]
        all_losses = [t for t in all_trades if t.get("pnl", 0) <= 0]

        win_rate = len(all_wins) / total_trades if total_trades else 0
        avg_profit = sum(t.get("pnl_pct", 0) for t in all_wins) / len(all_wins) if all_wins else 0
        avg_loss = sum(t.get("pnl_pct", 0) for t in all_losses) / len(all_losses) if all_losses else 0
        profit_loss_ratio = abs(avg_profit / avg_loss) if avg_loss else 0

        # 期望值 = 胜率 * 平均盈利 - 败率 * 平均亏损
        expectancy = win_rate * avg_profit - (1 - win_rate) * abs(avg_loss)

        # 最大回撤（简化计算）
        cumulative = 0
        peak = 0
        max_dd = 0
        for t in all_trades:
            cumulative += t.get("pnl_pct", 0)
            peak = max(peak, cumulative)
            dd = peak - cumulative
            max_dd = max(max_dd, dd)

        # 趋势分析
        recent_20 = all_trades[-20:] if len(all_trades) >= 20 else all_trades
        recent_60 = all_trades[-60:] if len(all_trades) >= 60 else all_trades

        def calc_expectancy(trades_list):
            wins = [t for t in trades_list if t.get("pnl", 0) > 0]
            losses = [t for t in trades_list if t.get("pnl", 0) <= 0]
            wr = len(wins) / len(trades_list) if trades_list else 0
            ap = sum(t.get("pnl_pct", 0) for t in wins) / len(wins) if wins else 0
            al = sum(t.get("pnl_pct", 0) for t in losses) / len(losses) if losses else 0
            return wr * ap - (1 - wr) * abs(al)

        recent_20d_expectancy = calc_expectancy(recent_20)
        recent_60d_expectancy = calc_expectancy(recent_60)

        # 是否衰减
        is_decaying = recent_20d_expectancy < recent_60d_expectancy * 0.7

        # 建议
        if expectancy < 0:
            suggestion = "pause"
            reason = "期望值为负，策略失效"
        elif is_decaying:
            suggestion = "optimize"
            reason = "近期表现衰减，需要优化参数"
        elif win_rate < 0.4:
            suggestion = "reduce"
            reason = "胜率偏低，建议降低仓位"
        else:
            suggestion = "continue"
            reason = "策略表现正常"

        return StrategyReview(
            date=date_str,
            strategy_name=strategy_name,
            signals_generated=signals_generated,
            signals_taken=signals_taken,
            win_count=win_count,
            loss_count=loss_count,
            total_pnl=round(total_pnl, 2),
            total_trades=total_trades,
            win_rate=round(win_rate, 3),
            avg_profit=round(avg_profit, 4),
            avg_loss=round(avg_loss, 4),
            profit_loss_ratio=round(profit_loss_ratio, 2),
            expectancy=round(expectancy, 4),
            max_drawdown=round(max_dd, 4),
            recent_20d_expectancy=round(recent_20d_expectancy, 4),
            recent_60d_expectancy=round(recent_60d_expectancy, 4),
            is_decaying=is_decaying,
            suggestion=suggestion,
            reason=reason,
        )

    def calc_behavior_review(
        self,
        date_str: str,
        plans: list[dict],
        trades: list[dict],
        holdings: list[dict],
    ) -> BehaviorReview:
        """计算行为复盘"""
        # 计划执行
        planned = [p for p in plans if p.get("ready")]
        planned_codes = {p["code"] for p in planned}
        traded_codes = {t["code"] for t in trades if t.get("action") == "buy"}

        executed = planned_codes & traded_codes
        skipped = planned_codes - traded_codes
        impulse = traded_codes - planned_codes

        plan_execution_rate = len(executed) / len(planned_codes) if planned_codes else 1.0

        # 冲动交易
        impulse_trades = [t for t in trades if t.get("code") in impulse]
        impulse_pnl = sum(t.get("pnl", 0) for t in impulse_trades)

        # 止损执行
        stop_triggered = 0
        stop_executed = 0
        for h in holdings:
            if h.get("stop_broken"):
                stop_triggered += 1
                # 检查是否实际卖出
                if any(t.get("code") == h["code"] and t.get("action") == "sell" for t in trades):
                    stop_executed += 1

        stop_loss_compliance = stop_executed / stop_triggered if stop_triggered else 1.0

        # 持仓纪律
        hold_days = [h.get("held_days", 0) for h in holdings if h.get("held_days")]
        avg_hold_days = sum(hold_days) / len(hold_days) if hold_days else 0
        max_hold_allowed = 5  # 默认
        over_hold = sum(1 for d in hold_days if d > max_hold_allowed)

        # 情绪化操作（需要从交易记录中识别）
        chase_high = sum(1 for t in trades if t.get("chase_high", False))
        panic_sell = sum(1 for t in trades if t.get("panic_sell", False))
        revenge = sum(1 for t in trades if t.get("revenge_trade", False))

        # 纪律性评分
        discipline_score = 100
        discipline_score -= len(impulse) * 15  # 冲动交易扣分
        discipline_score -= (1 - stop_loss_compliance) * 30  # 止损不执行扣分
        discipline_score -= over_hold * 10  # 超时持仓扣分
        discipline_score -= chase_high * 10  # 追高扣分
        discipline_score -= panic_sell * 10  # 恐慌卖出扣分
        discipline_score = max(0, discipline_score)

        # 主要问题
        main_issues = []
        if impulse:
            main_issues.append(f"冲动交易{len(impulse)}次")
        if stop_loss_compliance < 1:
            main_issues.append(f"止损执行率{stop_loss_compliance*100:.0f}%")
        if over_hold:
            main_issues.append(f"超时持仓{over_hold}次")
        if chase_high:
            main_issues.append(f"追高{chase_high}次")

        # 改进建议
        improvements = []
        if impulse:
            improvements.append("增加下单确认环节，非计划内交易需二次确认")
        if stop_loss_compliance < 1:
            improvements.append("设置自动止损，不给人工干预机会")
        if over_hold:
            improvements.append("严格执行时间止损，到期必须离场")
        if chase_high:
            improvements.append("设定追高禁区，涨幅>7%不追")

        return BehaviorReview(
            date=date_str,
            plan_execution_rate=round(plan_execution_rate, 2),
            planned_trades=len(planned_codes),
            executed_trades=len(executed),
            skipped_trades=len(skipped),
            impulse_trades=len(impulse),
            impulse_pnl=round(impulse_pnl, 2),
            stop_loss_triggered=stop_triggered,
            stop_loss_executed=stop_executed,
            stop_loss_compliance=round(stop_loss_compliance, 2),
            avg_hold_days=round(avg_hold_days, 1),
            over_hold_count=over_hold,
            max_hold_days_allowed=max_hold_allowed,
            chase_high_count=chase_high,
            panic_sell_count=panic_sell,
            revenge_trade_count=revenge,
            discipline_score=round(discipline_score, 1),
            main_issues=main_issues,
            improvements=improvements,
        )

    def generate_daily_review(
        self,
        date_str: str,
        pre_brief: dict,
        post_data: dict,
        trades: list[dict],
        holdings: list[dict],
        strategy_history: dict[str, list[dict]] = None,
    ) -> DailyReview:
        """生成每日复盘报告"""
        strategy_history = strategy_history or {}

        # 1. 市场复盘
        market = self.calc_market_review(date_str, pre_brief, post_data)

        # 2. 个股复盘
        stocks = []
        for watch in pre_brief.get("watch", []):
            code = watch.get("code")
            actual = post_data.get("stocks", {}).get(code, {})
            stock_review = self.calc_stock_review(date_str, watch, actual, trades)
            stocks.append(stock_review)

        # 3. 策略复盘
        strategies = []
        for strategy_name in ["board_hit", "trend_pullback", "oversold_bounce", "event_driven"]:
            signals = post_data.get("signals", {}).get(strategy_name, [])
            history = strategy_history.get(strategy_name, [])
            strategy_review = self.calc_strategy_review(
                date_str, strategy_name, signals, trades, history
            )
            strategies.append(strategy_review)

        # 4. 行为复盘
        behavior = self.calc_behavior_review(
            date_str,
            pre_brief.get("watch", []),
            trades,
            holdings,
        )

        # 综合评分
        overall = self._calc_overall_score(market, stocks, strategies, behavior)

        # 关键洞察
        insights = self._generate_insights(market, stocks, strategies, behavior)

        # 待办事项
        actions = self._generate_actions(market, strategies, behavior)

        return DailyReview(
            date=date_str,
            market=market,
            stocks=stocks,
            strategies=strategies,
            behavior=behavior,
            overall_score=overall,
            key_insights=insights,
            action_items=actions,
        )

    def _calc_overall_score(
        self,
        market: MarketReview,
        stocks: list[StockReview],
        strategies: list[StrategyReview],
        behavior: BehaviorReview,
    ) -> float:
        """计算综合评分"""
        # 市场判断准确性 (30%)
        market_score = market.sentiment_accuracy * 100

        # 选股质量 (25%)
        if stocks:
            avg_score_return = sum(s.score_vs_return for s in stocks) / len(stocks)
            stock_score = min(100, max(0, 50 + avg_score_return * 1000))
        else:
            stock_score = 50

        # 策略有效性 (25%)
        active_strategies = [s for s in strategies if s.total_trades > 0]
        if active_strategies:
            avg_expectancy = sum(s.expectancy for s in active_strategies) / len(active_strategies)
            strategy_score = min(100, max(0, 50 + avg_expectancy * 500))
        else:
            strategy_score = 50

        # 行为纪律 (20%)
        behavior_score = behavior.discipline_score

        overall = (
            market_score * 0.30 +
            stock_score * 0.25 +
            strategy_score * 0.25 +
            behavior_score * 0.20
        )

        return round(overall, 1)

    def _generate_insights(
        self,
        market: MarketReview,
        stocks: list[StockReview],
        strategies: list[StrategyReview],
        behavior: BehaviorReview,
    ) -> list[str]:
        """生成关键洞察"""
        insights = []

        # 市场判断
        if market.sentiment_accuracy < 0.6:
            insights.append(f"市场情绪判断偏差较大（准确率{market.sentiment_accuracy*100:.0f}%），需改进情绪指标")
        if market.sector_hit_rate < 0.5:
            insights.append(f"板块判断命中率{market.sector_hit_rate*100:.0f}%，热点切换较快")

        # 选股
        high_score_low_return = [s for s in stocks if s.pre_score >= 70 and s.actual_return < 0]
        if high_score_low_return:
            insights.append(f"{len(high_score_low_return)}只高评分股票当日下跌，评分模型可能需要调整")

        # 策略
        for s in strategies:
            if s.is_decaying:
                insights.append(f"{s.strategy_name}策略近期衰减，建议优化参数或暂停")
            if s.expectancy < 0 and s.total_trades >= 10:
                insights.append(f"{s.strategy_name}策略期望值为负，累计{s.total_trades}笔交易")

        # 行为
        if behavior.impulse_trades > 0:
            insights.append(f"当日{behavior.impulse_trades}次冲动交易，纪律性需加强")
        if behavior.stop_loss_compliance < 1:
            insights.append(f"止损执行率{behavior.stop_loss_compliance*100:.0f}%，存在侥幸心理")

        return insights

    def _generate_actions(
        self,
        market: MarketReview,
        strategies: list[StrategyReview],
        behavior: BehaviorReview,
    ) -> list[str]:
        """生成待办事项"""
        actions = []

        # 策略调整
        for s in strategies:
            if s.suggestion == "pause":
                actions.append(f"暂停{s.strategy_name}策略，等待市场企稳")
            elif s.suggestion == "optimize":
                actions.append(f"优化{s.strategy_name}策略参数")
            elif s.suggestion == "reduce":
                actions.append(f"降低{s.strategy_name}策略仓位")

        # 行为改进
        actions.extend(behavior.improvements)

        # 市场判断改进
        if market.sentiment_accuracy < 0.6:
            actions.append("复盘情绪指标计算逻辑，增加连板高度、昨涨停表现等指标")

        return actions


# ---------------- 复盘报告生成 ----------------
def format_review_report(review: DailyReview) -> str:
    """格式化复盘报告为 Markdown"""
    lines = []
    lines.append(f"# 交易复盘报告 - {review.date}")
    lines.append("")
    lines.append(f"**综合评分**: {review.overall_score}/100")
    lines.append("")

    # 一、市场复盘
    lines.append("## 一、市场复盘")
    m = review.market
    lines.append(f"- 情绪判断: 盘前{m.predicted_sentiment:.0f}分({m.predicted_phase}) → "
                 f"实际{m.actual_sentiment:.0f}分({m.actual_phase})，准确率{m.sentiment_accuracy*100:.0f}%")
    lines.append(f"- 板块判断: 预判{', '.join(m.predicted_strong_sectors[:3])} → "
                 f"实际{', '.join(m.actual_strong_sectors[:3])}，命中率{m.sector_hit_rate*100:.0f}%")
    lines.append(f"- 仓位决策: 实际{m.position_decision*100:.0f}% vs 建议{m.position_suggestion*100:.0f}%，"
                 f"{'合理' if m.position_reasonable else '需调整'}")
    lines.append("")

    # 二、个股复盘
    lines.append("## 二、个股复盘")
    lines.append("| 代码 | 名称 | 评分 | 涨幅 | 交易 | 盈亏 | 计划执行 |")
    lines.append("|------|------|------|------|------|------|----------|")
    for s in review.stocks[:10]:
        traded = "是" if s.traded else "否"
        pnl = f"{s.pnl_pct*100:.1f}%" if s.pnl_pct else "-"
        executed = "✓" if s.plan_executed else ("✗" if s.traded else "-")
        lines.append(f"| {s.code} | {s.name} | {s.pre_score} | {s.actual_return*100:.1f}% | "
                     f"{traded} | {pnl} | {executed} |")
    lines.append("")

    # 三、策略复盘
    lines.append("## 三、策略复盘")
    lines.append("| 策略 | 信号 | 执行 | 胜率 | 盈亏比 | 期望值 | 建议 |")
    lines.append("|------|------|------|------|--------|--------|------|")
    for s in review.strategies:
        if s.total_trades > 0:
            lines.append(f"| {s.strategy_name} | {s.signals_generated} | {s.signals_taken} | "
                         f"{s.win_rate*100:.0f}% | {s.profit_loss_ratio:.1f} | "
                         f"{s.expectancy*100:.1f}% | {s.suggestion} |")
    lines.append("")

    # 四、行为复盘
    lines.append("## 四、行为复盘")
    b = review.behavior
    lines.append(f"- 纪律评分: {b.discipline_score}/100")
    lines.append(f"- 计划执行率: {b.plan_execution_rate*100:.0f}% ({b.executed_trades}/{b.planned_trades})")
    lines.append(f"- 冲动交易: {b.impulse_trades}次，盈亏{b.impulse_pnl:.0f}元")
    lines.append(f"- 止损执行率: {b.stop_loss_compliance*100:.0f}% ({b.stop_loss_executed}/{b.stop_loss_triggered})")
    lines.append(f"- 平均持仓: {b.avg_hold_days:.1f}天，超时{b.over_hold_count}次")
    if b.main_issues:
        lines.append(f"- 主要问题: {', '.join(b.main_issues)}")
    lines.append("")

    # 五、关键洞察
    if review.key_insights:
        lines.append("## 五、关键洞察")
        for i, insight in enumerate(review.key_insights, 1):
            lines.append(f"{i}. {insight}")
        lines.append("")

    # 六、待办事项
    if review.action_items:
        lines.append("## 六、待办事项")
        for i, action in enumerate(review.action_items, 1):
            lines.append(f"{i}. [ ] {action}")
        lines.append("")

    return "\n".join(lines)


def save_review(review: DailyReview, path: str | Path):
    """保存复盘报告到文件"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # 保存 JSON
    json_path = path.with_suffix(".json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(asdict(review), f, ensure_ascii=False, indent=2)

    # 保存 Markdown
    md_path = path.with_suffix(".md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(format_review_report(review))

    return json_path, md_path
