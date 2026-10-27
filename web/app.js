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
};

const TITLES = {
  dashboard: ['仪表盘', '今天的市场状态与可执行计划'],
  plans: ['交易计划', '买点 / 止损 / 目标 / 仓位，全部由规则算出'],
  watch: ['自选池', '短线池建议 10–20 只，太多会看不过来'],
  holdings: ['持仓', '填了持仓才算得出止损距离和时间止损'],
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
    dashboard: viewDashboard, plans: viewPlans, watch: viewWatch,
    holdings: viewHoldings, alerts: viewAlerts, history: viewHistory, settings: viewSettings,
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

  const p0 = d.brief ? (d.brief.alerts_all || []).filter((a) => a.level === 'P0').length : 0;
  const na = $('#nav-alerts');
  na.textContent = p0 || '';
  na.className = 'n' + (p0 ? ' hot' : '');
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

  <h2 class="sec">指数与外围</h2>
  <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(148px,1fr))">
    ${(m.indices || []).map((i) => `
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
        return `<tr>
          <td class="name"><b>${esc(r.name)}</b>${r.is_holding ? ' <span class="pill hold">持仓</span>' : ''}
            <div class="code">${esc(r.code)} · ${esc(r.tag || '')}</div></td>
          <td class="r n"><b style="color:${scoreColor(r.score)}">${r.score}</b></td>
          <td class="r n">${num((r.snap || {}).close)}</td>
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
  </div>`;
}

/* ---------------- 视图：交易计划 ---------------- */
function viewPlans() {
  const b = S.data.brief;
  if (!b) return `<div class="empty"><b>还没有简报</b>先运行一次。</div>`;
  const rows = (b.watch || []).map((r) => ({
    date: b.date, name: r.name, code: r.code, score: r.score, price: (r.snap || {}).close,
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
          <td class="r n">${num(r.price)}</td>
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
        const sig = (r.alerts || []).slice(0, 2).map((a) => `<span class="pill ${a.level}">${esc(a.rule)}</span>`).join(' ')
          || '<span class="muted">—</span>';
        return `<tr>
          <td><input class="w-code" type="text" value="${esc(it.code)}" data-bind="watchlist.${i}.code" maxlength="6"></td>
          <td><input type="text" value="${esc(it.name)}" data-bind="watchlist.${i}.name" placeholder="名称"></td>
          <td><input type="text" value="${esc(it.tag || '')}" data-bind="watchlist.${i}.tag" placeholder="行业/主题"></td>
          <td class="r n">${r.score != null ? `<b style="color:${scoreColor(r.score)}">${r.score}</b>` : '<span class="muted">—</span>'}</td>
          <td class="r n ${cls(snap.pct_chg)}">${pct(snap.pct_chg)}</td>
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
        const broken = r.stop_broken;
        return `<tr>
          <td><input class="w-code" type="text" value="${esc(h.code)}" data-bind="holdings.${i}.code" maxlength="6"></td>
          <td class="r"><input type="number" value="${esc(h.qty)}" data-bind="holdings.${i}.qty" style="text-align:right"></td>
          <td class="r"><input type="number" step="0.001" value="${esc(h.cost)}" data-bind="holdings.${i}.cost" style="text-align:right"></td>
          <td class="r n">${num(r.price)}</td>
          <td class="r n ${cls(r.pnl_pct)}">${pct(r.pnl_pct)}<div class="code">${r.pnl_amt != null ? num(r.pnl_amt, 0) : ''}</div></td>
          <td class="r n">${r.market_value != null ? amt(r.market_value) : '—'}</td>
          <td class="r n">${r.held_days != null ? r.held_days + ' 天' : '—'}</td>
          <td class="r n">${num(r.stop)}<div class="code">${esc(r.stop_basis || '')}</div></td>
          <td class="r n ${cls(r.stop_dist_pct)}">${r.stop_dist_pct != null ? pct(r.stop_dist_pct, 1) : '—'}
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
});

document.addEventListener('change', (e) => {
  if (e.target.id === 'alert-date') { S.alertDate = e.target.value; render(); }
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
  const v = (location.hash || '#/dashboard').replace('#/', '');
  setView(v || 'dashboard');
}

window.addEventListener('hashchange', routeFromHash);
$('#btn-run').addEventListener('click', runBrief);
$('#btn-reload').addEventListener('click', async () => { await load(); toast('已重新读取'); });

(async () => {
  tickClock();
  setInterval(tickClock, 1000);
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
