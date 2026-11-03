"""多策略框架 —— 短线交易不是只有一种打法。

为什么需要多策略：
  市场情绪有周期，不同阶段适合不同策略。
  冰点期打板是送钱，高潮期低吸是踏空。
  单一策略在震荡市会反复止损，在趋势市会过早止盈。
  多策略的核心是"匹配"：把正确的策略用在正确的市场环境里。

四种基础策略：
  1. 打板/半路（board_hit）：情绪高潮期，追涨停或涨停前介入
  2. 趋势波段（trend_pullback）：情绪中性期，多头排列回踩买入
  3. 超跌反弹（oversold_bounce）：情绪冰点期，恐慌杀跌后抢反弹
  4. 事件驱动（event_driven）：任何时期，有明确催化剂的个股

策略选择器（selector）：
  根据市场情绪阶段自动推荐策略组合和仓位上限。
  不是完全自动切换，而是给出建议，最终由交易者决定。

与现有系统的关系：
  策略模块输出"信号"和"计划"，格式与 shortterm.plan() 兼容，
  可以直接接入现有的简报渲染和数据库存储。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TradeSignal:
    """交易信号统一格式"""
    strategy: str              # 策略名称
    code: str                  # 股票代码
    name: str                  # 股票名称
    direction: str             # "buy" / "sell"
    strength: float            # 信号强度 0-1
    price: float               # 当前价格
    reason: str                # 触发原因

    # 买入信号特有
    buy_lo: Optional[float] = None      # 买入区间下限
    buy_hi: Optional[float] = None      # 买入区间上限
    stop: Optional[float] = None        # 止损价
    target1: Optional[float] = None     # 第一目标
    target2: Optional[float] = None     # 第二目标
    rr: Optional[float] = None          # 盈亏比
    shares: Optional[int] = None        # 建议股数
    hold_days: int = 3                  # 建议持仓天数

    # 卖出信号特有
    sell_reason: Optional[str] = None   # 卖出原因
    urgency: str = "normal"             # "immediate" / "normal" / "watch"

    # 元数据
    confidence: float = 0.5             # 置信度 0-1
    market_phase: str = ""              # 触发时的市场情绪阶段
    extra: dict = field(default_factory=dict)


class BaseStrategy(ABC):
    """策略基类。

    所有策略必须实现：
    - name: 策略名称
    - scan(): 扫描市场，返回候选信号
    - validate(): 验证信号是否仍然有效
    - suitable_phases: 适合的市场情绪阶段
    """

    name: str = "base"
    description: str = ""
    suitable_phases: list[str] = []  # 适合的情绪阶段
    max_hold_days: int = 5           # 最大持仓天数
    min_rr: float = 2.0              # 最小盈亏比

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.enabled = True

    @abstractmethod
    def scan(self, stocks: list[dict], market: dict) -> list[TradeSignal]:
        """扫描市场，生成交易信号。

        stocks: 股票列表（含技术指标快照）
        market: 市场数据（情绪、板块等）

        返回：信号列表
        """
        pass

    def validate(self, signal: TradeSignal, current_data: dict) -> bool:
        """验证信号是否仍然有效（用于盘中复查）。

        默认实现：检查价格是否还在买入区间内。
        子类可覆盖实现更复杂的验证逻辑。
        """
        if signal.direction != "buy":
            return True

        price = current_data.get("close")
        if not price:
            return False

        # 价格不能偏离买入区间太远
        if signal.buy_hi and price > signal.buy_hi * 1.03:
            return False  # 已经涨太多，不追
        if signal.buy_lo and price < signal.buy_lo * 0.97:
            return False  # 已经跌太多，信号失效

        return True

    def is_suitable(self, market_phase: str) -> bool:
        """判断当前市场环境是否适合该策略。"""
        if not self.suitable_phases:
            return True  # 没有指定则任何环境都可用
        return market_phase in self.suitable_phases

    def calc_position_size(
        self,
        capital: float,
        risk_pct: float,
        entry_price: float,
        stop_price: float,
    ) -> int:
        """计算仓位（股数）。

        基于固定风险比例：单笔亏损不超过总资金的 risk_pct%。
        """
        if entry_price <= stop_price or entry_price <= 0:
            return 0

        risk_per_share = entry_price - stop_price
        max_loss = capital * risk_pct / 100
        shares = int(max_loss / risk_per_share)

        # A 股最小交易单位 100 股
        shares = (shares // 100) * 100

        # 单票不超过总资金 30%
        max_shares = int(capital * 0.30 / entry_price / 100) * 100
        shares = min(shares, max_shares)

        return max(0, shares)


# ---------------- 策略注册表 ----------------
_STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {}


def register_strategy(cls: type[BaseStrategy]) -> type[BaseStrategy]:
    """策略注册装饰器"""
    _STRATEGY_REGISTRY[cls.name] = cls
    return cls


def get_strategy(name: str, config: dict | None = None) -> Optional[BaseStrategy]:
    """获取策略实例"""
    cls = _STRATEGY_REGISTRY.get(name)
    if cls:
        return cls(config)
    return None


def list_strategies() -> list[dict]:
    """列出所有已注册策略"""
    return [
        {
            "name": cls.name,
            "description": cls.description,
            "suitable_phases": cls.suitable_phases,
            "max_hold_days": cls.max_hold_days,
        }
        for cls in _STRATEGY_REGISTRY.values()
    ]


# ---------------- 策略选择器 ----------------
def select_strategies(
    market_phase: str,
    sentiment_score: float = 50,
    config: dict | None = None,
) -> list[tuple[str, float]]:
    """根据市场环境推荐策略组合。

    返回：[(策略名, 权重), ...]，权重之和为 1.0

    设计原则：
    - 冰点期：只做超跌反弹，仓位极低
    - 退潮期：超跌反弹 + 趋势波段，防守为主
    - 中性期：趋势波段为主，打板为辅
    - 回暖期：打板 + 趋势 + 事件，进攻为主
    - 高潮期：打板 + 事件，但要快进快出
    """
    config = config or {}

    # 基础权重配置
    phase_weights = {
        "冰点": [("oversold_bounce", 1.0)],
        "退潮": [("oversold_bounce", 0.6), ("trend_pullback", 0.4)],
        "中性": [("trend_pullback", 0.5), ("board_hit", 0.3), ("event_driven", 0.2)],
        "回暖": [("board_hit", 0.4), ("trend_pullback", 0.35), ("event_driven", 0.25)],
        "高潮": [("board_hit", 0.5), ("event_driven", 0.3), ("trend_pullback", 0.2)],
    }

    weights = phase_weights.get(market_phase, phase_weights["中性"])

    # 根据情绪分数微调
    # 情绪极好时增加打板权重，情绪极差时增加超跌权重
    if sentiment_score > 80:
        weights = _adjust_weight(weights, "board_hit", 0.1)
    elif sentiment_score < 25:
        weights = _adjust_weight(weights, "oversold_bounce", 0.15)

    return weights


def _adjust_weight(
    weights: list[tuple[str, float]],
    strategy: str,
    delta: float,
) -> list[tuple[str, float]]:
    """调整某策略的权重，并归一化"""
    result = []
    for name, w in weights:
        if name == strategy:
            w = max(0, min(1, w + delta))
        result.append((name, w))

    # 归一化
    total = sum(w for _, w in result)
    if total > 0:
        result = [(n, w / total) for n, w in result]

    return result


# ---------------- 仓位建议 ----------------
def suggest_position(
    market_phase: str,
    sentiment_score: float,
    strategy_name: str,
    base_capital: float = 100000,
) -> dict:
    """根据市场环境和策略给出仓位建议。

    返回：
      {
        "max_total_position": 0.0-1.0,    # 总仓位上限
        "max_single_position": 0.0-0.3,   # 单票仓位上限
        "suggested_capital": float,        # 建议投入资金
        "reason": str,                     # 建议原因
      }
    """
    from src.sentiment import PHASE_POSITION

    # 基础仓位范围
    pos_range = PHASE_POSITION.get(market_phase, (0.3, 0.7))
    min_pos, max_pos = pos_range

    # 根据情绪分数在范围内插值
    # 分数 0-100 映射到 min_pos-max_pos
    ratio = max(0, min(1, sentiment_score / 100))
    total_position = min_pos + ratio * (max_pos - min_pos)

    # 策略调整
    single_position = 0.20  # 默认单票 20%
    if strategy_name == "board_hit":
        single_position = 0.15  # 打板风险高，单票仓位降低
        total_position *= 0.9   # 总仓位也略降
    elif strategy_name == "oversold_bounce":
        single_position = 0.15  # 超跌反弹不确定性高
        total_position *= 0.8   # 冰点期总仓位要低

    # 高潮期特殊处理：虽然情绪好，但要警惕退潮
    if market_phase == "高潮":
        total_position = min(total_position, 0.6)
        single_position = min(single_position, 0.15)

    suggested_capital = base_capital * total_position

    reason = f"情绪{market_phase}（{sentiment_score:.0f}分），{strategy_name}策略"
    if market_phase == "冰点":
        reason += "，轻仓试探，严格止损"
    elif market_phase == "高潮":
        reason += "，警惕退潮，不追最后一棒"

    return {
        "max_total_position": round(total_position, 2),
        "max_single_position": round(single_position, 2),
        "suggested_capital": round(suggested_capital, 0),
        "reason": reason,
    }
