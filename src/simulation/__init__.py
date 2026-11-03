"""模拟交易模块。

包含：
- Account: 账户管理（现金、持仓、盈亏）
- Matcher: 撮合引擎（订单执行、A股规则模拟）
- RiskManager: 风控管理（仓位控制、单日亏损保护）

使用方式：
    from src.simulation import Account, Matcher, RiskManager

    account = Account("test", 100000, db_path="data/sim.db")
    matcher = Matcher(account)
    risk = RiskManager(account)

    # 提交订单
    order = matcher.submit_order("600519", "贵州茅台", "buy", 100, price=1500)

    # 撮合
    trades = matcher.match_orders(market_data)
"""

from src.simulation.account import Account, Position, calc_fees, FEES
from src.simulation.matcher import (
    Matcher,
    RiskManager,
    Order,
    OrderType,
    OrderSide,
    OrderStatus,
    get_limit_pct,
    calc_limit_prices,
    is_limit_up,
    is_limit_down,
    is_trading_time,
    is_auction_time,
)

__all__ = [
    "Account",
    "Position",
    "Matcher",
    "RiskManager",
    "Order",
    "OrderType",
    "OrderSide",
    "OrderStatus",
    "calc_fees",
    "FEES",
    "get_limit_pct",
    "calc_limit_prices",
    "is_limit_up",
    "is_limit_down",
    "is_trading_time",
    "is_auction_time",
]
