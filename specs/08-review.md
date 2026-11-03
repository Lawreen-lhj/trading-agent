# Spec 08 · 盘后复盘

> 模块：`src/review/`（reviewer）+ 入口 `run_review.py`
> 定位：把"计划→执行→复盘→改进"串成闭环。产出目标不是报告本身，而是**改进动作**。

## 能力概述

对比"盘前预判"与"实际收盘"，评估判断准确性、选股质量、策略有效性和执行纪律。避免同样错误反复犯（如某策略连续负期望→暂停、止损执行率<100%→强制自动止损、冲动交易>0→加下单确认）。

## 四维模型

### 维度1 市场复盘 MarketReview —— 我判断对了市场情绪吗？
- 情绪准确率 = max(0, 1−|盘前分−实际分|/50)。
- 板块命中率 = 命中数/预判板块数（对比盘前与实际 sectors.top）。
- 仓位合理性 = |实际仓位−建议仓位|<0.2。
- 另含 predicted/actual_phase、market_signals、lessons。

### 维度2 个股复盘 StockReview —— 我选对了吗？
- 逐只记录盘前 pre_score/pre_label/pre_plan，实际 actual_return/high/low/close，交易执行 traded/entry_price/exit_price/pnl。
- 计划执行判定 plan_executed：仅当 pre_plan.ready 且实际买入价落在 [buy_lo,buy_hi] 内为 True；否则记 deviation_reason（买入价不在区间/有计划未执行/无计划却交易=冲动交易）。

### 维度3 策略复盘 StrategyReview —— 策略还有效吗？
- 对四策略统计：胜率、平均盈利/亏损、盈亏比、期望值(=胜率×均盈−败率×|均亏|)、最大回撤。
- 衰减检测：近20日期望 < 近60日期望×0.7 → is_decaying。
- 建议优先级：期望<0→**pause**；衰减→**optimize**；胜率<0.4→**reduce**；否则→**continue**。

### 维度4 行为复盘 BehaviorReview —— 我按计划执行了吗？
- 计划执行率、止损执行率(执行止损数/触发止损数)、平均持仓天数、超时持仓数。
- 情绪化操作：追高/恐慌卖出/报复性交易计数。
- **纪律性评分**：从100扣分——冲动交易每次−15、止损不执行−(1−compliance)×30、超时持仓−10、追高−10、恐慌卖出−10，下限0。

## generate_daily_review 输入/输出

输入：`date_str`、`pre_brief`(盘前简报)、`post_data`(收盘数据)、`trades`(当日交易)、`holdings`(持仓)、`strategy_history`(历史交易，用于累计统计)。

输出 `DailyReview`：market/stocks/strategies/behavior 四维 + `overall_score` + `key_insights[]` + `action_items[]` + created_at。

**综合评分（加权）**：市场判断30%(情绪准确率×100) + 选股质量25% + 策略有效性25% + 行为纪律20%(discipline_score)。

## 周汇总（--week / run_weekly_review）

回溯最近5个工作日，从 reviews 表读回汇总：平均综合评分、复盘天数、每日概览表、策略表现汇总（累加笔数/盈亏/取最新建议）、行为改进追踪。写入 `data/reviews/weekly_{最早日}.md`。

## 报告格式与落库

- **文件**：以 `data/reviews/{day}` 为基，同时写 `.json`（结构化）和 `.md`（人类可读，六节：市场/个股/策略/行为/关键洞察/待办事项）。
- **落库**：`Store.save_review`（INSERT OR REPLACE）写 `market.db` 的 **reviews 表**：run_date(PK)/overall_score/market_json/stocks_json/strategies_json/behavior_json/insights_json/actions_json/created_at。读回 review_by_date / reviews_recent。

## 触发方式

- **CLI**（run_review.py）：`--date`(缺省今天)、`--week`(周汇总)、`--quiet`。单日流程：加载 `data/briefings/{day}.json` → 构建 post_data（从 brief + signals_by_date + simulation.db 仓位）→ 加载 trades/holdings → generate_daily_review → 保存文件+落库。缺简报则报错退出码1。
- **Web**：POST `/api/review/run`（同步触发）、GET `/api/review/latest|history`。
- **auto_review**：config.review.auto_review(true) 声明"简报后自动触发"，但**代码中未见任何流程消费该键**——目前是声明性配置，尚未接线到简报流水线。真实入口是 CLI 与 Web。

> 实现注记：`_load_strategy_history` 当前为简化桩，恒返回空列表，故策略累计统计目前只覆盖当日交易，衰减检测在缺历史时区分度有限。

## 配置项（config.yaml → review）

enabled(true)、auto_review(true)、report_format(markdown)、save_path(data/reviews)。
