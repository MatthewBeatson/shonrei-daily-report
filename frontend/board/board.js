// Dispatch Board (TV). Polls /board/api/summary every minute, pages the
// plan list if it doesn't fit, and reloads itself every few hours so a
// TV left on for weeks still picks up new deploys. No $ figures come
// through this API at all -- see backend/src/routes/board.js.

const CODE_KEY = 'dispatchBoardCode';
const POLL_MS = 60 * 1000;
const PAGE_MS = 12 * 1000;
const RELOAD_MS = 6 * 60 * 60 * 1000;

const $ = (id) => document.getElementById(id);
const fmt = (n) => Math.round(Number(n) || 0).toLocaleString('en-NZ');

function readCode() {
  try { return localStorage.getItem(CODE_KEY) || ''; } catch (e) { return ''; }
}
function saveCode(code) {
  try { localStorage.setItem(CODE_KEY, code); } catch (e) { /* private mode: code lasts this session only */ }
}
function clearCode() {
  try { localStorage.removeItem(CODE_KEY); } catch (e) { /* ignore */ }
}

// Setting up a TV with a remote is painful -- /board/?code=XXXX once stores
// the code and strips it from the address bar.
let code = readCode();
const urlCode = new URLSearchParams(location.search).get('code');
if (urlCode) {
  code = urlCode;
  saveCode(code);
  history.replaceState(null, '', location.pathname);
}

async function api(path) {
  const res = await fetch(`/board/api${path}`, { headers: { 'X-Board-Code': code } });
  if (res.status === 401) {
    const err = new Error('bad code');
    err.unauthorized = true;
    throw err;
  }
  if (!res.ok) throw new Error(`Request failed (${res.status})`);
  return res.json();
}

function showGate(message) {
  $('board').classList.add('hidden');
  $('gate').classList.remove('hidden');
  $('gate-error').textContent = message || '';
  $('gate-code').focus();
}

$('gate-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  code = $('gate-code').value.trim();
  try {
    await api('/check');
    saveCode(code);
    $('gate').classList.add('hidden');
    start();
  } catch (err) {
    showGate(err.unauthorized ? 'That code isn\'t right.' : 'Can\'t reach the server -- try again.');
  }
});

// -- Dates ----------------------------------------------------------------

function parseDate(iso) {
  // Plain YYYY-MM-DD from Postgres -- build it as a local date so it never
  // shifts a day across the UTC boundary.
  const [y, m, d] = String(iso).slice(0, 10).split('-').map(Number);
  return new Date(y, m - 1, d);
}
function dayLabel(iso) {
  return parseDate(iso).toLocaleDateString('en-NZ', { weekday: 'short', day: 'numeric' });
}
function rangeLabel(a, b) {
  const s = parseDate(a);
  const e = parseDate(b);
  e.setDate(e.getDate() - 2); // Mon-Fri, not Mon-Sun
  const opts = { day: 'numeric', month: 'short' };
  return `Week of ${s.toLocaleDateString('en-NZ', opts)} – ${e.toLocaleDateString('en-NZ', opts)}`;
}

function tickClock() {
  const now = new Date();
  $('clock-time').textContent = now.toLocaleTimeString('en-NZ', { hour: 'numeric', minute: '2-digit' });
  $('clock-date').textContent = now.toLocaleDateString('en-NZ', { weekday: 'long', day: 'numeric', month: 'long' });
}

// -- Rendering ------------------------------------------------------------

let today = null;

// One entry per list pane; each pages on its own if it doesn't fit.
const lists = {
  plan: { el: $('pane-plan'), rows: [], page: 0, empty: 'No SOs planned for this week yet.<br>The plan rebuilds every Monday morning.' },
  plain: { el: $('pane-plain'), rows: [], page: 0, empty: 'No plain packaging orders to go.' },
  printed: { el: $('pane-printed'), rows: [], page: 0, empty: 'No printed packaging orders to go.' },
};

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function isoDay(d) { return String(d).slice(0, 10); }

function tomorrowIso() {
  const t = parseDate(today);
  t.setDate(t.getDate() + 1);
  return `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, '0')}-${String(t.getDate()).padStart(2, '0')}`;
}

function rowHtml(o) {
  const cls = ['row'];
  const due = isoDay(o.due);
  if (o.picked) cls.push('picked');
  else if (o.late) cls.push('late');
  else if (due === today) cls.push('today');
  const ref = o.group_label || o.reference || '';
  let dueHtml;
  if (o.late) dueHtml = `LATE<small>${esc(dayLabel(due))}</small>`;
  else if (due === today) dueHtml = 'TODAY';
  else if (due === tomorrowIso()) dueHtml = 'Tomorrow';
  else dueHtml = esc(dayLabel(due));
  return `<div class="${cls.join(' ')}">
    <div class="mark">${o.picked ? '✓' : (o.late ? '!' : '')}</div>
    <div class="so">${esc(String(o.order_number).replace(/^SO-/, ''))}</div>
    <div class="who"><div class="name">${esc(o.customer)}</div>${ref ? `<div class="ref">${esc(ref)}</div>` : ''}</div>
    <div class="due">${dueHtml}</div>
    <div class="units">${o.units_remaining === null ? '–' : fmt(o.units_remaining)}</div>
  </div>`;
}

function renderList(list) {
  const body = list.el.querySelector('[data-body]');
  const pager = list.el.querySelector('[data-pager]');
  const open = list.rows.filter((o) => !o.picked);
  const openUnits = open.reduce((sum, o) => sum + (Number(o.units_remaining) || 0), 0);
  list.el.querySelector('[data-sub]').innerHTML = `<b>${fmt(open.length)}</b> SOs · <b>${fmt(openUnits)}</b> units`;

  if (!list.rows.length) {
    body.innerHTML = `<div class="empty">${list.empty}</div>`;
    pager.innerHTML = '';
    return;
  }
  const per = Math.max(1, Math.floor(body.clientHeight / (window.innerHeight * 0.066)));
  const pages = Math.ceil(list.rows.length / per);
  if (list.page >= pages) list.page = 0;
  body.innerHTML = list.rows.slice(list.page * per, list.page * per + per).map(rowHtml).join('');
  pager.innerHTML = pages > 1 ? Array.from({ length: pages }, (_, i) => `<i class="${i === list.page ? 'on' : ''}"></i>`).join('') : '';
}

function renderAll() {
  Object.values(lists).forEach(renderList);
}

function nextPages() {
  Object.values(lists).forEach((l) => { l.page += 1; });
  renderAll();
}

function renderKpis(k, data) {
  $('week-label').textContent = rangeLabel(data.week_start, data.week_end);

  const planned = k.planned_week;
  $('k-picked-week').textContent = fmt(k.picked_week);
  $('k-picked-week-sub').innerHTML = planned
    ? `<b>${fmt(k.planned_week_picked)}</b> of <b>${fmt(planned)}</b> due this week`
    : 'nothing due this week';
  $('k-picked-bar').style.width = `${planned ? Math.min(100, (k.planned_week_picked / planned) * 100) : 0}%`;

  const max = Math.max(1, ...k.picks_by_day);
  const todayDow = parseDate(today).getDay(); // 0 = Sun
  $('k-days').innerHTML = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'].map((d, i) => {
    const n = k.picks_by_day[i];
    const isNow = todayDow === i + 1 || (i === 4 && (todayDow === 6 || todayDow === 0));
    return `<div class="day ${isNow ? 'now' : ''}"><em>${n || ''}</em><div class="col" style="height:${(n / max) * 70}%"></div><span>${d}</span></div>`;
  }).join('');

  $('k-picked-today').textContent = fmt(k.picked_today);

  $('k-late').textContent = fmt(k.late_sos);
  $('t-late').classList.toggle('bad', k.late_sos > 0);
  $('t-late').classList.toggle('good', k.late_sos === 0);
  $('k-late-sub').textContent = k.late_sos > 0 ? 'past due, not picked' : 'all on track';

  $('k-togo-week').textContent = fmt(k.togo_week_sos);
  $('k-togo-week-sub').innerHTML = `SOs · <b>${fmt(k.togo_week_units)}</b> units`;
  $('k-togo-month').textContent = fmt(k.togo_month_sos);
  $('k-togo-month-sub').innerHTML = `SOs · <b>${fmt(k.togo_month_units)}</b> units`;

  $('k-produced').textContent = k.produced_week_units === null ? '–' : fmt(k.produced_week_units);
  $('k-held').textContent = fmt(k.held_sos);

  $('next-week').innerHTML = `Next week: <b>${fmt(k.next_week_sos)}</b> SOs · <b>${fmt(k.next_week_units)}</b> units`;
}

let lastOk = null;

async function refresh() {
  try {
    const data = await api('/summary');
    today = isoDay(data.today);
    Object.entries(lists).forEach(([name, l]) => { l.rows = data.orders.filter((o) => o.list === name); });
    renderKpis(data.kpis, data);
    renderAll();
    lastOk = new Date();
    const gen = data.plan_generated_at ? new Date(data.plan_generated_at) : null;
    const pkg = data.packaging_refreshed_at ? new Date(data.packaging_refreshed_at) : null;
    $('status').className = 'status';
    $('status').textContent = `Updated ${lastOk.toLocaleTimeString('en-NZ', { hour: 'numeric', minute: '2-digit' })}`
      + (gen ? ` · plan built ${gen.toLocaleDateString('en-NZ', { weekday: 'short', day: 'numeric', month: 'short' })}` : '')
      + (pkg ? ` · packaging from ${pkg.toLocaleTimeString('en-NZ', { hour: 'numeric', minute: '2-digit' })}` : '');
  } catch (err) {
    if (err.unauthorized) {
      clearCode();
      stop();
      showGate('The board access code has changed -- enter the new one.');
      return;
    }
    $('status').className = 'status warn';
    $('status').textContent = `Can't reach the server${lastOk ? ` -- showing figures from ${lastOk.toLocaleTimeString('en-NZ', { hour: 'numeric', minute: '2-digit' })}` : ''}`;
  }
}

let timers = [];
function stop() {
  timers.forEach(clearInterval);
  timers = [];
}
function start() {
  $('board').classList.remove('hidden');
  tickClock();
  refresh();
  timers.push(setInterval(tickClock, 1000));
  timers.push(setInterval(refresh, POLL_MS));
  timers.push(setInterval(nextPages, PAGE_MS));
}

window.addEventListener('resize', renderAll);
setTimeout(() => location.reload(), RELOAD_MS);

if (code) start(); else showGate();
