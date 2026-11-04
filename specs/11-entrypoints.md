# Spec 11 · 入口与工作流

> 模块：`run_brief.py`、`run_monitor.py`、`run_review.py`、`run_screen.py`、`run_sim.py`、`run_sim_live.py`、`server.py`、`preview_demo.py`
> 定位：多个命令行入口（简报/监控/复盘/选股/模拟）+ 一个 Web 控制台，串起盘前→盘中→盘后的完整交易日闭环。

## 一、入口清单

所有命令在项目根目录执行，用 `python3`（或 `.venv/bin/python`）。

### run_brief.py —— 盘前简报（主入口）

```bash
python3 run_brief.py                 # 采集→评分→计划→信号→渲染→通知→落库
python3 run_brief.py --no-notify     # 不弹 macOS 通知
python3 run_brief.py --quiet         # 只写文件不打印
python3 run_brief.py --config <path> # 指定配置
```
输出：`data/briefings/YYYY-MM-DD.html`（给人看）+ `.json`（给 agent 读），落库 market.db。

**内部流程（build_brief）**：
1. 市场层：index_quotes + breadth + sector_rank → market_state + market_temp。
2. 情绪增强：collect_sentiment_data → enhance_market_temp → sentiment_data（含炸板率/晋级率/连板梯队/北向）。
3. 外围层：美股/港股/加密（只看不做）。
4. 自选池：daily → indicators.compute/snapshot → shortterm.score/plan/signals。
5. 多策略扫描：get_all_strategies → is_suitable(phase) → scan → strategy_signals。
6. 持仓：止损/时间止损/成本口径。
7. 汇总提醒 + 首屏结论（build_summary，纯规则不调 LLM）。
8. run_once：渲染 HTML+JSON → 落库（save_run/sentiment/signals）→ 存档当日涨停代码 → 通知。

### run_monitor.py —— 盘中监控

```bash
python3 run_monitor.py               # 交易时段轮询（15秒/次，到15:05）
python3 run_monitor.py --test        # 测试模式，立即执行一轮
python3 run_monitor.py --duration 30 # 只监控30分钟
python3 run_monitor.py -v            # 详细日志
```
详见 Spec 07。

### run_review.py —— 盘后复盘

```bash
python3 run_review.py                    # 复盘今日
python3 run_review.py --date 2026-11-03  # 指定日期
python3 run_review.py --week             # 周汇总
python3 run_review.py --quiet
```
详见 Spec 08。

### run_screen.py —— 全市场选股（两段漏斗）

```bash
python3 run_screen.py                     # 打印 Top 30 候选（不写盘）
python3 run_screen.py --top 30 --pick 10  # 域收窄 30，展示/挑选前 10
python3 run_screen.py --write             # Top pick 合并写回 config.yaml watchlist（自动备份）
python3 run_screen.py --write --replace   # 替换整个自选池（而非合并）
python3 run_screen.py --save              # 落库 screening_results 表
python3 run_screen.py --quiet
```
第一段用轻量全市场快照硬过滤 + 预排序收窄到 `--top` 只；第二段只为这批取日线算完整指标，再交 `screen_stocks()` 打分、`shortterm.plan()` 出计划。东财 push2 全市场快照被限流时降级到 push2ex 涨停板池 + 炸板池；push2his 日线被限流时切腾讯备源。详见 Spec 04。

### run_sim.py —— 多策略并行模拟交易

```bash
python3 run_sim.py                  # 每策略一个 10 万账户，自选池选股→风控→撮合→落库
python3 run_sim.py --capital 100000 # 每策略账户本金（默认 config.simulation.initial_capital）
python3 run_sim.py --reset          # 先清空这几个策略账户历史再重跑（幂等）
python3 run_sim.py --no-events      # 跳过事件源采集
python3 run_sim.py --dry-run        # 只演算不落库
```
为 board_hit/trend_pullback/oversold_bounce/event_driven 各开独立 `Account`（account_id 即策略名），流程 `scan()`→`RiskManager.check_buy`→`Matcher` 撮合（当日 OHLC，打板单封板才成交）→`take_snapshot`，结果写 `data/simulation.db`。候选域为 config watchlist，`simulation.include_screening` 打开时再并最近一次选股结果（`screening_universe(cfg)`，按 code 去重打 `tag=选股`，默认关闭）。情绪用 push2ex 涨停池 + 北向（breadth 被限流时相关因子按中性估计并标注）、行情用腾讯报价、事件用 datacenter。**日线源被限流(HTTP 501)时 trend/oversold 无法算均线/ret20/bias5/rsi6，如实空仓说明原因；相位不适配同理，绝不编造成交。** 详见 Spec 06。

### run_sim_live.py —— 盘中自动循环模拟交易（结合开盘收盘时间）

```bash
python3 run_sim_live.py               # 常驻：开盘按 plan 建仓，盘中轮询止盈止损，尾盘结算快照
python3 run_sim_live.py --interval 30 # 盘中轮询间隔秒（默认 config，典型 15~30s）
python3 run_sim_live.py --duration 60 # 只运行 60 分钟后自动退出（调试用）
python3 run_sim_live.py --once        # 只跑一轮「建仓→卖出→快照」即退出（盘后回测口径）
python3 run_sim_live.py --force       # 忽略时段，强制执行一轮（配合 --once 测试）
python3 run_sim_live.py -v            # 详细日志
```
定时启动（工作日 9:25 拉起）：`crontab` → `25 9 * * 1-5 cd <项目根> && python3 run_sim_live.py >> data/sim_live.log 2>&1`。

与 `run_sim.py`（一次性建仓）不同，本入口是**常驻盘中循环**，用 `market_session()`（Spec 06）判断时段驱动四阶段：
1. **开盘建仓**（每个交易日一次）：`buy_pass()` 采集情绪/涨停池/事件→`build_stocks`→各策略 `scan()`→`RiskManager.check_buy`→`Matcher.submit_order/match_orders`（打板单封板才成交），落库并更新市值。候选池走 `build_universe(cfg)` = 自选池 ∪ 当日主力净流入 TopN（`pulse.stock_flow_pool`，`simulation.flow_pool_topn` 默认 15、0 关闭；源失败回落纯自选池）∪（可选）最近一次选股结果（`simulation.include_screening`，默认 false；`run_sim.screening_universe(cfg)` 读 `screening_results` 最新一日、按 code 去重打 `tag=选股`，`screening_max_age_days` 防过期、`screening_topn` 限只数），返回 `(extended, codes, flow_added, screen_added)`，治"只扫自选池常年无候选"。
2. **盘中补扫**（10:30 / 13:30 各一次）：`rescan_pass()` 重新探测日线源与 48h 事件源后调 `buy_pass(only_idle=True)`，仅为空仓/低仓位（仓位比<0.5）的策略补建仓；过 14:30（`RESCAN_CUTOFF`）不再补开新仓。同日重启从心跳恢复 `bought_today`/`snapshot_done`，补扫 `done_slots` 不跨重启保留。
3. **盘中卖出**（每次轮询）：`sell_pass()` 仅对 `available>0`（T+1 可卖）的持仓，按 `evaluate_exit()` 判定——跌停封死不卖、破止损、封板后跌破成本、达目标①、尾盘时间止损；用 `account.sell(current_price)` 直接成交（不走 Matcher，因其 MARKET 卖单按开盘价成交属盘后口径）。
4. **尾盘结算**（`closing_auction` 或 ≥14:50 一次）：`take_snapshot` 落每日净值；随后 `_refresh_brief_after_close()` 在 ≥15:00 时**自动静默重跑 `run_brief.run_once(notify_enabled=False)`**，让 Web 仪表盘日期/温度/次日计划每日滚动（否则停留在前一交易日快照）。

**系统活动流**：上述各阶段（scan/skip/buy/sell/rescan/snapshot/start/warn/info）均调 `log_activity()` 写 `market.db activity_log`，server `/api/activity` 与 WebUI 仪表盘/脉搏页实时展示；落库失败仅 debug，不影响交易主循环。扩容池日线抓取走 `run_sim._daily_bars()` 当日缓存 + 每日预算（`KLINE_DAILY_BUDGET=45`），多轮不重复抓、超预算如实降级。

心跳写 `data/sim_live_state.json`（running/session/bought_today/snapshot_done/accounts/**notes 每策略结论**），供 server `/api/simulation/accounts`、`/api/simulation/compare` 与 WebUI 模拟交易页读取展示。非交易日/已收盘自动退出；`--once --force` 可在盘后安全演练。详见 Spec 06、Spec 09。

### server.py —— 本地 Web 控制台

```bash
python3 server.py                    # http://127.0.0.1:8765，自动弹浏览器
python3 server.py --port 9000        # 换端口
python3 server.py --no-open          # 不自动弹浏览器
python3 server.py --host 127.0.0.1   # 只监听本机（默认，安全）
```
或双击 `启动控制台.command`。详见 Spec 09。

### preview_demo.py —— 模板预览

离线示例数据渲染简报 HTML，用于调试模板，不联网、不发通知。

## 二、日常交易日工作流

### 盘前（9:00 前）
```bash
python3 run_brief.py
```
看 HTML 简报：市场温度 → 情绪阶段 → 可执行计划 → P0/P1 告警 → 回避区。

### 盘中（9:40-15:00）
```bash
python3 run_monitor.py
```
自动轮询持仓和自选，命中阈值才通知。

### 盘后（15:10 后）
```bash
python3 run_review.py
```
生成四维复盘，核对当日计划 vs 实际走势。

### 模拟记账
通过 Web 控制台手动买卖（auto_trade: false），或调 `/api/simulation/buy|sell`。想让四种策略各带 10 万本金一次性建仓跑一遍，用 `python3 run_sim.py`；想让它们**结合开盘收盘时间盘中自动循环买卖**（开盘建仓、盘中止盈止损、尾盘结算），用 `python3 run_sim_live.py`（可 crontab 工作日 9:25 拉起），过程在 WebUI 模拟交易页实时展示。三者均每策略独立账户，详见 Spec 06。

## 三、数据流串联全景

```
数据源层(datasource+extras+net)
   │  行情/涨跌家数/板块/涨停板池/北向/事件
   ▼
指标层(indicators.compute/snapshot)
   │  MA/RSI/KDJ/BOLL/ATR/BIAS/close_pos...
   ▼
评分计划层(shortterm.score/plan/signals)  ← 波段走 scoring
   │  分数/标签/买卖点/盈亏比/股数/信号
   ▼
情绪层(sentiment.enhance_market_temp)  ← extras.collect_sentiment_data
   │  情绪分/阶段/仓位/偏好策略
   ▼
策略层(strategies.scan)  ← 情绪阶段驱动启停
   │  TradeSignal 列表
   ▼
呈现层(briefing.render + server/web)
   │  HTML 简报 + Web 控制台
   ▼
存储层(db.Store → market.db)
   │  runs/watch_rows/alerts/plans/signals/sentiment/screening/reviews/monitor_alerts
   ▼
复盘层(review)  ← 盘前简报 + 盘后实际 + 模拟交易(simulation.db)
      四维复盘 → 改进动作 → reviews 表
```

## 四、当前集成状态（待打通点）

以下是能力已实现但**尚未串入主流程**的集成点，供后续迭代参考：

1. **screener 已有独立入口 run_screen.py，但未接入 run_brief 主流程**：选股可经 `run_screen.py` 独立运行并 `--write` 回写 watchlist（间接影响后续简报），但 `build_brief()` 仍未直接调用 `screen_stocks`；简报内策略扫描标的依旧来自 watchlist 而非当日全市场候选。
2. **review.auto_review 未接线**：配置声明"简报后自动触发复盘"，但无代码消费该键；复盘真实入口仅 CLI 与 Web。
3. **Web 前端缺复盘页**：复盘后端 API 完备但前端导航未包含；模拟交易页已接入（`#/simulation`，四策略账户切换 + 时段徽章 + 自动循环心跳）。
4. **_load_strategy_history 为简化桩**：恒返回空，策略累计统计目前只覆盖当日交易。
5. **select_strategies/suggest_position 未消费**：框架提供权重与仓位建议，run_brief 只用 is_suitable 做启停。
