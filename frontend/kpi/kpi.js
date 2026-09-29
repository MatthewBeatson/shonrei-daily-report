// Staff KPI entry -- "SO picking done" (green sheets). Same access code
// and API as the TV board (/board/api), no management login, no $.

const CODE_KEY = 'dispatchBoardCode';
const PICKER_KEY = 'dispatchKpiPicker';
const ADD_NEW = '__add__';
const TYPE_SO = '__type__';

const $ = (id) => document.getElementById(id);

function store(key, value) {
  try {
    if (value === null) localStorage.removeItem(key); else localStorage.setItem(key, value);
  } catch (e) { /* private mode -- just won't remember */ }
}
function recall(key) {
  try { return localStorage.getItem(key) || ''; } catch (e) { return ''; }
}

let code = recall(CODE_KEY);

async function api(path, options = {}) {
  const res = await fetch(`/board/api${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', 'X-Board-Code': code, ...(options.headers || {}) },
  });
  if (res.status === 401) {
    store(CODE_KEY, null);
    showGate('Enter the board access code.');
    throw new Error('Access code needed');
  }
  const body = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) throw new Error(body?.error || `Request failed (${res.status})`);
  return body;
}

function showGate(message) {
  $('page').classList.add('hidden');
  $('gate').classList.remove('hidden');
  $('gate-error').textContent = message || '';
}

$('gate-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  code = $('gate-code').value.trim();
  try {
    await api('/check');
    store(CODE_KEY, code);
    $('gate').classList.add('hidden');
    start();
  } catch (err) {
    $('gate-error').textContent = err.message === 'Access code needed' ? 'That code isn\'t right.' : err.message;
  }
});

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function dateLabel(iso) {
  if (!iso) return '';
  const [y, m, d] = String(iso).slice(0, 10).split('-').map(Number);
  return new Date(y, m - 1, d).toLocaleDateString('en-NZ', { weekday: 'short', day: 'numeric', month: 'short' });
}

// -- Picker names ---------------------------------------------------------

async function loadStaff(selectName) {
  const { staff } = await api('/staff');
  const wanted = selectName || recall(PICKER_KEY);
  $('picker').innerHTML = '<option value="">Choose your name…</option>'
    + staff.map((n) => `<option ${n === wanted ? 'selected' : ''}>${esc(n)}</option>`).join('')
    + `<option value="${ADD_NEW}">+ Add my name</option>`;
  $('add-name').classList.add('hidden');
}

$('picker').addEventListener('change', () => {
  const adding = $('picker').value === ADD_NEW;
  $('add-name').classList.toggle('hidden', !adding);
  if (adding) $('new-name').focus();
  else store(PICKER_KEY, $('picker').value);
});

$('add-name-btn').addEventListener('click', async () => {
  const name = $('new-name').value.trim();
  if (!name) return;
  try {
    const res = await api('/staff', { method: 'POST', body: JSON.stringify({ name }) });
    store(PICKER_KEY, res.name);
    $('new-name').value = '';
    await loadStaff(res.name);
  } catch (err) {
    setMsg(err.message, false);
  }
});

// -- SOs ------------------------------------------------------------------

async function loadOrders() {
  const { open, recent } = await api('/orders');
  const current = $('so').value;
  $('so').innerHTML = '<option value="">Choose SO…</option>'
    + open.map((o) => {
      const kind = o.list === 'plain' ? ' · packaging' : (o.list === 'printed' ? ' · printed packaging' : '');
      const label = `${o.order_number.replace(/^SO-/, '')} · ${o.customer || ''}${kind}${o.due ? ` · due ${dateLabel(o.due)}` : ''}`;
      return `<option value="${esc(o.order_number)}" ${o.order_number === current ? 'selected' : ''}>${esc(label)}</option>`;
    }).join('')
    + `<option value="${TYPE_SO}">SO not listed – type it in</option>`;
  $('so-typed').classList.toggle('hidden', $('so').value !== TYPE_SO);

  $('recent').innerHTML = recent.length
    ? recent.map((r) => `<li>
        <div><div class="so">${esc(r.order_number.replace(/^SO-/, ''))} ${r.customer ? `· ${esc(r.customer)}` : ''}</div>
        <div class="meta">${esc(r.picked_by || 'Unknown')} · ${new Date(r.picked_at).toLocaleString('en-NZ', { weekday: 'short', hour: 'numeric', minute: '2-digit' })}</div></div>
        <button type="button" data-undo="${esc(r.order_number)}">Undo</button>
      </li>`).join('')
    : '<li class="meta">Nothing picked yet.</li>';
}

$('so').addEventListener('change', () => {
  const typing = $('so').value === TYPE_SO;
  $('so-typed').classList.toggle('hidden', !typing);
  if (typing) $('so-typed').focus();
});

$('recent').addEventListener('click', async (e) => {
  const so = e.target.dataset?.undo;
  if (!so) return;
  if (!confirm(`Undo "picking done" for ${so}?`)) return;
  try {
    await api(`/picks/${encodeURIComponent(so)}`, { method: 'DELETE' });
    setMsg(`${so} set back to not picked.`, true);
    await loadOrders();
  } catch (err) {
    setMsg(err.message, false);
  }
});

function setMsg(text, ok) {
  $('pick-msg').textContent = text;
  $('pick-msg').className = `msg ${ok ? 'ok' : 'bad'}`;
}

$('pick-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const picker = $('picker').value;
  const so = $('so').value === TYPE_SO ? $('so-typed').value.trim() : $('so').value;
  if (!picker || picker === ADD_NEW) return setMsg('Choose your name first.', false);
  if (!so) return setMsg('Choose the SO.', false);

  $('pick-btn').disabled = true;
  try {
    const { pick } = await api('/picks', { method: 'POST', body: JSON.stringify({ order_number: so, picked_by: picker }) });
    setMsg(`✓ ${pick.order_number} marked as picked.`, true);
    $('so').value = '';
    $('so-typed').value = '';
    await loadOrders();
  } catch (err) {
    setMsg(err.message, false);
  } finally {
    $('pick-btn').disabled = false;
  }
});

async function start() {
  $('page').classList.remove('hidden');
  try {
    await Promise.all([loadStaff(), loadOrders()]);
  } catch (err) {
    setMsg(err.message, false);
  }
}

if (code) start(); else showGate();
