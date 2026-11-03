# Spec 05 · 多策略框架

> 模块：`src/strategies/`（base + board_hit + trend_pullback + oversold_bounce + event_driven）
> 定位：四种短线打法，核心理念是"匹配"——把正确策略用在正确的情绪阶段。

## 能力概述

统一信号格式（`TradeSignal`）、统一注册/选择机制，给出随情绪变化的仓位建议。输出信号兼容 `shortterm.plan()`，可直接接入简报渲染与数据库。

## 注册与选择机制

- **注册**：`base.py` 维护全局注册表，各策略类用 `@register_strategy` 装饰器注册，`__init__.py` 触发导入。
- **`get_all_strategies(config)`**：按 `config.strategies.<name>.enabled`（默认 True）过滤并实例化，返回启用策略列表。**run_brief 实际使用的入口**。
- **`select_strategies(phase, score, config)`**：按情绪阶段给推荐组合 `[(策略名,权重)]`（权重和1.0）——冰点全给超跌反弹；高潮打板为主。设计用途，run_brief 当前未消费权重。
- **`is_suitable(phase)`**：按 `suitable_phases` 判断策略是否适用当前情绪，run_brief 扫描前用它跳过不适用策略。
- **`suggest_position(phase,score,strategy,capital)`**：基于情绪仓位区间插值，打板/超跌单票降至15%，高潮期总仓位封顶60%。

## Signal 数据结构（TradeSignal）

| 字段 | 含义 |
|------|------|
| strategy | 策略名 |
| code / name | 代码 / 名称 |
| direction | "buy" / "sell" |
| strength | 信号强度 0-1，用于排序 |
| price | 当前价 |
| buy_lo / buy_hi | 买入区间下/上限 |
| stop | 止损价 |
| target1 / target2 | 第一/第二目标价 |
| rr | 盈亏比 |
| shares | 建议股数（固定风险法，取整100股，单票≤30%） |
| hold_days | 建议持仓天数 |
| confidence | 置信度 0-1 |
| market_phase | 触发时情绪阶段 |
| reason | 触发原因文本 |
| urgency | immediate/normal/watch |
| extra | dict，各策略自定义元数据 |

`validate()` 校验买价是否仍在区间±3%（打板改为校验是否炸板）；非打板策略另有 `check_sell_signal()` 做持仓卖出判断。

## 四个策略

### 1. 打板/半路 board_hit
- **适用阶段**：回暖/高潮/中性；max_hold=3；min_rr=1.5。
- **市场门槛**：冰点/退潮不做；昨涨停今表现<−1% 不做；涨停家数<40 不做。
- **入场**：涨停或半路（涨幅达涨停70%-98%）；流通市值50-300亿；排除一字板；连板≤3；已涨停需封单比≥0.003。
- **买卖点**：buy=close；止损 涨停×0.97/半路×0.95；target1×1.05、target2×1.10；仓位用 single_risk_pct×0.8。
- **extra**：board_height/is_limit_up/sector_limit_count/seal_ratio。scan 最多10条。

### 2. 趋势回踩 trend_pullback
- **适用阶段**：中性/回暖/退潮；max_hold=10；min_rr=2.0；冰点不做。
- **入场**：多头排列 ma5>ma10>ma20（必须）；MA10斜率≥0；ret20∈[8%,35%]；贴近MA5/MA10（偏离<3%）；不追高 bias5≤5%；缩量 vol_ratio≤0.85；close_pos≥0.4；收盘在MA20上。
- **买卖点**：buy围绕较近均线±1%；止损 min(ma20×0.98, close−1.5ATR)；target1=前高或close+2R；仓位用完整 single_risk_pct。
- **卖出**：跌破MA20/放量破MA10/亏>5%→immediate；BIAS5>10%/接近前高/持仓≥10天→normal。scan 最多15条。

### 3. 超跌反弹 oversold_bounce
- **适用阶段**：冰点/退潮；max_hold=5；min_rr=1.8。
- **入场**：rsi6≤25；回撤 drawdown60≤−15%；close_pos≥0.55；bias5≤−5%；长期结构未破（close≥ma60×0.95）；前期强势；排除放量暴跌（不接飞刀）。冰点期 strength×0.8。
- **买卖点**：buy用ATR区间；止损 min(close×0.96, low60)；target1=MA5或×1.05；仓位用 single_risk_pct×0.7。
- **卖出**：反弹至MA5/MA10或盈>5%→normal止盈；亏>4%或创60日新低→immediate。scan 最多8条。

### 4. 事件驱动 event_driven
- **适用阶段**：全阶段；max_hold=7；min_rr=2.0。
- **事件源**：内部 Event 池（`add_events`，保留最新100条）；事件类型权重表（并购0.95/业绩超预期0.9/政策支持0.85/内部人增持0.75/股权激励0.7/中标0.7/回购0.55 等，各带 hold_days）。
- **入场**：仅处理新鲜事件（age≤48h）且正面；流动性 amount≥1亿；未被充分定价（ret3/ret5≤10%）；技术面配合（close≥ma20×0.9）。
- **买卖点**：buy用ATR区间；止损 close×0.95 但不上破MA20；target 按事件权重放大；仓位用完整 single_risk_pct。
- **卖出**：盈>15%→已定价；公布后跌>3%且持仓≤2天→见光死 immediate；亏>5%→immediate。scan 最多10条（无事件返回空）。

## 触发方式（run_brief.py「3.5 多策略扫描」，已接入）

1. 取 `market_phase = temp.sentiment_phase`。
2. 条件：`strategies.enabled`(true) 且 watch_rows 非空。
3. `get_all_strategies(cfg.strategies)` 加载启用策略。
4. 构建输入：`stock_snaps`（**当前范围是自选池 watchlist，非 screener 全市场候选**）+ `market_data{phase,sentiment_score,temp,sectors}`。
5. 逐策略：`is_suitable(phase)` 跳过不适用；否则 `scan()`，异常记 notes。
6. 每条 TradeSignal 展平汇入 `brief.strategy_signals`。
7. 落库：`store.save_signals(date, signals)` 写 `strategy_signals` 表。

> 集成提示：select_strategies/suggest_position 是框架提供但 run_brief 未消费的能力；策略扫描标的池来自 watchlist 而非 screener 输出，两子系统尚未在主流程打通。

## 配置项（config.yaml → strategies）

每策略含 `enabled` 与参数：board_hit（min/max_float_cap、min_seal_ratio、max_board_height、suitable_phases）、trend_pullback（min/max_ret20、max_bias5、max_vol_ratio）、oversold_bounce（min_drawdown、max_rsi6、min_close_pos、max_bias5）、event_driven（event_types 白名单）。
