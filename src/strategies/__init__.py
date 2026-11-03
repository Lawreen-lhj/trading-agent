"""多策略框架模块。

包含四种短线交易策略：
- board_hit: 打板/半路策略
- trend_pullback: 趋势波段策略
- oversold_bounce: 超跌反弹策略
- event_driven: 事件驱动策略

使用方式：
    from src.strategies import get_strategy, get_all_strategies, select_strategies

    # 获取策略实例
    strategy = get_strategy("board_hit", config)

    # 获取所有已启用策略
    strategies = get_all_strategies(config)

    # 根据市场环境选择策略
    recommended = select_strategies(market_phase="回暖", sentiment_score=75)

    # 扫描信号
    signals = strategy.scan(stocks, market)
"""

from src.strategies.base import (
    BaseStrategy,
    TradeSignal,
    _STRATEGY_REGISTRY,
    get_strategy,
    list_strategies,
    register_strategy,
    select_strategies,
    suggest_position,
)

# 导入策略实现以触发注册
from src.strategies import board_hit
from src.strategies import trend_pullback
from src.strategies import oversold_bounce
from src.strategies import event_driven


def get_all_strategies(config: dict | None = None) -> list[BaseStrategy]:
    """获取所有已注册且已启用的策略实例。

    config: strategies 配置段，用于控制各策略的 enabled 和参数。
    """
    config = config or {}
    instances = []
    for name, cls in _STRATEGY_REGISTRY.items():
        strat_cfg = config.get(name, {})
        if isinstance(strat_cfg, dict) and not strat_cfg.get("enabled", True):
            continue
        instances.append(cls(strat_cfg if isinstance(strat_cfg, dict) else {}))
    return instances


__all__ = [
    "BaseStrategy",
    "TradeSignal",
    "get_all_strategies",
    "get_strategy",
    "list_strategies",
    "register_strategy",
    "select_strategies",
    "suggest_position",
]
