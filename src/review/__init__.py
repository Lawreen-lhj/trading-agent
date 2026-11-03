"""交易复盘模块。

四维复盘模型：
- 市场复盘：情绪判断准确性、板块命中率、仓位合理性
- 个股复盘：评分与收益相关性、计划执行率、买卖时机
- 策略复盘：胜率、盈亏比、期望值、策略衰减检测
- 行为复盘：纪律性评分、冲动交易、止损执行率

使用方式：
    from src.review import ReviewCalculator, DailyReview, format_review_report

    calc = ReviewCalculator()
    review = calc.generate_daily_review(date_str, pre_brief, post_data, trades, holdings)
    report = format_review_report(review)
"""

from src.review.reviewer import (
    MarketReview,
    StockReview,
    StrategyReview,
    BehaviorReview,
    DailyReview,
    ReviewCalculator,
    format_review_report,
    save_review,
)

__all__ = [
    "MarketReview",
    "StockReview",
    "StrategyReview",
    "BehaviorReview",
    "DailyReview",
    "ReviewCalculator",
    "format_review_report",
    "save_review",
]
