# Spec 02 · 指标与评分

> 模块：`src/indicators.py`、`src/shortterm.py`、`src/scoring.py`
> 定位：把原始行情算成指标，把指标算成分数、交易计划和信号。全部确定性规则，可复现。

## 一、技术指标（indicators）

### 能力概述

纯标准库实现（不依赖 pandas/TA-Lib）的技术指标计算层。核心原则：**所有指标在这里算完，LLM 只读结果，不允许自己推算**。

### `compute(bars, code)` —— 补齐指标列

输入日线 bars，返回带指标的新列表（不改原数据）。按类别：

- **均线**：ma5/ma10/ma20/ma60（SMA）
- **RSI**：rsi6(n=6)、rsi14(n=14)，Wilder 平滑
- **MACD**：dif/dea/macd(hist)
- **KDJ(9,3,3)**：kdj_k/kdj_d/kdj_j
- **BOLL(20,2)**：boll_mid/boll_up/boll_low
- **ATR**：atr14、atr_pct(=atr14/close×100)
- **成交量**：vol_ma5/10/20、vol_ratio(当日量/vol_ma5)、vol_ratio10
- **换手**：turnover_ma20、turnover_ratio(当日换手/20日均换手，"相对自身"口径，跨市值可比)
- **乖离**：bias5(=close/ma5−1)、bias10 —— 判断追高核心
- **区间高低点**：high10/low10、high20/low20、high60/low60
- **周期涨幅**：ret3/ret5/ret10/ret20/ret60
- **波动率**：vol20（年化）
- **回撤**：drawdown60(=close/high60−1)
- **均线斜率**：ma20_slope、ma10_slope3
- **盘中强弱**：close_pos(收盘在当日振幅中位置)、high_pct(盘中最高涨幅)
- **连续涨跌**：up_days/down_days
- **涨跌停**：limit_pct(按代码判定：688/689→20%、300/301→20%、8/4/9开头→30%、主板→10%)、near_limit_up

### `snapshot(bars)` —— 最新指标快照

取最后一行输出关键指标 dict，额外派生 `pos60`(60日区间位置)、`boll_pos`(布林带位置0-1)，非法值统一转 None。

### `sparkline(values)` —— 内联 SVG 迷你走势图

零 JS 依赖，红涨绿跌，用于简报与 Web 表格。

## 二、短线评分与计划（shortterm）—— style=short 默认

与波段模式的根本区别：**不追高是硬约束**（过热为负分项）、动量用短周期(RET5/RET10 甜点区)、必须给得出出场价、含时间止损。核心工具 `_curve` 分段线性映射表达"甜点区"非单调关系。

### `score(snap)` —— 短线评分（返回 总分, 标签, 分项）

满分 100，五个正分维度 + 一个负分维度：

| 维度 | 分值 | 逻辑 |
|------|------|------|
| 动量 | 28 | RET5 甜点曲线（3%~12%最优，涨超20%反降分）；RET10<0 时 ×0.82 |
| 量价 | 24 | 18×量比曲线(甜点1.1~2.2) + 6×换手曲线；量价配合修正（无量上涨×0.78、放量下跌×0.55、量价齐升×1.08） |
| 形态 | 22 | 距20日高点(10) + 站上MA5(6) + BOLL位置(4) + 尾盘强弱(2) |
| 结构 | 16 | 多头排列MA5>MA10>MA20(9) + MA10上行(最多7) |
| 空间 | 10 | ATR% 甜点曲线（2.5%~6%最优） |
| **过热** | **−34~0** | BIAS5偏离>6%(最多−14)、RSI6>85(−12)、RET5>18%(−10)、连涨≥5天(−5)、贴布林上轨(−4) |

**标签**：≥72 可跟 / ≥62 待突破 / ≥48 观察 / ≥34 偏弱 / 否则 回避；heat≤−14 追加"·过热"。

### `plan(snap, cfg, score)` —— 交易计划（核心输出）

从 `cfg.risk` 读参数（capital/single_risk_pct/atr_stop_mult/min_rr/max_hold_days/min_score）。输出 `{ready,why,buy_lo,buy_hi,stop,target1,target2,rr,risk_pct,shares,amount,hold_days,resistance}`。

- **否决前置**：指标不全 / 收盘<MA10（结构已破）/ close<MA5×0.97 / score<min_score → `ready=False` 并给 why。
- **买点区间**：偏离5日线>5% 则等回踩 `[MA5, MA5×1.02]`；否则 `[max(MA5×0.985, close−0.6ATR), close+0.25ATR]`。
- **止损**：取 `MA10−0.4ATR` 与 `close−atr_mult×ATR` 的**更宽者**，夹在 1.5%~6% 幅度内。
- **目标**：仅当 close 仍在前高下方才用20日高作阻力，否则退60日高或视为创新高无阻力。target1=min(阻力, buy_mid+2R)，target2=buy_mid+3R。
- **ready 条件**：`rr ≥ min_rr`(默认1.8) 且股数≥1手。
- **股数**：按"单笔风险=single_risk_pct%总资金"反推，取整100股，单票≤总资金30%。

### `signals(snap, cfg, cost, opened_days)` —— 短线信号

返回 `[{level,rule,message}]`。阈值取 `cfg.thresholds`。持仓以 cost 算浮盈亏。

- **P0（今天必须处理）**：涨停、跌停、单日涨跌≥pct_change_p0(7%)、持仓浮亏≤−7%、时间止损(opened_days≥max_hold)。
- **P1**：放量破MA10、跌破MA10、放量滞涨(vr≥2.2且0≤pct<1.5%)、冲高回落(high_pct≥4%且pct<1%)、偏离过大(bias5≥10%)、RSI6过热(≥88)、连涨过多(≥5)、涨跌异动(≥4%)、放量(vr≥1.8)、放量突破20日高、持仓亏损扩大(≤−4%)。
- **P2**：触布林上轨、缩量回踩MA5(低吸窗口)、RSI6超跌(≤15)、高位回撤(drawdown60≤−20%且守MA10)。

### `market_temp(breadth)` —— 短线市场温度

看涨停家数与赚钱效应，不看指数涨跌。需 `total_count` 否则返回空。
- **赚钱效应分**：涨停数曲线 + 上涨占比曲线 + 中位涨幅曲线，三者平均×100。
- **标签**：score≥68且limit_down≤12→强 / ≥50→中性 / 否则→弱。
- **警示**：sample_suspect→"数据不可用"；lu≥60且ld≥20→"分歧剧烈"；lu≤15→"题材熄火"。
- 输出 `{score,limit_up,limit_down,up_ratio,median_pct,tag,tone,warn}`。

## 三、波段评分与市场状态（scoring）—— style=swing

分数只用于排序分层，不代表预期收益；买卖判断由人下。用 `_lin` 线性映射保留排序区分度。

### `score(snap)` —— 波段评分（四维加权，满分100）

| 维度 | 分值 | 逻辑 |
|------|------|------|
| 趋势 | 45 | 站上MA20(+18)/守住MA60(+7)/MA5>MA20(+12)/MA20>MA60(+6)/MA20斜率(+8) |
| 动量 | 25 | 25×_lin(ret20, −0.15, +0.15) |
| 量能 | 15 | 15×_lin(vol_ratio, 0.7, 1.6) |
| 位置 | 15 | 15×clip(pos60) |

**标签**：≥75 强势 / ≥60 偏强 / ≥45 中性 / ≥30 偏弱 / 否则 走弱。

### `signals(snap, cfg, cost)` —— 波段信号

P0：单日涨跌≥pct_change_p0、持仓浮亏≤−8%。P1：涨跌≥pct_change_p1、放量、创20日新高/新低、高位回撤。P2：RSI14超买/超卖。

### `market_state(breadth, index_quotes)` —— 市场状态判定

涨跌家数比为主、指数为辅，只描述不预测。
- **退化口径**（breadth 缺失/sample_suspect）：只看上证 pct_chg，state 追加"（仅指数口径）"，up_ratio=None。
- **正常口径**：ratio=up/total_count，≥0.65普涨 / ≥0.55偏强 / ≥0.45分化 / ≥0.35偏弱 / 否则普跌；中性时若指数与个股背离追加说明。
- 输出 `{state,tone,up_ratio,sh_pct}`。

## 触发方式

`run_brief.py` 对每只自选股：`indicators.compute → snapshot → shortterm.score/plan/signals`（或 swing 走 scoring），汇总入 brief。
