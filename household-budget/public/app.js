'use strict';
// Household Budget -- single-page front end. No framework, no build step.
// Note: the CSP forbids inline style attributes, so widths/positions for
// meters are set from data-* attributes after each render (applyGeometry).

// ------------------------------------------------------------------ helpers

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const view = $('#view');

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

const nzd = new Intl.NumberFormat('en-NZ', { style: 'currency', currency: 'NZD' });
const nzd0 = new Intl.NumberFormat('en-NZ', { style: 'currency', currency: 'NZD', maximumFractionDigits: 0 });
function money(cents, { whole = false } = {}) {
  if (cents == null) return '—';
  return (whole ? nzd0 : nzd).format(cents / 100);
}
function signedMoney(cents) {
  return `<span class="${cents > 0 ? 'pos' : 'neg'}">${cents > 0 ? '+' : ''}${esc(money(cents))}</span>`;
}
function toCents(str) {
  const t = String(str ?? '').replace(/[$,\s]/g, '');
  if (t === '') return null;
  const n = Number(t);
  return Number.isFinite(n) ? Math.round(n * 100) : NaN;
}
function centsToInput(c) { return c == null ? '' : (c / 100).toFixed(2); }

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
function fmtDate(s, { year = false } = {}) {
  if (!s) return '—';
  const [y, m, d] = s.slice(0, 10).split('-').map(Number);
  return `${d} ${MONTHS[m - 1]}${year ? ' ' + y : ''}`;
}
function ago(iso) {
  if (!iso) return 'never';
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins} min ago`;
  if (mins < 60 * 24) return `${Math.round(mins / 60)} h ago`;
  return `${Math.round(mins / 1440)} days ago`;
}

class ApiError extends Error {}

async function api(method, path, body, { raw = false, contentType } = {}) {
  const headers = { 'X-Requested-With': 'household-budget' };
  if (body !== undefined) headers['Content-Type'] = contentType || 'application/json';
  const res = await fetch('/api' + path, {
    method, headers, credentials: 'same-origin',
    body: body === undefined ? undefined : raw ? body : JSON.stringify(body),
  });
  if (res.status === 401 && path !== '/login') { showLogin(); throw new ApiError('Signed out'); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(data.error || `Request failed (${res.status})`);
  return data;
}

let toastTimer;
function toast(html, { actions = [], ms = 5000 } = {}) {
  const t = $('#toast');
  t.innerHTML = `<span>${html}</span>`;
  for (const a of actions) {
    const b = document.createElement('button');
    b.className = 'btn small'; b.type = 'button'; b.textContent = a.label;
    b.addEventListener('click', () => { t.hidden = true; a.onClick(); });
    t.appendChild(b);
  }
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, ms);
}
function toastError(e) { if (!(e instanceof ApiError && e.message === 'Signed out')) toast(esc(e.message)); }

function applyGeometry(root = view) {
  for (const e of $$('[data-w]', root)) e.style.width = `${e.dataset.w}%`;
  for (const e of $$('[data-left]', root)) e.style.left = `${e.dataset.left}%`;
}

// Open a modal form. `fields` is HTML; resolves with FormData or null.
function modal(title, fieldsHtml, { submit = 'Save', onInput } = {}) {
  const dlg = $('#dialog');
  const form = $('#dialog-form');
  form.innerHTML = `<h2>${esc(title)}</h2><div class="stack">${fieldsHtml}</div>
    <p class="error" id="dialog-error"></p>
    <div class="row spread"><button class="btn" value="cancel" formnovalidate>Cancel</button>
    <button class="btn primary" value="ok">${esc(submit)}</button></div>`;
  if (onInput) form.addEventListener('input', () => onInput(form));
  dlg.showModal();
  return new Promise((resolve) => {
    dlg.addEventListener('close', () => {
      resolve(dlg.returnValue === 'ok' ? new FormData(form) : null);
    }, { once: true });
  });
}

// ------------------------------------------------------------------ state

const state = { meta: null, period: null };

async function loadMeta() {
  state.meta = await api('GET', '/meta');
  if (!state.period) state.period = state.meta.current_period.key;
  const badge = $('#review-badge');
  badge.hidden = !state.meta.to_review;
  badge.textContent = state.meta.to_review;
  const s = state.meta.last_sync;
  const status = $('#sync-status');
  if (!state.meta.bank_connected) status.textContent = 'Bank feed not connected';
  else if (s && s.status === 'error') status.textContent = `Sync failed ${ago(s.started_at)}`;
  else status.textContent = `Synced ${ago(state.meta.last_sync_ok_at)}`;
}

const cats = () => state.meta.categories;
const catById = (id) => cats().find((c) => c.id === id);
const accountById = (id) => state.meta.accounts.find((a) => a.id === id);

function categoryOptions(selected, { includeNone = true, includeArchived = false } = {}) {
  const groups = new Map();
  for (const c of cats()) {
    if (c.archived && !includeArchived && c.id !== selected) continue;
    if (!groups.has(c.group_name)) groups.set(c.group_name, []);
    groups.get(c.group_name).push(c);
  }
  let html = includeNone ? `<option value="">— To review —</option>` : '';
  for (const [g, list] of groups) {
    html += `<optgroup label="${esc(g)}">${list.map((c) => `<option value="${c.id}" ${c.id === selected ? 'selected' : ''}>${esc(c.name)}</option>`).join('')}</optgroup>`;
  }
  return html;
}

function accountOptions(selected, { any = null, filter = () => true } = {}) {
  return (any ? `<option value="">${esc(any)}</option>` : '')
    + state.meta.accounts.filter(filter).map((a) => `<option value="${esc(a.id)}" ${a.id === selected ? 'selected' : ''}>${esc([a.bank, a.name].filter(Boolean).join(' · '))}</option>`).join('');
}

function periodNav(p, onChange) {
  const html = `<div class="period-nav">
    <button class="btn small" data-p="${esc(p.prev)}" aria-label="Previous period">‹</button>
    <span class="label">${esc(p.label)}</span>
    <button class="btn small" data-p="${esc(p.next)}" aria-label="Next period">›</button>
    ${p.is_current ? '' : `<button class="link small" data-p="${esc(state.meta.current_period.key)}">This period</button>`}
  </div>`;
  setTimeout(() => $$('[data-p]').forEach((b) => b.addEventListener('click', () => { state.period = b.dataset.p; onChange(); })));
  return html;
}

// ------------------------------------------------------------------ auth

function showLogin() {
  $('#shell').hidden = true;
  $('#login').hidden = false;
  $('#login-form [name=username]').focus();
}

$('#login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  $('#login-error').textContent = '';
  try {
    await api('POST', '/login', { username: f.get('username'), password: f.get('password'), code: f.get('code') });
    e.target.reset();
    await start();
  } catch (err) {
    $('#login-error').textContent = err.message;
    e.target.code.value = '';
  }
});

// Lock the screen after inactivity, matching the server's idle timeout.
let idleTimer;
function resetIdle() {
  clearTimeout(idleTimer);
  idleTimer = setTimeout(async () => { try { await api('POST', '/logout', {}); } catch { /* ignore */ } showLogin(); }, 60 * 60 * 1000);
}
['click', 'keydown', 'touchstart'].forEach((ev) => document.addEventListener(ev, resetIdle, { passive: true }));

$('#sync-btn').addEventListener('click', async (e) => {
  const b = e.currentTarget;
  b.disabled = true; b.textContent = 'Syncing…';
  try {
    const r = await api('POST', '/sync', { refresh: true });
    toast(`Synced: ${r.added} new, ${r.updated} updated. Banks can take a few minutes to post new transactions — sync again shortly if something's missing.`, { ms: 7000 });
  } catch (err) { toastError(err); }
  b.disabled = false; b.textContent = 'Sync';
  await loadMeta();
  route();
});

// ------------------------------------------------------------------ router

const routes = {
  '': renderOverview,
  transactions: renderTransactions,
  budgets: renderBudgets,
  rules: renderRules,
  mortgage: renderMortgage,
  settings: renderSettings,
};

function parseHash() {
  const [path, qs] = location.hash.replace(/^#\/?/, '').split('?');
  return { path, params: new URLSearchParams(qs || '') };
}

async function route() {
  const { path, params } = parseHash();
  const fn = routes[path] || renderOverview;
  $$('#nav a').forEach((a) => a.classList.toggle('active', a.getAttribute('href') === `#/${path}`));
  try { await fn(params); applyGeometry(); } catch (e) { toastError(e); }
}
window.addEventListener('hashchange', route);

async function start() {
  await loadMeta();
  $('#login').hidden = true;
  $('#shell').hidden = false;
  resetIdle();
  await route();
}

(async () => {
  try { await start(); } catch { showLogin(); }
})();

// ================================================================ OVERVIEW

function meterStatus(c, pace) {
  if (!c.budget_cents) return null;
  const used = c.actual_cents / c.budget_cents;
  if (used > 1) return { cls: 'critical', text: `Over by ${money(c.actual_cents - c.budget_cents, { whole: true })}` };
  // Pace warnings only make sense for day-to-day spending, not a bill paid once.
  if (c.count >= 3 && used > pace + 0.1 && used > 0.5) return { cls: 'warning', text: 'Ahead of pace' };
  return { cls: 'good', text: 'On track' };
}

function meter(actual, budget, pace, count = 0) {
  if (!budget) return '<span class="muted small">No budget</span>';
  const pct = Math.max(0, Math.min(100, (actual / budget) * 100));
  const st = actual > budget ? 'critical' : (count >= 3 && actual / budget > pace + 0.1 && actual / budget > 0.5) ? 'warning' : '';
  return `<div class="meter" role="img" aria-label="${Math.round((actual / budget) * 100)}% of budget used">
    <div class="fill ${st}" data-w="${pct.toFixed(1)}"></div>
    <div class="pace" data-left="${(pace * 100).toFixed(1)}" title="Where spending would be if it were even across the period"></div></div>`;
}

async function renderOverview() {
  const s = await api('GET', `/periods/${state.period}/summary`);
  const m = await api('GET', '/mortgage').catch(() => null);
  const p = s.period;
  const t = s.totals;
  const pace = p.days_total ? p.days_elapsed / p.days_total : 0;
  const expenseCats = s.categories.filter((c) => c.kind === 'expense' && (c.actual_cents || c.budget_cents));
  const incomeCats = s.categories.filter((c) => c.kind === 'income' && (c.actual_cents || c.budget_cents));
  const left = t.expense_budget_cents - t.expense_cents;

  const groups = new Map();
  for (const c of expenseCats) { if (!groups.has(c.group)) groups.set(c.group, []); groups.get(c.group).push(c); }

  const budgetAccounts = state.meta.accounts.filter((a) => !a.is_mortgage);
  const nextFixed = m?.splits.filter((x) => x.fixed_days_left != null).sort((a, b) => a.fixed_days_left - b.fixed_days_left)[0];

  view.innerHTML = `
    <div class="row spread">
      ${periodNav(p, renderOverviewAgain)}
      <span class="muted small">${p.is_current ? `Day ${p.days_elapsed} of ${p.days_total}` : `${fmtDate(p.start, { year: true })} – ${fmtDate(p.end, { year: true })}`}</span>
    </div>
    <div class="grid tiles">
      <div class="card tile"><div class="label">Income</div><div class="value">${money(t.income_cents, { whole: true })}</div>
        <div class="sub">${t.income_budget_cents ? `of ${money(t.income_budget_cents, { whole: true })} expected` : 'no income budget set'}</div></div>
      <div class="card tile"><div class="label">Spent</div><div class="value">${money(t.expense_cents, { whole: true })}</div>
        <div class="sub">${t.expense_budget_cents ? `of ${money(t.expense_budget_cents, { whole: true })} budgeted` : 'no budgets set yet'}</div></div>
      <div class="card tile"><div class="label">Left to spend</div><div class="value">${t.expense_budget_cents ? money(left, { whole: true }) : '—'}</div>
        <div class="sub">${t.expense_budget_cents && p.is_current && p.days_total > p.days_elapsed ? `${money(Math.max(0, left) / (p.days_total - p.days_elapsed + 1), { whole: true })} / day for ${p.days_total - p.days_elapsed + 1} days` : '&nbsp;'}</div></div>
      <div class="card tile"><div class="label">Net (in − out)</div><div class="value">${signedMoney(t.net_cents)}</div>
        <div class="sub">transfers between your accounts excluded</div></div>
    </div>
    ${t.uncategorised_count ? `<div class="card callout row spread">
      <div><strong>${t.uncategorised_count} transaction${t.uncategorised_count === 1 ? '' : 's'} to review</strong>
      <div class="muted small">${money(t.uncategorised_out_cents)} out and ${money(t.uncategorised_in_cents)} in hasn't been coded yet. Code one and the app learns that merchant.</div></div>
      <a class="btn primary" href="#/transactions?review=1">Review now</a></div>` : ''}
    <div class="grid cols">
      <section class="card">
        <div class="row spread"><h2>Spending vs budget</h2><a class="small" href="#/budgets">Edit budgets</a></div>
        ${expenseCats.length ? `<div class="table-wrap"><table>
          <thead><tr><th>Category</th><th class="num">Spent</th><th class="num hide-sm">Budget</th><th class="num">Left</th><th class="hide-sm"></th></tr></thead>
          <tbody>${[...groups].map(([g, list]) => `<tr class="group"><td colspan="5">${esc(g)}</td></tr>` + list.map((c) => {
            const st = meterStatus(c, pace);
            return `<tr>
              <td><a href="#/transactions?period=${esc(p.key)}&category=${c.id}">${esc(c.name)}</a>
                ${st ? `<div class="status ${st.cls}">${esc(st.text)}</div>` : ''}</td>
              <td class="num">${money(c.actual_cents, { whole: true })}</td>
              <td class="num hide-sm">${c.budget_cents ? money(c.budget_cents, { whole: true }) : '—'}</td>
              <td class="num">${c.budget_cents ? money(c.budget_cents - c.actual_cents, { whole: true }) : '—'}</td>
              <td class="hide-sm">${meter(c.actual_cents, c.budget_cents, p.is_current ? pace : 1, c.count)}</td></tr>`;
          }).join('')).join('')}</tbody></table></div>
          ${p.is_current ? '<p class="muted small">The thin mark on each bar is where you\'d be if spending were spread evenly across the period.</p>' : ''}`
        : '<div class="empty">No spending coded in this period yet.</div>'}
        ${incomeCats.length ? `<h3>Income</h3><table><tbody>${incomeCats.map((c) => `<tr>
          <td><a href="#/transactions?period=${esc(p.key)}&category=${c.id}">${esc(c.name)}</a></td>
          <td class="num">${money(c.actual_cents)}</td><td class="num muted">${c.budget_cents ? 'of ' + money(c.budget_cents, { whole: true }) : ''}</td></tr>`).join('')}</tbody></table>` : ''}
      </section>
      <aside class="stack">
        <section class="card">
          <h2>Accounts</h2>
          ${budgetAccounts.length ? `<table><tbody>${budgetAccounts.map((a) => `<tr>
            <td><div class="desc">${esc(a.name)}</div><div class="sub">${esc(a.bank || '')} ${a.include_in_budget ? '' : '· not in budget'}</div></td>
            <td class="num">${money(a.balance_cents)}</td></tr>`).join('')}</tbody></table>`
          : `<p class="muted small">No accounts yet. ${state.meta.bank_connected ? 'Press Sync to pull them from your banks.' : 'Connect Akahu (see README) or import a CSV in <a href="#/settings">Settings</a>.'}</p>`}
        </section>
        <section class="card">
          <div class="row spread"><h2>Mortgage</h2><a class="small" href="#/mortgage">Details</a></div>
          ${m && m.splits.length ? `<dl class="kv">
            <dt>Owing</dt><dd>${money(m.total_balance_cents, { whole: true })}</dd>
            <dt>Average rate</dt><dd>${m.weighted_rate_pct.toFixed(2)}%</dd>
            <dt>Interest this period</dt><dd>${t.mortgage_interest_cents ? money(t.mortgage_interest_cents) : `~${money(m.monthly_interest_cents, { whole: true })} est.`}</dd>
            ${nextFixed ? `<dt>Next rate change</dt><dd>${esc(nextFixed.name)}: ${nextFixed.fixed_days_left < 0 ? 'expired' : `${nextFixed.fixed_days_left} days`}</dd>` : ''}
          </dl>` : '<p class="muted small">Add your loan splits on the <a href="#/mortgage">Mortgage</a > page to track rates, fixed-term expiries and payoff.</p>'}
        </section>
      </aside>
    </div>`;
}
function renderOverviewAgain() { route(); }

// ============================================================ TRANSACTIONS

async function renderTransactions(params) {
  const review = params.get('review') === '1';
  const period = params.get('period') ?? (review ? '' : state.period);
  const q = new URLSearchParams();
  if (period) q.set('period', period);
  for (const k of ['account', 'category', 'q']) if (params.get(k)) q.set(k, params.get(k));
  if (review) q.set('review', '1');
  const { items } = await api('GET', `/transactions?${q}`);

  const setParam = (k, v) => {
    const np = new URLSearchParams(params);
    if (v) np.set(k, v); else np.delete(k);
    location.hash = `#/transactions?${np}`;
  };

  const periodChoices = [];
  let k = state.meta.current_period.key;
  for (let i = 0; i < 18; i++) {
    periodChoices.push(k);
    const [y, m] = k.split('-').map(Number);
    k = m === 1 ? `${y - 1}-12` : `${y}-${String(m - 1).padStart(2, '0')}`;
  }
  const inTotal = items.filter((t) => t.amount_cents > 0).reduce((s, t) => s + t.amount_cents, 0);
  const outTotal = items.filter((t) => t.amount_cents < 0).reduce((s, t) => s - t.amount_cents, 0);

  view.innerHTML = `
    <div class="row spread"><h1>${review ? 'To review' : 'Transactions'}</h1>
      <label class="inline"><input type="checkbox" id="f-review" ${review ? 'checked' : ''}> Only uncoded</label></div>
    <div class="card row">
      <select id="f-period" aria-label="Period"><option value="">All dates</option>${periodChoices.map((pk) => `<option value="${pk}" ${pk === period ? 'selected' : ''}>${pk === state.meta.current_period.key ? 'This period' : esc(pk)}</option>`).join('')}</select>
      <select id="f-account" aria-label="Account">${accountOptions(params.get('account'), { any: 'All accounts' })}</select>
      <select id="f-category" aria-label="Category"><option value="">All categories</option><option value="none" ${params.get('category') === 'none' ? 'selected' : ''}>Uncoded</option>${categoryOptions(Number(params.get('category')) || null, { includeNone: false, includeArchived: true })}</select>
      <input id="f-q" type="search" placeholder="Search description, reference…" value="${esc(params.get('q') || '')}">
      <span class="muted small">${items.length} shown · in ${money(inTotal)} · out ${money(outTotal)}</span>
    </div>
    ${items.length ? `<div class="card table-wrap"><table class="txn-table">
      <thead><tr><th>Date</th><th>Description</th><th class="hide-sm">Account</th><th class="num">Amount</th><th>Category</th></tr></thead>
      <tbody>${items.map((t) => txnRow(t)).join('')}</tbody></table></div>`
    : `<div class="card empty">${review ? 'Nothing to review — every transaction is coded.' : 'No transactions match.'}</div>`}`;

  $('#f-review').addEventListener('change', (e) => setParam('review', e.target.checked ? '1' : ''));
  $('#f-period').addEventListener('change', (e) => setParam('period', e.target.value));
  $('#f-account').addEventListener('change', (e) => setParam('account', e.target.value));
  $('#f-category').addEventListener('change', (e) => setParam('category', e.target.value));
  $('#f-q').addEventListener('change', (e) => setParam('q', e.target.value.trim()));

  for (const sel of $$('select[data-txn]')) {
    sel.addEventListener('change', async () => {
      const id = sel.dataset.txn;
      const categoryId = sel.value ? Number(sel.value) : null;
      try {
        const t = await api('PATCH', `/transactions/${encodeURIComponent(id)}`, { category_id: categoryId });
        await loadMeta();
        route();
        if (categoryId) {
          const c = catById(categoryId);
          toast(`Coded as <strong>${esc(c.name)}</strong>. Similar transactions will be coded the same way.`, {
            ms: 9000,
            actions: [{ label: `Make a rule for “${t.suggested_pattern}”`, onClick: () => editRule({ pattern: t.suggested_pattern, category_id: categoryId, direction: t.amount_cents < 0 ? 'out' : 'in' }) }],
          });
        }
      } catch (e) { toastError(e); }
    });
  }
  for (const b of $$('[data-note]')) b.addEventListener('click', () => editNote(b.dataset.note, items));
}

const CODED_BY = { manual: 'you', rule: 'rule', transfer: 'transfer', memory: 'learned', bank: 'bank', none: '' };

function txnRow(t) {
  const meta = [t.merchant && t.merchant !== t.description ? t.merchant : null, t.particulars, t.code, t.reference].filter(Boolean).join(' · ');
  return `<tr>
    <td class="small txn-date">${fmtDate(t.date)}</td>
    <td class="txn-desc"><div class="desc">${esc(t.description)}</div>
      <div class="sub">${esc(meta)}${t.note ? ` · <em>${esc(t.note)}</em>` : ''}
      <button class="link small" data-note="${esc(t.id)}">${t.note ? 'edit note' : 'note'}</button></div></td>
    <td class="hide-sm small">${esc(t.account_name)}</td>
    <td class="num txn-amt">${signedMoney(t.amount_cents)}</td>
    <td class="txn-cat"><select data-txn="${esc(t.id)}" aria-label="Category">${categoryOptions(t.category_id)}</select>
      ${CODED_BY[t.coded_by] ? `<span class="pill" title="How this was coded">${esc(CODED_BY[t.coded_by])}</span>` : ''}</td>
  </tr>`;
}

async function editNote(id, items) {
  const t = items.find((x) => x.id === id);
  const f = await modal('Note', `<label>Note for “${esc(t.description)}”<textarea name="note" rows="3" maxlength="500">${esc(t.note || '')}</textarea></label>`);
  if (!f) return;
  try { await api('PATCH', `/transactions/${encodeURIComponent(id)}`, { note: f.get('note') }); route(); } catch (e) { toastError(e); }
}

// ================================================================ BUDGETS

async function renderBudgets() {
  const s = await api('GET', `/periods/${state.period}/summary`);
  const b = await api('GET', `/budgets/${state.period}`);
  const own = new Map(b.items.map((i) => [i.category_id, i]));
  const rows = cats().filter((c) => c.kind !== 'transfer' && !c.archived);
  const byId = new Map(s.categories.map((c) => [c.id, c]));
  const groups = new Map();
  for (const c of rows) { if (!groups.has(c.group_name)) groups.set(c.group_name, []); groups.get(c.group_name).push(c); }

  view.innerHTML = `
    <div class="row spread"><h1>Budgets</h1>${periodNav(s.period, route)}</div>
    <p class="muted small">Set a standing monthly budget once and it applies to every period. Use “Just this period” for a one-off (e.g. Christmas, a holiday month).</p>
    <form id="budget-form" class="card table-wrap">
      <table><thead><tr><th>Category</th><th class="num hide-sm">3-period avg</th><th class="num">This period so far</th><th class="num">Budget</th></tr></thead>
      <tbody>${[...groups].map(([g, list]) => `<tr class="group"><td colspan="4">${esc(g)}</td></tr>` + list.map((c) => {
        const x = byId.get(c.id) || {};
        const o = own.get(c.id);
        return `<tr><td>${esc(c.name)} ${o?.own ? '<span class="pill">this period only</span>' : ''}</td>
          <td class="num hide-sm muted">${x.avg3_cents ? money(x.avg3_cents, { whole: true }) : '—'}</td>
          <td class="num">${money(x.actual_cents || 0, { whole: true })}</td>
          <td class="num"><input class="money-input" name="c${c.id}" inputmode="decimal" value="${o ? centsToInput(o.amount_cents) : ''}" placeholder="0.00" aria-label="Budget for ${esc(c.name)}"></td></tr>`;
      }).join('')).join('')}
      <tr><td><strong>Total spending budget</strong></td><td class="hide-sm"></td><td></td><td class="num" id="budget-total"></td></tr>
      </tbody></table>
      <div class="row">
        <button class="btn primary" type="submit" name="scope" value="default">Save for every period</button>
        <button class="btn" type="submit" name="scope" value="period">Save for just this period</button>
        <button class="btn" type="button" id="fill-avg">Fill blanks with 3-period average</button>
      </div>
    </form>`;

  const form = $('#budget-form');
  const total = () => {
    let sum = 0;
    for (const c of rows) if (c.kind === 'expense') sum += toCents(form[`c${c.id}`].value) || 0;
    $('#budget-total').textContent = money(sum, { whole: true });
  };
  total();
  form.addEventListener('input', total);
  $('#fill-avg').addEventListener('click', () => {
    for (const c of rows) {
      const input = form[`c${c.id}`];
      const avg = byId.get(c.id)?.avg3_cents;
      if (!input.value && avg > 0) input.value = (Math.ceil(avg / 1000) * 10).toFixed(2); // round up to $10
    }
    total();
  });
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const scope = e.submitter?.value || 'default';
    const items = [];
    for (const c of rows) {
      const v = toCents(form[`c${c.id}`].value);
      if (Number.isNaN(v)) { toast(`“${esc(form[`c${c.id}`].value)}” isn't a valid amount for ${esc(c.name)}`); return; }
      items.push({ category_id: c.id, amount_cents: v ?? 0 });
    }
    try {
      await api('PUT', `/budgets/${state.period}`, { scope, items });
      toast(scope === 'default' ? 'Budgets saved for every period.' : 'Budgets saved for this period only.');
      route();
    } catch (err) { toastError(err); }
  });
}

// ================================================================== RULES

const FIELD_LABEL = { any: 'Any text', description: 'Description', merchant: 'Merchant', particulars: 'Particulars', code: 'Code', reference: 'Reference' };
const MATCH_LABEL = { contains: 'contains', equals: 'is exactly', starts: 'starts with', regex: 'matches regex' };

async function renderRules() {
  const { items } = await api('GET', '/rules');
  view.innerHTML = `
    <div class="row spread"><h1>Coding rules</h1>
      <div class="row"><button class="btn" id="recode">Re-run auto-coding</button><button class="btn primary" id="add-rule">Add rule</button></div></div>
    <div class="card callout info small">
      New transactions are coded automatically in this order: <strong>your rules</strong> (top to bottom) →
      <strong>transfers</strong> between your own accounts (money into the mortgage counts as a repayment) →
      <strong>learned</strong> from how you've coded that merchant before → the <strong>bank's</strong> own category →
      otherwise it lands in <a href="#/transactions?review=1">To review</a>. Anything you code by hand is never changed.
    </div>
    ${items.length ? `<div class="card table-wrap"><table>
      <thead><tr><th class="num">Order</th><th>When</th><th>Code as</th><th class="num hide-sm">Hits</th><th></th></tr></thead>
      <tbody>${items.map((r) => `<tr class="${r.enabled ? '' : 'muted'}">
        <td class="num">${r.priority}</td>
        <td>${esc(FIELD_LABEL[r.field])} ${esc(MATCH_LABEL[r.match_type])} “<strong>${esc(r.pattern)}</strong>”
          <div class="sub">${[r.direction !== 'any' ? (r.direction === 'out' ? 'money out' : 'money in') : null,
            r.account_name ? `in ${esc(r.account_name)}` : null,
            r.min_cents != null ? `≥ ${money(r.min_cents)}` : null, r.max_cents != null ? `≤ ${money(r.max_cents)}` : null,
            r.enabled ? null : 'disabled'].filter(Boolean).join(' · ')}</div></td>
        <td>${esc(r.category_name)}</td>
        <td class="num hide-sm">${r.hits}</td>
        <td class="num"><button class="btn small" data-edit="${r.id}">Edit</button></td></tr>`).join('')}</tbody></table></div>`
    : '<div class="card empty">No rules yet. The quickest way to make one: code a transaction on the Transactions page and choose “Make a rule”.</div>'}`;

  $('#add-rule').addEventListener('click', () => editRule({}));
  $('#recode').addEventListener('click', async () => {
    try { const r = await api('POST', '/recode', {}); toast(`Checked ${r.examined} transactions, updated ${r.changed}.`); loadMeta(); } catch (e) { toastError(e); }
  });
  for (const b of $$('[data-edit]')) b.addEventListener('click', () => editRule(items.find((r) => r.id === Number(b.dataset.edit))));
}

async function editRule(r) {
  const opt = (map, v) => Object.entries(map).map(([k, l]) => `<option value="${k}" ${k === v ? 'selected' : ''}>${esc(l)}</option>`).join('');
  const fields = `
    <div class="form-grid">
      <label>Look in<select name="field">${opt(FIELD_LABEL, r.field || 'any')}</select></label>
      <label>Match<select name="match_type">${opt(MATCH_LABEL, r.match_type || 'contains')}</select></label>
    </div>
    <label>Text<input name="pattern" required maxlength="200" value="${esc(r.pattern || '')}"></label>
    <label>Code as<select name="category_id" required>${categoryOptions(r.category_id, { includeNone: false })}</select></label>
    <div class="form-grid">
      <label>Direction<select name="direction">${opt({ any: 'In or out', out: 'Money out', in: 'Money in' }, r.direction || 'any')}</select></label>
      <label>Account<select name="account_id">${accountOptions(r.account_id, { any: 'Any account' })}</select></label>
      <label>Min amount<input name="min" inputmode="decimal" value="${centsToInput(r.min_cents)}"></label>
      <label>Max amount<input name="max" inputmode="decimal" value="${centsToInput(r.max_cents)}"></label>
      <label>Order (lower runs first)<input name="priority" type="number" value="${r.priority ?? 100}"></label>
      <label class="inline"><input type="checkbox" name="enabled" ${r.enabled === 0 ? '' : 'checked'}> Enabled</label>
    </div>
    <p class="small muted" id="rule-preview">&nbsp;</p>
    ${r.id ? '<button class="btn danger small" type="button" id="delete-rule">Delete rule</button>' : ''}`;

  const toBody = (f) => ({
    field: f.get('field'), match_type: f.get('match_type'), pattern: f.get('pattern'), category_id: Number(f.get('category_id')),
    direction: f.get('direction'), account_id: f.get('account_id') || null, priority: Number(f.get('priority') || 100),
    min_cents: toCents(f.get('min')), max_cents: toCents(f.get('max')), enabled: f.get('enabled') === 'on',
  });
  let previewTimer;
  const preview = (form) => {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(async () => {
      const out = $('#rule-preview');
      try {
        const body = toBody(new FormData(form));
        if (!body.pattern) { out.textContent = ' '; return; }
        const res = await api('POST', '/rules/test', body);
        out.innerHTML = `Matches <strong>${res.count}</strong> existing transaction${res.count === 1 ? '' : 's'}${res.sample.length ? ', e.g. ' + res.sample.slice(0, 3).map((t) => `“${esc(t.description)}” (${esc(money(t.amount_cents))})`).join(', ') : ''}.`;
      } catch (e) { out.textContent = e.message; }
    }, 300);
  };
  const p = modal(r.id ? 'Edit rule' : 'New rule', fields, { onInput: preview });
  preview($('#dialog-form'));
  $('#delete-rule')?.addEventListener('click', async () => {
    if (!confirm('Delete this rule? Transactions it coded will be re-coded automatically.')) return;
    $('#dialog').close('cancel');
    try { await api('DELETE', `/rules/${r.id}`); toast('Rule deleted.'); route(); } catch (e) { toastError(e); }
  });
  const f = await p;
  if (!f) return;
  try {
    const body = toBody(f);
    if ([body.min_cents, body.max_cents].some(Number.isNaN)) throw new Error('Amounts must be numbers');
    const res = r.id ? await api('PUT', `/rules/${r.id}`, body) : await api('POST', '/rules', body);
    toast(`Rule saved${res.recoded ? ` — re-coded ${res.recoded.changed} transaction${res.recoded.changed === 1 ? '' : 's'}` : ''}.`);
    await loadMeta();
    route();
  } catch (e) { toastError(e); }
}

// =============================================================== MORTGAGE

const FREQ = { weekly: 'Weekly', fortnightly: 'Fortnightly', monthly: 'Monthly' };
let mortgageExtra = 0;

async function renderMortgage() {
  const m = await api('GET', `/mortgage?extra=${mortgageExtra}`);
  const statusText = (s) => (s.fixed_status === 'expired' ? 'Fixed term ended — now floating?'
    : s.fixed_status ? `Fixed until ${fmtDate(s.fixed_until, { year: true })} (${s.fixed_days_left} days)` : `${s.rate_type[0].toUpperCase()}${s.rate_type.slice(1)} rate`);
  const payoff = (d) => (d === 'never' ? 'Never at current repayments' : d ? fmtDate(d, { year: true }) : '—');

  view.innerHTML = `
    <div class="row spread"><h1>Mortgage</h1><button class="btn primary" id="add-split">Add loan split</button></div>
    ${m.splits.length ? `
    <div class="grid tiles">
      <div class="card tile"><div class="label">Total owing</div><div class="value">${money(m.total_balance_cents, { whole: true })}</div><div class="sub">${m.splits.length} split${m.splits.length === 1 ? '' : 's'}</div></div>
      <div class="card tile"><div class="label">Average rate</div><div class="value">${m.weighted_rate_pct.toFixed(2)}%</div><div class="sub">weighted by balance</div></div>
      <div class="card tile"><div class="label">Interest per month</div><div class="value">${money(m.monthly_interest_cents, { whole: true })}</div><div class="sub">at today's balance</div></div>
      <div class="card tile"><div class="label">Repayments per month</div><div class="value">${money(m.monthly_repayment_cents, { whole: true })}</div><div class="sub">${money(m.monthly_repayment_cents - m.monthly_interest_cents, { whole: true })} off principal</div></div>
      <div class="card tile"><div class="label">Mortgage-free</div><div class="value">${payoff(m.payoff_date)}</div><div class="sub">${m.total_interest_cents ? `${money(m.total_interest_cents, { whole: true })} interest to go` : ''}${m.splits_without_repayment ? ' · excludes splits with no set repayment' : ''}</div></div>
    </div>
    <div class="grid tiles">${m.splits.map((s) => `
      <section class="card split-card">
        <h2><span>${esc(s.name)}</span><button class="btn small" data-split="${s.id}">Edit</button></h2>
        <div class="status ${esc(s.fixed_status || 'good')}">${esc(statusText(s))}</div>
        <dl class="kv">
          <dt>Balance</dt><dd>${money(s.balance_cents, { whole: true })}</dd>
          <dt>Rate</dt><dd>${s.rate_pct.toFixed(2)}%</dd>
          <dt>Repayment</dt><dd>${money(s.repayment_cents)} ${esc((FREQ[s.frequency] || '').toLowerCase())}</dd>
          <dt>Interest / month</dt><dd>${money(s.monthly_interest_cents, { whole: true })}</dd>
          <dt>Paid off</dt><dd>${payoff(s.projection.paysOff ? s.projection.payoffDate : 'never')}</dd>
          ${s.account_name ? `<dt>Balance from</dt><dd class="small">${esc(s.account_name)} (bank feed)</dd>` : ''}
        </dl>
        ${s.notes ? `<p class="small muted">${esc(s.notes)}</p>` : ''}
      </section>`).join('')}
    </div>
    <section class="card">
      <h2>What if we paid extra?</h2>
      <div class="row">
        <label class="inline">Extra per month $<input id="extra" class="money-input" inputmode="decimal" value="${mortgageExtra ? (mortgageExtra / 100).toFixed(0) : ''}" placeholder="0"></label>
        <button class="btn" id="extra-go">Calculate</button>
      </div>
      ${mortgageExtra ? `<p>Paying an extra <strong>${money(mortgageExtra, { whole: true })}/month</strong> clears the mortgage by <strong>${payoff(m.payoff_date_with_extra)}</strong>
        instead of ${payoff(m.payoff_date)}${m.total_interest_cents && m.total_interest_with_extra_cents != null ? `, saving about <strong>${money(m.total_interest_cents - m.total_interest_with_extra_cents, { whole: true })}</strong> in interest` : ''}.
        <span class="muted small">Fixed splits usually limit extra repayments (often ~5% a year) — floating/revolving splits don't.</span></p>` : ''}
    </section>` : `<div class="card empty">Add each split of your home loan (e.g. a 1-year fixed, a 2-year fixed, a floating/revolving portion).
      ${m.loan_accounts.length ? 'Your synced loan accounts can be linked so balances update automatically.' : ''}</div>`}
    ${m.history.length > 1 ? `<section class="card"><h2>Loan balance over time</h2><div class="chart" id="balance-chart"></div></section>` : ''}
    <section class="card table-wrap">
      <h2>Interest and repayments by period</h2>
      <table><thead><tr><th>Period</th><th class="num">Repaid</th><th class="num">Interest charged</th></tr></thead>
      <tbody>${m.by_period.slice().reverse().map((r) => `<tr><td>${esc(r.label)}</td><td class="num">${r.repaid_cents ? money(r.repaid_cents) : '—'}</td><td class="num">${r.interest_cents ? money(r.interest_cents) : '—'}</td></tr>`).join('')}</tbody></table>
      <p class="muted small">Repaid = transactions coded “Mortgage Repayment”. Interest = interest debits on accounts marked as mortgage (from the bank feed).</p>
    </section>`;

  $('#add-split').addEventListener('click', () => editSplit({}, m.loan_accounts));
  for (const b of $$('[data-split]')) b.addEventListener('click', () => editSplit(m.splits.find((s) => s.id === Number(b.dataset.split)), m.loan_accounts));
  $('#extra-go')?.addEventListener('click', () => { mortgageExtra = Math.max(0, toCents($('#extra').value) || 0); route(); });
  $('#extra')?.addEventListener('keydown', (e) => { if (e.key === 'Enter') $('#extra-go').click(); });
  if (m.history.length > 1) lineChart($('#balance-chart'), m.history.map((h) => ({ x: h.date, y: h.balance_cents })));
}

async function editSplit(s, loanAccounts) {
  const fields = `
    <div class="form-grid">
      <label>Name<input name="name" required maxlength="60" value="${esc(s.name || '')}" placeholder="e.g. 2-year fixed"></label>
      <label>Lender<input name="lender" maxlength="40" value="${esc(s.lender || '')}" placeholder="ANZ / ASB"></label>
    </div>
    <label>Take balance from<select name="account_id"><option value="">Enter manually</option>${loanAccounts.map((a) => `<option value="${esc(a.id)}" ${a.id === s.account_id ? 'selected' : ''}>${esc([a.bank, a.name, a.number].filter(Boolean).join(' · '))} (${money(Math.abs(a.balance_cents || 0), { whole: true })})</option>`).join('')}</select></label>
    <label id="bal-label">Balance owing<input name="balance" inputmode="decimal" value="${s.account_id ? '' : centsToInput(s.balance_cents)}"></label>
    <div class="form-grid">
      <label>Interest rate %<input name="rate_pct" inputmode="decimal" required value="${s.rate_pct ?? ''}"></label>
      <label>Rate type<select name="rate_type">${['fixed', 'floating', 'revolving'].map((t) => `<option ${t === (s.rate_type || 'fixed') ? 'selected' : ''}>${t}</option>`).join('')}</select></label>
      <label>Fixed until<input name="fixed_until" type="date" value="${esc(s.fixed_until || '')}"></label>
      <label>Repayment<input name="repayment" inputmode="decimal" value="${centsToInput(s.repayment_cents)}"></label>
      <label>Every<select name="frequency">${Object.entries(FREQ).map(([k, l]) => `<option value="${k}" ${k === (s.frequency || 'fortnightly') ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
    </div>
    <label>Notes<textarea name="notes" rows="2" maxlength="500">${esc(s.notes || '')}</textarea></label>
    ${s.id ? '<button class="btn danger small" type="button" id="delete-split">Delete split</button>' : ''}`;

  const sync = (form) => {
    const acct = loanAccounts.find((a) => a.id === form.account_id.value);
    $('#bal-label').hidden = Boolean(acct);
    form.fixed_until.disabled = form.rate_type.value !== 'fixed';
  };
  const p = modal(s.id ? 'Edit loan split' : 'Add loan split', fields, { onInput: sync });
  const form = $('#dialog-form');
  sync(form);
  form.account_id.addEventListener('change', () => {
    // Prefill from the bank's loan details where Akahu provides them.
    const acct = loanAccounts.find((a) => a.id === form.account_id.value);
    const sg = acct?.suggested || {};
    if (sg.rate_pct != null && !form.rate_pct.value) form.rate_pct.value = sg.rate_pct;
    if (sg.rate_type && ['fixed', 'floating', 'revolving'].includes(sg.rate_type)) form.rate_type.value = sg.rate_type;
    if (sg.fixed_until && !form.fixed_until.value) form.fixed_until.value = sg.fixed_until;
    if (sg.repayment_cents != null && !form.repayment.value) form.repayment.value = centsToInput(sg.repayment_cents);
    if (sg.frequency && FREQ[sg.frequency]) form.frequency.value = sg.frequency;
    if (acct && !form.lender.value) form.lender.value = acct.bank || '';
    sync(form);
  });
  $('#delete-split')?.addEventListener('click', async () => {
    if (!confirm('Delete this split?')) return;
    $('#dialog').close('cancel');
    try { await api('DELETE', `/mortgage/splits/${s.id}`); route(); } catch (e) { toastError(e); }
  });
  const f = await p;
  if (!f) return;
  const body = {
    name: f.get('name'), lender: f.get('lender'), account_id: f.get('account_id') || null,
    balance_cents: f.get('account_id') ? null : toCents(f.get('balance')), rate_pct: Number(f.get('rate_pct')),
    rate_type: f.get('rate_type'), fixed_until: f.get('fixed_until') || null,
    repayment_cents: toCents(f.get('repayment')) || 0, frequency: f.get('frequency'), notes: f.get('notes'),
  };
  try {
    if (s.id) await api('PUT', `/mortgage/splits/${s.id}`, body); else await api('POST', '/mortgage/splits', body);
    route();
  } catch (e) { toastError(e); }
}

// Single-series line chart with crosshair + tooltip.
function lineChart(el, points) {
  const W = 800, H = 220, L = 64, R = 12, T = 12, B = 26;
  const ys = points.map((p) => p.y);
  let min = Math.min(...ys), max = Math.max(...ys);
  if (min === max) { min -= 100000; max += 100000; }
  const pad = (max - min) * 0.08;
  min = Math.max(0, min - pad); max += pad;
  const dayNum = (s) => Date.UTC(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10));
  const x0 = dayNum(points[0].x), x1 = dayNum(points[points.length - 1].x) || x0 + 1;
  const sx = (d) => L + ((dayNum(d) - x0) / Math.max(1, x1 - x0)) * (W - L - R);
  const sy = (v) => T + (1 - (v - min) / (max - min)) * (H - T - B);
  const ticks = [0, 1, 2, 3].map((i) => min + ((max - min) * i) / 3);
  const path = points.map((p, i) => `${i ? 'L' : 'M'}${sx(p.x).toFixed(1)},${sy(p.y).toFixed(1)}`).join('');
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Loan balance from ${fmtDate(points[0].x, { year: true })} to ${fmtDate(points[points.length - 1].x, { year: true })}">
    ${ticks.map((v) => `<line class="grid-line" x1="${L}" x2="${W - R}" y1="${sy(v)}" y2="${sy(v)}"/><text class="axis-label" x="${L - 6}" y="${sy(v) + 4}" text-anchor="end">${esc(money(v, { whole: true }).replace(/,\d{3}$/, 'k'))}</text>`).join('')}
    <text class="axis-label" x="${L}" y="${H - 6}">${fmtDate(points[0].x, { year: true })}</text>
    <text class="axis-label" x="${W - R}" y="${H - 6}" text-anchor="end">${fmtDate(points[points.length - 1].x, { year: true })}</text>
    <path class="line" d="${path}"/>
    <line class="cross" y1="${T}" y2="${H - B}" x1="-10" x2="-10" hidden/>
    <circle class="dot" r="5" cx="-10" cy="-10" hidden/>
    <rect x="${L}" y="0" width="${W - L - R}" height="${H}" fill="transparent" class="hit"/>
  </svg><div class="tooltip" hidden></div>`;
  const svg = $('svg', el), cross = $('.cross', el), dot = $('.dot', el), tip = $('.tooltip', el);
  const show = (evt) => {
    const rect = svg.getBoundingClientRect();
    const vx = ((evt.clientX - rect.left) / rect.width) * W;
    let best = points[0];
    for (const p of points) if (Math.abs(sx(p.x) - vx) < Math.abs(sx(best.x) - vx)) best = p;
    const cx = sx(best.x), cy = sy(best.y);
    cross.setAttribute('x1', cx); cross.setAttribute('x2', cx); cross.hidden = false;
    dot.setAttribute('cx', cx); dot.setAttribute('cy', cy); dot.hidden = false;
    tip.innerHTML = `<strong>${esc(money(best.y, { whole: true }))}</strong><br><span class="muted">${fmtDate(best.x, { year: true })}</span>`;
    tip.hidden = false;
    const px = (cx / W) * rect.width, py = (cy / H) * rect.height;
    tip.style.left = `${Math.min(px + 12, rect.width - tip.offsetWidth - 4)}px`;
    tip.style.top = `${Math.max(0, py - tip.offsetHeight - 10)}px`;
  };
  const hide = () => { cross.hidden = true; dot.hidden = true; tip.hidden = true; };
  svg.addEventListener('pointermove', show);
  svg.addEventListener('pointerleave', hide);
}

// =============================================================== SETTINGS

async function renderSettings() {
  const log = await api('GET', '/sync/log');
  const m = state.meta;
  view.innerHTML = `
    <h1>Settings</h1>
    <div class="grid cols">
      <div class="stack">
        <section class="card">
          <h2>Budget period</h2>
          <form id="period-form" class="row">
            <label class="inline">Each period starts on day <input name="day" type="number" min="1" max="28" value="${m.period_start_day}" class="money-input"></label>
            <button class="btn primary">Save</button>
          </form>
          <p class="muted small">Use 1 for calendar months, or your payday (e.g. 15) so each period runs pay-to-pay. Current period: ${esc(m.current_period.label)}.</p>
        </section>

        <section class="card table-wrap">
          <div class="row spread"><h2>Accounts</h2><button class="btn small" id="add-account">Add manual account</button></div>
          ${m.accounts.length ? `<table><thead><tr><th>Account</th><th class="num">Balance</th><th>In budget</th><th>Mortgage</th></tr></thead><tbody>
          ${m.accounts.map((a) => `<tr>
            <td><div class="desc">${esc(a.name)}</div><div class="sub">${esc([a.bank, a.number, a.type, a.source === 'akahu' ? 'bank feed' : 'manual'].filter(Boolean).join(' · '))}</div></td>
            <td class="num">${money(a.balance_cents)}${a.source === 'manual' ? `<br><button class="link small" data-bal="${esc(a.id)}">update</button>` : ''}</td>
            <td><input type="checkbox" data-acct="${esc(a.id)}" data-field="include_in_budget" ${a.include_in_budget ? 'checked' : ''} aria-label="Include ${esc(a.name)} in budget"></td>
            <td><input type="checkbox" data-acct="${esc(a.id)}" data-field="is_mortgage" ${a.is_mortgage ? 'checked' : ''} aria-label="${esc(a.name)} is a mortgage account"></td></tr>`).join('')}
          </tbody></table>
          <p class="muted small">“In budget” accounts count towards spending. Leave loans, KiwiSaver and investments out. Money moved into a “Mortgage” account is coded as a mortgage repayment.</p>`
          : '<p class="muted small">No accounts yet.</p>'}
        </section>

        <section class="card">
          <h2>Import a bank statement (CSV)</h2>
          <p class="muted small">For history from before the bank feed was connected, or if you're not using Akahu. In ANZ Internet Banking: account → Export → CSV. In ASB: account → Export → CSV (“Comma separated”). Rows already in the app are skipped.</p>
          <form id="import-form" class="stack">
            <div class="form-grid">
              <label>Account<select name="account" required>${accountOptions(null)}</select></label>
              <label>File<input type="file" name="file" accept=".csv,text/csv" required></label>
            </div>
            <label class="inline"><input type="checkbox" name="invert"> Amounts are the wrong way round (purchases show as positive)</label>
            <div class="row"><button class="btn" name="mode" value="preview">Preview</button><button class="btn primary" name="mode" value="import">Import</button></div>
            <div id="import-result" class="small"></div>
          </form>
        </section>

        <section class="card table-wrap">
          <div class="row spread"><h2>Categories</h2><button class="btn small" id="add-cat">Add category</button></div>
          <table><tbody>${cats().filter((c) => c.kind !== 'transfer').map((c) => `<tr class="${c.archived ? 'muted' : ''}">
            <td>${esc(c.name)} <span class="sub">${esc(c.group_name)} · ${c.kind}${c.archived ? ' · archived' : ''}</span></td>
            <td class="num"><button class="btn small" data-cat="${c.id}">Edit</button></td></tr>`).join('')}</tbody></table>
        </section>
      </div>

      <div class="stack">
        <section class="card">
          <h2>Bank feed</h2>
          <p class="small">${m.bank_connected ? 'Connected via Akahu (read-only). Syncs automatically in the background.' : 'Not connected. Set AKAHU_APP_TOKEN and AKAHU_USER_TOKEN on the server — see the README.'}</p>
          <table class="small"><tbody>${log.items.slice(0, 8).map((l) => `<tr><td>${esc(ago(l.started_at))}</td>
            <td><span class="status ${l.status === 'ok' ? 'good' : l.status === 'error' ? 'critical' : 'warning'}">${esc(l.status)}</span></td>
            <td>${l.status === 'ok' ? `${l.new_count} new` : esc(l.message || '')}</td></tr>`).join('') || '<tr><td class="muted">No syncs yet</td></tr>'}</tbody></table>
        </section>
        <section class="card">
          <h2>Security</h2>
          <form id="pw-form" class="stack">
            <label>Current password<input type="password" name="current" autocomplete="current-password" required></label>
            <label>New password (12+ characters)<input type="password" name="next" autocomplete="new-password" minlength="12" required></label>
            <button class="btn">Change password</button>
          </form>
          <p class="muted small">Signing in needs your password and an authenticator code. You're signed out after 60 minutes idle.</p>
          <button class="btn" id="logout">Sign out</button>
        </section>
      </div>
    </div>`;

  $('#period-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      await api('PUT', '/settings', { period_start_day: Number(e.target.day.value) });
      state.period = null; await loadMeta(); toast('Saved.'); route();
    } catch (err) { toastError(err); }
  });
  for (const cb of $$('[data-acct]')) {
    cb.addEventListener('change', async () => {
      try { await api('PATCH', `/accounts/${encodeURIComponent(cb.dataset.acct)}`, { [cb.dataset.field]: cb.checked }); await loadMeta(); toast('Saved — transactions re-coded.'); } catch (e) { toastError(e); }
    });
  }
  for (const b of $$('[data-bal]')) {
    b.addEventListener('click', async () => {
      const a = accountById(b.dataset.bal);
      const f = await modal(`Balance of ${a.name}`, `<label>Current balance (negative for money owed)<input name="bal" inputmode="decimal" value="${centsToInput(a.balance_cents)}"></label>`);
      if (!f) return;
      try { await api('PATCH', `/accounts/${encodeURIComponent(a.id)}`, { balance_cents: toCents(f.get('bal')) }); await loadMeta(); route(); } catch (e) { toastError(e); }
    });
  }
  $('#add-account').addEventListener('click', async () => {
    const f = await modal('Add manual account', `
      <label>Name<input name="name" required maxlength="80"></label>
      <div class="form-grid"><label>Bank<input name="bank" maxlength="40" placeholder="ANZ / ASB"></label>
      <label>Type<select name="type"><option value="CHECKING">Everyday</option><option value="SAVINGS">Savings</option><option value="CREDITCARD">Credit card</option><option value="LOAN">Loan / mortgage</option></select></label></div>`, { submit: 'Add' });
    if (!f) return;
    try { await api('POST', '/accounts', Object.fromEntries(f)); await loadMeta(); route(); } catch (e) { toastError(e); }
  });
  $('#import-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const form = e.target;
    const file = form.file.files[0];
    const out = $('#import-result');
    if (!file) return;
    if (file.size > 5 * 1024 * 1024) { out.textContent = 'That file is over 5 MB.'; return; }
    const preview = e.submitter?.value === 'preview';
    const qs = new URLSearchParams({ account: form.account.value, invert: form.invert.checked ? '1' : '0', preview: preview ? '1' : '0' });
    try {
      const r = await api('POST', `/import?${qs}`, await file.text(), { raw: true, contentType: 'text/csv' });
      if (preview) {
        out.innerHTML = `Found <strong>${r.rows}</strong> transactions${r.skipped ? ` (${r.skipped} lines skipped)` : ''}. First few:
          <table>${r.sample.map((t) => `<tr><td>${fmtDate(t.date, { year: true })}</td><td>${esc(t.description)}</td><td class="num">${signedMoney(t.amount_cents)}</td></tr>`).join('')}</table>`;
      } else {
        out.innerHTML = `Imported <strong>${r.added}</strong> new transactions; ${r.duplicates} already here were skipped.`;
        await loadMeta();
      }
    } catch (err) { out.textContent = err.message; }
  });
  $('#add-cat').addEventListener('click', async () => {
    const groups = [...new Set(cats().map((c) => c.group_name))];
    const f = await modal('Add category', `
      <label>Name<input name="name" required maxlength="60"></label>
      <div class="form-grid"><label>Group<input name="group_name" list="groups" required maxlength="60"><datalist id="groups">${groups.map((g) => `<option value="${esc(g)}">`).join('')}</datalist></label>
      <label>Kind<select name="kind"><option value="expense">Spending</option><option value="income">Income</option></select></label></div>`, { submit: 'Add' });
    if (!f) return;
    try { await api('POST', '/categories', Object.fromEntries(f)); await loadMeta(); route(); } catch (e) { toastError(e); }
  });
  for (const b of $$('[data-cat]')) {
    b.addEventListener('click', async () => {
      const c = catById(Number(b.dataset.cat));
      const f = await modal('Edit category', `
        <label>Name<input name="name" required maxlength="60" value="${esc(c.name)}"></label>
        <label>Group<input name="group_name" required maxlength="60" value="${esc(c.group_name)}"></label>
        <label class="inline"><input type="checkbox" name="archived" ${c.archived ? 'checked' : ''}> Archived (hidden from menus; history kept)</label>`);
      if (!f) return;
      try { await api('PUT', `/categories/${c.id}`, { name: f.get('name'), group_name: f.get('group_name'), archived: f.get('archived') === 'on' }); await loadMeta(); route(); } catch (e) { toastError(e); }
    });
  }
  $('#pw-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    try { await api('POST', '/password', { current: e.target.current.value, next: e.target.next.value }); toast('Password changed — please sign in again.'); showLogin(); } catch (err) { toastError(err); }
  });
  $('#logout').addEventListener('click', async () => { try { await api('POST', '/logout', {}); } catch { /* ignore */ } showLogin(); });
}
