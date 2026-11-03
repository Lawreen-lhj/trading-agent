# Spec 01 · 市场数据层

> 模块：`src/datasource.py`、`src/net.py`、`src/em.py`、`src/tx.py`、`src/extras.py`
> 定位：为全系统提供行情与市场数据，主备双源 + 双缓存 + 诚实降级。

## 能力概述

统一数据层解决一个核心问题：**在公开接口不稳定、会限流的前提下，仍然给出一份诚实的数据**。三条设计原则——故障切换（主源东财失败自动切备源腾讯，并回传实际用到的源名）、当日缓存（日线与市场级数据落盘，反复调试不重复打接口）、降级诚实（拿不到返回 `None`，由上层写入数据缺口，绝不编数）。

所有 HTTP 走纯标准库 `urllib`，无 requests 依赖。

## 一、基础行情接口（datasource）

| 能力 | 输入 | 输出 | 主源→备源 | 降级 |
|------|------|------|-----------|------|
| 指数行情 `index_quotes()` | 无 | `(list, 源名)`，每项 `{code,name,close,pct_chg,amount}`，固定5个指数 | 东财 push2 → 腾讯 | 双源失败 `(None,"none")` |
| 涨跌家数 `breadth()` | 无 | `{up,down,flat,limit_up,limit_down,median_pct,total_amount,total_count,sample_suspect,top_amount}` | 仅东财 | 失败读当日缓存补 `stale_asof`；无缓存 `None` |
| 行业板块 `sector_rank()` | 无 | `{top:[领涨前5],bottom:[领跌前5]}` | 仅东财 | 失败读缓存补 `stale_asof` |
| 个股实时 `spot(codes)` | 代码列表 | `{code: quote}` | 东财 → 腾讯 | 双源失败 `{}` |
| 日线 `daily(code,days)` | 代码 | `(list[bars], 源名)` | 东财 → 腾讯 | 双源失败 `(None,"none")`；当日缓存命中源名带"(缓存)" |
| 美股/港股 `global_quotes(kind)` | "us"/"hk" | `(list, 源名)`，每项 `{name,close,pct_chg}` | 东财 → 腾讯 | 双源失败 `(None,"none")` |
| 加密 `crypto(symbols)` | 符号列表 | `list`，每项 `{name,close,pct_chg}` | OKX → Binance | 全失败 `None` |

**加密合规**：仅作风险偏好观测，只读公开行情，不要任何交易所密钥，不提供下单接口。

### 数据健康标注（关键字段）

- **源名**（第二返回值）：标明实际用了 `eastmoney`/`tencent`/`none`/`xxx(缓存)`，简报页脚注明。
- **`stale_asof`**：breadth/sector 走缓存时补上的"数据真实时点"，上层显式声明为陈旧数据，不当实时用。
- **`sample_suspect`**：样本量体检。A股全市场 5000+ 家，若 `total_count < 3000` 说明接口被截断，此时涨跌家数/中位数/赚钱效应分全部不可信，必须显式标出。

## 二、网络基础设施（net）

- **全局限速**：所有请求 ≥350ms 间隔，配重试 + 指数退避。
- **东财专用限流通道 `em_fetch`/`em_get_json`**：东财对高频请求直接掐连接（RemoteDisconnected），本机被封过多次。专用通道强制 ≥1.2s 间隔 + 随机抖动（0.1~0.5s），串行发送。
- **分域独立 WAF**：东财不同子域（push2 / push2ex / datacenter-web）WAF 独立，一个被封不影响其他，分域降级。
- **宽松 SSL 上下文**：datacenter-web 证书缺 keyUsage 扩展，需用 `CERT_NONE` 上下文。
- **Cookie jar**：应对 WAF 挑战页，检测 HTML 响应（`<!DOCTYPE`/`<html` 开头）识别被拦截。

## 三、扩展数据源（extras）—— 精确打板情绪与事件数据

这是 V2 重写的核心：用专业级精确端点替代推断数据。全部带缓存（TTL）+ 优雅降级。

### 涨停板池（东财 push2ex，精确）

| 能力 | 输出关键字段 |
|------|-------------|
| 涨停池 `limit_up_pool(date)` | code/name/price/limit_days(连板数)/seal_fund(封单)/break_times(炸板次数)/industry/first_seal(首次封板时间) |
| 炸板池 `broken_board_pool(date)` | code/name/limit_price/break_times/amplitude/industry |
| 跌停池 `limit_down_pool(date)` | code/name/seal_fund/dt_days/open_times/industry |
| 昨涨停池 `yesterday_limit_up_pool(date)` | code/name/pct(今日表现)/y_limit_days/industry |

### 复合打板情绪 `limit_up_sentiment(date)`

一次请求聚合四池，产出精确情绪指标：
- `zt_count`/`zb_count`/`dt_count`：涨停/炸板/跌停家数
- `break_rate`：炸板率 = 炸板/(涨停+炸板)
- `max_height`：最高连板数
- `ladder`：连板梯队 `{1板:N, 2板:N, ...}`
- `promotion_rate`：晋级率 = 昨涨停今仍涨停/昨涨停总数
- `yesterday_perf`：昨涨停今日平均涨幅
- `top_boards`：连板龙头列表（code/name/height/industry）

### 北向资金 `north_flow()`

主源同花顺 hsgtApi（不封 IP），备源东财 kamt。输出 `{hgt,sgt,total,asof,source}`。另有 `north_flow_history(days)` 取历史。

### 板块资金流 `sector_fund_flow(board_type,period,top_n)`

东财 push2 clist 带资金流字段。走 push2 域名，限流时降级返回空（不阻塞主流程）。

### 事件数据（东财 datacenter-web）

| 能力 | 用途 | 输出关键字段 |
|------|------|-------------|
| 龙虎榜 `dragon_tiger(code,days)` | 游资/机构席位 | date/reason/close/pct/buy_total/sell_total |
| 业绩预告 `earnings_forecast(code,limit)` | 业绩驱动 | notice_date/indicator/type/net_profit_lower/upper/change_pct |
| 股东增减持 `holder_trades(code,direction,limit)` | 大股东动向 | holder/direction/change_shares/change_pct/price/notice_date |
| 回购 `share_buyback(code,limit)` | 回购利好 | amount_lower/upper/shares/price_cap/notice_date |
| 机构调研 `institution_survey(code,limit)` | 机构关注度 | survey_date/org_count/survey_type/notice_date |

> datacenter-web 有间歇性 WAF 拦截，代码优雅降级（返回空 + 记警告），不阻塞主流程。

### 综合采集入口 `collect_sentiment_data(market_rows=None)`

情绪周期的主数据入口，一次聚合上述能力，返回：
`{max_board_height, board_ladder, break_rate, promotion_rate, yesterday_limit_up_perf:{avg_pct,count}, limit_up_stats:{...,source}, north_flow:{...}, sector_limit_up:{...}, sector_fund_flow:[...], collected_at}`。
优先用 push2ex 精确数据；涨停池不可用时退回快照推断（`calc_limit_up_stats`）。

### 涨停历史存档

`save_yesterday_limit_ups(codes,day)` / `load_yesterday_limit_ups(day)`：存当日涨停代码到本地 JSON，供次日"昨涨停今表现"本地对比（push2ex 不可用时的兜底）。

## 触发方式

- `run_brief.py` 采集阶段调 `datasource` 各接口 + `extras.collect_sentiment_data()`。
- `run_monitor.py` 盘中调 `ds.spot/breadth/index_quotes`。
- Web 控制台经简报间接消费。

## 已知限制

- `sector_fund_flow` 走 push2，限流时降级为空。
- datacenter-web 事件端点间歇 WAF 拦截。
- 不判断交易日历。
- 腾讯备源不提供换手率，走备源当天换手强度退化为绝对口径。
