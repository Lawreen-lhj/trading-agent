# Spec 06 · 模拟交易引擎

> 模块：`src/simulation/`（account + matcher + __init__）
> 定位：本地纯模拟的 A股交易引擎，**不接券商、不做任何真实下单**，用于验证策略逻辑。

## 能力概述

在本地 SQLite 维护虚拟资金、持仓与交易历史，严格模拟 A股规则（T+1、涨跌停、100股整数手、佣金/印花税/过户费、滑点）。三部分构成：`Account`（资金/持仓/盈亏）、`Matcher`（撮合）、`RiskManager`（风控闸门）。支持多账户并行（account_id 区分），状态可持久化、重启不丢。采用"简化撮合"：不依赖 Level-2 订单簿，而根据当日 OHLC + prev_close 判断成交与否及成交价。

## 一、账户模型 Account

- **构造**：account_id(默认"default")、initial_capital(默认10万)、db_path、fee_config。有 db_path 则从库恢复账户/持仓/最近100笔交易。
- **状态**：cash(可用现金)、frozen_cash(冻结)、positions(code→Position)、trade_history、daily_snapshots。
- **派生属性**：total_assets(现金+冻结+持仓市值)、position_value、position_ratio、total_pnl、total_return_pct。
- **操作**：`buy()`/`sell()`（返回 `{success,reason,trade}`）、`on_new_day(trade_date)`（T+1解冻，**只解冻 open_date<trade_date 的持仓**，避免同日重启把当日买入错误放开）、`update_market_prices()`、`take_snapshot()`、`save()`、查询 `get_summary/get_positions/get_trades/get_performance`。
- **持久化恢复**：有 db_path 时 `_load_from_db` 恢复账户/持仓/最近100笔交易/**全部每日快照**（sim_snapshots，按日期正序），因此重启后 `get_performance()` 与净值曲线仍可用。
- **Position 字段**：quantity/available(可卖)/frozen(当日新买)/cost_price(含费)/total_cost/open_date/strategy/original_plan + 实时 current_price/market_value/profit_loss/profit_pct。
- **绩效 `get_performance()`**：总收益、年化(252交易日)、波动率、夏普(无风险2%)、最大回撤、胜率。

## 二、撮合规则 Matcher

- **订单类型**：LIMIT(限价)、MARKET(市价)、BOARD_HIT(打板/涨停价)。
- **订单状态**：PENDING/FILLED/CANCELLED/REJECTED/EXPIRED（PARTIAL_FILLED 已定义但实现为全量成交或整单失败，不产生部分成交）。
- **挂单校验**：非交易时段（strict_time_check）拒；数量≤0或非100整数倍拒。
- **撮合数据要求**：需 open/high/low/close/prev_close，缺则 EXPIRED。
- **涨跌停幅度**：688/689→20%、300/301→20%、8/4/9开头→30%、主板→10%。
- **一字板**：open==high==low==close 时，买单涨停/卖单跌停直接拒。
- **T+1**：买入当日 available=0、frozen=quantity；卖出校验 available；on_new_day 解冻。
- **成交价**：市价单以 open 成交（叠加滑点）；打板单 close≥涨停价×0.998 才以涨停价成交；限价单买单需委托价≥low、卖单需委托价≤high，否则 EXPIRED。
- 另有 cancel_order/get_pending_orders/get_order_history。
- **市场时段工具 `market_session(dt)`（模块级，全项目唯一时段来源）**：返回 `{key,label,tradable,in_session,is_trading_day,time}`。key ∈ pre_open(盘前)/auction(集合竞价 9:15-9:30)/morning(交易中 9:30-11:30)/lunch(午间休市)/afternoon(交易中 13:00-14:57)/closing_auction(尾盘竞价 14:57-15:00)/closed(已收盘)/non_trading_day(非交易日)。tradable=当前可撮合（连续/集合竞价且为交易日）。只按星期判断交易日，不含法定节假日日历。run_sim_live、server API、WebUI 徽标共用。

## 三、风控 RiskManager

默认配置（可被 `config.simulation.risk` 覆盖）：max_single_position_pct(0.30)、max_total_position_pct(0.90)、max_daily_loss_pct(0.05)、max_daily_trades(20)、max_hold_days(5)、min_cash_reserve(5000)。

- **`check_buy(code,price,quantity) → (bool,reason)`**，顺序校验：现金充足（含最低保留）→单票仓位≤30%→总仓位≤90%→单日交易次数→单日亏损保护（亏损达5%停开新仓）。
- **`check_sell`**：一般不限卖出，仅校验持有与 T+1 可卖数量。
- **`check_hold_days()`**：返回超时持仓（时间止损候选）。
- **`on_new_day()`**：重置 daily_trades 与当日起始资产。
- **重要边界**：check_buy **不在撮合链路内自动生效**，仅在外层（Web API、主流程）显式调用；`match_orders→account.buy` 本身不过风控。

## 四、费用模型

默认费率：commission_rate(万3，双边)、min_commission(5元)、stamp_tax_rate(千0.5，**仅卖出**)、transfer_fee_rate(万0.1，双边)、slippage(0.2%)。
- 买入：佣金+过户费；actual_price=price×(1+slippage)。
- 卖出：额外印花税；actual_price=price×(1−slippage)。
- 已实现盈亏仅卖出时结算；持有天数按工作日粗算（不含节假日）；加仓重算成本价。

## 五、数据库 simulation.db（4 张表）

- **sim_accounts**：account_id(PK)/initial_capital/cash/created_at/updated_at。
- **sim_positions**（PK=account_id,code）：code/name/quantity/available/frozen/cost_price/total_cost/open_date/strategy/original_plan(JSON)。保存时先 DELETE 再全量 INSERT。
- **sim_trades**（PK=trade_id，**19 字段**）：trade_id/account_id/trade_date/trade_time/code/name/direction/price(含滑点)/orig_price/quantity/amount/fees/net_amount/realized_pnl/realized_pct/reason/strategy/hold_days/cash_after。
- **sim_snapshots**（PK=account_id,date，**10 字段**）：account_id/date/cash/position_value/total_assets/position_ratio/total_pnl/total_return_pct/position_count/positions_json。

## 触发方式

- **`auto_trade` 开关**（config.simulation.auto_trade，默认 false）：默认关闭走人工确认；开启后主流程才把信号自动落撮合。
- **Web 控制台手动下单**：POST `/api/simulation/buy`（先过 RiskManager.check_buy → Account.buy → save）、`/api/simulation/sell`（Account.sell → save）；只读查询 `/api/simulation/summary|positions|trades|performance|snapshots`，均支持 `?account=` 指定策略账户；`/api/simulation/accounts` 返回四策略总览 + `market_session()` 时段 + 自动循环心跳；`/api/simulation/compare` 一次返回四账户概览 + 完整持仓 + 空仓原因 note（供对比视图）。服务端用腾讯实时价刷新持仓市值（失败回退最近快照价）。
- **多策略并行模拟 `run_sim.py`**：为 board_hit/trend_pullback/oversold_bounce/event_driven 各开一个独立 `Account`（account_id 即策略名，默认各 10 万本金），从自选池按各策略 `scan()` 选股 → `RiskManager.check_buy` → `Matcher` 撮合（当日 OHLC）→ `take_snapshot` 落库。行情用腾讯报价（`tx.quotes` 已含 open/high/low）、情绪用 push2ex 涨停池、事件用 datacenter；**日线源被限流(HTTP 501)时依赖均线/ret20/bias5/rsi6 的 trend/oversold 如实空仓并说明原因，相位不适配同理，绝不编造成交**。支持 `--capital/--reset/--no-events/--dry-run`。
- **盘中自动循环 `run_sim_live.py`**：交易日常驻循环，结合开盘/收盘时间自动买卖。① 进入交易时段后用腾讯实时价（当日运行中 OHLC）为四账户各跑一次 `scan→风控→Matcher` 建仓（当日一次）；② 盘中每轮轮询只对 **T+1 可卖(available>0)** 持仓按 `original_plan` 评估卖出：止损触达/打板炸板翻绿/第一目标触达/临近收盘持仓达计划天数，卖出直接 `account.sell(当前价)` 并做跌停封死守卫（不用 Matcher 市价卖单，因其按开盘价成交是盘后口径）；③ 14:50 后 `take_snapshot`、15:00 退出。每轮写 `data/sim_live_state.json` 心跳（running/时段/各账户摘要/**每策略结论 note**：空仓原因或建仓笔数，供 `/api/simulation/compare` 展示）。支持 `--interval/--duration/--once/--force`。
- **注意**：Web 前端已接入「模拟交易」页（四策略账户切换 + 时段徽标 + 全景，见 Spec 09）；run_sim/run_sim_live 开的是 4 个策略账户，与 Web 默认账户（config.simulation.account_id）并存互不干扰。

## 配置项（config.yaml → simulation）

initial_capital/account_id、fees（5项费率）、risk（6项风控）、auto_trade。
