# Spec 03 · 情绪周期

> 模块：`src/sentiment.py`（+ 数据来自 `src/extras.py`，见 Spec 01）
> 定位：短线交易的**环境判断层**——决定"能不能做"和"用什么策略做"，比指数涨跌更重要。

## 能力概述

指数涨 1% 但只有 800 家上涨，短线是亏钱的行情；指数跌 0.5% 但涨停 80 家、连板 5 板，短线是赚钱的行情。情绪决定的是能不能做，指数只决定仓位上限。

情绪周期五阶段（短线核心框架）：**冰点 → 修复 → 回暖 → 高潮 → 退潮 → 冰点 ...**

| 阶段 | 特征 | 仓位建议 | 偏好策略 |
|------|------|----------|----------|
| 冰点 ICE | 涨停<30、跌停>50、连板≤2、昨涨停今表现<0% | 0-20% | oversold_bounce |
| 退潮 EBB | 涨停30-50、跌停20-50、连板2-3、昨涨停-2%~0% | 10-40% | oversold_bounce, trend_pullback |
| 中性 NEUTRAL | 涨停50-80、跌停<20、连板3-4、昨涨停0%~2% | 30-70% | trend_pullback, board_hit |
| 回暖 WARM | 涨停80-120、跌停<10、连板4-6、昨涨停2%~5% | 50-90% | board_hit, trend_pullback, event_driven |
| 高潮 CLIMAX | 涨停>120、连板≥6、昨涨停>5% | 30-60%（高潮反而降仓） | board_hit, event_driven（快进快出） |

## 一、综合情绪分 `calc_sentiment_score(...)`

输入 8 个指标，输出 0-100 分 + 阶段 + 仓位区间 + 偏好策略 + 因子明细 + 信号。

**七因子加权**（权重固定）：

| 因子 | 权重 | 甜点/规则 |
|------|------|-----------|
| 涨停家数 limit_up | 25% | 60-120 最活跃，>150 反降（过亢奋） |
| 跌停家数 limit_down | 15% | 反向，越多越恐慌分越低 |
| 涨跌比 up_ratio | 15% | 0.65+ 高分 |
| 中位数涨幅 median_pct | 10% | 0.8%~2% 最优 |
| 连板高度 max_board_height | 15% | 6板满分，>8板警惕 |
| 昨涨停今表现 yesterday_limit_up_perf | 15% | **情绪最敏感指标**：昨天打板的人今天赚不赚钱 |
| 成交额 total_amount | 5% | 万亿以上活跃 |

**北向资金修正**（不纳入主分）：净流出>50亿 −5分并记信号；净流入>50亿 +3分。

**阶段判定 `_determine_phase`**：不只看分数，还看结构——分数高但跌停也多→分歧非高潮；分数低但连板高→局部热点非冰点。

## 二、精确打板情绪（V2 核心增强）

数据来自 `extras.limit_up_sentiment()`（push2ex 精确池，见 Spec 01），替代旧的快照推断：

- **炸板率 break_rate** = 炸板家数/(涨停+炸板)：封板意愿强弱，>40% 说明接力差。
- **晋级率 promotion_rate** = 昨涨停今仍涨停/昨涨停总数：赚钱效应延续性。
- **连板梯队 ladder / board_ladder**：各高度家数分布 + 连板龙头（code/name/height/industry），情绪天花板。
- **昨涨停今表现 yesterday_perf**：打板策略盈亏反馈。

这些精确指标经 `collect_sentiment_data()` 汇入，再由 `run_brief.py` 写入 `brief.sentiment`，最终在简报情绪卡片和 Web 仪表盘展示。

## 三、情绪趋势 `calc_sentiment_trend(history)`

输入近 N 日情绪分序列，输出 `{direction(上升/下降/震荡), momentum(近5日回归斜率), consecutive_days, signal}`。用于判断情绪修复中还是恶化中。

## 四、板块情绪 `calc_sector_sentiment(sector_data)`

输入板块列表，输出 `{hot_sectors(涨停最多), strong_sectors(涨幅最大), weak_sectors, rotation_signal, concentration(热度集中度HHI)}`。集中度高→主线明确跟随热点；低→轮动快不宜追高。

## 五、集成入口 `enhance_market_temp(breadth, extra)`

把基础市场温度（`shortterm.market_temp`）与情绪周期融合：
- 输入 `breadth`（涨跌家数）+ `extra`（max_board_height / yesterday_limit_up_perf / north_net_flow / sector_data）。
- 内部调 `calc_sentiment_score` + `calc_sector_sentiment`。
- 输出在基础温度上追加：`sentiment_score / sentiment_phase / position_range / preferred_strategies / sentiment_factors / sentiment_signals / sector_sentiment`。

## 触发方式与数据流

`run_brief.py` 的「1.5 情绪周期增强」段：
1. `extras.collect_sentiment_data()` 采集精确数据。
2. 组装 `sentiment_extra`（连板高度/昨涨停表现/北向）。
3. `enhance_market_temp(breadth, sentiment_extra)` 得增强温度 `temp`。
4. `temp.sentiment_phase` 作为 `market_phase` 驱动后续策略启停（见 Spec 05）。
5. 情绪数据落 `sentiment_history` 表，供复盘核对判断准确性（见 Spec 08）。

## 配置项（config.yaml → sentiment）

- `enabled`: 开关（默认 true）
- `board_height_sample`: 连板高度抽样请求数上限（50）
- `north_flow_threshold`: 北向影响总分的阈值（50亿）
