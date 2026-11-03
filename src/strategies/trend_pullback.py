"""趋势波段策略 —— 情绪中性期的稳健打法。

核心逻辑：
  上升趋势中，股价不会直线上涨，而是"涨一段、回调一段"。
  回调是上车机会，而不是趋势结束。
  关键判断：回调是"洗盘"还是"出货"？
  
  洗盘特征：缩量回调、不破关键均线、回调幅度有限
  出货特征：放量下跌、跌破均线、反弹无力

适用环境：
  - 情绪中性/回暖期（市场不温不火，个股趋势明确）
  - 不适用于冰点期（趋势容易被破坏）和高潮期（回调可能是见顶）

选股条件：
  1. 趋势确认：MA5 > MA10 > MA20 多头排列
  2. 中期涨幅适中：近20日涨幅 10%-30%（太少没趋势，太多已过热）
  3. 回调到位：价格回踩至 MA5 或 MA10 附近（偏离<3%）
  4. 缩量确认：量比 < 0.8（缩量回调是洗盘）
  5. 结构未破：收盘价仍在 MA20 之上

买入时机：
  - 缩量回踩 MA5/MA10 后企稳
  - RSI6 从超卖区（<30）回升
  - 出现止跌 K 线（长下影、十字星、小阳线）

卖出时机：
  - 止盈：涨至前高附近，或 BIAS5 > 10%
  - 止损：跌破 MA20，或亏损 > 5%
  - 时间止损：持仓超过 10 天未走出预期

风控要点：
  - 仓位：单票≤25%，总仓位≤70%
  - 止损必须严格：趋势破了就是破了，不抱幻想
  - 不追高：偏离 MA5 超过 5% 不买入
"""
from __future__ import annotations

from typing import Optional

from src.strategies.base import BaseStrategy, TradeSignal, register_strategy


@register_strategy
class TrendPullbackStrategy(BaseStrategy):
    """趋势波段策略（回踩买入）"""

    name = "trend_pullback"
    description = "多头排列趋势中，缩量回踩均线后买入"
    suitable_phases = ["中性", "回暖", "退潮"]
    max_hold_days = 10
    min_rr = 2.0

    # 策略参数
    MIN_RET20 = 0.08          # 近20日最小涨幅 8%
    MAX_RET20 = 0.35          # 近20日最大涨幅 35%
    MAX_BIAS5 = 0.05          # 最大偏离MA5 5%
    MAX_VOL_RATIO = 0.85      # 最大量比（缩量）
    MIN_CLOSE_POS = 0.4       # 最小收盘位置（日内不能太弱）

    def scan(self, stocks: list[dict], market: dict) -> list[TradeSignal]:
        """扫描趋势回踩机会"""
        signals = []

        # 检查市场环境
        if not self._market_suitable(market):
            return signals

        for s in stocks:
            sig = self._evaluate_stock(s, market)
            if sig:
                signals.append(sig)

        signals.sort(key=lambda x: -x.strength)
        return signals[:15]

    def _market_suitable(self, market: dict) -> bool:
        """检查市场环境"""
        sentiment = market.get("sentiment", {})
        phase = sentiment.get("phase", "中性")

        # 冰点期不做趋势（趋势容易被破坏）
        if phase == "冰点":
            return False

        return True

    def _evaluate_stock(self, s: dict, market: dict) -> Optional[TradeSignal]:
        """评估单只股票的趋势回踩机会"""
        code = s.get("code", "")
        name = s.get("name", "")
        close = s.get("close")

        if not close:
            return None

        ma5 = s.get("ma5")
        ma10 = s.get("ma10")
        ma20 = s.get("ma20")
        ret20 = s.get("ret20")
        vol_ratio = s.get("vol_ratio")
        bias5 = s.get("bias5")
        rsi6 = s.get("rsi6")
        close_pos = s.get("close_pos")
        high20 = s.get("high20")
        atr14 = s.get("atr14")
        slope10 = s.get("ma10_slope3")

        # 1. 多头排列检查（必须）
        if not (ma5 and ma10 and ma20):
            return None
        if not (ma5 > ma10 > ma20):
            return None

        # 2. MA10 必须上行（趋势确认）
        if slope10 is not None and slope10 < 0:
            return None

        # 3. 中期涨幅检查
        if ret20 is not None:
            if ret20 < self.MIN_RET20 or ret20 > self.MAX_RET20:
                return None

        # 4. 价格位置：回踩至 MA5 或 MA10 附近
        if not (ma5 and ma10):
            return None

        dist_ma5 = abs(close - ma5) / ma5 if ma5 else 999
        dist_ma10 = abs(close - ma10) / ma10 if ma10 else 999

        # 必须贴近 MA5 或 MA10（偏离<3%）
        if dist_ma5 > 0.03 and dist_ma10 > 0.03:
            return None

        # 5. 不能偏离 MA5 太远（不追高）
        if bias5 is not None and bias5 > self.MAX_BIAS5:
            return None

        # 6. 缩量确认（量比<0.85）
        if vol_ratio is not None and vol_ratio > self.MAX_VOL_RATIO:
            # 放量回调可能是出货，排除
            return None

        # 7. 收盘位置不能太弱
        if close_pos is not None and close_pos < self.MIN_CLOSE_POS:
            return None

        # 8. 必须在 MA20 之上（结构未破）
        if close < ma20:
            return None

        # 9. RSI 检查：从超卖回升更好
        rsi_recovering = False
        if rsi6 is not None:
            if rsi6 < 30:
                return None  # 还在超卖，等企稳
            elif rsi6 < 45:
                rsi_recovering = True  # 从超卖回升，加分

        # 计算信号强度
        strength = self._calc_strength(s, dist_ma5, dist_ma10, rsi_recovering)

        # 生成交易计划
        # 买点：当前价附近，或回踩 MA5/MA10
        buy_ref = ma5 if dist_ma5 < dist_ma10 else ma10
        buy_lo = min(close, buy_ref) * 0.99
        buy_hi = max(close, buy_ref) * 1.01

        # 止损：MA20 下方，或 ATR 止损
        stop_ma = ma20 * 0.98
        stop_atr = close - 1.5 * atr14 if atr14 else close * 0.95
        stop = min(stop_ma, stop_atr)

        # 目标：前高，或 2R/3R
        risk = close - stop
        if risk <= 0:
            return None

        target1 = high20 if high20 and high20 > close else close + 2 * risk
        target2 = close + 3 * risk

        rr = (target1 - close) / risk
        if rr < self.min_rr:
            return None

        # 仓位
        capital = self.config.get("capital", 100000)
        risk_pct = self.config.get("single_risk_pct", 1.0)
        shares = self.calc_position_size(capital, risk_pct, close, stop)

        reason = f"多头排列，缩量回踩{'MA5' if dist_ma5 < dist_ma10 else 'MA10'}，量比{vol_ratio:.1f}"

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
            market_phase=market.get("sentiment", {}).get("phase", ""),
            extra={
                "dist_ma5": round(dist_ma5 * 100, 2),
                "dist_ma10": round(dist_ma10 * 100, 2),
                "ret20": round(ret20 * 100, 1) if ret20 else None,
                "rsi_recovering": rsi_recovering,
            },
        )

    def _calc_strength(
        self,
        s: dict,
        dist_ma5: float,
        dist_ma10: float,
        rsi_recovering: bool,
    ) -> float:
        """计算信号强度"""
        strength = 0.5

        # 贴近均线程度
        min_dist = min(dist_ma5, dist_ma10)
        if min_dist < 0.01:
            strength += 0.15  # 非常贴近
        elif min_dist < 0.02:
            strength += 0.10

        # 缩量程度
        vol_ratio = s.get("vol_ratio", 1)
        if vol_ratio < 0.5:
            strength += 0.15  # 极度缩量
        elif vol_ratio < 0.7:
            strength += 0.10

        # RSI 回升
        if rsi_recovering:
            strength += 0.10

        # 趋势强度（MA10 斜率）
        slope10 = s.get("ma10_slope3")
        if slope10 and slope10 > 0.02:
            strength += 0.10

        # 收盘位置（日内强势）
        close_pos = s.get("close_pos", 0.5)
        if close_pos > 0.7:
            strength += 0.05

        return max(0, min(1, strength))

    def check_sell_signal(self, s: dict, cost: float, hold_days: int) -> Optional[dict]:
        """检查持仓的卖出信号。

        返回：{"action": "sell", "reason": "...", "urgency": "..."} 或 None
        """
        close = s.get("close")
        ma10 = s.get("ma10")
        ma20 = s.get("ma20")
        bias5 = s.get("bias5")
        vol_ratio = s.get("vol_ratio")
        pct_chg = s.get("pct_chg", 0)

        if not close:
            return None

        # 1. 跌破 MA20：趋势破坏，必须卖
        if ma20 and close < ma20:
            return {
                "action": "sell",
                "reason": f"跌破MA20（{ma20:.2f}），趋势破坏",
                "urgency": "immediate",
            }

        # 2. 放量跌破 MA10：减仓信号
        if ma10 and close < ma10 and vol_ratio and vol_ratio > 1.5:
            return {
                "action": "sell",
                "reason": f"放量跌破MA10，量比{vol_ratio:.1f}",
                "urgency": "immediate",
            }

        # 3. 止损：亏损>5%
        if cost and close < cost * 0.95:
            return {
                "action": "sell",
                "reason": f"亏损{(close/cost-1)*100:.1f}%，触及止损",
                "urgency": "immediate",
            }

        # 4. 止盈：BIAS5>10% 或涨至前高
        if bias5 and bias5 > 0.10:
            return {
                "action": "sell",
                "reason": f"偏离MA5达{bias5*100:.1f}%，短期过热",
                "urgency": "normal",
            }

        high20 = s.get("high20")
        if high20 and close >= high20 * 0.98:
            return {
                "action": "sell",
                "reason": "接近20日前高，阻力位止盈",
                "urgency": "normal",
            }

        # 5. 时间止损
        if hold_days >= self.max_hold_days:
            return {
                "action": "sell",
                "reason": f"持仓{hold_days}天，时间止损",
                "urgency": "normal",
            }

        # 6. 放量滞涨
        if vol_ratio and vol_ratio > 2.0 and abs(pct_chg) < 1.5:
            return {
                "action": "sell",
                "reason": f"放量滞涨，量比{vol_ratio:.1f}但涨幅仅{pct_chg:.1f}%",
                "urgency": "normal",
            }

        return None
