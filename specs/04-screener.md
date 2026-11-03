# Spec 04 · 选股发现引擎

> 模块：`src/screener.py`
> 定位：解决"自选池太窄、抓不住当日主线龙头"——盘前从全市场 5000+ 只里筛出可执行候选池。

## 能力概述

三层漏斗从全市场扩池，再复用 `shortterm.score()` 打分排序，输出 Top N（默认 30）只候选，作为现有评分/计划模型的**输入扩展**（不替代它）。设计调用链：`screen_stocks()` 输出候选 → `shortterm.score()` 评分 → `shortterm.plan()` 生成计划。

## 触发方式

- 配置开关：`screener.enabled`(true)、`screener.top_n`(30)。
- **独立入口（已打通）**：`run_screen.py` 是选股引擎的专用运行器，采用**两段漏斗**规避逐只取日线的限流：第一段用轻量全市场快照做硬过滤 + 启发式预排序，收窄到 `--top`（默认30）只；第二段只为这批候选取日线算完整指标，再交 `screen_stocks()` 打分、`shortterm.plan()` 出计划。支持 `--write`（经 configstore 备份后合并/替换回写 config.yaml watchlist）、`--save`（落 `screening_results` 表）。
- **数据源降级**：`build_universe()` 首选东财 push2 全市场快照（`em.market_list`，需 ≥3000 行才可信）；被限流时自动降级到 push2ex **涨停板池 + 炸板池**（短线最强域，保留 limit_days/seal_fund/break_times/industry）。第二段日线东财 push2his 被限流时自动切腾讯备源 `tx.kline`。
- **与 run_brief 的关系**：`run_brief.py` 已 import `screen_stocks`，但主流程 `build_brief()` 仍未调用它——每日简报的策略扫描标的依旧来自 watchlist。选股走独立的 `run_screen.py`，其输出通过 `--write` 回写 watchlist 间接影响后续简报。将 screener 直接串入 run_brief 流水线仍是一处待打通点。

## 输入

`screen_stocks(all_stocks, market_sentiment, config)`：
- `all_stocks`：全市场股票快照列表（与自选股走同一套 indicators 快照）。硬过滤依赖 code/name/close/pct_chg/market_cap/amount/turnover；量价与位置层还需 vol_ratio/turnover_ratio/high20/low20/ma5/ma10/ma20/rsi6/ret5/bias5/atr_pct/boll_pos/close_pos 等。
- `market_sentiment`：情绪结果，关键读 `phase`。
- `config`：screener 配置段。

## 处理逻辑（三层漏斗）

**第一层 硬性过滤**（排除不可交易）：ST/*ST、上市<min_list_days(60)、停牌、一字板（开=收=高=低且|涨跌|>9）、市值<min_market_cap(30亿)、日均成交<min_avg_amount(5000万)。产出各类排除计数。

**第二层 市场环境过滤**（按情绪 phase）：
- 冰点：只保留超跌反弹候选（rsi6<30 且 close>ma20×0.9）。
- 高潮：排除过热标的（ret5≤0.25 且 bias5≤0.12）。
- 其余 phase：正常筛选。

**第三层 个股筛选**：
- 3a 量价：量比区间[1.0,5.0]、相对换手≥0.8、涨时需放量。
- 3b 价格位置：距20日高点≤20%、距20日低点≥5%、站上MA5；回暖/高潮期额外要求均线多头排列。
- 3c 形态识别（**加分不过滤**）：6 类形态——平台突破、缩量回踩、放量长阳、N字形态、尾盘强势、突破布林上轨。
- 3d 打分排序：对每只候选调 `shortterm.score()`，按分降序取前 top_n。

## 输出

`screen_stocks` 返回 dict：
- 顶层：`candidates`(已排序)、`stats`(各层计数)、`market_filter`、`patterns_found`({code:[形态名]})、`total_screened`、`final_count`。
- 每条候选：原始快照全字段 + `screen_score`/`screen_label`/`screen_parts`/`patterns`。
- `format_screen_result(result)`：输出可读文本（前20只）。
- **入选理由（run_screen.py）**：`build_reasons(cand)` 把 `screen_parts` 六分项（动量/量价/形态/结构/空间/过热）+ 快照指标证据（RET5/量比/换手倍数/距20日高/均线排列/MA10斜率/布林位置/尾盘分位/ATR%/连板/封单/行业）翻译成逐条带数字的理由；报告逐票打印，并随记录存入 `screening_results.detail_json`。原则：每个判断都给出触发它的数值，不说"形态好"这类无法核对的空话。
- 落库：设计落 `screening_results` 表（run_date/code/name/layer1_pass/layer2_pass/layer3_pass/patterns/score/rank/detail_json），经 `Store.save_screening()`。

## 配置项（config.yaml → screener）

- `enabled`(true)、`top_n`(30)
- `hard_filters`：exclude_st(true)、min_list_days(60)、exclude_suspended(true)、exclude_one_word(true)、min_market_cap(30亿)、min_avg_amount(5000万)
- `volume_price`：min_vol_ratio(1.0)、max_vol_ratio(5.0)、min_turnover_ratio(0.8)
- `price_position`：max_distance_to_high20(0.20)、min_distance_to_low20(0.05)、require_above_ma5(true)

> 实现注记：量价/位置层当前用函数默认参数，未读取 config 的 volume_price/price_position 段；`save_screening` 期望的记录字段与 `screen_stocks` 输出结构需调用方适配。
