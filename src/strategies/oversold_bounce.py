"""超跌反弹策略 —— 情绪冰点期的防守反击打法。

核心逻辑：
  市场恐慌时，好股票也会被错杀。
  超跌反弹的本质是"情绪修复"，不是"趋势反转"。
  所以这个策略的核心是：快进快出，不贪。

  冰点期最危险的不是买入，而是"不敢买"。
  当所有人都恐慌时，往往是短期底部。
  但底部不等于反转，反弹后可能继续跌。
  所以仓位要轻，止损要严，止盈要快。

适用环境：
  - 情绪冰点/退潮期（涨停<30家，跌停>50家）
  - 市场恐慌情绪蔓延（中位数涨幅<-1%）
  - 但：ETF资金开始流入、跌停板打开（修复信号）

选股条件：
  1. 前期强势：20日内有过涨停（说明有资金关注）
  2. 回调充分：从高点回撤>15%（恐慌已释放）
  3. 超卖确认：RSI6<20（短期极度超卖）
  4. 止跌信号：收盘位置>0.6（收在日内高位）
  5. 结构未完全破：收盘仍在MA60之上（长期趋势还在）

买入时机：
  - 恐慌杀跌后出现长下影线
  - 跌停板打开（情绪修复信号）
  - RSI6从<15回升至>20

卖出时机：
  - 反弹至MA5/MA10即减仓（不贪）
  - 盈利>5%即可止盈
  - 止损：继续创新低，或亏损>4%
  - 时间止损：最多持有5天

风控要点：
  - 仓位：单票≤15%，总仓位≤40%（防守）
  - 止损必须严格：冰点期不确定性极高
  - 不补仓：错了就认，不摊低成本
  - 快进快出：这是反弹，不是反转
"""
from __future__ import annotations

from typing import Optional

from src.strategies.base import BaseStrategy, TradeSignal, register_strategy


@register_strategy
class OversoldBounceStrategy(BaseStrategy):
    """超跌反弹策略"""

    name = "oversold_bounce"
    description = "市场冰点期，前期强势股超跌后抢反弹"
    suitable_phases = ["冰点", "退潮"]
    max_hold_days = 5
    min_rr = 1.8

    # 策略参数
    MIN_DRAWDOWN = 0.15       # 最小回撤幅度 15%
    MAX_RSI6 = 25             # 最大 RSI6（超卖）
    MIN_CLOSE_POS = 0.55      # 最小收盘位置（止跌信号）
    MAX_BIAS5 = -0.05         # 最大 BIAS5（必须偏离均线）
    REQUIRE_PRIOR_STRENGTH = True  # 要求前期强势

    def scan(self, stocks: list[dict], market: dict) -> list[TradeSignal]:
        """扫描超跌反弹机会"""
        signals = []

        # 检查市场环境（必须是冰点或退潮）
        if not self._market_suitable(market):
            return signals

        for s in stocks:
            sig = self._evaluate_stock(s, market)
            if sig:
                signals.append(sig)

        signals.sort(key=lambda x: -x.strength)
        return signals[:8]  # 冰点期机会少，最多8个

    def _market_suitable(self, market: dict) -> bool:
        """检查市场环境"""
        sentiment = market.get("sentiment", {})
        phase = sentiment.get("phase", "中性")

        # 只适用于冰点和退潮期
        if phase not in ("冰点", "退潮"):
            return False

        # 检查修复信号（可选，但有的话更好）
        factors = sentiment.get("factors", {})
        limit_down = factors.get("limit_down", {}).get("value", 0)
        limit_up = factors.get("limit_up", {}).get("value", 0)

        # 如果跌停还很多且涨停极少，说明还在恐慌中，可以再等等
        # 但如果有跌停板打开的迹象，可以开始布局
        if limit_down > 80 and limit_up < 15:
            # 极度恐慌，信号强度会降低但不完全排除
            pass

        return True

    def _evaluate_stock(self, s: dict, market: dict) -> Optional[TradeSignal]:
        """评估单只股票的超跌反弹机会"""
        code = s.get("code", "")
        name = s.get("name", "")
        close = s.get("close")

        if not close:
            return None

        rsi6 = s.get("rsi6")
        drawdown60 = s.get("drawdown60")
        close_pos = s.get("close_pos")
        bias5 = s.get("bias5")
        ma60 = s.get("ma60")
        ma20 = s.get("ma20")
        high20 = s.get("high20")
        vol_ratio = s.get("vol_ratio")
        ret20 = s.get("ret20")

        # 1. 超卖确认：RSI6 < 25
        if rsi6 is None or rsi6 > self.MAX_RSI6:
            return None

        # 2. 回调充分：从高点回撤 > 15%
        if drawdown60 is None or drawdown60 > -self.MIN_DRAWDOWN:
            return None

        # 3. 止跌信号：收盘位置 > 0.55
        if close_pos is not None and close_pos < self.MIN_CLOSE_POS:
            return None

        # 4. 偏离均线：BIAS5 < -5%（必须超跌）
        if bias5 is not None and bias5 > self.MAX_BIAS5:
            return None

        # 5. 长期结构未完全破：收盘仍在 MA60 之上
        if ma60 and close < ma60 * 0.95:
            return None  # 长期趋势也破了，不是超跌而是下跌趋势

        # 6. 前期强势检查（可选但推荐）
        if self.REQUIRE_PRIOR_STRENGTH:
            # 20日内有过涨停，或者 ret20 > 0（之前是涨的）
            had_limit_up = s.get("had_limit_up_20d", False)
            if not had_limit_up and ret20 is not None and ret20 < -0.10:
                # 之前一直在跌，不是"超跌"而是"弱势"
                return None

        # 7. 不能是放量下跌（那是出货，不是洗盘）
        pct_chg = s.get("pct_chg", 0)
        if vol_ratio and vol_ratio > 2.0 and pct_chg < -3:
            return None  # 放量暴跌，不接飞刀

        # 计算信号强度
        strength = self._calc_strength(s, rsi6, drawdown60, market)

        # 冰点期信号强度打折（不确定性高）
        phase = market.get("sentiment", {}).get("phase", "")
        if phase == "冰点":
            strength *= 0.8

        # 生成交易计划
        # 买点：当前价附近，或等进一步下探
        atr14 = s.get("atr14")
        if atr14:
            buy_lo = close - 0.5 * atr14
            buy_hi = close + 0.3 * atr14
        else:
            buy_lo = close * 0.98
            buy_hi = close * 1.02

        # 止损：继续创新低即出，或亏损>4%
        stop = min(close * 0.96, (s.get("low60") or close * 0.95))

        # 目标：反弹至 MA5 或 MA10
        ma5 = s.get("ma5")
        ma10 = s.get("ma10")
        target1 = ma5 if ma5 and ma5 > close else close * 1.05
        target2 = ma10 if ma10 and ma10 > close else close * 1.08

        risk = close - stop
        if risk <= 0:
            return None

        rr = (target1 - close) / risk
        if rr < self.min_rr:
            return None

        # 仓位（冰点期仓位要轻）
        capital = self.config.get("capital", 100000)
        risk_pct = self.config.get("single_risk_pct", 1.0) * 0.7  # 再降30%
        shares = self.calc_position_size(capital, risk_pct, close, stop)

        reason = f"超跌反弹：RSI6={rsi6:.0f}，回撤{abs(drawdown60)*100:.0f}%，止跌企稳"

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
            hold_days=self.max_hold_days,
            confidence=strength,
            market_phase=phase,
            extra={
                "rsi6": round(rsi6, 1),
                "drawdown60": round(drawdown60 * 100, 1),
                "close_pos": round(close_pos, 2) if close_pos else None,
                "had_limit_up_20d": s.get("had_limit_up_20d", False),
            },
        )

    def _calc_strength(
        self,
        s: dict,
        rsi6: float,
        drawdown60: float,
        market: dict,
    ) -> float:
        """计算信号强度"""
        strength = 0.4  # 基础分较低（超跌反弹不确定性高）

        # RSI 越低越强
        if rsi6 < 15:
            strength += 0.20
        elif rsi6 < 20:
            strength += 0.15
        else:
            strength += 0.10

        # 回撤越深，反弹空间越大
        if drawdown60 < -0.30:
            strength += 0.15
        elif drawdown60 < -0.20:
            strength += 0.10

        # 收盘位置（止跌信号）
        close_pos = s.get("close_pos", 0.5)
        if close_pos > 0.8:
            strength += 0.10  # 长下影或收在高位

        # 前期强势（有资金关注过）
        if s.get("had_limit_up_20d"):
            strength += 0.10

        # 市场修复信号
        sentiment = market.get("sentiment", {})
        signals = sentiment.get("signals", [])
        for sig in signals:
            if "修复" in sig or "打开" in sig:
                strength += 0.05
                break

        return max(0, min(1, strength))

    def check_sell_signal(self, s: dict, cost: float, hold_days: int) -> Optional[dict]:
        """检查持仓的卖出信号（超跌反弹要快进快出）"""
        close = s.get("close")
        ma5 = s.get("ma5")
        ma10 = s.get("ma10")
        rsi6 = s.get("rsi6")

        if not close:
            return None

        # 1. 反弹至 MA5/MA10：止盈
        if ma5 and close >= ma5 * 0.98:
            return {
                "action": "sell",
                "reason": f"反弹至MA5（{ma5:.2f}），超跌反弹目标达成",
                "urgency": "normal",
            }

        if ma10 and close >= ma10 * 0.98:
            return {
                "action": "sell",
                "reason": f"反弹至MA10（{ma10:.2f}），止盈离场",
                "urgency": "normal",
            }

        # 2. 盈利>5%：止盈（不贪）
        if cost and close > cost * 1.05:
            return {
                "action": "sell",
                "reason": f"盈利{(close/cost-1)*100:.1f}%，超跌反弹止盈",
                "urgency": "normal",
            }

        # 3. 止损：亏损>4% 或创新低
        if cost and close < cost * 0.96:
            return {
                "action": "sell",
                "reason": f"亏损{(close/cost-1)*100:.1f}%，触及止损",
                "urgency": "immediate",
            }

        low60 = s.get("low60")
        if low60 and close < low60:
            return {
                "action": "sell",
                "reason": "创60日新低，超跌反弹失败",
                "urgency": "immediate",
            }

        # 4. 时间止损：最多5天
        if hold_days >= self.max_hold_days:
            return {
                "action": "sell",
                "reason": f"持仓{hold_days}天，时间止损",
                "urgency": "normal",
            }

        # 5. RSI 回升至正常区间：反弹可能结束
        if rsi6 and rsi6 > 50:
            return {
                "action": "sell",
                "reason": f"RSI6回升至{rsi6:.0f}，超跌修复完成",
                "urgency": "normal",
            }

        return None
