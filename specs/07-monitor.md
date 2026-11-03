# Spec 07 · 盘中监控

> 模块：`src/monitor/`（watcher）+ 入口 `run_monitor.py`
> 定位：交易时段内实时轮询，命中规则即产出分级告警，经冷却去重后多渠道通知并落库。与主流程解耦，崩溃不影响简报。

## 能力概述

覆盖持仓、自选、市场三个维度的实时监控。核心类：`Monitor`（监控循环+规则）、`Alert`（告警数据类）、`MonitorConfig`（阈值）、`AlertNotifier`（通知渠道）。

- **轮询频率**：主间隔 15 秒；集合竞价时段 30 秒；盘前盘后不监控（非监控时段 sleep 30 跳过）。

## 交易时段判定

- **是否监控**：周末不监控；监控时段 09:15–11:30 或 13:00–15:00。
- **间隔选择**：09:15–09:30 与 14:55–15:00 用集合竞价间隔（30秒），其余 15 秒。
- **运行时长**：默认算到当日 15:05（收盘后5分钟），已过则直接退出"已收盘"。

## 监控对象

数据由注入的 `data_fetcher()` 提供 `{stocks:{code:data}, market:{...}}`。
- **持仓**（set_positions 注入 code→{name,cost_price,quantity,stop_price,hold_days,strategy}）：监控破止损、跌停、接近止损、单日大跌、时间止损临近、单日大涨、放量滞涨。
- **自选**（set_watchlist，仅监控不在持仓中的代码）：放量突破20日高、大幅异动。
- **市场**：涨停潮、跌停潮、指数跳水。market_history 保留近1小时用于5分钟增量对比。

## 告警规则（逐条）

阈值来自 MonitorConfig / `config.monitor.thresholds`（两处默认一致）。级别：P0=立即处理，P1=需关注，P2=信息提示。

**持仓类**

| rule | 级别 | 触发条件 | 阈值 |
|------|------|----------|------|
| stop_loss_broken | P0 | price ≤ stop_price | 持仓止损价 |
| limit_down | P0 | pct_chg ≤ −9.8（硬编码） | — |
| near_stop_loss | P1 | price ≤ stop×(1+buffer) | stop_loss_buffer_pct(0.01) |
| daily_drop | P1 | 跌幅 ≥ position_drop_pct | 0.05(跌5%) |
| time_stop_warning | P1 | hold_days ≥ 阈值 | time_stop_warn_days(4) |
| daily_gain | P2 | 涨幅 ≥ position_gain_pct | 0.08(涨8%止盈提示) |
| volume_stall | P1 | vol_ratio>2 且 0≤pct<1.5（硬编码） | — |

**自选类**（跳过已持仓）

| rule | 级别 | 触发条件 | 阈值 |
|------|------|----------|------|
| breakout | P1 | price≥high20×0.999 且 vol_ratio≥阈值 | breakout_volume_ratio(2.0) |
| jump | P2 | |pct_chg| ≥ 阈值 | watchlist_jump_pct(0.03) |

**市场类**

| rule | 级别 | 触发条件 | 阈值 |
|------|------|----------|------|
| limit_up_surge | P0 | 近5分钟涨停增量≥阈值 | market_surge_limit_up(30) |
| limit_down_surge | P0 | 近5分钟跌停增量≥阈值 | market_crash_limit_down(30) |
| index_drop | P1 | 任一指数跌幅≥阈值 | index_drop_pct(0.02) |

- **冷却机制**：cooldown_key=`{category}:{rule}:{code}`，距上次 < alert_cooldown_minutes(30) 则跳过不重复。市场类 code 为空，按 category:rule 去重。
- **Alert 字段**：alert_id/level/category/rule/code/name/message/data(结构化上下文)/created_at/notified/acknowledged。

## 告警输出（多渠道）

- **macOS 通知**（monitor.notify.macos）：osascript，标题带级别图标（P0🔴/P1🟡/P2🔵），声音 Glass，正文截断200字。
- **JSONL 日志**（log_file: data/monitor_alerts.jsonl）：每条一行 JSON 追加。
- **Webhook**（可选，默认空）：POST 钉钉/企业微信文本消息。
- **落库**：`Store.save_monitor_alerts()` 写 `market.db` 的 **monitor_alerts 表**（区别于简报的 alerts 表），`INSERT OR IGNORE` 增量去重。读取接口 monitor_alerts_recent / monitor_alert_stats(近7天P0/P1/P2计数)。
- **状态持久化**：另写 `data/monitor_state.json`（最近500条告警+冷却时间戳），重启恢复上下文。

## 触发方式（run_monitor.py）

- `--duration <分钟>`：指定监控时长。
- `--test`：不等交易时段，立即执行一轮检查，打印告警数与前10条并落库。
- `--verbose/-v`：DEBUG 日志。
- 默认运行到当日15:05；注册 SIGINT/SIGTERM 优雅退出。
- **数据来源**：config.yaml（阈值/holdings/watchlist）；持仓优先从 simulation.db 的模拟 Account 读（stop_price 取 original_plan.stop），并入 config holdings，用最新简报补齐止损价；行情由 datasource 的 spot/breadth/index_quotes 提供。

## 配置项（config.yaml → monitor）

interval_seconds(15)、auction_interval_seconds(30)、alert_cooldown_minutes(30)、thresholds（9项）、notify（macos/log_file/webhook）。
