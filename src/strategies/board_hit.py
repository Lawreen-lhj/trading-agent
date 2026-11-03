"""打板/半路策略 —— 情绪高潮期的进攻型打法。

核心逻辑：
  涨停板是 A 股短线最确定的信号之一。
  封板说明多头力量极度强势，次日大概率有溢价。
  但打板的风险也最高：一旦炸板，当日可能浮亏 5-10%。

两种打法：
  1. 打板（排板）：涨停价挂单排队，等封板后买入
     - 优点：确定性高，封板后次日溢价概率大
     - 缺点：排队不一定能成交，炸板风险

  2. 半路板：涨停前介入（涨幅 7-9% 时）
     - 优点：成本低，即使不封板也有空间
     - 缺点：需要判断封板概率，失败率高

适用环境：
  - 情绪回暖/高潮期（涨停>80家，连板高度≥4）
  - 昨涨停今日表现>2%（打板有利可图）
  - 板块效应明显（同板块≥3只涨停）

选股条件：
  - 流通市值 50-300 亿（太小易被操纵，太大封不住）
  - 非一字板（有成交才能买入）
  - 首次涨停或二板（三板以上风险急剧上升）
  - 封单充足（封单金额/流通市值 > 0.3%）

风控要点：
  - 仓位：单票≤15%，总仓位≤60%
  - 止损：次日低开>3%即出，或跌破涨停价
  - 止盈：次日冲高不封板即出，或持有至第3日
  - 时间止损：最多持有3天
"""
from __future__ import annotations

from typing import Optional

from src.strategies.base import BaseStrategy, TradeSignal, register_strategy


@register_strategy
class BoardHitStrategy(BaseStrategy):
    """打板/半路策略"""

    name = "board_hit"
    description = "涨停板打板或半路介入，适合情绪高潮期"
    suitable_phases = ["回暖", "高潮", "中性"]
    max_hold_days = 3
    min_rr = 1.5  # 打板盈亏比要求可以低一些（胜率高）

    # 策略参数
    MIN_FLOAT_CAP = 50e8      # 最小流通市值 50 亿
    MAX_FLOAT_CAP = 300e8     # 最大流通市值 300 亿
    MIN_SEAL_RATIO = 0.003    # 最小封单比例 0.3%
    MAX_BOARD_HEIGHT = 3      # 最大连板数（超过3板风险太高）
    MIN_SECTOR_LIMIT = 2      # 同板块最少涨停数

    def scan(self, stocks: list[dict], market: dict) -> list[TradeSignal]:
        """扫描打板/半路机会"""
        signals = []

        # 检查市场环境
        if not self._market_suitable(market):
            return signals

        for s in stocks:
            sig = self._evaluate_stock(s, market)
            if sig:
                signals.append(sig)

        # 按信号强度排序
        signals.sort(key=lambda x: -x.strength)
        return signals[:10]  # 最多返回 10 个信号

    def _market_suitable(self, market: dict) -> bool:
        """检查市场环境是否适合打板"""
        sentiment = market.get("sentiment", {})
        phase = sentiment.get("phase", "中性")

        # 冰点和退潮期不打板
        if phase in ("冰点", "退潮"):
            return False

        # 昨涨停今日表现太差
        yesterday_perf = sentiment.get("factors", {}).get("yesterday_limit_up", {}).get("value", 0)
        if yesterday_perf < -1:
            return False

        # 涨停家数太少
        limit_up = sentiment.get("factors", {}).get("limit_up", {}).get("value", 0)
        if limit_up < 40:
            return False

        return True

    def _evaluate_stock(self, s: dict, market: dict) -> Optional[TradeSignal]:
        """评估单只股票的打板机会"""
        code = s.get("code", "")
        name = s.get("name", "")
        close = s.get("close")
        pct_chg = s.get("pct_chg", 0)
        limit_pct = s.get("limit_pct", 10)

        if not close or not pct_chg:
            return None

        # 1. 涨停或接近涨停判断
        is_limit_up = pct_chg >= limit_pct * 0.98
        is_near_limit = limit_pct * 0.7 <= pct_chg < limit_pct * 0.98

        if not (is_limit_up or is_near_limit):
            return None

        # 2. 流通市值过滤
        float_cap = s.get("float_market_cap") or s.get("market_cap", 0)
        if float_cap < self.MIN_FLOAT_CAP or float_cap > self.MAX_FLOAT_CAP:
            return None

        # 3. 一字板排除（无法买入）
        open_ = s.get("open")
        high = s.get("high")
        low = s.get("low")
        if open_ and high and low and open_ == high == low == close:
            return None  # 一字板

        # 4. 连板高度检查
        board_height = s.get("board_height", 1)  # 默认首板
        if board_height > self.MAX_BOARD_HEIGHT:
            return None  # 高位板风险太大

        # 5. 封单检查（仅对已涨停的）
        if is_limit_up:
            seal_amount = s.get("seal_amount", 0)
            if float_cap > 0 and seal_amount / float_cap < self.MIN_SEAL_RATIO:
                return None  # 封单不足

        # 6. 板块效应检查
        sector_limit_count = s.get("sector_limit_count", 0)
        if sector_limit_count < self.MIN_SECTOR_LIMIT:
            # 没有板块效应，降低信号强度但不完全排除
            pass

        # 计算信号强度
        strength = self._calc_strength(s, is_limit_up, board_height, sector_limit_count)

        # 生成交易计划
        direction = "buy"
        if is_limit_up:
            # 打板：涨停价买入
            buy_price = close
            reason = f"涨停封板，{board_height}连板，封单充足"
        else:
            # 半路：当前价附近买入
            buy_price = close
            reason = f"涨幅{pct_chg:.1f}%，接近涨停，半路介入"

        # 止损：次日低开>3%或跌破涨停价
        stop = buy_price * 0.97 if is_limit_up else buy_price * 0.95

        # 目标：次日溢价
        target1 = buy_price * 1.05  # 5%溢价
        target2 = buy_price * 1.10  # 10%溢价

        # 盈亏比
        risk = buy_price - stop
        reward = target1 - buy_price
        rr = reward / risk if risk > 0 else 0

        if rr < self.min_rr:
            return None

        # 仓位计算
        capital = self.config.get("capital", 100000)
        risk_pct = self.config.get("single_risk_pct", 1.0)
        shares = self.calc_position_size(capital, risk_pct * 0.8, buy_price, stop)  # 打板仓位再降20%

        return TradeSignal(
            strategy=self.name,
            code=code,
            name=name,
            direction=direction,
            strength=strength,
            price=close,
            reason=reason,
            buy_lo=round(buy_price * 0.99, 2),
            buy_hi=round(buy_price * 1.01, 2),
            stop=round(stop, 2),
            target1=round(target1, 2),
            target2=round(target2, 2),
            rr=round(rr, 2),
            shares=shares,
            hold_days=self.max_hold_days,
            confidence=strength,
            market_phase=market.get("sentiment", {}).get("phase", ""),
            extra={
                "board_height": board_height,
                "is_limit_up": is_limit_up,
                "sector_limit_count": sector_limit_count,
                "seal_ratio": round(s.get("seal_amount", 0) / float_cap, 4) if float_cap else 0,
            },
        )

    def _calc_strength(
        self,
        s: dict,
        is_limit_up: bool,
        board_height: int,
        sector_limit_count: int,
    ) -> float:
        """计算信号强度 0-1"""
        strength = 0.5  # 基础分

        # 涨停比半路强
        if is_limit_up:
            strength += 0.15

        # 首板比连板安全（但连板说明强势）
        if board_height == 1:
            strength += 0.10  # 首板溢价确定性高
        elif board_height == 2:
            strength += 0.05  # 二板也可以
        else:
            strength -= 0.10  # 三板以上风险大

        # 板块效应
        if sector_limit_count >= 5:
            strength += 0.15
        elif sector_limit_count >= 3:
            strength += 0.10

        # 封单充足度
        seal_ratio = s.get("seal_ratio", 0)
        if seal_ratio > 0.01:
            strength += 0.10
        elif seal_ratio > 0.005:
            strength += 0.05

        # 量比（放量涨停更可靠）
        vol_ratio = s.get("vol_ratio", 1)
        if 1.5 <= vol_ratio <= 3.0:
            strength += 0.05
        elif vol_ratio > 5:
            strength -= 0.10  # 异常放量可能是出货

        return max(0, min(1, strength))

    def validate(self, signal: TradeSignal, current_data: dict) -> bool:
        """盘中验证：涨停是否仍然封住"""
        if not signal.extra.get("is_limit_up"):
            return super().validate(signal, current_data)

        # 打板信号：检查是否炸板
        current_pct = current_data.get("pct_chg", 0)
        limit_pct = current_data.get("limit_pct", 10)

        if current_pct < limit_pct * 0.95:
            return False  # 炸板了

        return True
