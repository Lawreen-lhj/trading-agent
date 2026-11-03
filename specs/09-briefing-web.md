# Spec 09 · 简报渲染与 Web 控制台

> 模块：`src/briefing.py`、`server.py`、`web/`
> 定位：把分析结果呈现给人——HTML 简报（离线可读）+ 本地 Web 控制台（交互操作）。

## 一、HTML 简报渲染（briefing）

### 能力概述

唯一入口 `render(brief, cfg) → HTML字符串`。**刻意纯 CSS + 内联 SVG，零 JS、零 CDN 图表库**——永远不会因某段 JS 失配导致整页白屏。样式内联，响应式（≤760px 折叠栅格）。

### render() 渲染板块（按顺序）

1. **头部**：标题、市场状态徽章（按 tone 着色）、数据日期/生成时间/数据源。
2. **结论先行**：brief.summary 逐条（首屏结论）。
3. **市场概览**：6卡（5指数+成交额）+ 4卡（涨跌家数占比/中位数/涨停跌停/样本数）。
4. **短线市场温度**：温度标签、score/100 徽章、涨停跌停、上涨占比、中位数、warn 警示。
5. **情绪周期**（需 sentiment + market_phase）：
   - 第一行4卡：当前阶段（配色）、建议仓位、适配策略、连板高度。
   - **第二行4卡（V2 精确打板情绪）**：炸板率（>40%红/>25%橙/否则绿）、晋级率、北向资金（正红负绿+源标注）、涨停/跌停（+数据源）。
   - 连板龙头梯队（board_ladder 前4，"名称(N板)"）。
   - 情绪信号逐条。
6. **今日交易计划**（style=short）：可执行计划表（按 rr/score 排序，列含标的/评分/买点区间/止损/目标①②/盈亏比/止损幅度/股数+金额）+ 执行说明卡；无可执行时显式说明"空仓也是决策"；另列未通过计划（plan.why）。
7. **多策略信号**：strategy_signals 表（策略/标的/方向/强度/买点区间/止损/盈亏比/原因）。
8. **板块强弱**：领涨领跌行业 + 成交额居前。
9. **外围观测（只看不做）**：美股/港股/加密 3卡，缺数据显示"数据暂不可用"。
10. **提醒/风险与卖出信号**：级别(P0/P1/P2 pill)/标的/规则/说明。
11. **自选池明细**：短线与波段不同表头，附"怎么读这张表"说明卡 + sparkline 走势。
12. **持仓**：数量/成本/现价/浮盈亏/市值/持有天数(超时⚠)/止损位/距止损（击穿显式标"已破止损"，绝不显示正百分比以免误读）+ 集中度卡。
13. **页脚 + 数据健康**：数据来源与时点声明、data_health（缺口）、data_notes（说明）、免责声明。

### 输入 brief dict 关键结构

顶层：date/generated_at/style/summary/market/global/watch/alerts_all/sentiment/market_phase/strategy_signals/holdings/risk/data_health/data_notes。cfg 需含 meta.name/source_primary/source_secondary。

## 二、Web 控制台（server + web 前端）

### 能力概述

基于标准库 `http.server.ThreadingHTTPServer`，**只监听 127.0.0.1**，不对外暴露。界面无状态，读写落回 config.yaml / data/。默认端口 8765。安全：静态文件路径越界防护、简报文件名白名单正则、POST 有 CSRF 同源校验。

### 前端页面（web/index.html + app.js，hash 路由）

当前导航暴露 **9 个页面**：仪表盘、**选股**、交易计划、自选池、持仓、**模拟交易**、提醒、简报历史、设置。

**选股页（#/screening）**：进入时懒加载 `/api/screening`（数据量大、非每页需要，故不进 /api/state），顶部日期下拉切换历史选股日；每只候选渲染一张卡片——排名/代码/名称/标签/评分/现价/涨幅/形态 + `build_reasons()` 的**逐条入选理由**（`.reasons` 列表）+ 交易计划（ready 时绿色 note）。理由读自落库的 `screening_results.detail_json`，故仅 `--save` 跑过的选股有理由；旧记录显示「该次运行未记录理由」。无记录时提示运行 `run_screen.py --save`。

**模拟交易页（#/simulation）**：四策略并行模拟盘的前端。进入时懒加载 `/api/simulation/accounts`（含 `market_session()` 时段徽章 + 读自 `data/sim_live_state.json` 的自动循环心跳），点某账户再 `Promise.all` 拉 `/api/simulation/summary|positions|trades?limit=50|snapshots`（均带 `?account=`）。顶部工具栏：市场时段 pill（盘前/开盘/午休/收盘，按 key 着色）+ 自动循环运行 pill + 心跳时间 + 「单账户/四策略对比」`.seg` 切换 + 刷新按钮；`.simtabs` 账户切换条（打板/半路/超跌/事件，各显中文名/收益率/总资产）。单账户主体：6 张 KPI 卡（总资产/可用现金/持仓市值/仓位比/浮动盈亏/收益率）+ 持仓表（标的/数量/可卖/T+1冻结/成本价/现价/市值/浮动盈亏/盈亏%/止损目标）+ 成交流水表（买入/卖出 pill + 已实现盈亏 + reason）+ 每日净值内联 SVG sparkline（涨红跌绿）与快照明细表（不足两天时提示）。**对比模式**调 `/api/simulation/compare` 一次取全四账户，渲染横向对比表（每策略一行：总资产/现金/持仓市值/仓位/浮动盈亏/累计收益/持仓数/成交数）+ `.simcmp` 四列持仓卡片；空仓策略显示心跳 `notes` 落盘的空仓原因（相位不适配/K线源限流/无新鲜利好/未选出标的）而非留白。

> **前后端不对齐点（可核对）**：server 端已提供 simulation 与 review 的完整 REST 接口；模拟交易页前端**已接入**，但**复盘页仍只有后端 API、前端导航尚未包含**。

### GET 接口

| 路径 | 返回 |
|------|------|
| /api/state | 一次性汇总：config/brief/history/runs/alerts/plans/stats/alert_counts/meta（前端首屏用） |
| /api/run/status | 运行状态 running/started_at/finished_at/result/log（前端1.2s轮询） |
| /api/alerts | 简报告警检索（limit/level/date） |
| /api/config | 原始配置 |
| /api/screening | 选股结果（?date= 切换，默认最近一日）：{date, dates[], rows[]}，rows 为 detail_json 解析出的候选（含 reasons/plan/patterns/score） |
| /api/simulation/accounts | 四策略账户全景：各账户中文名/总资产/现金/持仓市值/仓位/浮动盈亏/收益率/持仓数 + market_session() 时段 + 读 data/sim_live_state.json 的自动循环心跳 |
| /api/simulation/compare | 四策略对比一次取全：每账户概览 + 完整持仓列表 + note（心跳落盘的空仓原因/建仓笔数）+ 时段 + 心跳 |
| /api/simulation/summary\|positions\|trades\|performance\|snapshots | 模拟账户概览/持仓/流水/绩效/每日净值（均支持 `?account=` 选账户；持仓市值经 tx.quotes 实时刷新，失败回退最近快照现价） |
| /api/review/latest\|history | 最近复盘详情/历史列表 |
| /api/signals | 策略信号（date/strategy）+ 统计 |
| /api/sentiment | 情绪历史（latest + history） |
| /api/monitor/alerts | 盘中告警（limit/level）+ 近7天统计 |
| /api/strategies | 可用策略清单 |

静态：`/`、`/index.html`、`/briefings/{name}`（白名单）、`/static/{rel}`。

### POST 接口（均先过同源校验）

| 路径 | 作用 |
|------|------|
| /api/config | 保存配置：patch 深合并 → configstore.save（校验+备份+原子写），失败 400 带 errors |
| /api/run | 触发运行简报（异步），已有任务在跑返回 **409**，成功 202 |
| /api/notify/test | 发一条 macOS 测试通知 |
| /api/reveal | 访达打开目录（data/briefings/config） |
| /api/simulation/buy | 模拟买入：先过 RiskManager.check_buy → Account.buy → save |
| /api/simulation/sell | 模拟卖出：Account.sell → save |
| /api/review/run | 触发复盘（同步），返回 date+score |

### 并发控制：为何运行简报同一时刻只允许一次

`RunManager` 用 `threading.Lock` + running 标志：**并发跑会同时打同一接口，容易被限流**。已在跑则 start() 返回 False（HTTP 409）。运行时临时挂 LogHandler 采集实时日志（deque≤300）供前端轮询，异常时把真实错误+traceback 写入 result/log。
