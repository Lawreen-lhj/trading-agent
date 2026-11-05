/* 交易 Agent 控制台 —— 前端逻辑（无框架，纯 DOM） */
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const S = {
  data: null,
  cfg: null,
  view: 'dashboard',
  planOnly: false,
  alertLevel: 'all',
  alertDate: 'all',
  histSel: null,
  running: false,
  simAccount: 'board_hit',
  simOverview: null,
  simDetail: null,
  simMode: 'single',
  simCompare: null,
  simKlineCode: null,
  simKline: null,
  simKlineLoading: null,
  simLoading: false,
  simErr: null,
  pulse: null,
  pulseErr: null,
  pulseBoard: 'industry',
  activity: null,
  activityErr: null,
  quotes: null,
  quotesTs: null,
};

const TITLES = {
  dashboard: ['仪表盘', '今天的市场状态与可执行计划'],
  pulse: ['行情脉搏', '当日主力资金 / 大单异动 / 涨停炸板 · 60s 自动刷新'],
  screening: ['选股', '两段漏斗全市场选股 · 每只票附入选理由'],
  plans: ['交易计划', '买点 / 止损 / 目标 / 仓位，全部由规则算出'],
  watch: ['自选池', '短线池建议 10–20 只，太多会看不过来'],
  holdings: ['持仓', '填了持仓才算得出止损距离和时间止损'],
  simulation: ['模拟交易', '四策略并行模拟盘 · 结合开盘收盘时间自动买卖'],
  alerts: ['提醒', '历史告警留痕，用于校验规则有没有误报'],
  history: ['简报历史', '每天一份 HTML 存档'],
  settings: ['设置', '风控参数与通知方式'],
};

/* ---------------- 工具 ---------------- */
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const num = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v))
  ? '—' : Number(v).toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d });

const pct = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v))
  ? '—' : `${v > 0 ? '+' : ''}${Number(v).toFixed(d)}%`;

const cls = (v) => (v === null || v === undefined || Number.isNaN(v)) ? 'flat' : (v > 0 ? 'up' : (v < 0 ? 'down' : 'flat'));

const amt = (v) => {
  if (v === null || v === undefined) return '—';
  if (Math.abs(v) >= 1e8) return (v / 1e8).toFixed(2) + ' 亿';
  if (Math.abs(v) >= 1e4) return (v / 1e4).toFixed(1) + ' 万';
  return Number(v).toFixed(0);
};

const scoreColor = (s) => s >= 75 ? '#B4382E' : s >= 60 ? '#C97B63' : s >= 45 ? '#B4B1A6' : '#3E8E68';

function toast(msg, bad = false) {
  const el = document.createElement('div');
  el.className = 'toast' + (bad ? ' bad' : '');
  el.textContent = msg;
  $('#toast').appendChild(el);
  setTimeout(() => el.remove(), bad ? 5200 : 2600);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  let j;
  try { j = await res.json(); } catch { throw new Error(`响应解析失败（HTTP ${res.status}）`); }
  if (!res.ok || !j.ok) {
    const e = new Error(j.error || `HTTP ${res.status}`);
    e.payload = j;
    throw e;
  }
  return j.data;
}

const clone = (o) => JSON.parse(JSON.stringify(o ?? {}));

/* ---------------- 加载 ---------------- */
async function load(keepView = true) {
  const d = await api('/api/state');
  S.data = d;
  S.cfg = clone(d.config);
  if (!S.histSel && d.history.length) S.histSel = d.history[0].date;
  render();
}

/* ---------------- 路由 ---------------- */
function setView(v) {
  if (!TITLES[v]) v = 'dashboard';
  S.view = v;
  $$('#nav a').forEach((a) => a.classList.toggle('on', a.dataset.view === v));
  render();
}

function render() {
  if (!S.data) return;
  const [t, sub] = TITLES[S.view];
  $('#page-title').textContent = t;
  const b = S.data.brief;
  $('#page-sub').textContent = sub + (b ? `　·　数据日期 ${b.date}（生成于 ${b.generated_at}）` : '　·　尚无简报');
  updateNav();
  const fn = {
    dashboard: viewDashboard, pulse: viewPulse, screening: viewScreening, plans: viewPlans,
    watch: viewWatch, holdings: viewHoldings, simulation: viewSimulation, alerts: viewAlerts,
    history: viewHistory, settings: viewSettings,
  }[S.view];
  $('#stage').innerHTML = fn();
}

function updateNav() {
  const d = S.data;
  const ready = d.brief ? d.brief.watch.filter((r) => (r.plan || {}).ready).length : 0;
  const nPlans = $('#nav-plans');
  nPlans.textContent = ready || '';
  nPlans.className = 'n' + (ready ? ' hot' : '');

  $('#nav-watch').textContent = (d.config.watchlist || []).length;
  $('#nav-hold').textContent = (d.config.holdings || []).length || '';

  const ns = $('#nav-screen');
  const scRows = (S.screening && S.screening.rows) || [];
  ns.textContent = scRows.length || '';

  const p0 = d.brief ? (d.brief.alerts_all || []).filter((a) => a.level === 'P0').length : 0;
  const na = $('#nav-alerts');
  na.textContent = p0 || '';
  na.className = 'n' + (p0 ? ' hot' : '');

  const nSim = $('#nav-sim');
  if (nSim) {
    const held = S.simOverview ? (S.simOverview.accounts || []).filter((a) => a.position_count > 0).length : 0;
    nSim.textContent = held || '';
    nSim.className = 'n' + (held ? ' hot' : '');
  }
}

/* ---------------- 数据：行情脉搏 / 系统活动流 ---------------- */
function loadPulse(force) {
  if (S.pulse && !force) return Promise.resolve();
  return api('/api/pulse')
    .then((d) => { S.pulse = d; S.pulseErr = null; })
    .catch((e) => { S.pulseErr = e.message; });
}
function loadActivity(limit) {
  return api(`/api/activity?limit=${limit || 40}`)
    .then((d) => { S.activity = d; S.activityErr = null; })
    .catch((e) => { S.activityErr = e.message; });
}
function ensurePulse() {
  loadPulse().then(() => { if (S.view === 'pulse') render(); });
  loadActivity(40).then(() => { if (S.view === 'pulse' || S.view === 'dashboard') render(); });
}
function ensureActivity() {
  loadActivity(10).then(() => { if (S.view === 'dashboard') render(); });
}

/* ---------------- 数据：实时现价（计划表/自选池/持仓） ---------------- */
function collectQuoteCodes() {
  const set = new Set();
  const pad = (c) => String(c).padStart(6, '0');
  const b = (S.data || {}).brief || {};
  if (S.view === 'dashboard' || S.view === 'plans') {
    (b.watch || []).forEach((r) => r.code && set.add(pad(r.code)));
  } else if (S.view === 'watch') {
    ((S.cfg || {}).watchlist || []).forEach((it) => it.code && set.add(pad(it.code)));
  } else if (S.view === 'holdings') {
    ((S.cfg || {}).holdings || []).forEach((h) => h.code && set.add(pad(h.code)));
  }
  return [...set];
}

async function loadQuotes() {
  const codes = collectQuoteCodes();
  if (!codes.length) return;
  try {
    const d = await api('/api/quotes?codes=' + codes.join(','));
    S.quotes = (d && d.quotes) || {};
    S.quotesTs = d && d.ts;
  } catch (e) {
    /* 现价拿不到就退回简报快照，不打断渲染 */
  }
}

// 按代码取实时报价（无则 null）
const lq = (code) => (S.quotes || {})[String(code).padStart(6, '0')] || null;
// 现价：优先实时，退回快照价
function livePrice(code, snapPrice) {
  const q = lq(code);
  return q && q.price != null ? q.price : snapPrice;
}

const ACT_KIND_STYLE = {
  buy: 'hold', sell: 'ok', skip: 'gray', scan: 'blue', warn: 'bad',
  start: 'blue', rescan: 'blue', snapshot: 'ok', info: 'gray',
};
const ACT_KIND_CN = {
  buy: '买入', sell: '卖出', skip: '跳过', scan: '扫描', warn: '告警',
  start: '启动', rescan: '补扫', snapshot: '快照', info: '信息',
};

function activitySection(limit) {
  const rows = (S.activity || []).slice(-limit);
  const body = rows.length ? `
  <div class="card tight"><div class="tw"><table>
    <thead><tr><th style="width:70px">时间</th><th style="width:64px">类型</th><th style="width:96px">策略</th><th>内容</th></tr></thead>
    <tbody>
    ${rows.slice().reverse().map((r) => `<tr>
      <td class="muted" style="font-size:11.5px">${esc(r.ts)}</td>
      <td><span class="pill ${ACT_KIND_STYLE[r.kind] || 'gray'}">${esc(ACT_KIND_CN[r.kind] || r.kind)}</span></td>
      <td class="muted" style="font-size:11.5px">${esc(r.strategy || '—')}</td>
      <td style="font-size:12.5px">${esc(r.message || '')}</td>
    </tr>`).join('')}
    </tbody>
  </table></div></div>`
    : `<div class="empty">暂无系统活动。run_sim_live.py 运行后每轮扫描/跳过/买卖/快照会逐条落盘到这里。</div>`;
  return `<h2 class="sec">系统活动流</h2>${body}`;
}

function pulseSpark(trend, key, color) {
  const vals = (trend || []).map((t) => t[key]).filter((v) => v != null);
  if (vals.length < 2) return `<div class="muted" style="font-size:11.5px">日内采样不足（每次刷新脉搏落一个点）。</div>`;
  const W = 600, H = 90, P = 6;
  const lo = Math.min(...vals), hi = Math.max(...vals), span = (hi - lo) || 1;
  const X = (i) => P + i / (vals.length - 1) * (W - 2 * P);
  const Y = (v) => P + (hi - v) / span * (H - 2 * P);
  const pts = vals.map((v, i) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(' ');
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:70px">
    <polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.6"/>
  </svg>
  <div class="muted" style="font-size:11px">区间 ${lo.toFixed(1)} ~ ${hi.toFixed(1)} · ${vals.length} 个采样点</div>`;
}

/* ---------------- 视图：行情脉搏 ---------------- */
function viewPulse() {
  if (!S.pulse) {
    ensurePulse();
    if (S.pulseErr) return `<div class="empty"><b>行情脉搏加载失败</b>${esc(S.pulseErr)}</div>`;
    return `<div class="empty">加载行情脉搏…</div>`;
  }
  const p = S.pulse;
  const zt = p.zt || {};
  const north = p.north || {};
  const board = S.pulseBoard;
  const sectors = board === 'concept' ? (p.sector_concept || []) : (p.sector_industry || []);

  const kpis = `
  <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(140px,1fr));margin-bottom:14px">
    ${simKpi('涨停', zt.zt_count != null ? zt.zt_count : '—', 'up')}
    ${simKpi('炸板', zt.zb_count != null ? zt.zb_count : '—', 'down')}
    ${simKpi('跌停', zt.dt_count != null ? zt.dt_count : '—', 'down')}
    ${simKpi('最高连板', zt.max_height != null ? zt.max_height + ' 板' : '—', '')}
    ${simKpi('炸板率', zt.break_rate != null ? zt.break_rate + '%' : '—', '')}
    ${simKpi('北向净流入', north.total != null ? north.total + ' 亿' : '—', cls(north.total))}
  </div>`;

  const sectorRows = (sectors || []).map((s) => `<tr>
    <td class="name"><b>${esc(s.name)}</b></td>
    <td class="r n ${cls(s.pct_chg)}">${pct(s.pct_chg)}</td>
    <td class="r n ${cls(s.net_flow)}">${(s.net_flow / 1e8).toFixed(1)} 亿</td>
    <td class="r n ${cls(s.super_large)}">${(s.super_large / 1e8).toFixed(1)} 亿</td>
    <td class="r n ${cls(s.large)}">${(s.large / 1e8).toFixed(1)} 亿</td>
    <td class="muted" style="font-size:11.5px">${esc(s.lead_stock || '')}${s.lead_pct != null ? ' ' + pct(s.lead_pct) : ''}</td>
  </tr>`).join('');

  const stockTable = (rows, title) => `
  <div class="card tight"><div class="tw">
    <div style="padding:10px 12px 6px;font-weight:600">${esc(title)}</div>
    <table>
    <thead><tr><th>标的</th><th class="r">现价</th><th class="r">涨幅</th><th class="r">主力净流入</th><th class="r">超大单</th><th class="r">净占比</th></tr></thead>
    <tbody>
    ${(rows || []).map((s) => `<tr>
      <td class="name"><b>${esc(s.name)}</b><div class="code">${esc(s.code)}</div></td>
      <td class="r n">${num(s.price)}</td>
      <td class="r n ${cls(s.pct_chg)}">${pct(s.pct_chg)}</td>
      <td class="r n ${cls(s.net_flow)}">${(s.net_flow / 1e8).toFixed(2)} 亿</td>
      <td class="r n ${cls(s.super_large)}">${(s.super_large / 1e8).toFixed(2)} 亿</td>
      <td class="r n ${Math.abs(s.net_ratio || 0) >= 10 ? 'up' : 'muted'}">${(s.net_ratio || 0).toFixed(1)}%</td>
    </tr>`).join('')}
    </tbody>
    </table>
  </div></div>`;

  const errLine = (p.errors || []).length
    ? `<div class="empty" style="margin-top:8px">部分数据源降级：${esc((p.errors || []).join('；'))}</div>` : '';

  return `
  <div class="toolbar" style="margin-bottom:12px;align-items:center">
    <span class="pill ok" style="font-size:13px">采样 ${esc(p.ts)}</span>
    <span class="muted" style="font-size:12px">60s 自动刷新 · 东财 push2 主力净流入口径</span>
    <span class="spacer"></span>
    <div class="seg">
      <button data-act="pulse-board" data-v="industry" class="${board === 'industry' ? 'on' : ''}">行业板块</button>
      <button data-act="pulse-board" data-v="concept" class="${board === 'concept' ? 'on' : ''}">概念板块</button>
    </div>
    <button class="btn ghost" data-act="pulse-refresh">刷新</button>
  </div>

  ${kpis}

  <h2 class="sec">${board === 'concept' ? '概念' : '行业'}板块主力净流入 Top</h2>
  <div class="card tight"><div class="tw"><table>
    <thead><tr><th>板块</th><th class="r">涨跌幅</th><th class="r">主力净流入</th><th class="r">超大单</th><th class="r">大单</th><th>领涨股</th></tr></thead>
    <tbody>${sectorRows || `<tr><td colspan="6" class="muted">暂无板块资金流数据。</td></tr>`}</tbody>
  </table></div></div>

  <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(380px,1fr));margin-top:14px">
    ${stockTable(p.stock_in, '个股主力净流入 Top（大手笔买入）')}
    ${stockTable(p.stock_out, '个股主力净流出 Top（大手笔卖出）')}
  </div>

  <h2 class="sec">北向资金日内趋势</h2>
  <div class="card">${pulseSpark(p.trend, 'north_net', 'var(--ink-2)')}</div>

  ${errLine}

  ${activitySection(40)}
  `;
}

/* ---------------- 视图：仪表盘 ---------------- */
function viewDashboard() {
  const d = S.data, b = d.brief;
  if (!b) {
    return `<div class="empty"><b>还没有生成过简报</b>
      点左侧「运行简报」采集一次真实行情，约 30 秒。</div>`;
  }
  const m = b.market || {};
  const temp = m.temp || {};
  const breadth = m.breadth || {};
  const state = m.state || {};
  const ready = (b.watch || []).filter((r) => (r.plan || {}).ready)
    .sort((x, y) => y.score - x.score);
  const p0 = (b.alerts_all || []).filter((a) => a.level === 'P0');
  const p1 = (b.alerts_all || []).filter((a) => a.level === 'P1');

  const health = (b.data_health || []);
  const notes = (b.data_notes || []);

  if (!S.activity) ensureActivity();
  if (!S.pulse) loadPulse().then(() => { if (S.view === 'dashboard') render(); });
  if (!S.quotes) loadQuotes().then(() => { if (S.view === 'dashboard') render(); });
  const liveIdx = (S.pulse && (S.pulse.indices || []).length) ? S.pulse.indices : null;

  return `
  ${health.length ? `<div class="banner"><div><b>数据缺口 ${health.length} 项：</b>${esc(health.join('；'))}
    <div class="muted" style="margin-top:2px">相关结论可信度下降，宁可少数据也不编数据。</div></div></div>` : ''}

  <div class="grid g-2-1">
    <div class="card">
      <div class="card-h">
        <div class="card-t">短线市场温度</div>
        <div class="card-s">${esc(b.date)} · ${esc(b.style === 'short' ? '短线模式' : '波段模式')}</div>
      </div>
      ${temp.score !== undefined ? `
      <div class="temp">
        <div class="ring" style="--v:${Math.max(0, Math.min(100, temp.score))}%;--ring-c:${temp.score >= 60 ? 'var(--up)' : temp.score >= 42 ? '#C9A227' : 'var(--down)'}">
          <div class="ring-in"><b>${Math.round(temp.score)}</b><span>赚钱效应</span></div>
        </div>
        <div class="temp-body">
          <div class="temp-tag">${esc(temp.tag || '—')}</div>
          <div class="temp-grid">
            <div>涨停 <b class="up">${temp.limit_up ?? '—'}</b> 家</div>
            <div>跌停 <b class="down">${temp.limit_down ?? '—'}</b> 家</div>
            <div>上涨占比 <b>${temp.up_ratio != null ? (temp.up_ratio * 100).toFixed(0) + '%' : '—'}</b></div>
            <div>涨跌中位数 <b class="${cls(temp.median_pct)}">${pct(temp.median_pct)}</b></div>
          </div>
        </div>
      </div>
      ${temp.warn ? `<div class="note" style="margin-top:14px;border-left-color:var(--warn)">${esc(temp.warn)}</div>` : ''}
      ` : `<div class="empty" style="padding:22px 14px">全市场快照未取到（东财接口不可用），温度暂缺。<br>
        成交额与涨跌家数也只有东财提供，此时不建议下结论。</div>`}
    </div>

    <div class="card">
      <div class="card-h"><div class="card-t">今日结论</div>
        <div class="card-s">${ready.length} 个可执行</div></div>
      <div class="note-row">
        ${(b.summary || []).map((s) => `<div class="note">${s}</div>`).join('') || '<div class="muted">无</div>'}
      </div>
    </div>
  </div>

  <h2 class="sec">指数与外围${liveIdx ? `　<span class="pill blue">实时 ${esc(S.pulse.ts)}</span>` : '　<span class="pill gray">来自日报快照</span>'}</h2>
  <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(148px,1fr))">
    ${(liveIdx || m.indices || []).map((i) => `
      <div class="card" style="padding:13px 15px">
        <div class="kpi-l">${esc(i.name)}</div>
        <div class="kpi-v">${num(i.close)}</div>
        <div class="kpi-f ${cls(i.pct_chg)}">${pct(i.pct_chg)}</div>
      </div>`).join('')}
    ${['us', 'hk'].flatMap((k) => (((b.global || {})[k] || {}).data || []).map((i) => `
      <div class="card" style="padding:13px 15px;background:var(--panel-2)">
        <div class="kpi-l">${esc(i.name)}${k === 'us' ? ' · 美股' : ''}</div>
        <div class="kpi-v">${num(i.close)}</div>
        <div class="kpi-f ${cls(i.pct_chg)}">${pct(i.pct_chg)}</div>
      </div>`)).join('')}
    ${(((b.global || {}).crypto || {}).data || []).map((i) => `
      <div class="card" style="padding:13px 15px;background:var(--panel-2)">
        <div class="kpi-l">${esc(i.name)} · 加密</div>
        <div class="kpi-v">${num(i.close)}</div>
        <div class="kpi-f ${cls(i.pct_chg)}">${pct(i.pct_chg)}</div>
      </div>`).join('')}
  </div>

  <h2 class="sec">可执行交易计划</h2>
  ${ready.length ? `
  <div class="card tight">
    <div class="tw"><table>
      <thead><tr>
        <th>标的</th><th class="r">评分</th><th class="r">现价</th>
        <th class="r">买点区间</th><th class="r">止损</th><th class="r">目标①</th>
        <th class="r">盈亏比</th><th class="r">建议股数</th><th class="r">金额</th><th>执行条件</th>
      </tr></thead>
      <tbody>
      ${ready.map((r) => {
        const p = r.plan;
        const q = lq(r.code);
        const px = livePrice(r.code, (r.snap || {}).close);
        return `<tr>
          <td class="name"><b>${esc(r.name)}</b>${r.is_holding ? ' <span class="pill hold">持仓</span>' : ''}
            <div class="code">${esc(r.code)} · ${esc(r.tag || '')}</div></td>
          <td class="r n"><b style="color:${scoreColor(r.score)}">${r.score}</b></td>
          <td class="r n">${num(px)}${q ? ` <span class="pill blue" style="font-size:10px" title="实时 ${pct(q.pct_chg)}">●</span>` : ''}</td>
          <td class="r n"><b>${num(p.buy_lo)}–${num(p.buy_hi)}</b></td>
          <td class="r n">${num(p.stop)} <span class="muted">${p.risk_pct != null ? `-${p.risk_pct.toFixed(1)}%` : ''}</span></td>
          <td class="r n">${num(p.target1)}</td>
          <td class="r n">${p.rr != null ? p.rr.toFixed(1) : '—'}</td>
          <td class="r n">${p.shares ?? '—'}</td>
          <td class="r n">${p.amount ? amt(p.amount) : '—'}</td>
          <td class="muted" style="font-size:11.5px;max-width:220px">${esc(p.why || '')}</td>
        </tr>`;
      }).join('')}
      </tbody>
    </table></div>
  </div>` : `<div class="empty"><b>今日没有满足条件的标的</b>
      不是没有强势票，而是上方空间不够、或盈亏比低于
      ${(S.cfg.risk || {}).min_rr ?? 1.8}。这种日子空仓也是决策。</div>`}

  <div class="grid g2" style="margin-top:14px">
    <div class="card">
      <div class="card-h"><div class="card-t">需要处理</div>
        <div class="card-s">P0 ${p0.length} · P1 ${p1.length}</div></div>
      ${(p0.length + p1.length) ? `<div class="list">
        ${[...p0, ...p1].slice(0, 8).map((a) => `
          <div class="list-i ${a.level === 'P0' ? 'p0' : 'p1'}">
            <i class="dot"></i>
            <div class="t"><b>${esc(a.name)}</b> · ${esc(a.rule)}
              <div class="m">${esc(a.message)}</div></div>
            <span class="pill ${a.level}">${a.level}</span>
          </div>`).join('')}
        ${(p0.length + p1.length) > 8 ? `<div class="muted" style="font-size:11.5px;padding-top:8px">
          还有 ${p0.length + p1.length - 8} 条，见「提醒」页</div>` : ''}
      </div>` : `<div class="muted" style="padding:8px 0">今日无 P0/P1 提醒。</div>`}
    </div>

    <div class="card">
      <div class="card-h"><div class="card-t">数据状态</div>
        <div class="card-s">${health.length ? `${health.length} 项缺口` : '完整'}</div></div>
      <div class="list">
        ${(m.indices || []).length ? `<div class="list-i"><i class="dot" style="background:var(--down)"></i>
          <div class="t">指数行情已取到 ${(m.indices || []).length} 个</div></div>` : ''}
        <div class="list-i"><i class="dot" style="background:${breadth.up != null ? 'var(--down)' : 'var(--up)'}"></i>
          <div class="t">全市场快照${breadth.up != null ? `：涨 ${breadth.up} / 跌 ${breadth.down}，中位数 ${pct(breadth.median_pct)}` : '：缺失'}</div></div>
        <div class="list-i"><i class="dot" style="background:${(m.sectors || {}).top ? 'var(--down)' : 'var(--up)'}"></i>
          <div class="t">行业板块${(m.sectors || {}).top ? `：${esc(((m.sectors || {}).top || []).slice(0, 3).map((s) => s.name).join('、'))}` : '：缺失'}</div></div>
        ${notes.map((n) => `<div class="list-i"><i class="dot"></i><div class="t muted">${esc(n)}</div></div>`).join('')}
      </div>
    </div>
  </div>

  ${activitySection(10)}`;
}

/* ---------------- 视图：交易计划 ---------------- */
function viewPlans() {
  const b = S.data.brief;
  if (!b) return `<div class="empty"><b>还没有简报</b>先运行一次。</div>`;
  if (!S.quotes) loadQuotes().then(() => { if (S.view === 'plans') render(); });
  const rows = (b.watch || []).map((r) => ({
    date: b.date, name: r.name, code: r.code, score: r.score, price: livePrice(r.code, (r.snap || {}).close),
    tag: r.tag, p: r.plan || {}, is_holding: r.is_holding,
  })).sort((x, y) => (y.p.ready ? 1 : 0) - (x.p.ready ? 1 : 0) || y.score - x.score);

  const shown = S.planOnly ? rows.filter((r) => r.p.ready) : rows;
  const readyN = rows.filter((r) => r.p.ready).length;
  const rrList = rows.filter((r) => r.p.ready && r.p.rr).map((r) => r.p.rr);
  const avgRR = rrList.length ? rrList.reduce((a, c) => a + c, 0) / rrList.length : null;
  const st = S.data.stats || {};

  return `
  <div class="grid g4" style="margin-bottom:16px">
    <div class="card kpi"><div class="kpi-l">今日可执行</div>
      <div class="kpi-v">${readyN}<small>/ ${rows.length} 只</small></div>
      <div class="kpi-f">评分与盈亏比双达标才给计划</div></div>
    <div class="card kpi"><div class="kpi-l">平均盈亏比</div>
      <div class="kpi-v">${avgRR ? avgRR.toFixed(2) : '—'}</div>
      <div class="kpi-f">下限 ${(S.cfg.risk || {}).min_rr ?? 1.8}</div></div>
    <div class="card kpi"><div class="kpi-l">计划资金占用</div>
      <div class="kpi-v">${(() => {
        const s = rows.filter((r) => r.p.ready).reduce((a, r) => a + (r.p.amount || 0), 0);
        return s ? amt(s) : '—';
      })()}</div>
      <div class="kpi-f">总资金 ${amt((S.cfg.risk || {}).capital)}</div></div>
    <div class="card kpi"><div class="kpi-l">历史出手频率</div>
      <div class="kpi-v">${st.ready_rows ?? 0}<small>/ ${st.total_rows ?? 0} 次</small></div>
      <div class="kpi-f">${st.days || 0} 个交易日留痕</div></div>
  </div>

  <div class="toolbar" style="margin-bottom:10px">
    <div class="seg">
      <button data-act="plan-filter" data-v="all" class="${!S.planOnly ? 'on' : ''}">全部 ${rows.length}</button>
      <button data-act="plan-filter" data-v="ready" class="${S.planOnly ? 'on' : ''}">仅可执行 ${readyN}</button>
    </div>
    <span class="spacer"></span>
    <button class="btn ghost sm" data-act="open-brief">打开完整简报</button>
  </div>

  <div class="card tight">
    ${shown.length ? `<div class="tw"><table>
      <thead><tr>
        <th>标的</th><th class="r">评分</th><th class="r">现价</th>
        <th class="r">买点区间</th><th class="r">止损</th><th class="r">距止损</th>
        <th class="r">目标①</th><th class="r">目标②</th><th class="r">盈亏比</th>
        <th class="r">股数</th><th class="r">金额</th><th>判定</th>
      </tr></thead>
      <tbody>${shown.map((r) => {
        const p = r.p, ok = !!p.ready;
        const dist = (ok && p.stop && r.price) ? (p.stop / r.price - 1) * 100 : null;
        return `<tr>
          <td class="name"><b>${esc(r.name)}</b>${r.is_holding ? ' <span class="pill hold">持仓</span>' : ''}
            <div class="code">${esc(r.code)} · ${esc(r.tag || '')}</div></td>
          <td class="r n"><b style="color:${scoreColor(r.score)}">${r.score}</b>
            <div class="bar"><i style="width:${r.score}%;background:${scoreColor(r.score)}"></i></div></td>
          <td class="r n">${num(r.price)}${lq(r.code) ? ` <span class="pill blue" style="font-size:10px" title="实时 ${pct((lq(r.code)||{}).pct_chg)}">●</span>` : ''}</td>
          <td class="r n">${ok ? `<b>${num(p.buy_lo)}–${num(p.buy_hi)}</b>` : '<span class="muted">—</span>'}</td>
          <td class="r n">${ok ? num(p.stop) : '<span class="muted">—</span>'}</td>
          <td class="r n ${cls(dist)}">${ok ? pct(dist, 1) : '—'}</td>
          <td class="r n">${ok ? num(p.target1) : '<span class="muted">—</span>'}</td>
          <td class="r n">${ok ? num(p.target2) : '<span class="muted">—</span>'}</td>
          <td class="r n">${ok && p.rr != null ? p.rr.toFixed(1) : '—'}</td>
          <td class="r n">${ok ? p.shares : '—'}</td>
          <td class="r n">${ok && p.amount ? amt(p.amount) : '—'}</td>
          <td>${ok ? '<span class="pill ok">可执行</span>' : '<span class="pill gray">不满足</span>'}
            <div class="muted" style="font-size:11px;margin-top:2px;max-width:230px">${esc(p.why || '')}</div></td>
        </tr>`;
      }).join('')}</tbody>
    </table></div>` : `<div class="empty">没有符合条件的标的</div>`}
  </div>

  <h2 class="sec">历史计划留痕（${(S.data.plans || []).length} 条）</h2>
  <div class="card tight">
    <div class="tw"><table>
      <thead><tr><th>日期</th><th>标的</th><th class="r">评分</th><th class="r">买点</th>
        <th class="r">止损</th><th class="r">目标①</th><th class="r">盈亏比</th><th>状态</th></tr></thead>
      <tbody>${(S.data.plans || []).slice(0, 60).map((r) => `<tr>
        <td class="n muted">${esc(r.run_date)}</td>
        <td class="name"><b>${esc(r.name)}</b><div class="code">${esc(r.code)}</div></td>
        <td class="r n">${r.score ?? '—'}</td>
        <td class="r n">${r.ready ? `${num(r.buy_lo)}–${num(r.buy_hi)}` : '—'}</td>
        <td class="r n">${r.ready ? num(r.stop) : '—'}</td>
        <td class="r n">${r.ready ? num(r.target1) : '—'}</td>
        <td class="r n">${r.rr != null ? Number(r.rr).toFixed(1) : '—'}</td>
        <td>${r.ready ? '<span class="pill ok">可执行</span>' : '<span class="pill gray">未触发</span>'}</td>
      </tr>`).join('') || '<tr><td colspan="8" class="muted c">暂无历史</td></tr>'}</tbody>
    </table></div>
  </div>`;
}

/* ---------------- 视图：自选池 ---------------- */
function viewWatch() {
  const list = S.cfg.watchlist || [];
  const briefMap = {};
  ((S.data.brief || {}).watch || []).forEach((r) => { briefMap[r.code] = r; });
  if (!S.quotes) loadQuotes().then(() => { if (S.view === 'watch') render(); });

  return `
  <div class="banner info">短线池建议 10–20 只。代码必须是 6 位数字（沪市 6 开头、深市 0/3 开头、科创 688）。
    改完点「保存」，写入 config.yaml（旧版本自动备份）。</div>

  <div class="toolbar" style="margin-bottom:10px">
    <button class="btn" data-act="add-watch">+ 添加标的</button>
    <button class="btn primary" data-act="save" data-scope="watch">保存自选池</button>
    <span class="spacer"></span>
    <span class="muted">共 ${list.length} 只</span>
  </div>

  <div class="card tight">
    <div class="tw"><table>
      <thead><tr><th style="width:110px">代码</th><th>名称</th><th style="width:130px">标签</th>
        <th class="r" style="width:90px">评分</th><th class="r" style="width:90px">今日涨跌</th>
        <th style="width:150px">信号</th><th style="width:52px"></th></tr></thead>
      <tbody>
      ${list.map((it, i) => {
        const r = briefMap[String(it.code).padStart(6, '0')] || {};
        const snap = r.snap || {};
        const q = lq(it.code);
        const pc = q && q.pct_chg != null ? q.pct_chg : snap.pct_chg;
        const sig = (r.alerts || []).slice(0, 2).map((a) => `<span class="pill ${a.level}">${esc(a.rule)}</span>`).join(' ')
          || '<span class="muted">—</span>';
        return `<tr>
          <td><input class="w-code" type="text" value="${esc(it.code)}" data-bind="watchlist.${i}.code" maxlength="6"></td>
          <td><input type="text" value="${esc(it.name)}" data-bind="watchlist.${i}.name" placeholder="名称"></td>
          <td><input type="text" value="${esc(it.tag || '')}" data-bind="watchlist.${i}.tag" placeholder="行业/主题"></td>
          <td class="r n">${r.score != null ? `<b style="color:${scoreColor(r.score)}">${r.score}</b>` : '<span class="muted">—</span>'}</td>
          <td class="r n ${cls(pc)}">${pct(pc)}${q ? ` <span class="pill blue" style="font-size:10px" title="实时现价 ${num(q.price)}">●</span>` : ''}</td>
          <td>${sig}</td>
          <td class="c"><button class="btn sm danger" data-act="del-watch" data-idx="${i}">删</button></td>
        </tr>`;
      }).join('')}
      </tbody>
    </table></div>
  </div>`;
}

/* ---------------- 视图：持仓 ---------------- */
function viewHoldings() {
  const list = S.cfg.holdings || [];
  const rows = S.data.brief ? (S.data.brief.holdings || []) : [];
  const rowMap = {};
  rows.forEach((h) => { rowMap[h.code] = h; });
  if (!S.quotes) loadQuotes().then(() => { if (S.view === 'holdings') render(); });

  return `
  <div class="banner info">填了持仓才能算：浮动盈亏、持有交易日数（时间止损）、距止损距离。
    成本填成交均价即可。只有出现在<b>自选池</b>里的票才会被采集，持仓票请一并加入自选池。</div>

  <div class="toolbar" style="margin-bottom:10px">
    <button class="btn" data-act="add-hold">+ 添加持仓</button>
    <button class="btn primary" data-act="save" data-scope="holdings">保存持仓</button>
    <span class="spacer"></span>
    <span class="muted">共 ${list.length} 笔</span>
  </div>

  ${list.length ? `<div class="card tight">
    <div class="tw"><table>
      <thead><tr>
        <th style="width:110px">代码</th><th class="r" style="width:100px">数量</th>
        <th class="r" style="width:110px">成本</th><th class="r" style="width:100px">现价</th>
        <th class="r" style="width:120px">浮动盈亏</th><th class="r" style="width:100px">市值</th>
        <th class="r" style="width:80px">持有</th><th class="r" style="width:100px">止损</th>
        <th class="r" style="width:90px">距止损</th><th style="width:130px">开仓日</th><th style="width:52px"></th>
      </tr></thead>
      <tbody>
      ${list.map((h, i) => {
        const r = rowMap[String(h.code).padStart(6, '0')] || {};
        const q = lq(h.code);
        const qty = Number(h.qty), cost = Number(h.cost);
        const canPnl = q && cost > 0;
        const price = q && q.price != null ? q.price : r.price;
        const pnl_pct = canPnl ? (price / cost - 1) * 100 : r.pnl_pct;
        const pnl_amt = canPnl && qty ? (price - cost) * qty : r.pnl_amt;
        const mktval = q && q.price != null && qty ? price * qty : r.market_value;
        const stopDist = q && q.price != null && r.stop ? (r.stop / price - 1) * 100 : r.stop_dist_pct;
        const broken = q && q.price != null && r.stop ? price <= r.stop : r.stop_broken;
        return `<tr>
          <td><input class="w-code" type="text" value="${esc(h.code)}" data-bind="holdings.${i}.code" maxlength="6"></td>
          <td class="r"><input type="number" value="${esc(h.qty)}" data-bind="holdings.${i}.qty" style="text-align:right"></td>
          <td class="r"><input type="number" step="0.001" value="${esc(h.cost)}" data-bind="holdings.${i}.cost" style="text-align:right"></td>
          <td class="r n">${num(price)}${q ? ` <span class="pill blue" style="font-size:10px" title="实时 ${pct(q.pct_chg)}">●</span>` : ''}</td>
          <td class="r n ${cls(pnl_pct)}">${pct(pnl_pct)}<div class="code">${pnl_amt != null ? num(pnl_amt, 0) : ''}</div></td>
          <td class="r n">${mktval != null ? amt(mktval) : '—'}</td>
          <td class="r n">${r.held_days != null ? r.held_days + ' 天' : '—'}</td>
          <td class="r n">${num(r.stop)}<div class="code">${esc(r.stop_basis || '')}</div></td>
          <td class="r n ${cls(stopDist)}">${stopDist != null ? pct(stopDist, 1) : '—'}
            ${broken ? '<div class="pill P0">已破</div>' : ''}</td>
          <td><input type="date" value="${esc(h.opened || '')}" data-bind="holdings.${i}.opened"></td>
          <td class="c"><button class="btn sm danger" data-act="del-hold" data-idx="${i}">删</button></td>
        </tr>`;
      }).join('')}
      </tbody>
    </table></div>
  </div>` : `<div class="empty"><b>还没有持仓记录</b>
    短线最该先填的就是这个——没有成本价，系统算不出你的止损该放哪。</div>`}`;
}

/* ---------------- 视图：提醒 ---------------- */
function viewAlerts() {
  const all = S.data.alerts || [];
  const dates = [...new Set(all.map((a) => a.run_date))].sort().reverse();
  const list = all.filter((a) =>
    (S.alertLevel === 'all' || a.level === S.alertLevel) &&
    (S.alertDate === 'all' || a.run_date === S.alertDate));

  const cnt = { P0: 0, P1: 0, P2: 0 };
  all.forEach((a) => { cnt[a.level] = (cnt[a.level] || 0) + 1; });

  return `
  <div class="toolbar" style="margin-bottom:10px">
    <div class="seg">
      <button data-act="alert-level" data-v="all" class="${S.alertLevel === 'all' ? 'on' : ''}">全部 ${all.length}</button>
      <button data-act="alert-level" data-v="P0" class="${S.alertLevel === 'P0' ? 'on' : ''}">P0 ${cnt.P0}</button>
      <button data-act="alert-level" data-v="P1" class="${S.alertLevel === 'P1' ? 'on' : ''}">P1 ${cnt.P1}</button>
      <button data-act="alert-level" data-v="P2" class="${S.alertLevel === 'P2' ? 'on' : ''}">P2 ${cnt.P2}</button>
    </div>
    <select id="alert-date" data-act="alert-date" style="width:auto;min-width:130px">
      <option value="all">全部日期</option>
      ${dates.map((d) => `<option value="${d}" ${S.alertDate === d ? 'selected' : ''}>${d}</option>`).join('')}
    </select>
    <span class="spacer"></span>
    <span class="muted">最近 ${all.length} 条</span>
  </div>

  <div class="card tight">
    ${list.length ? `<div class="tw"><table>
      <thead><tr><th style="width:96px">日期</th><th style="width:60px">级别</th>
        <th style="width:120px">标的</th><th style="width:130px">规则</th><th>说明</th></tr></thead>
      <tbody>${list.map((a) => `<tr>
        <td class="n muted">${esc(a.run_date)}</td>
        <td><span class="pill ${a.level}">${esc(a.level)}</span></td>
        <td class="name"><b>${esc(a.name || '—')}</b><div class="code">${esc(a.code || '')}</div></td>
        <td>${esc(a.rule)}</td>
        <td class="muted">${esc(a.message)}</td>
      </tr>`).join('')}</tbody>
    </table></div>` : `<div class="empty">没有匹配的提醒</div>`}
  </div>`;
}

/* ---------------- 视图：历史 ---------------- */
function viewHistory() {
  const hs = S.data.history || [];
  if (!hs.length) return `<div class="empty"><b>没有历史简报</b>每次运行都会在这里留一份。</div>`;
  const sel = S.histSel || hs[0].date;
  return `
  <div class="hist">
    <div class="card" style="padding:10px 8px;align-self:start">
      <div class="hist-list">
        ${hs.map((h) => `<div class="hist-i ${h.date === sel ? 'on' : ''}" data-act="hist-sel" data-v="${h.date}">
          <span style="color:inherit;font-size:12.5px">${h.date}</span><span>${h.mtime}</span></div>`).join('')}
      </div>
    </div>
    <div class="frame-wrap">
      <iframe src="/briefings/${sel}.html" title="简报预览"></iframe>
    </div>
  </div>`;
}

/* ---------------- 视图：选股（含入选理由） ---------------- */
function viewScreening() {
  // 选股结果不在 /api/state 里（数据量大且非每页都需要），首次进入本页时懒加载。
  if (!S.screening) {
    if (!S.screeningLoading) {
      S.screeningLoading = true;
      api('/api/screening').then((d) => {
        S.screening = d; S.screeningLoading = false; S.screeningErr = null;
        updateNav();
        if (S.view === 'screening') render();
      }).catch((e) => {
        S.screeningLoading = false; S.screeningErr = e.message;
        if (S.view === 'screening') render();
      });
    }
    return `<div class="empty">加载选股结果…</div>`;
  }
  if (S.screeningErr) {
    return `<div class="empty"><b>选股结果加载失败</b>${esc(S.screeningErr)}</div>`;
  }
  const sc = S.screening;
  if (!sc.date || !(sc.rows || []).length) {
    return `<div class="empty"><b>还没有选股记录</b>
      在项目根目录运行 <code>python3 run_screen.py --save</code> 生成一次选股并落库，
      每只票的入选理由会显示在这里。</div>`;
  }
  const dates = sc.dates || [];
  const rows = sc.rows || [];
  const inPool = watchCodes();
  const notIn = rows.filter((r) => !inPool.has(String(r.code).padStart(6, '0')));
  return `
  <div class="toolbar" style="margin-bottom:10px">
    <select id="screen-date" style="width:auto;min-width:140px">
      ${dates.map((d) => `<option value="${d}" ${d === sc.date ? 'selected' : ''}>${d}</option>`).join('')}
    </select>
    <button class="btn" data-act="watch-add-all" ${notIn.length ? '' : 'disabled'}>
      + 未入池 ${notIn.length} 只全部加入自选池
    </button>
    <span class="spacer"></span>
    <span class="muted">共 ${rows.length} 只候选 · 理由 = 评分分项 + 触发它的具体指标</span>
  </div>
  ${rows.map((r) => screenCard(r, inPool)).join('')}`;
}

function watchCodes() {
  const w = (S.state && S.state.config && S.state.config.watchlist) || S.cfg.watchlist || [];
  return new Set(w.map((x) => String(x.code).padStart(6, '0')));
}

async function addWatch(items) {
  const d = await api('/api/watchlist/add', { method: 'POST', body: JSON.stringify({ items }) });
  const added = (d.added || []).length;
  const skipped = (d.skipped || []).length;
  await load();
  toast(`已加入自选池 ${added} 只` + (skipped ? `，跳过已在池/非法 ${skipped} 只` : '') + '（config.yaml 已备份）');
  render();
}

function screenCard(r, inPool) {
  const reasons = r.reasons || [];
  const plan = r.plan || {};
  const code = String(r.code).padStart(6, '0');
  const inW = (inPool || watchCodes()).has(code);
  const pct = r.pct_chg;
  const pctTxt = (pct == null) ? '—' : `${pct > 0 ? '+' : ''}${Number(pct).toFixed(1)}%`;
  const pctCls = (pct == null) ? 'muted' : (pct >= 0 ? 'up' : 'down');
  const closeTxt = (r.close == null) ? '—' : Number(r.close).toFixed(2);
  const planTxt = plan.ready
    ? `买 ${Number(plan.buy_lo).toFixed(2)}-${Number(plan.buy_hi).toFixed(2)}　止损 ${Number(plan.stop).toFixed(2)}　盈亏比 ${Number(plan.rr).toFixed(1)}　建议 ${plan.shares} 股`
    : `暂无计划：${esc(plan.why || '不满足条件')}`;
  const pats = (r.patterns || []).join('、');
  const hits = r.strategy_hits || [];
  const hitTxt = hits
    .map((h) => `${esc(h.cn || h.strategy)} ${Number(h.strength || 0).toFixed(2)}`)
    .join(' · ');
  return `
  <div class="card" style="margin-bottom:10px">
    <div class="card-h">
      <div class="card-t">
        <span class="muted">#${r.rank}</span>　${esc(r.code)}　${esc(r.name)}
        <span class="pill" style="margin-left:6px">${esc(r.screen_label || '')}</span>
        ${hits.length ? `<span class="pill blue" style="margin-left:6px">适配策略 ${hits.length}：${hitTxt}</span>` : ''}
        ${r.via_strategy ? `<span class="pill P1" style="margin-left:6px">策略直命·未过漏斗</span>` : ''}
      </div>
      <div class="card-s">
        评分 <b class="num">${r.score ?? '—'}</b>　现价 <b class="num">${closeTxt}</b>
        <b class="${pctCls}">${pctTxt}</b>
      </div>
    </div>
    ${pats ? `<div class="muted" style="margin:0 0 8px;font-size:12px">形态：${esc(pats)}</div>` : ''}
    <div class="note" style="margin-bottom:8px"><b>入选理由</b></div>
    <ul class="reasons">
      ${reasons.length ? reasons.map((x) => `<li>${esc(x)}</li>`).join('') : '<li class="muted">（该次运行未记录理由）</li>'}
    </ul>
    <div class="note ${plan.ready ? 'up' : ''}" style="margin-top:8px">${planTxt}</div>
    <div style="margin-top:8px;text-align:right">
      ${inW
        ? '<span class="pill gray">已在自选池</span>'
        : `<button class="btn sm" data-act="watch-add" data-code="${code}" data-name="${esc(r.name || code)}" data-tag="${esc(r.industry || '选股')}">+ 加入自选池</button>`}
    </div>
  </div>`;
}

/* ---------------- 视图：模拟交易（四策略账户切换 + 全景） ---------------- */
const SIM_SESSION_STYLE = {
  morning: 'ok', afternoon: 'ok', auction: 'hold', closing_auction: 'hold',
  pre_open: 'gray', lunch: 'gray', closed: 'gray', non_trading_day: 'gray',
};

function ensureSim(force = false) {
  if (S.simLoading) return;
  const cmpMode = S.simMode === 'compare';
  const have = cmpMode ? !!S.simCompare : !!(S.simDetail && S.simDetail[S.simAccount]);
  if (!force && S.simOverview && have) return;
  S.simLoading = true; S.simErr = null;
  const aid = S.simAccount || 'board_hit';
  api('/api/simulation/accounts')
    .then((ov) => { S.simOverview = ov; return cmpMode ? loadSimCompare() : loadSimDetail(aid); })
    .then(() => { S.simLoading = false; updateNav(); if (S.view === 'simulation') render(); })
    .catch((e) => { S.simLoading = false; S.simErr = e.message; if (S.view === 'simulation') render(); });
}

function loadSimDetail(aid) {
  const q = `account=${encodeURIComponent(aid)}`;
  return Promise.all([
    api(`/api/simulation/summary?${q}`),
    api(`/api/simulation/positions?${q}`),
    api(`/api/simulation/trades?${q}&limit=50`),
    api(`/api/simulation/snapshots?${q}`),
  ]).then(([summary, positions, trades, snapshots]) => {
    S.simDetail = S.simDetail || {};
    S.simDetail[aid] = { summary, positions, trades, snapshots };
  });
}

function loadSimCompare() {
  return api('/api/simulation/compare').then((cmp) => { S.simCompare = cmp; });
}

function viewSimulation() {
  const cmpMode = S.simMode === 'compare';
  const ready = cmpMode ? !!S.simCompare
    : !!(S.simOverview && S.simDetail && S.simDetail[S.simAccount]);
  if (!ready) {
    ensureSim();
    if (S.simErr) return `<div class="empty"><b>模拟交易数据加载失败</b>${esc(S.simErr)}</div>`;
    return `<div class="empty">加载模拟交易数据…</div>`;
  }
  const ov = S.simOverview || S.simCompare || {};
  const aid = S.simAccount;
  const d = (S.simDetail && S.simDetail[aid]) || {};
  const s = d.summary || {};
  const sess = ov.session || {};
  const live = ov.live || {};

  // 顶部：时段徽标 + 自动循环状态
  const sessPill = SIM_SESSION_STYLE[sess.key] || 'gray';
  const liveTxt = live.running
    ? `<span class="pill ok">盘中自动循环运行中</span>`
    : `<span class="pill gray">自动循环未运行</span>`;
  const liveUpd = live.updated_at ? live.updated_at.replace('T', ' ').slice(0, 19) : '—';

  // 账户切换标签
  const tabs = (ov.accounts || []).map((a) => {
    const on = a.account_id === aid;
    const rp = a.total_return_pct || 0;
    return `<button class="simtab${on ? ' on' : ''}" data-act="sim-account" data-v="${esc(a.account_id)}">
      <span class="simtab-cn">${esc(a.cn)}</span>
      <span class="simtab-r ${cls(rp)}">${pct(rp)}</span>
      <span class="simtab-a">${num(a.total_assets, 0)}</span>
    </button>`;
  }).join('');

  const positions = d.positions || [];
  const trades = d.trades || [];
  const snaps = d.snapshots || [];

  const toolbar = `
  <div class="toolbar" style="margin-bottom:12px;align-items:center">
    <span class="pill ${sessPill}" style="font-size:13px">市场时段 · ${esc(sess.label || '—')}　${esc(sess.time || '')}</span>
    ${liveTxt}
    <span class="muted" style="font-size:12px">心跳 ${esc(liveUpd)}${live.bought_today ? ' · 今日已建仓' : ''}${live.snapshot_done ? ' · 已写快照' : ''}</span>
    <span class="spacer"></span>
    <div class="seg">
      <button data-act="sim-mode" data-v="single" class="${cmpMode ? '' : 'on'}">单账户</button>
      <button data-act="sim-mode" data-v="compare" class="${cmpMode ? 'on' : ''}">四策略对比</button>
    </div>
    <button class="btn ghost" data-act="sim-refresh">刷新</button>
  </div>`;

  if (cmpMode) return toolbar + simCompareBody(S.simCompare);

  const klineSec = simKlineSection(positions, trades);

  return toolbar + `
  <div class="simtabs">${tabs}</div>

  <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr));margin-bottom:14px">
    ${simKpi('总资产', num(s.total_assets), '')}
    ${simKpi('可用现金', num(s.cash), '')}
    ${simKpi('持仓市值', num(s.position_value), '')}
    ${simKpi('仓位', ((s.position_ratio || 0) * 100).toFixed(1) + '%', '')}
    ${simKpi('浮动盈亏', num(s.unrealized_pnl), cls(s.unrealized_pnl))}
    ${simKpi('累计收益', pct(s.total_return_pct), cls(s.total_return_pct))}
  </div>

  <h2 class="sec">持仓明细（${esc(s.cn || aid)}）</h2>
  ${positions.length ? `
  <div class="card tight"><div class="tw"><table>
    <thead><tr>
      <th>标的</th><th class="r">数量</th><th class="r">可卖</th><th class="r">T+1冻结</th>
      <th class="r">成本价</th><th class="r">现价</th><th class="r">市值</th>
      <th class="r">浮动盈亏</th><th class="r">盈亏%</th><th>止损 / 目标</th>
    </tr></thead>
    <tbody>
    ${positions.map((p) => {
      const plan = p.original_plan || {};
      return `<tr>
        <td class="name"><b>${esc(p.name)}</b><div class="code">${esc(p.code)} · ${esc(p.strategy || '')}</div></td>
        <td class="r n">${p.quantity}</td>
        <td class="r n">${p.available}</td>
        <td class="r n ${p.frozen ? 'muted' : ''}">${p.frozen || 0}</td>
        <td class="r n">${num(p.cost_price, 3)}</td>
        <td class="r n">${num(p.current_price)}</td>
        <td class="r n">${num(p.market_value)}</td>
        <td class="r n ${cls(p.profit_loss)}">${num(p.profit_loss)}</td>
        <td class="r n ${cls(p.profit_pct)}">${pct(p.profit_pct)}</td>
        <td class="muted" style="font-size:11.5px">损 ${plan.stop != null ? num(plan.stop) : '—'} / 盈 ${plan.target1 != null ? num(plan.target1) : '—'}</td>
      </tr>`;
    }).join('')}
    </tbody>
  </table></div></div>` : `<div class="empty">当前空仓。该策略账户在本轮情绪相位/数据条件下未建仓（详见运行说明）。</div>`}

  ${klineSec}

  <h2 class="sec">成交流水</h2>
  ${trades.length ? `
  <div class="card tight"><div class="tw"><table>
    <thead><tr>
      <th>时间</th><th>方向</th><th>标的</th><th class="r">数量</th><th class="r">成交价</th>
      <th class="r">金额</th><th class="r">费用</th><th class="r">已实现盈亏</th><th>原因 / 策略</th>
    </tr></thead>
    <tbody>
    ${trades.map((t) => `<tr>
      <td class="muted" style="font-size:11.5px">${esc((t.trade_time || '').replace('T', ' ').slice(5, 16))}</td>
      <td><span class="pill ${t.direction === 'buy' ? 'hold' : 'ok'}">${t.direction === 'buy' ? '买入' : '卖出'}</span></td>
      <td class="name"><b>${esc(t.name)}</b><div class="code">${esc(t.code)}</div></td>
      <td class="r n">${t.quantity}</td>
      <td class="r n">${num(t.price)}</td>
      <td class="r n">${num(t.direction === 'sell' ? t.net_amount : t.amount)}</td>
      <td class="r n muted">${num(t.fees)}</td>
      <td class="r n ${cls(t.realized_pnl)}">${t.direction === 'sell' ? num(t.realized_pnl) : '—'}</td>
      <td class="muted" style="font-size:11.5px;max-width:240px">${esc(t.reason || t.strategy || '')}</td>
    </tr>`).join('')}
    </tbody>
  </table></div></div>` : `<div class="empty">暂无成交记录。</div>`}

  <h2 class="sec">每日净值</h2>
  ${snaps.length ? `
  <div class="card">
    ${simSpark(snaps)}
    <div class="tw" style="margin-top:10px"><table>
      <thead><tr><th>日期</th><th class="r">总资产</th><th class="r">现金</th><th class="r">持仓市值</th><th class="r">仓位</th><th class="r">当日盈亏</th><th class="r">收益率</th></tr></thead>
      <tbody>
      ${snaps.slice().reverse().map((x) => `<tr>
        <td class="muted">${esc(x.date)}</td>
        <td class="r n">${num(x.total_assets)}</td>
        <td class="r n">${num(x.cash)}</td>
        <td class="r n">${num(x.position_value)}</td>
        <td class="r n">${((x.position_ratio || 0) * 100).toFixed(1)}%</td>
        <td class="r n ${cls(x.total_pnl)}">${num(x.total_pnl)}</td>
        <td class="r n ${cls(x.total_return_pct)}">${pct(x.total_return_pct)}</td>
      </tr>`).join('')}
      </tbody>
    </table></div>
  </div>` : `<div class="empty">暂无每日快照（收盘后由 run_sim_live / run_sim 写入）。</div>`}
  `;
}

function simCompareBody(cmp) {
  const accts = (cmp && cmp.accounts) || [];
  if (!accts.length) return `<div class="empty">暂无策略账户数据。</div>`;

  // 横向对比表：一行一策略
  const table = `
  <div class="card tight"><div class="tw"><table>
    <thead><tr>
      <th>策略</th><th class="r">总资产</th><th class="r">可用现金</th><th class="r">持仓市值</th>
      <th class="r">仓位</th><th class="r">浮动盈亏</th><th class="r">累计收益</th>
      <th class="r">持仓数</th><th class="r">成交数</th>
    </tr></thead>
    <tbody>
    ${accts.map((a) => `<tr>
      <td class="name"><b>${esc(a.cn)}</b><div class="code">${esc(a.account_id)}</div></td>
      <td class="r n">${num(a.total_assets)}</td>
      <td class="r n">${num(a.cash)}</td>
      <td class="r n">${num(a.position_value)}</td>
      <td class="r n">${((a.position_ratio || 0) * 100).toFixed(1)}%</td>
      <td class="r n ${cls(a.unrealized_pnl)}">${num(a.unrealized_pnl)}</td>
      <td class="r n ${cls(a.total_return_pct)}">${pct(a.total_return_pct)}</td>
      <td class="r n">${a.position_count || 0}</td>
      <td class="r n">${a.trade_count || 0}</td>
    </tr>`).join('')}
    </tbody>
  </table></div></div>`;

  // 四列卡片：每策略的持仓明细（空仓则显示原因）
  const cards = accts.map((a) => {
    const rp = a.total_return_pct || 0;
    const pos = a.positions || [];
    const body = pos.length ? `
      <table class="simcmp-t">
        <thead><tr><th>标的</th><th class="r">数量</th><th class="r">现价</th><th class="r">盈亏%</th></tr></thead>
        <tbody>
        ${pos.map((p) => `<tr>
          <td class="name"><b>${esc(p.name)}</b><div class="code">${esc(p.code)}</div></td>
          <td class="r n">${p.quantity}</td>
          <td class="r n">${num(p.current_price)}</td>
          <td class="r n ${cls(p.profit_pct)}">${pct(p.profit_pct)}</td>
        </tr>`).join('')}
        </tbody>
      </table>`
      : `<div class="simcmp-empty">${esc(a.note || '空仓（本轮未建仓）')}</div>`;
    return `
    <div class="card simcmp-card">
      <div class="simcmp-h">
        <b>${esc(a.cn)}</b>
        <span class="simtab-r ${cls(rp)}">${pct(rp)}</span>
      </div>
      <div class="simcmp-kv"><span>总资产</span><b>${num(a.total_assets)}</b></div>
      <div class="simcmp-kv"><span>仓位</span><b>${((a.position_ratio || 0) * 100).toFixed(1)}%</b></div>
      <div class="simcmp-kv"><span>浮动盈亏</span><b class="${cls(a.unrealized_pnl)}">${num(a.unrealized_pnl)}</b></div>
      ${body}
    </div>`;
  }).join('');

  return `
  <h2 class="sec">四策略横向对比</h2>
  ${table}
  <div class="simcmp">${cards}</div>`;
}

function ensureSimKline(code) {
  S.simKline = S.simKline || {};
  if (S.simKline[code] || S.simKlineLoading === code) return;
  S.simKlineLoading = code;
  api(`/api/simulation/kline?code=${encodeURIComponent(code)}&days=160`)
    .then((d) => { S.simKline[code] = d; })
    .catch((e) => { S.simKline[code] = { code, name: '', candles: [], marks: [], source_error: e.message }; })
    .finally(() => { S.simKlineLoading = null; if (S.view === 'simulation') render(); });
}

function simKlineSection(positions, trades) {
  const pos = positions || [], trd = trades || [];
  const codes = [...new Set([...pos.map((p) => p.code), ...trd.map((t) => t.code)])].filter(Boolean);
  if (!codes.length) return '';
  const label = (c) => {
    const hit = pos.find((p) => p.code === c) || trd.find((t) => t.code === c);
    return `${hit && hit.name ? hit.name + ' ' : ''}${c}`;
  };
  const code = codes.includes(S.simKlineCode) ? S.simKlineCode : codes[0];
  S.simKline = S.simKline || {};
  const kd = S.simKline[code];
  let body;
  if (!kd) { ensureSimKline(code); body = `<div class="empty">加载K线…</div>`; }
  else if (!(kd.candles || []).length) body = `<div class="empty">${esc(kd.source_error || '无K线数据。')}</div>`;
  else body = simCandle(kd);
  return `
  <h2 class="sec">K线与买卖点</h2>
  <div class="card" style="padding:12px 14px">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap">
      <select data-act="sim-kline" style="min-width:190px">${codes.map((c) => `<option value="${esc(c)}"${c === code ? ' selected' : ''}>${esc(label(c))}</option>`).join('')}</select>
      <span class="muted" style="font-size:12px">${kd && kd.name ? esc(kd.name) + ' · ' : ''}日K · 近 ${Math.min((kd && kd.candles || []).length || 120, 120)} 个交易日</span>
    </div>
    ${body}
  </div>`;
}

function simCandle(kd) {
  const cs = (kd.candles || []).slice(-120);
  if (!cs.length) return `<div class="empty">无K线数据。</div>`;
  const marks = kd.marks || [];
  const W = 920, H = 340, PL = 50, PR = 12, PT = 16, PB = 24;
  const n = cs.length, step = (W - PL - PR) / n;
  let lo = Math.min(...cs.map((c) => c.low)), hi = Math.max(...cs.map((c) => c.high));
  const span0 = (hi - lo) || 1; lo -= span0 * 0.08; hi += span0 * 0.08;
  const span = hi - lo;
  const X = (i) => PL + i * step + step / 2;
  const Y = (p) => PT + (hi - p) / span * (H - PT - PB);
  const idx = {}; cs.forEach((c, i) => { idx[c.date] = i; });
  const grid = [0, 0.25, 0.5, 0.75, 1].map((f) => {
    const p = hi - span * f, y = Y(p);
    return `<line x1="${PL}" y1="${y.toFixed(1)}" x2="${W - PR}" y2="${y.toFixed(1)}" stroke="var(--line)" stroke-width="1"/>` +
      `<text x="${PL - 6}" y="${(y + 3).toFixed(1)}" text-anchor="end" font-size="10" fill="var(--ink-3)">${p.toFixed(2)}</text>`;
  }).join('');
  const bw = Math.max(2, step * 0.62);
  const candles = cs.map((c, i) => {
    const up = c.close >= c.open, col = up ? 'var(--up)' : 'var(--down)';
    const yO = Y(c.open), yC = Y(c.close);
    const top = Math.min(yO, yC), hgt = Math.max(1.2, Math.abs(yO - yC));
    return `<line x1="${X(i).toFixed(1)}" y1="${Y(c.high).toFixed(1)}" x2="${X(i).toFixed(1)}" y2="${Y(c.low).toFixed(1)}" stroke="${col}" stroke-width="1"/>` +
      `<rect x="${(X(i) - bw / 2).toFixed(1)}" y="${top.toFixed(1)}" width="${bw.toFixed(1)}" height="${hgt.toFixed(1)}" fill="${col}">` +
      `<title>${esc(c.date)} 开${c.open} 收${c.close} 高${c.high} 低${c.low}</title></rect>`;
  }).join('');
  // 买卖点标记：圆环+三角锚定成交价位（横向虚线=该价位），旁注常显「日期 方向 成交价」
  const mks = marks.map((m) => {
    const i = idx[m.date]; if (i == null) return null;
    const buy = m.direction === 'buy';
    const p = m.price != null ? m.price : (buy ? cs[i].low : cs[i].high);
    const yp = Y(p);
    return { m, buy, x: X(i), yp, cy: yp + (buy ? 11 : -11) };
  }).filter(Boolean);
  // 相邻日/同价位的标记（如次日止损卖）圆环会糊成一团：按 y 排序后两两推开，保证环间留白
  mks.sort((a, b) => a.cy - b.cy);
  for (let a = 0; a < mks.length; a++)
    for (let b = a + 1; b < mks.length; b++) {
      const A = mks[a], B = mks[b];
      if (Math.abs(A.x - B.x) < 26 && Math.abs(A.cy - B.cy) < 26) {
        const mid = (A.cy + B.cy) / 2;
        A.cy = mid - 14; B.cy = mid + 14;
      }
    }
  const mk = mks.map((d) => {
    const { m, buy, x, yp } = d;
    const cy = Math.min(Math.max(d.cy, PT + 10), H - PB - 10);
    const col = buy ? 'var(--up)' : 'var(--down)';
    const tri = buy
      ? `${x.toFixed(1)},${(cy - 5).toFixed(1)} ${(x - 4.5).toFixed(1)},${(cy + 3.5).toFixed(1)} ${(x + 4.5).toFixed(1)},${(cy + 3.5).toFixed(1)}`
      : `${x.toFixed(1)},${(cy + 5).toFixed(1)} ${(x - 4.5).toFixed(1)},${(cy - 3.5).toFixed(1)} ${(x + 4.5).toFixed(1)},${(cy - 3.5).toFixed(1)}`;
    const lx = Math.max(x - 13, PL + 64);
    const ly = Math.min(Math.max(cy + 3.5, PT + 10), H - PB - 2);
    const lbl = `${m.date.slice(5)} ${buy ? '买' : '卖'} ${m.price}`;
    const tipTxt = `${esc(m.date)} ${esc(m.cn)} ${buy ? '买入' : '卖出'} ${m.quantity}股 @ ${m.price} ${esc(m.reason || '')}`;
    return `<line x1="${PL}" y1="${yp.toFixed(1)}" x2="${W - PR}" y2="${yp.toFixed(1)}" stroke="${col}" stroke-width="1" stroke-dasharray="3 4" opacity="0.45"><title>${tipTxt}</title></line>` +
      `<circle cx="${x.toFixed(1)}" cy="${cy.toFixed(1)}" r="8.5" fill="var(--panel)" fill-opacity="0.85" stroke="${col}" stroke-width="2"><title>${tipTxt}</title></circle>` +
      `<polygon points="${tri}" fill="${col}"><title>${tipTxt}</title></polygon>` +
      `<text x="${lx.toFixed(1)}" y="${ly.toFixed(1)}" text-anchor="end" font-size="10.5" font-weight="700" fill="${col}" stroke="var(--panel)" stroke-width="3" paint-order="stroke"><title>${tipTxt}</title>${esc(lbl)}</text>`;
  }).join('');
  const xl = [0, Math.floor(n / 2), n - 1].map((i, k) =>
    `<text x="${X(i).toFixed(1)}" y="${H - 6}" text-anchor="${k === 0 ? 'start' : k === 2 ? 'end' : 'middle'}" font-size="10" fill="var(--ink-3)">${esc(cs[i].date)}</text>`).join('');
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto">${grid}${candles}${mk}${xl}</svg>
  <div class="muted" style="font-size:11.5px;margin-top:6px">图例：<span style="color:var(--up)">▲ 买入</span> / <span style="color:var(--down)">▼ 卖出</span> · 圆环+三角锚定成交价位（横向虚线=该价位，相邻标记自动错开），旁注「日期 方向 成交价」· 悬停看策略/股数/原因 · 红涨绿跌 · 前复权日K</div>`;
}

function simKpi(label, value, colorCls) {
  return `<div class="card" style="padding:13px 15px">
    <div class="kpi-l">${esc(label)}</div>
    <div class="kpi-v ${colorCls || ''}">${value}</div>
  </div>`;
}

function simSpark(snaps) {
  const pts = snaps.filter((x) => x.total_assets != null);
  if (pts.length < 2) return `<div class="muted" style="font-size:12px">快照不足两天，暂无法画净值曲线。</div>`;
  const w = 640, h = 90, pad = 6;
  const vals = pts.map((p) => p.total_assets);
  const min = Math.min(...vals), max = Math.max(...vals), span = (max - min) || 1;
  const step = (w - pad * 2) / (pts.length - 1);
  const coords = vals.map((v, i) => [pad + i * step, h - pad - ((v - min) / span) * (h - pad * 2)]);
  const line = coords.map((c) => `${c[0].toFixed(1)},${c[1].toFixed(1)}`).join(' ');
  const up = vals[vals.length - 1] >= vals[0];
  const color = up ? 'var(--up)' : 'var(--down)';
  const dots = coords.map((c, i) => `<circle cx="${c[0].toFixed(1)}" cy="${c[1].toFixed(1)}" r="2.5" fill="${color}"><title>${esc(pts[i].date)}：${num(pts[i].total_assets)}</title></circle>`).join('');
  return `<svg viewBox="0 0 ${w} ${h}" style="width:100%;height:${h}px" preserveAspectRatio="none">
    <polyline points="${line}" fill="none" stroke="${color}" stroke-width="2" />
    ${dots}
  </svg>`;
}

/* ---------------- 视图：设置 ---------------- */
const RISK_FIELDS = [
  ['capital', '总资金（元）', '仅用于反推建议股数，填实际可用资金', 1000, 1],
  ['single_risk_pct', '单笔风险占总资金（%）', '1% 是常见上限，超过 2% 很难长期活下来', 0.1, 0.1],
  ['max_hold_days', '最长持有（交易日）', '超期没走出来就时间止损', 1, 1],
  ['atr_stop_mult', '止损 ATR 倍数', '止损 = 收盘 − N×ATR，越小越容易被噪音打掉', 0.5, 0.1],
  ['min_rr', '最低盈亏比', '低于此值不给计划', 0.5, 0.1],
  ['min_score', '最低评分', '分数不够不给计划', 0, 1],
];

const TH_LABELS = {
  pct_change_p1: ['单日涨跌 P1（%）', '超过触发提醒'],
  pct_change_p0: ['单日涨跌 P0（%）', '超过优先处理'],
  volume_ratio_p1: ['放量 P1（量比）', '量比超过此值'],
  volume_ratio_stall: ['放量滞涨（量比）', '放量但涨幅不足 → 出货嫌疑'],
  bias5_hot: ['偏离 5 日线（%）', '超过算追高风险'],
  rsi6_hot: ['RSI6 过热', '短期超买'],
  up_days_hot: ['连涨天数过热', '连续上涨后风险累积'],
  fade_gain: ['冲高回落（%）', '盘中高点涨幅超过但收盘不足 1%'],
  drawdown_from_high_p1: ['距 60 日高点回撤', '小数，0.2 = 20%'],
  stop_loss_buffer: ['止损缓冲', '小数'],
  rsi_overbought: ['RSI14 超买', '兜底阈值'],
  rsi_oversold: ['RSI14 超卖', '兜底阈值'],
};

function viewSettings() {
  const cfg = S.cfg, risk = cfg.risk || {}, th = cfg.thresholds || {}, n = cfg.notify || {};
  const style = (cfg.meta || {}).style || 'short';

  return `
  <div class="grid g2">
    <div class="card">
      <div class="card-h"><div class="card-t">交易风格</div>
        <div class="card-s">切换后评分因子与提醒规则都会变</div></div>
      <div class="seg" style="margin-bottom:10px">
        <button data-act="set-style" data-v="short" class="${style === 'short' ? 'on' : ''}">短线（持股 2–5 日）</button>
        <button data-act="set-style" data-v="swing" class="${style === 'swing' ? 'on' : ''}">波段（数周）</button>
      </div>
      <div class="note">短线的核心是出场：止损、时间止损、放量滞涨。波段更看趋势与均线结构。
        切到波段后交易计划表会消失，只保留强弱排序。</div>
      <div style="margin-top:14px">
        <button class="btn primary" data-act="save" data-scope="meta">保存风格</button>
      </div>
    </div>

    <div class="card">
      <div class="card-h"><div class="card-t">通知</div><div class="card-s">macOS 本地通知</div></div>
      <div style="display:flex;flex-direction:column;gap:12px">
        <label class="switch"><input type="checkbox" ${n.macos ? 'checked' : ''} data-bind="notify.macos"> 运行完成后弹通知</label>
        <label class="switch"><input type="checkbox" ${n.only_on_p0 ? 'checked' : ''} data-bind="notify.only_on_p0"> 仅在有 P0 时通知</label>
        <div class="field"><label>通知标题</label>
          <input type="text" value="${esc(n.title || '')}" data-bind="notify.title"></div>
      </div>
      <div style="margin-top:14px;display:flex;gap:8px">
        <button class="btn primary" data-act="save" data-scope="notify">保存通知设置</button>
        <button class="btn" data-act="test-notify">发送测试通知</button>
      </div>
    </div>
  </div>

  <h2 class="sec">风控参数</h2>
  <div class="card">
    <div class="form g2">
      ${RISK_FIELDS.map(([k, label, desc, min, step]) => `
        <div class="field"><label>${label}</label>
          <input type="number" min="${min}" step="${step}" value="${esc(risk[k] ?? '')}" data-bind="risk.${k}">
          <span class="desc">${desc}</span></div>`).join('')}
    </div>
    <div style="margin-top:16px;display:flex;gap:8px">
      <button class="btn primary" data-act="save" data-scope="risk">保存风控参数</button>
      <span class="muted" style="align-self:center;font-size:11.5px">
        改完不会立刻生效，下次运行简报时使用</span>
    </div>
  </div>

  <h2 class="sec">提醒阈值（高级）</h2>
  <div class="card">
    <div class="form g2">
      ${Object.entries(th).map(([k, v]) => {
        const [label, desc] = TH_LABELS[k] || [k, ''];
        return `<div class="field"><label>${label}</label>
          <input type="number" step="0.1" value="${esc(v)}" data-bind="thresholds.${k}">
          ${desc ? `<span class="desc">${desc}</span>` : ''}</div>`;
      }).join('')}
    </div>
    <div style="margin-top:16px"><button class="btn primary" data-act="save" data-scope="thresholds">保存阈值</button></div>
  </div>

  <h2 class="sec">数据与文件</h2>
  <div class="card">
    <div class="list">
      <div class="list-i"><i class="dot"></i><div class="t">配置文件：<span class="muted">${esc(S.data.meta.config_path)}</span></div></div>
      <div class="list-i"><i class="dot"></i><div class="t">工作目录：<span class="muted">${esc(S.data.meta.root)}</span></div></div>
      <div class="list-i"><i class="dot"></i><div class="t muted">
        界面保存配置时会重写 YAML，旧版本备份在 data/backup/（保留最近 30 份）。
        想保留手写注释就手改，别用界面保存这几项。</div></div>
    </div>
    <div style="margin-top:14px;display:flex;gap:8px;flex-wrap:wrap">
      <button class="btn" data-act="reveal" data-v="briefings">打开简报文件夹</button>
      <button class="btn" data-act="reveal" data-v="data">打开数据目录</button>
      <button class="btn ghost" data-act="reload">重新读取配置</button>
    </div>
  </div>`;
}

/* ---------------- 事件 ---------------- */
function setPath(path, value) {
  const seg = path.split('.');
  let o = S.cfg;
  for (let i = 0; i < seg.length - 1; i++) {
    const k = seg[i];
    if (o[k] === undefined || o[k] === null) o[k] = /^\d+$/.test(seg[i + 1]) ? [] : {};
    o = o[k];
  }
  o[seg[seg.length - 1]] = value;
}

document.addEventListener('input', (e) => {
  const el = e.target.closest('[data-bind]');
  if (!el) return;
  const v = el.type === 'checkbox' ? el.checked : el.value;
  setPath(el.dataset.bind, v);
});

document.addEventListener('change', (e) => {
  const el = e.target.closest('[data-bind]');
  if (!el) return;
  const v = el.type === 'checkbox' ? el.checked : el.value;
  setPath(el.dataset.bind, v);
});

document.addEventListener('click', async (e) => {
  const nav = e.target.closest('#nav a');
  if (nav) { e.preventDefault(); location.hash = '#/' + nav.dataset.view; return; }

  const el = e.target.closest('[data-act]');
  if (!el) return;
  const act = el.dataset.act;

  if (act === 'pulse-board') { S.pulseBoard = el.dataset.v; render(); return; }
  if (act === 'pulse-refresh') { S.pulse = null; loadPulse(true).then(() => render()); return; }

  if (act === 'plan-filter') { S.planOnly = el.dataset.v === 'ready'; render(); return; }
  if (act === 'alert-level') { S.alertLevel = el.dataset.v; render(); return; }
  if (act === 'hist-sel') { S.histSel = el.dataset.v; render(); return; }
  if (act === 'set-style') {
    S.cfg.meta = S.cfg.meta || {}; S.cfg.meta.style = el.dataset.v; render(); return;
  }

  if (act === 'add-watch') {
    S.cfg.watchlist = S.cfg.watchlist || [];
    S.cfg.watchlist.push({ code: '', name: '', tag: '' });
    render();
    const inputs = $$('#stage input[data-bind$=".code"]');
    if (inputs.length) inputs[inputs.length - 1].focus();
    return;
  }
  if (act === 'del-watch') {
    const i = +el.dataset.idx;
    const it = S.cfg.watchlist[i];
    if (!confirm(`从自选池移除 ${it.name || it.code}？`)) return;
    S.cfg.watchlist.splice(i, 1); render(); return;
  }
  if (act === 'watch-add') {
    const it = { code: el.dataset.code, name: el.dataset.name, tag: el.dataset.tag };
    try { await addWatch([it]); } catch (err) { toast(err.message, true); }
    return;
  }
  if (act === 'watch-add-all') {
    const rows = (S.screening && S.screening.rows) || [];
    const inPool = watchCodes();
    const items = rows
      .filter((r) => !inPool.has(String(r.code).padStart(6, '0')))
      .map((r) => ({ code: String(r.code).padStart(6, '0'), name: r.name, tag: r.industry || '选股' }));
    if (!items.length) { toast('候选均已在自选池'); return; }
    if (!confirm(`把 ${items.length} 只候选加入自选池？（config.yaml 会自动备份）`)) return;
    try { await addWatch(items); } catch (err) { toast(err.message, true); }
    return;
  }
  if (act === 'add-hold') {
    S.cfg.holdings = S.cfg.holdings || [];
    S.cfg.holdings.push({ code: '', qty: 100, cost: 0, opened: new Date().toISOString().slice(0, 10) });
    render();
    const inputs = $$('#stage input[data-bind$=".code"]');
    if (inputs.length) inputs[inputs.length - 1].focus();
    return;
  }
  if (act === 'del-hold') {
    const i = +el.dataset.idx;
    const h = S.cfg.holdings[i];
    if (!confirm(`删除持仓 ${h.code}？`)) return;
    S.cfg.holdings.splice(i, 1); render(); return;
  }

  if (act === 'save') return doSave(el.dataset.scope, el);
  if (act === 'test-notify') {
    try { await api('/api/notify/test', { method: 'POST', body: '{}' }); toast('已发送测试通知'); }
    catch (err) { toast(err.message, true); }
    return;
  }
  if (act === 'reload') { await load(); toast('已重新读取'); return; }
  if (act === 'reveal') {
    try { await api('/api/reveal', { method: 'POST', body: JSON.stringify({ target: el.dataset.v }) }); }
    catch (err) { toast(err.message, true); }
    return;
  }
  if (act === 'open-brief') {
    const d = (S.data.brief || {}).date;
    if (d) window.open(`/briefings/${d}.html`, '_blank');
    return;
  }
  if (act === 'sim-account') {
    const aid = el.dataset.v;
    if (aid && aid !== S.simAccount) {
      S.simAccount = aid;
      if (!(S.simDetail && S.simDetail[aid])) { ensureSim(); return; }
      render();
    }
    return;
  }
  if (act === 'sim-mode') {
    const m = el.dataset.v;
    if (m && m !== S.simMode) {
      S.simMode = m;
      if (m === 'compare' && !S.simCompare) { ensureSim(); return; }
      if (m === 'single' && !(S.simDetail && S.simDetail[S.simAccount])) { ensureSim(); return; }
      render();
    }
    return;
  }
  if (act === 'sim-refresh') {
    S.simOverview = null; S.simDetail = null; S.simCompare = null; S.simKline = null;
    ensureSim(true);
    toast('正在刷新模拟交易数据…');
    return;
  }
});

document.addEventListener('change', (e) => {
  if (e.target.id === 'alert-date') { S.alertDate = e.target.value; render(); }
  const ksel = e.target.closest('[data-act="sim-kline"]');
  if (ksel) { S.simKlineCode = ksel.value; ensureSimKline(ksel.value); render(); return; }
  if (e.target.id === 'screen-date') {
    const day = e.target.value;
    S.screening = null; S.screeningLoading = true; S.screeningErr = null;
    api(`/api/screening?date=${encodeURIComponent(day)}`)
      .then((d) => { S.screening = d; })
      .catch((err) => { S.screeningErr = err.message; S.screening = { date: day, dates: [], rows: [] }; })
      .finally(() => { S.screeningLoading = false; updateNav(); render(); });
  }
});

const SCOPES = {
  watch: (c) => ({ watchlist: c.watchlist }),
  holdings: (c) => ({ holdings: c.holdings }),
  risk: (c) => ({ risk: c.risk }),
  notify: (c) => ({ notify: c.notify }),
  thresholds: (c) => ({ thresholds: c.thresholds }),
  meta: (c) => ({ meta: c.meta }),
};

async function doSave(scope, btn) {
  const build = SCOPES[scope];
  if (!build) return;
  if (btn) { btn.disabled = true; btn.textContent = '保存中…'; }
  try {
    await api('/api/config', { method: 'POST', body: JSON.stringify({ patch: build(S.cfg) }) });
    toast('已保存到 config.yaml');
    await load();
  } catch (err) {
    const errs = err.payload && err.payload.errors;
    showErrors(errs || [err.message]);
  } finally {
    if (btn && btn.isConnected) { btn.disabled = false; btn.textContent = '保存'; }
  }
}

function showErrors(list) {
  const m = $('#modal');
  m.hidden = false;
  m.innerHTML = `<div class="modal">
    <div class="modal-h">配置未保存</div>
    <div class="modal-b"><div>下面这些问题需要先修掉，否则会算出错误的价格或仓位：</div>
      <ul>${list.map((x) => `<li>${esc(x)}</li>`).join('')}</ul></div>
    <div class="modal-f"><button class="btn primary" data-act="close-modal">知道了</button></div>
  </div>`;
}

document.addEventListener('click', (e) => {
  if (e.target.closest('[data-act="close-modal"]') || e.target.id === 'modal') {
    $('#modal').hidden = true;
  }
});

/* ---------------- 运行 ---------------- */
let runTimer = null;

async function runBrief() {
  if (S.running) return;
  const notify = !!(S.cfg.notify || {}).macos;
  try {
    await api('/api/run', { method: 'POST', body: JSON.stringify({ notify }) });
    S.running = true;
    setRunChip(true);
    showRunner(['正在启动…']);
    poll();
  } catch (err) {
    toast(err.message, true);
  }
}

async function poll() {
  try {
    const st = await api('/api/run/status');
    S.running = st.running;
    setRunChip(st.running);
    showRunner(st.log, st.result);
    if (st.running) {
      runTimer = setTimeout(poll, 1200);
    } else {
      if (st.result && st.result.ok) {
        toast(`完成：${st.result.plans_ready} 个可执行计划`);
        await load();
      } else if (st.result && st.result.error) {
        toast('运行失败：' + st.result.error, true);
      }
      setTimeout(hideRunner, 2600);
    }
  } catch {
    runTimer = setTimeout(poll, 2500);
  }
}

function setRunChip(running) {
  const chip = $('#run-chip');
  const btn = $('#btn-run');
  if (running) {
    chip.hidden = false;
    chip.innerHTML = '<span class="spin"></span> 正在采集行情…';
    btn.disabled = true;
    btn.textContent = '运行中…';
  } else {
    chip.hidden = true;
    btn.disabled = false;
    btn.textContent = '运行简报';
  }
}

let runnerEl = null;
function showRunner(log, result) {
  if (!runnerEl) {
    runnerEl = document.createElement('div');
    runnerEl.className = 'runner';
    document.body.appendChild(runnerEl);
  }
  runnerEl.innerHTML = `<div class="runner-h">
      <span>${S.running ? '正在运行' : (result && result.ok ? '运行完成' : '运行结束')}</span>
      <span class="muted" style="color:#8E9498">${S.running ? '约 30 秒' : '点击关闭'}</span>
    </div>
    <div class="log">${esc((log || []).join('\n')) || '…'}</div>`;
  const logEl = runnerEl.querySelector('.log');
  if (logEl) logEl.scrollTop = logEl.scrollHeight;
}

function hideRunner() {
  if (runnerEl) { runnerEl.remove(); runnerEl = null; }
}

/* ---------------- 启动 ---------------- */
function tickClock() {
  const now = new Date();
  const p = (n) => String(n).padStart(2, '0');
  $('#clock').textContent = `${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())} ${p(now.getHours())}:${p(now.getMinutes())}:${p(now.getSeconds())}`;
}

function routeFromHash() {
  const raw = (location.hash || '#/dashboard').replace('#/', '');
  const [v, qs] = raw.split('?');
  if (qs) {
    const k = new URLSearchParams(qs).get('kline');
    if (k) S.simKlineCode = k;   // 深链：#/simulation?kline=000591 直接打开该标的的K线与买卖点
  }
  setView(v || 'dashboard');
}

window.addEventListener('hashchange', routeFromHash);
$('#btn-run').addEventListener('click', runBrief);
$('#btn-reload').addEventListener('click', async () => { await load(); toast('已重新读取'); });

/* ---------------- 自动刷新调度 ----------------
   原则：用户停在哪个页面，就把那个页面上"该实时"的数据自动刷新到位——
   · 指数条 / 行情脉搏：30s 强制拉一次（服务端上游缓存 60s，多拉不额外打接口）
   · 计划表 / 自选池 / 持仓的现价：30s 走 /api/quotes 实时跳动
   · 模拟盘盈亏：30s force 刷新（服务端按实时价重估持仓市值）
   · 简报本身（日期/温度/结论）：60s 重读 /api/state，只有收盘任务重新生成了
     简报（date 或 generated_at 变了）才提交并重绘，避免无谓闪烁
   自动重绘一律保留滚动位置，且用户正在输入框里编辑时不打扰。 */
function editing() {
  const ae = document.activeElement;
  return !!(ae && /^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName));
}

function softRenderIf(view) {
  if (S.view !== view || editing()) return;
  const y = window.scrollY;
  render();
  window.scrollTo(0, y);
}

async function refreshState() {
  if (editing()) return;
  try {
    const d = await api('/api/state');
    const oldB = ((S.data || {}).brief) || {};
    const nb = d.brief || {};
    const changed = !S.data || oldB.date !== nb.date || oldB.generated_at !== nb.generated_at;
    S.data = d;
    if (changed) {
      S.cfg = clone(d.config);
      if (!S.histSel && (d.history || []).length) S.histSel = d.history[0].date;
      softRenderIf(S.view);
    }
  } catch (e) { /* 网络抖动，下一轮再试 */ }
}

function fastTick() {
  const v = S.view;
  if (v === 'pulse') {
    Promise.all([loadPulse(true), loadActivity(40)]).then(() => softRenderIf('pulse'));
  } else if (v === 'dashboard') {
    Promise.all([loadPulse(true), loadQuotes(), loadActivity(10)]).then(() => softRenderIf('dashboard'));
  } else if (v === 'plans' || v === 'watch' || v === 'holdings') {
    loadQuotes().then(() => softRenderIf(v));
  } else if (v === 'simulation') {
    ensureSim(true);
  }
}

function slowTick() {
  if (S.view === 'dashboard' || S.view === 'alerts') refreshState();
}

// 回到前台立刻刷一次，避免后台标签长时间挂起后仍显示旧数据
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') { fastTick(); slowTick(); }
});

(async () => {
  tickClock();
  setInterval(tickClock, 1000);
  setInterval(fastTick, 30000);   // 实时指数 / 现价 / 模拟盘盈亏
  setInterval(slowTick, 60000);   // 简报状态（收盘重生成后自动滚到当天）
  routeFromHash();
  try {
    await load();
    const st = await api('/api/run/status');
    if (st.running) { S.running = true; setRunChip(true); poll(); }
  } catch (err) {
    $('#stage').innerHTML = `<div class="banner bad"><div><b>加载失败：</b>${esc(err.message)}
      <div class="muted" style="margin-top:4px">若是网络类错误，请确认 server.py 还在运行。</div></div></div>`;
  }
})();
