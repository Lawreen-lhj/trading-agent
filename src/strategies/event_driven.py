"""事件驱动策略 —— 有明确催化剂的个股机会。

核心逻辑：
  技术分析看的是"过去"，事件驱动看的是"未来"。
  当一个明确的事件发生时，市场需要时间消化和定价。
  事件驱动策略就是在"事件发生"到"市场充分定价"之间获利。

  A 股常见的事件类型：
  - 业绩预增/超预期：基本面改善，估值重估
  - 政策利好：行业逻辑改变，资金涌入
  - 股权激励：管理层利益绑定，业绩预期提升
  - 大股东增持：内部人看好，信号意义强
  - 机构调研：聪明钱关注，后续可能有动作
  - 并购重组：资产注入，价值重估

适用环境：
  - 任何情绪阶段都可以做（事件是个股层面的）
  - 但情绪好时事件容易被放大，情绪差时容易被忽视

选股条件：
  1. 有明确事件催化剂（公告、新闻、数据）
  2. 事件尚未被充分定价（涨幅<10%，或刚发生）
  3. 基本面支撑（不是纯概念炒作）
  4. 流动性充足（日均成交>1亿）

买入时机：
  - 事件公告后首日低开或平开（给上车机会）
  - 盘中放量突破（资金开始反应）
  - 回踩确认支撑后企稳

卖出时机：
  - 事件被充分定价（涨幅>15-20%）
  - 利好兑现（如业绩公告后"见光死"）
  - 止损：事件逻辑被证伪，或亏损>5%
  - 时间止损：事件驱动一般3-7天

风控要点：
  - 仓位：单票≤20%
  - 必须确认事件的真实性（不看小道消息）
  - 区分"一次性事件"和"持续性事件"
  - 警惕"利好出尽是利空"
"""
from __future__ import annotations

from typing import Optional
from dataclasses import dataclass
from datetime import datetime

from src.strategies.base import BaseStrategy, TradeSignal, register_strategy


@dataclass
class Event:
    """事件数据结构"""
    event_type: str           # 事件类型
    code: str                 # 股票代码
    name: str                 # 股票名称
    title: str                # 事件标题
    content: str              # 事件内容摘要
    publish_time: datetime    # 发布时间
    source: str               # 来源
    impact_level: str         # 影响程度: "high" / "medium" / "low"
    is_positive: bool         # 是否利好
    extra: dict = None        # 额外数据


# 事件类型及其权重
EVENT_TYPES = {
    "earnings_surprise": {"weight": 0.9, "hold_days": 5, "desc": "业绩超预期"},
    "policy_support": {"weight": 0.85, "hold_days": 3, "desc": "政策利好"},
    "equity_incentive": {"weight": 0.7, "hold_days": 7, "desc": "股权激励"},
    "insider_buy": {"weight": 0.75, "hold_days": 5, "desc": "大股东增持"},
    "institution_research": {"weight": 0.6, "hold_days": 5, "desc": "机构调研"},
    "merger_acquisition": {"weight": 0.95, "hold_days": 7, "desc": "并购重组"},
    "contract_win": {"weight": 0.7, "hold_days": 3, "desc": "重大合同"},
    "product_launch": {"weight": 0.65, "hold_days": 5, "desc": "新产品发布"},
    "analyst_upgrade": {"weight": 0.6, "hold_days": 5, "desc": "分析师上调评级"},
    "buyback": {"weight": 0.55, "hold_days": 7, "desc": "股票回购"},
}


@register_strategy
class EventDrivenStrategy(BaseStrategy):
    """事件驱动策略"""

    name = "event_driven"
    description = "基于明确事件催化剂的交易机会"
    suitable_phases = ["冰点", "退潮", "中性", "回暖", "高潮"]  # 任何时期都可用
    max_hold_days = 7
    min_rr = 2.0

    # 策略参数
    MAX_PRICE_RUNUP = 0.10    # 事件后最大涨幅（超过说明已定价）
    MIN_AMOUNT = 1e8          # 最小日均成交额 1 亿
    EVENT_FRESHNESS_HOURS = 48  # 事件新鲜度（小时）

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.events: list[Event] = []  # 事件池

    def add_events(self, events: list[Event]):
        """添加事件到事件池"""
        self.events.extend(events)
        # 按时间排序，保留最新的
        self.events.sort(key=lambda e: e.publish_time, reverse=True)
        self.events = self.events[:100]  # 最多保留100个事件

    def scan(self, stocks: list[dict], market: dict) -> list[TradeSignal]:
        """扫描事件驱动机会"""
        signals = []

        if not self.events:
            return signals

        # 建立股票代码到事件的映射
        code_events: dict[str, list[Event]] = {}
        now = datetime.now()
        for evt in self.events:
            # 只处理新鲜事件
            age_hours = (now - evt.publish_time).total_seconds() / 3600
            if age_hours > self.EVENT_FRESHNESS_HOURS:
                continue
            if not evt.is_positive:
                continue
            if evt.code not in code_events:
                code_events[evt.code] = []
            code_events[evt.code].append(evt)

        # 扫描有事件的股票
        for s in stocks:
            code = s.get("code", "")
            if code not in code_events:
                continue

            events = code_events[code]
            sig = self._evaluate_stock(s, events, market)
            if sig:
                signals.append(sig)

        signals.sort(key=lambda x: -x.strength)
        return signals[:10]

    def _evaluate_stock(
        self,
        s: dict,
        events: list[Event],
        market: dict,
    ) -> Optional[TradeSignal]:
        """评估有事件的股票"""
        code = s.get("code", "")
        name = s.get("name", "")
        close = s.get("close")

        if not close:
            return None

        # 1. 流动性检查
        amount = s.get("amount") or s.get("avg_amount_20d") or 0
        if amount < self.MIN_AMOUNT:
            return None

        # 2. 事件后涨幅检查（不能已经涨太多）
        # 这里用 ret3 或 ret5 近似
        ret3 = s.get("ret3")
        ret5 = s.get("ret5")
        recent_runup = max(ret3 or 0, ret5 or 0)
        if recent_runup > self.MAX_PRICE_RUNUP:
            return None  # 已经被市场充分定价

        # 3. 选择最强的事件
        best_event = max(events, key=lambda e: EVENT_TYPES.get(e.event_type, {}).get("weight", 0.5))
        event_info = EVENT_TYPES.get(best_event.event_type, {})

        # 4. 技术面配合（不能是下跌趋势中的利好）
        ma5 = s.get("ma5")
        ma20 = s.get("ma20")
        if ma20 and close < ma20 * 0.9:
            return None  # 长期趋势太差，利好也救不了

        # 5. 计算信号强度
        strength = self._calc_strength(s, best_event, event_info, market)

        # 6. 生成交易计划
        atr14 = s.get("atr14")
        if atr14:
            buy_lo = close - 0.3 * atr14
            buy_hi = close + 0.5 * atr14
        else:
            buy_lo = close * 0.99
            buy_hi = close * 1.03

        # 止损：事件逻辑被证伪，或技术位破位
        stop = close * 0.95  # 默认5%止损
        if ma20 and ma20 < stop:
            stop = ma20 * 0.98  # 但不破MA20

        # 目标：根据事件类型和影响力
        event_weight = event_info.get("weight", 0.5)
        target1 = close * (1 + 0.08 * event_weight)  # 8-15%
        target2 = close * (1 + 0.15 * event_weight)  # 15-25%

        risk = close - stop
        if risk <= 0:
            return None

        rr = (target1 - close) / risk
        if rr < self.min_rr:
            return None

        # 持仓天数
        hold_days = event_info.get("hold_days", 5)

        # 仓位
        capital = self.config.get("capital", 100000)
        risk_pct = self.config.get("single_risk_pct", 1.0)
        shares = self.calc_position_size(capital, risk_pct, close, stop)

        reason = f"{event_info.get('desc', '事件驱动')}：{best_event.title}"

        return TradeSignal(
            strategy=self.name,
            code=code,
            name=name,
            direction="buy",
            strength=strength,
            price=close,
            reason=reason,
            buy_lo=round(buy_lo, 2),
            buy_hi=round(buy_hi, 2),
            stop=round(stop, 2),
            target1=round(target1, 2),
            target2=round(target2, 2),
            rr=round(rr, 2),
            shares=shares,
            hold_days=hold_days,
            confidence=strength,
            market_phase=market.get("sentiment", {}).get("phase", ""),
            extra={
                "event_type": best_event.event_type,
                "event_title": best_event.title,
                "event_time": best_event.publish_time.isoformat(),
                "event_impact": best_event.impact_level,
                "event_count": len(events),
            },
        )

    def _calc_strength(
        self,
        s: dict,
        event: Event,
        event_info: dict,
        market: dict,
    ) -> float:
        """计算信号强度"""
        strength = 0.4

        # 事件类型权重
        weight = event_info.get("weight", 0.5)
        strength += weight * 0.3

        # 事件影响程度
        if event.impact_level == "high":
            strength += 0.15
        elif event.impact_level == "medium":
            strength += 0.10

        # 技术面配合
        ma5 = s.get("ma5")
        ma10 = s.get("ma10")
        close = s.get("close")

        if ma5 and ma10 and close:
            if ma5 > ma10 and close > ma5:
                strength += 0.10  # 多头排列，事件更容易被放大

        # 量比（放量说明资金开始反应）
        vol_ratio = s.get("vol_ratio")
        if vol_ratio:
            if 1.2 <= vol_ratio <= 2.5:
                strength += 0.10  # 温和放量
            elif vol_ratio > 3:
                strength += 0.05  # 大幅放量，但可能是短期过热

        # 市场环境
        phase = market.get("sentiment", {}).get("phase", "")
        if phase in ("回暖", "高潮"):
            strength += 0.10  # 情绪好时事件容易被放大
        elif phase == "冰点":
            strength -= 0.10  # 情绪差时利好可能被忽视

        return max(0, min(1, strength))

    def check_sell_signal(self, s: dict, cost: float, hold_days: int, event_info: dict = None) -> Optional[dict]:
        """检查持仓的卖出信号"""
        close = s.get("close")
        if not close:
            return None

        event_info = event_info or {}
        max_hold = event_info.get("hold_days", self.max_hold_days)

        # 1. 利好兑现（涨幅已达目标）
        if cost:
            profit_pct = (close - cost) / cost
            if profit_pct > 0.15:
                return {
                    "action": "sell",
                    "reason": f"盈利{profit_pct*100:.1f}%，事件已充分定价",
                    "urgency": "normal",
                }

        # 2. 见光死（事件公布后反而下跌）
        pct_chg = s.get("pct_chg", 0)
        if pct_chg < -3 and hold_days <= 2:
            return {
                "action": "sell",
                "reason": "利好公布后大跌，见光死",
                "urgency": "immediate",
            }

        # 3. 止损
        if cost and close < cost * 0.95:
            return {
                "action": "sell",
                "reason": f"亏损{(close/cost-1)*100:.1f}%，触及止损",
                "urgency": "immediate",
            }

        # 4. 时间止损
        if hold_days >= max_hold:
            return {
                "action": "sell",
                "reason": f"持仓{hold_days}天，事件驱动时间止损",
                "urgency": "normal",
            }

        return None


# ---------------- 事件数据获取（示例接口） ----------------
def parse_announcement(announcement: dict) -> Optional[Event]:
    """解析公告数据为 Event 对象。

    这是一个示例实现，实际需要根据数据源格式调整。
    """
    title = announcement.get("title", "")
    content = announcement.get("content", "")

    # 简单的事件类型识别（实际应该用更复杂的NLP或规则）
    event_type = None
    is_positive = True

    if "预增" in title or "业绩" in title:
        event_type = "earnings_surprise"
    elif "政策" in title or "补贴" in title:
        event_type = "policy_support"
    elif "股权激励" in title:
        event_type = "equity_incentive"
    elif "增持" in title:
        event_type = "insider_buy"
    elif "调研" in title:
        event_type = "institution_research"
    elif "重组" in title or "并购" in title:
        event_type = "merger_acquisition"
    elif "合同" in title or "中标" in title:
        event_type = "contract_win"
    elif "回购" in title:
        event_type = "buyback"
        is_positive = True  # 回购是利好
    elif "减持" in title:
        is_positive = False  # 减持是利空
        return None

    if not event_type:
        return None

    return Event(
        event_type=event_type,
        code=announcement.get("code", ""),
        name=announcement.get("name", ""),
        title=title,
        content=content[:200],
        publish_time=datetime.fromisoformat(announcement.get("time", datetime.now().isoformat())),
        source=announcement.get("source", "unknown"),
        impact_level=EVENT_TYPES.get(event_type, {}).get("weight", 0.5) > 0.8 and "high" or "medium",
        is_positive=is_positive,
    )
