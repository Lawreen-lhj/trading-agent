# Spec 10 · 存储与配置

> 模块：`src/db.py`、`src/configstore.py`
> 定位：SQLite 双库留痕 + 配置的安全读写。**本项目不连接任何数据库服务器**，纯 SQLite 本地文件，首次运行自动建表。

## 一、SQLite 存储层（db）

### 能力概述

`data/market.db` 由 `Store(db_path)` 初始化时建全部表+索引。模拟交易另用 `data/simulation.db`（由 Account 管理，见 Spec 06）。设计动机：**第一天就落库，否则三个月后无法回答"这套规则到底有没有用"**。两个 .db 都被 .gitignore 排除（每天变化，二进制无版本价值）。

### Store 方法 → 表

| 方法 | 操作表 | 说明 |
|------|--------|------|
| save_run(brief) | runs+watch_rows+alerts+plans | 一次写入：upsert runs；逐只 upsert watch_rows；先删当日 alerts/plans 再重插 |
| history(code,limit) | watch_rows | 某标的历史 close/pct_chg/score/label |
| runs(limit) | runs | 运行记录倒序 |
| alert_counts() | alerts | P0/P1/P2 总数 + 最近日 P0 数 |
| alerts_recent(limit,level,date) | alerts | 告警检索（级别 P0>P1>P2） |
| plans_recent(limit) | plans | 计划倒序 |
| plan_stats() | plans | 出手频率：总数/可执行数/平均rr/平均risk_pct/覆盖交易日 |
| save_signals / signals_by_date / signal_stats | strategy_signals | 多策略信号写入/检索/统计 |
| save_sentiment / sentiment_history / sentiment_latest | sentiment_history | 情绪快照 upsert/倒序/最新 |
| save_screening / screening_by_date | screening_results | 选股结果写入/检索 |
| save_review / review_by_date / reviews_recent | reviews | 四维复盘 upsert/检索 |
| save_monitor_alerts / monitor_alerts_recent / monitor_alert_stats | monitor_alerts | 盘中告警增量写入(INSERT OR IGNORE)/检索/统计 |
| save_pulse_snapshot(run_date,ts,snap) / pulse_snapshots_today(run_date,limit) | pulse_snapshots | 行情脉搏每次采集落一行日内快照/当日按时间正序取（供北向趋势 sparkline） |
| log_activity(run_date,ts,kind,message,strategy,detail) / activity_recent(run_date,limit) | activity_log | 系统活动流写入/检索（正序返回，前端倒序展示） |

### market.db 全部表（11 张业务表）

| 表 | 主键 | 字段与用途 |
|----|------|-----------|
| **runs** | run_date | generated_at/state/up_count/down_count/total_amount/status(ok/degraded) —— 每日运行摘要 |
| **watch_rows** | run_date,code | name/close/pct_chg/score/label/snap_json —— 自选池逐只当日快照 |
| **alerts** | id(自增) | run_date/code/name/level/rule/message/created_at —— 简报产出的提醒 |
| **plans** | run_date,code | name/ready/score/buy_lo/buy_hi/stop/target1/target2/rr/risk_pct/shares/amount/why —— 交易计划存档，事后核对能否成交 |
| **strategy_signals** | id(自增) | run_date/strategy/code/name/direction/strength/price/buy_lo/buy_hi/stop/target1/target2/rr/shares/hold_days/confidence/market_phase/reason/extra_json/created_at |
| **sentiment_history** | run_date | score/phase/limit_up/limit_down/up_ratio/median_pct/board_height/yesterday_perf/total_amount/detail_json/created_at |
| **screening_results** | run_date,code | name/layer1_pass/layer2_pass/layer3_pass/patterns/score/rank/detail_json；detail_json 内含 reasons/plan/strategy_hits/via_strategy，「策略直命·未过漏斗」的补录票 layer3_pass 记 0（未走三层漏斗风控门），WebUI 据 via_strategy 打琥珀徽章 |
| **reviews** | run_date | overall_score/market_json/stocks_json/strategies_json/behavior_json/insights_json/actions_json/created_at |
| **monitor_alerts** | id(自增),alert_id UNIQUE | run_date/level/category/rule/code/name/message/data_json/notified/created_at —— 盘中实时告警（区别于 alerts 表） |
| **pulse_snapshots** | id(自增) | run_date/ts/north_net/zt_count/broken_count/ld_count/top_sector/top_sector_net/created_at —— 行情脉搏每次采集的日内快照（idx_pulse_date） |
| **activity_log** | id(自增) | run_date/ts/kind/strategy/message/detail_json/created_at —— 系统活动流（idx_activity_date(run_date,id)） |

## 二、配置存储（configstore）

### 能力概述

`config.yaml` 的读写与校验，是 **Web UI 的唯一写入口**。设计理由：一个 capital=0 或 min_rr=0 会让仓位计算得到 NaN/无穷大，界面不报错却安静给出错误建议，所以写入前**逐字段校验**，不静默修正关键字段（有问题抛 ConfigError）。

### 接口

- `load(path)`：yaml.safe_load，空文件返回 {}。
- `dump_text(cfg)`：safe_dump(allow_unicode,sort_keys=False) + 固定注释头。
- `normalize(cfg)`：核心校验+清洗，返回干净配置或抛 ConfigError(errors)。
- `backup(path,dir)`：备份到 `data/backup/config-{时间戳}.yaml`。
- `save(path,cfg,dir)`：normalize → backup → 写 .yaml.tmp → os.replace **原子替换**（避免写一半中断）。返回 {path,backup,config}。

### 校验规则

- **meta.style**：只能 short/swing，非法报错回退 short；补默认 name/timezone/source。
- **risk 数值区间**：capital 1000~1e9、single_risk_pct 0.1~10、max_hold_days 1~60、atr_stop_mult 0.5~5、min_rr 0.5~10、min_score 0~100；有默认值。
- **watchlist**：须列表；code 6位数字；name 空补 code；截断 name≤20/tag≤12；去重；**不能为空**；**最多60只**。
- **holdings**：可空；code 6位；不重复；qty 1~1e8、cost 0.01~1e6；opened 须 YYYY-MM-DD 否则清空。
- **thresholds**：逐值转 float，非数字报错。
- **notify**：macos(bool)、title(≤40)、sound、only_on_p0(bool)。
- **global_watch**：缺失补默认（美股3/港股HSI,HSTECH/加密BTC,ETH）。

### 备份机制

写前复制旧文件到 `data/backup/`（YAML 重写会丢注释），带时间戳命名，只保留最近 **30 份**，超出删最旧。前端提示"想保留手写注释就手改，别用界面保存"。
