"""模拟撮合引擎 —— 严格模拟 A 股交易规则。

核心规则：
  1. T+1：当日买入次日才能卖出
  2. 涨跌停：主板±10%，创业板/科创板±20%，北交所±30%
     - 涨停时无法买入（除非提前挂单排队）
     - 跌停时无法卖出
  3. 100股整数倍：最小交易单位
  4. 交易时间：9:30-11:30, 13:00-15:00（集合竞价 9:15-9:25, 14:57-15:00）
  5. 手续费：佣金（万三，最低5元）+ 印花税（千分之0.5，卖出）+ 过户费（万0.1）
  6. 滑点：模拟真实成交价与预期价的偏差

撮合模式：
  - 限价单：价格达到才成交
  - 市价单：立即以对手价成交（模拟滑点）
  - 涨停价挂单：视为打板，按封板概率成交

为什么不用真实的撮合逻辑：
  真实的撮合需要 Level-2 数据（订单簿），成本高。
  这里用"简化撮合"：根据当日 OHLC 判断是否能成交，
  虽然不精确，但足够验证策略逻辑。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, time
from enum import Enum
from typing import Optional

from src.simulation.account import Account, calc_fees

log = logging.getLogger(__name__)


class OrderType(Enum):
    LIMIT = "limit"       # 限价单
    MARKET = "market"     # 市价单
    BOARD_HIT = "board"   # 打板（涨停价挂单）


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(Enum):
    PENDING = "pending"           # 待成交
    PARTIAL_FILLED = "partial"    # 部分成交
    FILLED = "filled"             # 全部成交
    CANCELLED = "cancelled"       # 已撤单
    REJECTED = "rejected"         # 被拒绝
    EXPIRED = "expired"           # 已过期（当日未成交）


@dataclass
class Order:
    """订单"""
    order_id: str
    account_id: str
    code: str
    name: str
    side: OrderSide
    order_type: OrderType
    price: float                  # 委托价（市价单为0）
    quantity: int                 # 委托数量
    filled_quantity: int = 0      # 已成交数量
    filled_price: float = 0.0     # 成交均价
    status: OrderStatus = OrderStatus.PENDING
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    expire_date: str = ""         # 过期日期（当日有效）
    strategy: str = ""            # 关联策略
    reason: str = ""              # 拒绝/撤单原因
    plan: dict = field(default_factory=dict)  # 原始交易计划
    slippage: float = 0.0         # 实际滑点

    def to_dict(self) -> dict:
        d = asdict(self)
        d["side"] = self.side.value
        d["order_type"] = self.order_type.value
        d["status"] = self.status.value
        return d


# ---------------- 涨跌停判断 ----------------
def get_limit_pct(code: str) -> float:
    """获取涨跌停幅度"""
    code = str(code).zfill(6)
    if code.startswith(("688", "689")):
        return 20.0    # 科创板
    if code.startswith(("300", "301")):
        return 20.0    # 创业板
    if code.startswith(("8", "4", "9")):
        return 30.0    # 北交所
    return 10.0        # 主板


def calc_limit_prices(prev_close: float, code: str) -> tuple[float, float]:
    """计算涨停价和跌停价"""
    limit_pct = get_limit_pct(code)
    up = round(prev_close * (1 + limit_pct / 100), 2)
    down = round(prev_close * (1 - limit_pct / 100), 2)
    return up, down


def is_limit_up(code: str, price: float, prev_close: float) -> bool:
    """是否涨停"""
    up, _ = calc_limit_prices(prev_close, code)
    return price >= up


def is_limit_down(code: str, price: float, prev_close: float) -> bool:
    """是否跌停"""
    _, down = calc_limit_prices(prev_close, code)
    return price <= down


# ---------------- 交易时间检查 ----------------
def is_trading_time(dt: datetime = None) -> bool:
    """是否在交易时间内"""
    dt = dt or datetime.now()

    # 周末不交易
    if dt.weekday() >= 5:
        return False

    t = dt.time()
    # 上午 9:30 - 11:30
    if time(9, 30) <= t <= time(11, 30):
        return True
    # 下午 13:00 - 15:00
    if time(13, 0) <= t <= time(15, 0):
        return True
    return False


def is_auction_time(dt: datetime = None) -> bool:
    """是否在集合竞价时间"""
    dt = dt or datetime.now()
    if dt.weekday() >= 5:
        return False
    t = dt.time()
    # 早盘集合竞价 9:15 - 9:25
    if time(9, 15) <= t <= time(9, 25):
        return True
    # 尾盘集合竞价 14:57 - 15:00
    if time(14, 57) <= t <= time(15, 0):
        return True
    return False


# ---------------- 市场时段状态（共享） ----------------
# key -> (中文标签, 是否可撮合成交, 是否属于交易时段窗口)
_SESSIONS = {
    "non_trading_day": ("非交易日", False, False),
    "pre_open":        ("盘前",     False, False),
    "auction":         ("集合竞价", True,  True),
    "morning":         ("交易中",   True,  True),
    "lunch":           ("午间休市", False, False),
    "afternoon":       ("交易中",   True,  True),
    "closing_auction": ("尾盘竞价", True,  True),
    "closed":          ("已收盘",   False, False),
}


def market_session(dt: datetime = None) -> dict:
    """返回当前市场时段的离散状态，作为全项目时段判断的唯一来源。

    run_sim_live（盘中自动循环）、server（API 会话状态）、WebUI（时段徽标）共用。
    只按星期判断交易日，不含法定节假日日历（与项目其余部分口径一致）。

    返回：
        {
          "key":   "pre_open|auction|morning|lunch|afternoon|closing_auction|closed|non_trading_day",
          "label": 中文标签,
          "tradable": 当前是否可撮合成交（连续竞价/集合竞价为 True）,
          "in_session": 是否处于交易时段窗口（含集合竞价）,
          "is_trading_day": 是否为交易日（周一至周五）,
          "time": "HH:MM:SS",
        }
    """
    dt = dt or datetime.now()
    is_trading_day = dt.weekday() < 5
    t = dt.time()

    if not is_trading_day:
        key = "non_trading_day"
    elif t < time(9, 15):
        key = "pre_open"
    elif time(9, 15) <= t < time(9, 30):
        key = "auction"
    elif time(9, 30) <= t < time(11, 30):
        key = "morning"
    elif time(11, 30) <= t < time(13, 0):
        key = "lunch"
    elif time(13, 0) <= t < time(14, 57):
        key = "afternoon"
    elif time(14, 57) <= t <= time(15, 0):
        key = "closing_auction"
    else:
        key = "closed"

    label, tradable, in_session = _SESSIONS[key]
    return {
        "key": key,
        "label": label,
        "tradable": tradable and is_trading_day,
        "in_session": in_session and is_trading_day,
        "is_trading_day": is_trading_day,
        "time": dt.strftime("%H:%M:%S"),
    }


# ---------------- 撮合引擎 ----------------
class Matcher:
    """模拟撮合引擎"""

    def __init__(self, account: Account, strict_time_check: bool = False):
        """
        account: 关联的账户
        strict_time_check: 是否严格检查交易时间（回测时应关闭）
        """
        self.account = account
        self.strict_time_check = strict_time_check
        self.pending_orders: list[Order] = []  # 待成交订单
        self.order_history: list[Order] = []   # 历史订单

    def submit_order(
        self,
        code: str,
        name: str,
        side: str,
        quantity: int,
        price: float = 0.0,
        order_type: str = "limit",
        strategy: str = "",
        plan: dict = None,
    ) -> Order:
        """提交订单（不立即撮合）"""
        # 时间检查
        if self.strict_time_check and not (is_trading_time() or is_auction_time()):
            order = self._create_order(code, name, side, quantity, price, order_type, strategy, plan)
            order.status = OrderStatus.REJECTED
            order.reason = "非交易时间"
            self.order_history.append(order)
            return order

        # 数量检查
        if quantity <= 0 or quantity % 100 != 0:
            order = self._create_order(code, name, side, quantity, price, order_type, strategy, plan)
            order.status = OrderStatus.REJECTED
            order.reason = f"数量必须是100的整数倍，当前{quantity}"
            self.order_history.append(order)
            return order

        order = self._create_order(code, name, side, quantity, price, order_type, strategy, plan)
        self.pending_orders.append(order)
        return order

    def _create_order(
        self,
        code: str,
        name: str,
        side: str,
        quantity: int,
        price: float,
        order_type: str,
        strategy: str,
        plan: dict,
    ) -> Order:
        """创建订单对象"""
        order_id = f"ORD_{datetime.now().strftime('%Y%m%d%H%M%S')}_{code}_{len(self.order_history)}"
        return Order(
            order_id=order_id,
            account_id=self.account.account_id,
            code=str(code).zfill(6),
            name=name,
            side=OrderSide(side),
            order_type=OrderType(order_type),
            price=price,
            quantity=quantity,
            expire_date=date.today().isoformat(),
            strategy=strategy,
            plan=plan or {},
        )

    def match_orders(self, market_data: dict[str, dict]) -> list[dict]:
        """撮合所有待成交订单。

        market_data: {code: {open, high, low, close, prev_close, volume, ...}}

        返回：成交结果列表
        """
        results = []
        remaining = []

        for order in self.pending_orders:
            data = market_data.get(order.code)
            if not data:
                order.status = OrderStatus.EXPIRED
                order.reason = "无市场数据"
                self.order_history.append(order)
                continue

            result = self._match_single(order, data)
            if result:
                results.append(result)

            if order.status in (OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED):
                self.order_history.append(order)
            else:
                remaining.append(order)

        self.pending_orders = remaining
        return results

    def _match_single(self, order: Order, data: dict) -> Optional[dict]:
        """撮合单个订单"""
        open_ = data.get("open")
        high = data.get("high")
        low = data.get("low")
        close = data.get("close")
        prev_close = data.get("prev_close") or data.get("pre_close")

        if not (open_ and high and low and close and prev_close):
            order.status = OrderStatus.EXPIRED
            order.reason = "行情数据不完整"
            return None

        # 涨跌停价
        limit_up, limit_down = calc_limit_prices(prev_close, order.code)

        # 一字板检查（无法成交）
        if open_ == high == low == close:
            if order.side == OrderSide.BUY and close >= limit_up:
                order.status = OrderStatus.REJECTED
                order.reason = "一字涨停，无法买入"
                return None
            if order.side == OrderSide.SELL and close <= limit_down:
                order.status = OrderStatus.REJECTED
                order.reason = "一字跌停，无法卖出"
                return None

        # 撮合逻辑
        if order.side == OrderSide.BUY:
            return self._match_buy(order, data, limit_up)
        else:
            return self._match_sell(order, data, limit_down)

    def _match_buy(self, order: Order, data: dict, limit_up: float) -> Optional[dict]:
        """撮合买单"""
        open_ = data["open"]
        high = data["high"]
        low = data["low"]
        close = data["close"]
        prev_close = data.get("prev_close")

        fill_price = None

        if order.order_type == OrderType.MARKET:
            # 市价单：以开盘价成交（加滑点）
            fill_price = open_
        elif order.order_type == OrderType.BOARD_HIT:
            # 打板：如果收盘涨停，视为成交（简化处理）
            if close >= limit_up * 0.998:
                fill_price = limit_up
            else:
                order.status = OrderStatus.EXPIRED
                order.reason = "打板失败，未封涨停"
                return None
        else:
            # 限价单：委托价 >= 最低价，可以成交
            if order.price >= low:
                # 成交价：min(委托价, 开盘价) —— 模拟价格优先
                fill_price = min(order.price, open_) if open_ <= order.price else order.price
                # 但不能低于最低价
                fill_price = max(fill_price, low)
            else:
                order.status = OrderStatus.EXPIRED
                order.reason = f"限价{order.price}未达到，最低价{low}"
                return None

        # 涨停时无法买入（除非打板）
        if close >= limit_up and order.order_type != OrderType.BOARD_HIT:
            order.status = OrderStatus.REJECTED
            order.reason = "涨停无法买入"
            return None

        # 执行买入
        result = self.account.buy(
            code=order.code,
            name=order.name,
            price=fill_price,
            quantity=order.quantity,
            strategy=order.strategy,
            plan=order.plan,
            apply_slippage=(order.order_type == OrderType.MARKET),
        )

        if result["success"]:
            order.status = OrderStatus.FILLED
            order.filled_quantity = order.quantity
            order.filled_price = result["trade"]["price"]
            order.slippage = result["trade"]["price"] - fill_price
            order.updated_at = datetime.now().isoformat()
            return result["trade"]
        else:
            order.status = OrderStatus.REJECTED
            order.reason = result["reason"]
            return None

    def _match_sell(self, order: Order, data: dict, limit_down: float) -> Optional[dict]:
        """撮合卖单"""
        open_ = data["open"]
        high = data["high"]
        low = data["low"]
        close = data["close"]

        fill_price = None

        if order.order_type == OrderType.MARKET:
            # 市价单：以开盘价成交
            fill_price = open_
        else:
            # 限价单：委托价 <= 最高价，可以成交
            if order.price <= high:
                fill_price = max(order.price, open_) if open_ >= order.price else order.price
                fill_price = min(fill_price, high)
            else:
                order.status = OrderStatus.EXPIRED
                order.reason = f"限价{order.price}未达到，最高价{high}"
                return None

        # 跌停时无法卖出
        if close <= limit_down:
            order.status = OrderStatus.REJECTED
            order.reason = "跌停无法卖出"
            return None

        # 执行卖出
        result = self.account.sell(
            code=order.code,
            price=fill_price,
            quantity=order.quantity,
            reason=order.reason or order.strategy,
            apply_slippage=(order.order_type == OrderType.MARKET),
        )

        if result["success"]:
            order.status = OrderStatus.FILLED
            order.filled_quantity = order.quantity
            order.filled_price = result["trade"]["price"]
            order.updated_at = datetime.now().isoformat()
            return result["trade"]
        else:
            order.status = OrderStatus.REJECTED
            order.reason = result["reason"]
            return None

    def cancel_order(self, order_id: str) -> bool:
        """撤单"""
        for order in self.pending_orders:
            if order.order_id == order_id:
                order.status = OrderStatus.CANCELLED
                order.updated_at = datetime.now().isoformat()
                self.pending_orders.remove(order)
                self.order_history.append(order)
                return True
        return False

    def get_pending_orders(self) -> list[dict]:
        """获取待成交订单"""
        return [o.to_dict() for o in self.pending_orders]

    def get_order_history(self, limit: int = 50) -> list[dict]:
        """获取订单历史"""
        return [o.to_dict() for o in self.order_history[-limit:][::-1]]


# ---------------- 风控 ----------------
class RiskManager:
    """风控管理器"""

    def __init__(self, account: Account, config: dict = None):
        self.account = account
        self.config = config or {
            "max_single_position_pct": 0.30,   # 单票最大仓位 30%
            "max_total_position_pct": 0.90,    # 总仓位上限 90%
            "max_daily_loss_pct": 0.05,        # 单日最大亏损 5%
            "max_daily_trades": 20,            # 单日最多交易次数
            "max_hold_days": 5,                # 最大持仓天数
            "min_cash_reserve": 5000,          # 最低现金保留
        }
        self.daily_trades = 0
        self.daily_start_assets = account.total_assets

    def on_new_day(self):
        """新交易日重置"""
        self.daily_trades = 0
        self.daily_start_assets = self.account.total_assets

    def check_buy(self, code: str, price: float, quantity: int) -> tuple[bool, str]:
        """买入前风控检查"""
        amount = price * quantity

        # 1. 现金检查
        if amount > self.account.cash - self.config["min_cash_reserve"]:
            return False, f"现金不足或低于最低保留（{self.config['min_cash_reserve']}）"

        # 2. 单票仓位检查
        existing = self.account.positions.get(code)
        existing_value = existing.market_value if existing else 0
        new_position_value = existing_value + amount
        if new_position_value / self.account.total_assets > self.config["max_single_position_pct"]:
            return False, f"单票仓位超过{self.config['max_single_position_pct']*100:.0f}%上限"

        # 3. 总仓位检查
        new_total_position = self.account.position_value + amount
        if new_total_position / self.account.total_assets > self.config["max_total_position_pct"]:
            return False, f"总仓位超过{self.config['max_total_position_pct']*100:.0f}%上限"

        # 4. 单日交易次数
        if self.daily_trades >= self.config["max_daily_trades"]:
            return False, f"单日交易次数已达上限{self.config['max_daily_trades']}"

        # 5. 单日亏损保护
        current_pnl_pct = (self.account.total_assets - self.daily_start_assets) / self.daily_start_assets
        if current_pnl_pct < -self.config["max_daily_loss_pct"]:
            return False, f"单日亏损已达{abs(current_pnl_pct)*100:.1f}%，停止开新仓"

        return True, ""

    def check_sell(self, code: str, price: float, quantity: int) -> tuple[bool, str]:
        """卖出前风控检查（一般不限制卖出，但检查 T+1）"""
        pos = self.account.positions.get(code)
        if not pos:
            return False, "未持有该股票"

        if pos.available < quantity:
            return False, f"T+1限制，可卖{pos.available}"

        return True, ""

    def check_hold_days(self) -> list[dict]:
        """检查超时持仓"""
        today = date.today()
        overdue = []

        for code, pos in self.account.positions.items():
            if not pos.open_date:
                continue
            try:
                open_d = date.fromisoformat(pos.open_date)
            except ValueError:
                continue

            # 计算持仓工作日数
            hold_days = 0
            cur = open_d
            while cur < today:
                cur = date.fromordinal(cur.toordinal() + 1)
                if cur.weekday() < 5:
                    hold_days += 1

            if hold_days >= self.config["max_hold_days"]:
                overdue.append({
                    "code": code,
                    "name": pos.name,
                    "hold_days": hold_days,
                    "profit_pct": pos.profit_pct,
                    "action": "time_stop",
                })

        return overdue

    def on_trade_executed(self):
        """交易执行后更新计数"""
        self.daily_trades += 1
