// Production admin: trigger a backorder-demand sync and split a target
// into batches. Reuses the main dashboard's Supabase session
// (sessionStorage.access_token, same origin) rather than having its own
// login form -- see index.html's comment. No PIN/MFA handling here: if
// the token is missing or the API returns 401, this page just points
// back at the dashboard to sign in again.

const viewSignedOut = document.getElementById('view-signed-out');
const viewAdmin = document.getElementById('view-admin');
const adminError = document.getElementById('admin-error');
const adminSuccess = document.getElementById('admin-success');

function accessToken() {
  return sessionStorage.getItem('access_token');
}

function showSignedOut() {
  viewSignedOut.classList.remove('hidden');
  viewAdmin.classList.add('hidden');
}

async function api(path, options = {}) {
  const token = accessToken();
  if (!token) {
    showSignedOut();
    throw new Error('Not signed in');
  }
  const res = await fetch(path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
      ...(options.headers || {}),
    },
  });
  if (res.status === 401) {
    showSignedOut();
    throw new Error('Session expired');
  }
  const contentType = res.headers.get('content-type') || '';
  const body = contentType.includes('application/json') ? await res.json() : null;
  if (!res.ok) {
    throw new Error(body?.error || `Request failed (${res.status})`);
  }
  return body;
}

function setError(msg) {
  adminError.textContent = msg || '';
}
function setSuccess(msg) {
  adminSuccess.textContent = msg || '';
  if (msg) setTimeout(() => { if (adminSuccess.textContent === msg) adminSuccess.textContent = ''; }, 6000);
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}
function fmtDate(v) {
  if (!v) return '—';
  return new Date(v).toLocaleString('en-NZ', { dateStyle: 'medium', timeStyle: 'short' });
}

// -- Targets -----------------------------------------------------------

async function loadTargets() {
  const tbody = document.getElementById('targets-tbody');
  const empty = document.getElementById('targets-empty');
  try {
    const { targets } = await api('/production/targets');
    tbody.innerHTML = '';
    empty.hidden = targets.length > 0;
    for (const t of targets) {
      const tr = document.createElement('tr');
      if (t.status === 'closed') tr.className = 'closed-row';
      tr.innerHTML = `
        <td>${escapeHtml(t.sku)}</td>
        <td><span class="status-pill ${t.status}">${escapeHtml(t.status)}</span></td>
        <td>${escapeHtml(t.outstanding_qty)}</td>
        <td>${escapeHtml(t.cin7_assembly_id || '—')}</td>
        <td class="narrow"></td>
      `;
      if (t.status === 'active') {
        const cell = tr.querySelector('td.narrow');
        const input = document.createElement('input');
        input.type = 'number';
        input.min = '1';
        input.placeholder = 'run size';
        input.style.width = '80px';
        input.style.display = 'inline-block';
        const btn = document.createElement('button');
        btn.className = 'btn-link';
        btn.textContent = 'Plan batches';
        btn.addEventListener('click', () => planBatches(t.id, t.sku, input.value));
        cell.appendChild(input);
        cell.appendChild(document.createTextNode(' '));
        cell.appendChild(btn);
      }
      tbody.appendChild(tr);
    }
  } catch (err) {
    if (err.message !== 'Not signed in' && err.message !== 'Session expired') setError(err.message);
  }
}

async function planBatches(targetId, sku, runSizeStr) {
  const runSize = Number(runSizeStr);
  if (!runSize || runSize <= 0) {
    setError('Enter a positive suggested run size before planning batches.');
    return;
  }
  setError('');
  try {
    const data = await api(`/production/targets/${targetId}/plan-batches`, {
      method: 'POST',
      body: JSON.stringify({ suggested_run_size: runSize }),
    });
    setSuccess(`Planned ${data.batches.length} batch(es) for ${sku}.`);
    await loadBatches();
  } catch (err) {
    setError(err.message);
  }
}

// -- Sync demand ---------------------------------------------------------

function addDemandRow(values = {}) {
  const tbody = document.getElementById('demand-tbody');
  const tr = document.createElement('tr');
  tr.innerHTML = `
    <td><input type="text" class="d-sku" placeholder="FG-2400-BOX" value="${escapeHtml(values.sku || '')}"></td>
    <td><input type="text" class="d-so" placeholder="SO-1001" value="${escapeHtml(values.so_number || '')}"></td>
    <td><input type="date" class="d-date" value="${escapeHtml(values.order_date || '')}"></td>
    <td><input type="number" class="d-qty" min="0" step="any" value="${escapeHtml(values.qty ?? '')}"></td>
    <td class="narrow"><button class="link-danger">Remove</button></td>
  `;
  tr.querySelector('.link-danger').addEventListener('click', () => tr.remove());
  tbody.appendChild(tr);
}

function collectDemandRows() {
  const rows = [...document.querySelectorAll('#demand-tbody tr')];
  const demandBySku = {};
  for (const row of rows) {
    const sku = row.querySelector('.d-sku').value.trim();
    const soNumber = row.querySelector('.d-so').value.trim();
    const orderDate = row.querySelector('.d-date').value || null;
    const qty = Number(row.querySelector('.d-qty').value);
    if (!sku || !soNumber || !qty || qty <= 0) continue;
    (demandBySku[sku] ||= []).push({ so_number: soNumber, order_date: orderDate, qty_backordered: qty });
  }
  return demandBySku;
}

async function syncDemand() {
  const demandBySku = collectDemandRows();
  const resultEl = document.getElementById('sync-result');
  if (Object.keys(demandBySku).length === 0) {
    setError('Add at least one complete row (SKU, SO number, quantity) before syncing.');
    return;
  }
  setError('');
  try {
    const data = await api('/production/targets/sync', {
      method: 'POST',
      body: JSON.stringify({ demand_lines_by_sku: demandBySku }),
    });
    resultEl.hidden = false;
    resultEl.textContent = JSON.stringify(data.actions, null, 2);
    setSuccess(`Sync applied ${data.actions.length} target action(s).`);
    await loadTargets();
  } catch (err) {
    setError(err.message);
  }
}

// -- Batches -------------------------------------------------------------

async function loadBatches() {
  const tbody = document.getElementById('batches-tbody');
  const empty = document.getElementById('batches-empty');
  try {
    const { batches } = await api('/production/batches');
    tbody.innerHTML = '';
    empty.hidden = batches.length > 0;
    for (const b of batches) {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td><span class="batch-code">${escapeHtml(b.batch_code)}</span></td>
        <td>${escapeHtml(b.sku)}</td>
        <td>${escapeHtml(b.priority_rank)}</td>
        <td>${escapeHtml(b.qty_planned)}</td>
        <td><span class="status-pill ${b.status}">${escapeHtml(b.status)}</span></td>
        <td>${b.actual_qty != null ? escapeHtml(b.actual_qty) + (b.reject_qty ? ` (${escapeHtml(b.reject_qty)} rej.)` : '') : '—'}</td>
        <td>${b.reported_at ? `${fmtDate(b.reported_at)} (${escapeHtml(b.reported_via)})` : '—'}</td>
      `;
      tbody.appendChild(tr);
    }
  } catch (err) {
    if (err.message !== 'Not signed in' && err.message !== 'Session expired') setError(err.message);
  }
}

// -- Wire up -------------------------------------------------------------

document.getElementById('refresh-targets-btn').addEventListener('click', loadTargets);
document.getElementById('refresh-batches-btn').addEventListener('click', loadBatches);
document.getElementById('add-demand-row-btn').addEventListener('click', () => addDemandRow());
document.getElementById('sync-demand-btn').addEventListener('click', syncDemand);

(function init() {
  if (!accessToken()) {
    showSignedOut();
    return;
  }
  viewSignedOut.classList.add('hidden');
  viewAdmin.classList.remove('hidden');
  addDemandRow();
  addDemandRow();
  addDemandRow();
  loadTargets();
  loadBatches();
})();
