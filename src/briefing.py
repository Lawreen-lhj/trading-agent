"""每日简报 HTML 渲染。

刻意不用 CDN 图表库：纯 CSS + 内联 SVG，零 JS。
好处是永远不会因为某个 JS 括号失配导致整页白屏。
"""
from __future__ import annotations

STYLE = """
:root{
  --bg:#F6F6F4; --card:#FFFFFF; --line:#E4E3DE;
  --ink:#1A1A18; --ink2:#5F5E5A; --ink3:#8C8A83;
  --up:#C0392B; --down:#1E8449; --accent:#2A5C8A;
  --p0:#C0392B; --p1:#B8741A; --p2:#6B6A64;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB",sans-serif;
  font-size:14px;line-height:1.6;-webkit-font-smoothing:antialiased}
.wrap{max-width:960px;margin:0 auto;padding:40px 24px 64px}
.hd{border-bottom:2px solid var(--ink);padding-bottom:16px;margin-bottom:28px}
.hd h1{margin:0 0 6px;font-size:24px;font-weight:500;letter-spacing:-.3px}
.hd .meta{color:var(--ink3);font-size:12px}
.badge{display:inline-block;padding:2px 10px;border-radius:11px;font-size:12px;
  border:1px solid var(--line);background:#fff;color:var(--ink2);margin-left:8px;vertical-align:2px}
.badge.up{color:var(--up);border-color:#EBC9C4}
.badge.down{color:var(--down);border-color:#C4DFD0}
h2{font-size:14px;font-weight:500;color:var(--ink2);letter-spacing:.6px;
  margin:36px 0 14px;padding-left:9px;border-left:3px solid var(--ink)}
.cards{display:grid;gap:10px}
.g6{grid-template-columns:repeat(6,1fr)}
.g4{grid-template-columns:repeat(4,1fr)}
.g3{grid-template-columns:repeat(3,1fr)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.k{font-size:11px;color:var(--ink3);margin-bottom:4px}
.v{font-size:19px;font-weight:500;letter-spacing:-.4px;font-variant-numeric:tabular-nums}
.s{font-size:11px;color:var(--ink3);margin-top:2px}
.up{color:var(--up)} .down{color:var(--down)} .flat{color:var(--ink2)}
table{width:100%;border-collapse:collapse;background:var(--card);
  border:1px solid var(--line);border-radius:10px;overflow:hidden}
th{font-size:11px;font-weight:500;color:var(--ink3);text-align:left;
  padding:10px 12px;background:#FAFAF8;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:11px 12px;border-bottom:1px solid #F0EFEB;font-variant-numeric:tabular-nums;font-size:13px}
tr:last-child td{border-bottom:none}
tr:hover td{background:#FCFCFA}
.code{font-size:11px;color:var(--ink3);font-variant-numeric:tabular-nums}
.tag{display:inline-block;padding:1px 7px;background:#F1F1ED;border-radius:4px;
  font-size:11px;color:var(--ink2)}
.bar{height:5px;border-radius:3px;background:#EDECE7;overflow:hidden;width:74px;display:inline-block;vertical-align:middle}
.bar span{display:block;height:100%;border-radius:3px}
.score{font-weight:500;font-variant-numeric:tabular-nums;margin-right:8px}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;margin:2px 4px 2px 0;white-space:nowrap}
.pill.P0{background:#FBEAE8;color:var(--p0)}
.pill.P1{background:#FBF1E1;color:var(--p1)}
.pill.P2{background:#F1F1ED;color:var(--p2)}
.note{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--accent);
  border-radius:8px;padding:16px 18px;font-size:13px;line-height:1.85}
.note li{margin-bottom:4px}
.empty{color:var(--ink3);font-size:13px;padding:18px;text-align:center;
  background:var(--card);border:1px dashed var(--line);border-radius:10px}
.ft{margin-top:44px;padding-top:16px;border-top:1px solid var(--line);
  font-size:11px;color:var(--ink3);line-height:1.8}
.dis{margin-top:18px;padding:14px 16px;background:#FBFBF9;border:1px solid var(--line);
  border-radius:8px;font-size:11px;color:var(--ink2);line-height:1.8}
@media(max-width:760px){.g6{grid-template-columns:repeat(3,1fr)}.g4{grid-template-columns:repeat(2,1fr)}
  .g3{grid-template-columns:1fr}.wrap{padding:24px 16px 48px}}
"""


def _cls(v) -> str:
    if v is None:
        return "flat"
    return "up" if v > 0 else ("down" if v < 0 else "flat")


def _pct(v, digits: int = 2) -> str:
    return "—" if v is None else f"{v:+.{digits}f}%"


def _amt(v) -> str:
    """成交额：元 → 亿/万亿"""
    if not v:
        return "—"
    if v >= 1e12:
        return f"{v / 1e12:.2f} 万亿"
    return f"{v / 1e8:.0f} 亿"


def _num(v, digits: int = 2, suffix: str = "") -> str:
    if v is None:
        return "—"
    return f"{v:,.{digits}f}{suffix}"


def _score_color(score: int) -> str:
    if score >= 75:
        return "#C0392B"
    if score >= 60:
        return "#D98880"
    if score >= 45:
        return "#B9B7AE"
    return "#1E8449"


def _stop_cell(h: dict) -> str:
    """距止损一栏。止损已被击穿时必须显式说"已破"，不能显示正的百分比——
    那会被读成"还没到止损"，从而继续扛单。"""
    if h.get("stop_broken"):
        return "<span class='up' style='font-weight:500'>已破止损</span>"
    d = h.get("stop_dist_pct")
    if d is None:
        return "—"
    return f"<span class='{_cls(d)}'>{_pct(d, 1)}</span>"


def render(brief: dict, cfg: dict) -> str:
    m = brief["market"]
    state = m.get("state") or {}
    b = m.get("breadth") or {}
    indices = m.get("indices") or []
    g = brief["global"]
    watch = brief["watch"]
    alerts = brief["alerts_all"]

    D = brief["date"]
    p0 = [a for a in alerts if a["level"] == "P0"]
    p1 = [a for a in alerts if a["level"] == "P1"]
    p2 = [a for a in alerts if a["level"] == "P2"]

    parts: list[str] = []
    parts.append('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">')
    parts.append('<meta name="viewport" content="width=device-width,initial-scale=1">')
    parts.append(f"<title>{cfg['meta']['name']} {D}</title>")
    parts.append(f"<style>{STYLE}</style></head><body><div class='wrap'>")

    # ---------- 头部 ----------
    parts.append("<div class='hd'>")
    parts.append(f"<h1>{cfg['meta']['name']}")
    if state.get("state"):
        tone = state.get("tone", "neutral")
        cls = "up" if tone == "up" else ("down" if tone == "down" else "")
        parts.append(f"<span class='badge {cls}'>{state['state']}</span>")
    parts.append("</h1>")
    parts.append(
        f"<div class='meta'>数据日期 {D} ｜ 生成于 {brief['generated_at']} ｜ "
        f"数据源 {cfg['meta']['source_primary']}</div>"
    )
    parts.append("</div>")

    # ---------- 首屏结论 ----------
    parts.append("<h2>结论先行</h2><div class='note'><ul>")
    for line in brief["summary"]:
        parts.append(f"<li>{line}</li>")
    parts.append("</ul></div>")

    # ---------- 市场概览 ----------
    parts.append("<h2>市场概览</h2><div class='cards g6'>")
    idx_map = {i["code"]: i for i in indices}
    for code, label in (
        ("000001", "上证指数"),
        ("399001", "深证成指"),
        ("399006", "创业板指"),
        ("000688", "科创50"),
        ("000300", "沪深300"),
    ):
        it = idx_map.get(code)
        if it:
            parts.append(
                f"<div class='card'><div class='k'>{label}</div>"
                f"<div class='v {_cls(it['pct_chg'])}'>{_num(it['close'])}</div>"
                f"<div class='s {_cls(it['pct_chg'])}'>{_pct(it['pct_chg'])}</div></div>"
            )
    parts.append(
        f"<div class='card'><div class='k'>成交额</div>"
        f"<div class='v'>{_amt(b.get('total_amount'))}</div>"
        f"<div class='s'>全市场合计</div></div>"
    )
    parts.append("</div>")

    if b:
        up, down = b.get("up", 0), b.get("down", 0)
        total = up + down + b.get("flat", 0) or 1
        parts.append("<div class='cards g4' style='margin-top:10px'>")
        parts.append(
            f"<div class='card'><div class='k'>上涨 / 下跌</div>"
            f"<div class='v'><span class='up'>{up}</span>"
            f"<span style='color:#C9C7BF;font-size:15px'> / </span>"
            f"<span class='down'>{down}</span></div>"
            f"<div class='s'>上涨占比 {up / total * 100:.0f}%</div></div>"
        )
        parts.append(
            f"<div class='card'><div class='k'>涨跌中位数</div>"
            f"<div class='v {_cls(b.get('median_pct'))}'>{_pct(b.get('median_pct'))}</div>"
            f"<div class='s'>比指数更能反映体感</div></div>"
        )
        parts.append(
            f"<div class='card'><div class='k'>涨停 / 跌停</div>"
            f"<div class='v'><span class='up'>{b.get('limit_up', 0)}</span>"
            f"<span style='color:#C9C7BF;font-size:15px'> / </span>"
            f"<span class='down'>{b.get('limit_down', 0)}</span></div>"
            f"<div class='s'>按 ±9.8% 估算</div></div>"
        )
        parts.append(
            f"<div class='card'><div class='k'>样本数</div>"
            f"<div class='v'>{b.get('total_count', 0)}</div>"
            f"<div class='s'>沪深京 A 股</div></div>"
        )
        parts.append("</div>")

    # ---------- 短线市场温度 ----------
    temp = m.get("temp") or {}
    if temp:
        tone = temp.get("tone", "neutral")
        cls = "up" if tone == "up" else ("down" if tone == "down" else "")
        edge = "up" if tone == "up" else ("down" if tone == "down" else "ink3")
        parts.append("<h2>短线市场温度</h2>")
        parts.append(
            f"<div class='note' style='border-left-color:var(--{edge})'>"
            f"<b style='font-size:15px'>{temp.get('tag', '')}</b> "
            f"<span class='badge {cls}'>{temp.get('score', 0):.0f}/100</span><br>"
            f"涨停 <b>{temp.get('limit_up', 0)}</b> 家 ／ 跌停 <b>{temp.get('limit_down', 0)}</b> 家 ｜ "
            f"上涨占比 <b>{temp.get('up_ratio', 0) * 100:.0f}%</b> ｜ "
            f"涨跌中位数 <b class='{_cls(temp.get('median_pct'))}'>{_pct(temp.get('median_pct'))}</b>"
            + (f"<br><span style='color:var(--p1)'>⚠ {temp['warn']}</span>" if temp.get("warn") else "")
            + "</div>"
        )

    # ---------- 情绪周期 ----------
    sentiment = brief.get("sentiment") or {}
    phase = brief.get("market_phase", "")
    if sentiment and phase:
        phase_colors = {
            "冰点": ("#1E8449", "#E8F6EF"), "退潮": ("#B8741A", "#FBF1E1"),
            "中性": ("#5F5E5A", "#F1F1ED"), "回暖": ("#C0392B", "#FBEAE8"),
            "高潮": ("#8E44AD", "#F4ECF7"),
        }
        pc, pbg = phase_colors.get(phase, ("#5F5E5A", "#F1F1ED"))
        score = sentiment.get("score", 0)
        pos_range = temp.get("position_range", (0.3, 0.7))
        preferred = temp.get("preferred_strategies", [])
        sig_signals = temp.get("sentiment_signals", [])

        parts.append("<h2>情绪周期</h2>")
        parts.append("<div class='cards g4'>")
        parts.append(
            f"<div class='card' style='border-left:3px solid {pc}'>"
            f"<div class='k'>当前阶段</div>"
            f"<div class='v' style='color:{pc}'>{phase}</div>"
            f"<div class='s'>情绪分 {score:.0f}/100</div></div>"
        )
        parts.append(
            f"<div class='card'><div class='k'>建议仓位</div>"
            f"<div class='v'>{pos_range[0]*100:.0f}%–{pos_range[1]*100:.0f}%</div>"
            f"<div class='s'>根据情绪阶段推导</div></div>"
        )
        parts.append(
            f"<div class='card'><div class='k'>适配策略</div>"
            f"<div class='v' style='font-size:14px'>{'、'.join(preferred) if preferred else '—'}</div>"
            f"<div class='s'>情绪阶段决定策略选择</div></div>"
        )
        parts.append(
            f"<div class='card'><div class='k'>连板高度</div>"
            f"<div class='v'>{sentiment.get('board_height', 0)}</div>"
            f"<div class='s'>情绪天花板</div></div>"
        )
        parts.append("</div>")

        # V2 新增：精确打板情绪指标行
        break_rate = sentiment.get("break_rate", 0)
        promo_rate = sentiment.get("promotion_rate", 0)
        nf = sentiment.get("north_flow") or {}
        nf_total = nf.get("total")
        zt_count = sentiment.get("limit_up", 0)
        dt_count = sentiment.get("limit_down", 0)
        br_color = "#C0392B" if break_rate > 40 else ("#B8741A" if break_rate > 25 else "#1E8449")
        nf_color = "#C0392B" if (nf_total or 0) > 0 else "#1E8449"

        parts.append("<div class='cards g4' style='margin-top:8px'>")
        parts.append(
            f"<div class='card'><div class='k'>炸板率</div>"
            f"<div class='v' style='color:{br_color}'>{break_rate:.1f}%</div>"
            f"<div class='s'>{'封板意愿弱' if break_rate > 40 else '封板尚可' if break_rate > 25 else '封板强劲'}</div></div>"
        )
        parts.append(
            f"<div class='card'><div class='k'>晋级率</div>"
            f"<div class='v'>{promo_rate*100:.0f}%</div>"
            f"<div class='s'>昨涨停今连板比例</div></div>"
        )
        parts.append(
            f"<div class='card'><div class='k'>北向资金</div>"
            f"<div class='v' style='color:{nf_color}'>{nf_total:+.1f}亿</div>" if nf_total is not None else
            f"<div class='card'><div class='k'>北向资金</div><div class='v'>—</div>"
        )
        parts.append(f"<div class='s'>{'沪股通' if nf.get('source') == 'ths' else '东财'}</div></div>")
        parts.append(
            f"<div class='card'><div class='k'>涨停/跌停</div>"
            f"<div class='v'><span class='up'>{zt_count}</span> / <span class='down'>{dt_count}</span></div>"
            f"<div class='s'>数据源 {sentiment.get('source', '—')}</div></div>"
        )
        parts.append("</div>")

        # 连板梯队龙头
        ladder = sentiment.get("board_ladder") or []
        if ladder:
            ladder_str = " · ".join(
                f"<b>{s['name']}</b>({s['height']}板)" for s in ladder[:4]
            )
            parts.append(
                f"<div class='card' style='margin-top:8px;padding:8px 14px'>"
                f"<div class='k'>连板龙头</div>"
                f"<div class='s' style='margin-top:2px'>{ladder_str}</div></div>"
            )

        if sig_signals:
            parts.append(
                "<div class='card' style='margin-top:10px;border-left:3px solid var(--p1)'>"
                "<div class='k'>情绪信号</div><div class='s' style='line-height:1.8'>"
                + "<br>".join(f"• {s}" for s in sig_signals)
                + "</div></div>"
            )

    # ---------- 今日交易计划（短线核心）----------
    if brief.get("style") == "short" and watch:
        ready = sorted(
            [r for r in watch if (r.get("plan") or {}).get("ready")],
            key=lambda r: (-(r["plan"].get("rr") or 0), -r["score"]),
        )
        parts.append("<h2>今日交易计划</h2>")
        if ready:
            parts.append(
                "<table><thead><tr>"
                "<th style='width:30px'>#</th><th>标的</th><th style='width:52px'>评分</th>"
                "<th style='width:118px'>买点区间</th><th style='width:70px'>止损</th>"
                "<th style='width:70px'>目标①</th><th style='width:70px'>目标②</th>"
                "<th style='width:56px'>盈亏比</th><th style='width:58px'>止损幅度</th>"
                "<th style='width:86px'>建议股数</th></tr></thead><tbody>"
            )
            for i, row in enumerate(ready, 1):
                p = row["plan"]
                sc = row["score"]
                hold_badge = ("<span class='tag' style='background:#EAF0F6;color:#2A5C8A'>持仓</span>"
                              if row.get("is_holding") else "")
                parts.append(
                    f"<tr><td class='code'>{i}</td>"
                    f"<td><b style='font-weight:500'>{row['name']}</b> {hold_badge}"
                    f"<div class='code'>{row['code']} · {row.get('tag', '')}</div></td>"
                    f"<td><span class='score' style='color:{_score_color(sc)}'>{sc}</span>"
                    f"<div class='code'>{row['label']}</div></td>"
                    f"<td><b style='font-weight:500'>{p['buy_lo']:.2f} – {p['buy_hi']:.2f}</b>"
                    f"<div class='code'>现价 {(row['snap'].get('close') or 0):.2f}</div></td>"
                    f"<td class='down'>{p['stop']:.2f}</td>"
                    f"<td class='up'>{p['target1']:.2f}</td>"
                    f"<td>{p['target2']:.2f}</td>"
                    f"<td><b style='font-weight:500'>{p['rr']:.1f}</b></td>"
                    f"<td class='code'>{p['risk_pct']:.1f}%</td>"
                    f"<td class='code'>{p['shares']} 股<div style='color:var(--ink3)'>"
                    f"{p['amount'] / 10000:.1f} 万</div></td></tr>"
                )
            parts.append("</tbody></table>")
            parts.append(
                "<div class='card' style='margin-top:10px'><div class='k'>执行说明</div>"
                "<div class='s' style='line-height:1.75'>"
                "买点是挂单区间，不是保证能成交；开盘直接高开超区间上沿就放弃，不追。"
                "止损是收盘价口径，盘中破位不必立刻砍，但收盘站不回去必须走。"
                "目标①到了先减半仓，剩下的用成本价做保护。"
                "超过最大持有天数还没走出来，按时间止损离场——这条最容易违反，也最伤账户。"
                "</div></div>"
            )
        else:
            parts.append(
                "<div class='empty'>今日没有标的满足盈亏比与结构要求。<br>"
                "<span style='font-size:12px'>这不是数据缺失——上方空间不足时不建仓也是决策。</span></div>"
            )

        # 未通过的计划
        blocked = [r for r in watch if (r.get("plan") or {}).get("why")]
        if blocked:
            parts.append("<div class='cards' style='margin-top:10px'>")
            for row in sorted(blocked, key=lambda r: -r["score"]):
                p = row["plan"]
                ok = p.get("ready")
                parts.append(
                    f"<div class='card' style='padding:10px 14px'>"
                    f"<div style='display:flex;justify-content:space-between;align-items:baseline'>"
                    f"<span><b style='font-weight:500'>{row['name']}</b> "
                    f"<span class='code'>{row['code']}</span></span>"
                    f"<span class='code' style='color:{_score_color(row['score'])}'>"
                    f"{row['score']} 分 · {row['label']}</span></div>"
                    f"<div class='s' style='margin-top:3px'>{p['why']}</div></div>"
                )
            parts.append("</div>")

    # ---------- 多策略信号 ----------
    strategy_signals = brief.get("strategy_signals") or []
    if strategy_signals:
        strategy_names = {
            "board_hit": "打板", "trend_pullback": "趋势波段",
            "oversold_bounce": "超跌反弹", "event_driven": "事件驱动",
        }
        parts.append(f"<h2>多策略信号（{len(strategy_signals)} 条）</h2>")
        parts.append(
            "<table><thead><tr>"
            "<th style='width:80px'>策略</th><th>标的</th>"
            "<th style='width:50px'>方向</th><th style='width:56px'>强度</th>"
            "<th style='width:100px'>买点区间</th><th style='width:64px'>止损</th>"
            "<th style='width:56px'>盈亏比</th><th>触发原因</th>"
            "</tr></thead><tbody>"
        )
        for sig in sorted(strategy_signals, key=lambda s: -(s.get("strength") or 0)):
            sname = strategy_names.get(sig.get("strategy"), sig.get("strategy", ""))
            direction = sig.get("direction", "buy")
            dir_cls = "up" if direction == "buy" else "down"
            dir_txt = "买入" if direction == "buy" else "卖出"
            strength = sig.get("strength", 0)
            buy_lo = sig.get("buy_lo")
            buy_hi = sig.get("buy_hi")
            buy_txt = f"{buy_lo:.2f}–{buy_hi:.2f}" if buy_lo and buy_hi else "—"
            parts.append(
                f"<tr><td><span class='tag'>{sname}</span></td>"
                f"<td><b style='font-weight:500'>{sig.get('name', '')}</b>"
                f"<div class='code'>{sig.get('code', '')}</div></td>"
                f"<td class='{dir_cls}'>{dir_txt}</td>"
                f"<td><span class='score'>{strength:.0%}</span></td>"
                f"<td class='code'>{buy_txt}</td>"
                f"<td class='code down'>{_num(sig.get('stop'))}</td>"
                f"<td class='code'>{_num(sig.get('rr'), 1)}</td>"
                f"<td class='code' style='font-size:11px'>{sig.get('reason', '')}</td></tr>"
            )
        parts.append("</tbody></table>")

    # ---------- 板块 ----------
    sectors = m.get("sectors") or {}
    if sectors.get("top"):
        parts.append("<h2>板块强弱（今日主线线索）</h2><div class='cards g4'>")
        parts.append("<div class='card'><div class='k'>领涨行业</div>")
        for s in sectors["top"]:
            parts.append(
                f"<div style='display:flex;justify-content:space-between;padding:3px 0'>"
                f"<span>{s['name']}</span>"
                f"<span class='up' style='font-variant-numeric:tabular-nums'>{_pct(s['pct_chg'])}</span></div>"
            )
        parts.append("</div><div class='card'><div class='k'>领跌行业</div>")
        for s in sectors["bottom"]:
            parts.append(
                f"<div style='display:flex;justify-content:space-between;padding:3px 0'>"
                f"<span>{s['name']}</span>"
                f"<span class='down' style='font-variant-numeric:tabular-nums'>{_pct(s['pct_chg'])}</span></div>"
            )
        parts.append("</div>")
        top_amt = b.get("top_amount") or []
        if top_amt:
            parts.append("<div class='card'><div class='k'>成交额居前</div>")
            for s in top_amt[:5]:
                parts.append(
                    f"<div style='display:flex;justify-content:space-between;padding:3px 0'>"
                    f"<span>{s['name']}</span>"
                    f"<span class='{_cls(s['pct_chg'])}' style='font-variant-numeric:tabular-nums'>"
                    f"{_amt(s['amount'])}</span></div>"
                )
            parts.append("</div>")
        parts.append("<div class='card'><div class='k'>说明</div>"
                     "<div class='s' style='line-height:1.7'>行业分类按东方财富一级行业口径。"
                     "领涨板块只说明当日资金流向，不代表持续性。</div></div>")
        parts.append("</div>")

    # ---------- 外围 ----------
    parts.append("<h2>外围观测（只看不做）</h2><div class='cards g3'>")
    for title, payload in (
        ("美股（上一交易日收盘）", g.get("us")),
        ("港股（最新）", g.get("hk")),
        ("加密（24h）", g.get("crypto")),
    ):
        parts.append(f"<div class='card'><div class='k'>{title}</div>")
        if payload and payload.get("ok") and payload.get("data"):
            for it in payload["data"]:
                parts.append(
                    f"<div style='display:flex;justify-content:space-between;padding:3px 0'>"
                    f"<span>{it['name']}</span>"
                    f"<span class='{_cls(it.get('pct_chg'))}' style='font-variant-numeric:tabular-nums'>"
                    f"{_num(it.get('close'))}　{_pct(it.get('pct_chg'))}</span></div>"
                )
        else:
            parts.append("<div class='s'>数据暂不可用</div>")
        parts.append("</div>")
    parts.append("</div>")

    # ---------- 提醒 ----------
    short = brief.get("style") == "short"
    parts.append(f"<h2>{'风险与卖出信号' if short else '今日提醒'}</h2>")
    if alerts:
        parts.append("<table><thead><tr><th style='width:60px'>级别</th>"
                     "<th style='width:110px'>标的</th><th style='width:120px'>规则</th>"
                     "<th>说明</th></tr></thead><tbody>")
        for a in sorted(alerts, key=lambda x: (x["level"], x.get("code") or "")):
            parts.append(
                f"<tr><td><span class='pill {a['level']}'>{a['level']}</span></td>"
                f"<td>{a.get('name', '')}<div class='code'>{a.get('code', '')}</div></td>"
                f"<td>{a['rule']}</td><td>{a['message']}</td></tr>"
            )
        parts.append("</tbody></table>")
    else:
        parts.append("<div class='empty'>今日无触发阈值的异常</div>")

    # ---------- 自选池 ----------
    parts.append(f"<h2>自选池明细（{len(watch)} 只）</h2>")
    if watch:
        if short:
            parts.append("<table><thead><tr>"
                         "<th style='width:30px'>#</th><th>标的</th>"
                         "<th style='width:62px'>收盘</th><th style='width:58px'>今日</th>"
                         "<th style='width:58px'>5日</th><th style='width:52px'>量比</th>"
                         "<th style='width:52px'>换手</th><th style='width:50px'>RSI6</th>"
                         "<th style='width:58px'>偏离5日</th><th style='width:50px'>ATR%</th>"
                         "<th style='width:80px'>评分拆解</th></tr></thead><tbody>")
        else:
            parts.append("<table><thead><tr>"
                         "<th style='width:34px'>#</th><th>标的</th><th style='width:78px'>行业</th>"
                         "<th style='width:84px'>收盘</th><th style='width:74px'>涨跌</th>"
                         "<th style='width:96px'>20日走势</th><th style='width:150px'>评分</th>"
                         "<th style='width:60px'>RSI</th><th>信号</th></tr></thead><tbody>")
        for i, row in enumerate(sorted(watch, key=lambda x: -x["score"]), 1):
            snap = row.get("snap") or {}
            sc = row["score"]
            if short:
                partsd = row.get("parts") or {}
                brk = " ".join(
                    f"<span style='color:{'var(--up)' if v > 0 else 'var(--ink3)'}'>{k}{v:+.0f}</span>"
                    for k, v in partsd.items() if isinstance(v, (int, float))
                )
                hold_badge = ("<span class='tag' style='background:#EAF0F6;color:#2A5C8A'>持仓</span>"
                              if row.get("is_holding") else "")
                parts.append(
                    f"<tr><td class='code'>{i}</td>"
                    f"<td><b style='font-weight:500'>{row['name']}</b> {hold_badge}"
                    f"<div class='code'>{row['code']}</div></td>"
                    f"<td>{_num(snap.get('close'))}</td>"
                    f"<td class='{_cls(snap.get('pct_chg'))}'>{_pct(snap.get('pct_chg'))}</td>"
                    f"<td class='{_cls((snap.get('ret5') or 0) * 100)}'>"
                    f"{_pct((snap.get('ret5') or 0) * 100 if snap.get('ret5') is not None else None)}</td>"
                    f"<td class='code'>{_num(snap.get('vol_ratio'), 1)}</td>"
                    f"<td class='code'>{_num(snap.get('turnover'), 1)}</td>"
                    f"<td class='code'>{_num(snap.get('rsi6'), 0)}</td>"
                    f"<td class='code'>{_pct((snap.get('bias5') or 0) * 100 if snap.get('bias5') is not None else None, 1)}</td>"
                    f"<td class='code'>{_num(snap.get('atr_pct'), 1)}</td>"
                    f"<td><span class='score' style='color:{_score_color(sc)}'>{sc}</span>"
                    f"<div class='code' style='font-size:10px;white-space:nowrap'>{brk}</div></td></tr>"
                )
                continue
            sig = "".join(
                f"<span class='pill {a['level']}'>{a['rule']}</span>" for a in row["alerts"]
            ) or "<span style='color:#B9B7AE'>—</span>"
            parts.append(
                f"<tr><td class='code'>{i}</td>"
                f"<td><b style='font-weight:500'>{row['name']}</b>"
                f"<div class='code'>{row['code']}</div></td>"
                f"<td><span class='tag'>{row.get('tag', '')}</span></td>"
                f"<td>{_num(snap.get('close'))}</td>"
                f"<td class='{_cls(snap.get('pct_chg'))}'>{_pct(snap.get('pct_chg'))}</td>"
                f"<td>{row.get('spark', '')}</td>"
                f"<td><span class='score' style='color:{_score_color(sc)}'>{sc}</span>"
                f"<span class='bar'><span style='width:{sc}%;background:{_score_color(sc)}'></span></span>"
                f"<span class='code' style='margin-left:6px'>{row['label']}</span></td>"
                f"<td class='code'>{_num(snap.get('rsi14'), 0)}</td>"
                f"<td>{sig}</td></tr>"
            )
        parts.append("</tbody></table>")
        if short:
            parts.append(
                "<div class='card' style='margin-top:10px'><div class='k'>怎么读这张表</div>"
                "<div class='s' style='line-height:1.75'>"
                "「5日」是短线动量主因子；「量比」与「换手」看资金参与度，"
                "量比 1.2–2.2 最健康，超过 3.5 多为异动或出货；"
                "「偏离5日」为 BIAS5，超过 6% 属追高区，超过 10% 会触发 P1；"
                "「ATR%」是日均波幅，决定止损宽度——2.5%–6% 有短线空间，"
                "低于 1.8% 的大盘股做短线赚不到钱。"
                "</div></div>"
            )
    else:
        parts.append("<div class='empty'>自选池为空，请在 config.yaml 里配置 watchlist</div>")

    # ---------- 持仓 ----------
    holdings = brief.get("holdings") or []
    if holdings:
        parts.append("<h2>持仓</h2><table><thead><tr>"
                     "<th>标的</th><th>数量</th><th>成本</th><th>现价</th>"
                     "<th>浮动盈亏</th><th>市值</th><th style='width:64px'>持有天数</th>"
                     "<th style='width:76px'>止损位</th><th style='width:76px'>距止损</th>"
                     "</tr></thead><tbody>")
        total_mv = sum(h["market_value"] for h in holdings) or 1
        max_hold = (brief.get("risk") or {}).get("max_hold_days")
        for h in holdings:
            held = h.get("held_days")
            held_txt = "—" if held is None else f"{held}"
            if held is not None and max_hold and held >= max_hold:
                held_txt = f"<span class='up'>{held} ⚠</span>"
            sd = h.get("stop_dist_pct")
            parts.append(
                f"<tr><td><b style='font-weight:500'>{h['name']}</b>"
                f"<div class='code'>{h['code']}</div></td>"
                f"<td>{h['qty']}</td><td>{_num(h['cost'])}</td><td>{_num(h['price'])}</td>"
                f"<td class='{_cls(h['pnl_pct'])}'>{_pct(h['pnl_pct'])}"
                f"<span class='code'> {_num(h['pnl_amt'], 0)}</span></td>"
                f"<td>{_num(h['market_value'], 0)}</td>"
                f"<td class='code'>{held_txt}</td>"
                f"<td class='code'>{_num(h.get('stop'))}"
                f"<div style='font-size:10px;color:var(--ink3)'>{h.get('stop_basis', '')}</div></td>"
                f"<td class='code'>{_stop_cell(h)}</td></tr>"
            )
        parts.append("</tbody></table>")
        parts.append(
            "<div class='card' style='margin-top:10px'><div class='k'>集中度</div>"
            f"<div class='s'>最大单票占比 {max(h['market_value'] for h in holdings) / total_mv * 100:.1f}%，"
            f"共 {len(holdings)} 只。短线单票建议不超过 30%，持仓 3 只以上容易顾不过来。</div></div>"
        )

    # ---------- 页脚 ----------
    parts.append("<div class='ft'>")
    parts.append("<b>数据来源与时点</b><br>")
    parts.append(
        f"A 股行情/指数/板块/日线：{cfg['meta']['source_primary']}，数据日期 {D}；"
        f"主源不可用时自动切换至{cfg['meta']['source_secondary']}，页脚会注明。"
        "美股、港股、加密：东方财富或腾讯公开行情接口。<br>"
        "行业分类与涨跌停统计为估算口径，与交易所官方口径可能有差异。"
        "所有指标由本系统按日线前复权数据计算，非交易所实时推送；"
        "盘前简报的「现价」为上一交易日收盘价。"
    )
    if brief.get("data_health"):
        parts.append("<br><br><b>数据缺口</b><br>")
        parts.append("；".join(brief["data_health"]))
    if brief.get("data_notes"):
        parts.append("<br><br><b>数据说明</b><br>")
        parts.append("；".join(brief["data_notes"]))
    parts.append("</div>")

    parts.append(
        "<div class='dis'><b>免责声明</b>：以上内容基于公开数据和量化分析，仅供参考，"
        "不构成投资建议。市场有风险，投资需谨慎。任何投资决策应结合个人风险承受能力、"
        "资金状况和投资目标独立判断，必要时咨询持牌专业机构。过往表现不预示未来收益。"
        "本系统处于模拟观察阶段，不接入任何真实交易通道。</div>"
    )

    parts.append("</div></body></html>")
    return "\n".join(parts)
