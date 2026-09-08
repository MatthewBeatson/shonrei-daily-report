// Production admin: trigger a backorder-demand sync and split a target
// into batches. Reuses the main dashboard's Supabase session rather than
// having its own login form -- see index.html's comment. No PIN/MFA
// handling here: if the token is missing or the API returns 401, this
// page just points back at the dashboard to sign in again.
//
// This app now lives on its own domain (production.shonrei.co.nz, once
// configured), a different origin from the dashboard, so it can't just
// read the dashboard's sessionStorage directly -- the dashboard instead
// hands the token over once via a #token=... URL fragment when the nav
// link is clicked (see frontend/app.js). importTokenFromUrlFragment()
// picks that up on load, stores it in *this* origin's sessionStorage,
// and scrubs it from the address bar immediately so it doesn't linger
// in browser history.

const viewSignedOut = document.getElementById('view-signed-out');
const viewAdmin = document.getElementById('view-admin');
const adminError = document.getElementById('admin-error');
const adminSuccess = document.getElementById('admin-success');

function accessToken() {
  return sessionStorage.getItem('access_token');
}

function importTokenFromUrlFragment() {
  const match = /(?:^|&)token=([^&]+)/.exec(window.location.hash.slice(1));
  if (!match) return;
  sessionStorage.setItem('access_token', decodeURIComponent(match[1]));
  history.replaceState(null, '', window.location.pathname + window.location.search);
}

function wireDashboardLink() {
  const url = window.__APP_CONFIG__?.DASHBOARD_URL;
  if (!url) return;
  for (const id of ['dashboard-link', 'signin-dashboard-link']) {
    const link = document.getElementById(id);
    if (link) link.href = url;
  }
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

// Fetches a ZPL label file and triggers a normal browser download --
// whoever's at the printer sends the .zpl file to it via Zebra's own
// tool/driver (see production/README.md -- this app doesn't push labels
// to the printer over the network itself).
async function downloadLabel(path, suggestedFilename) {
  const token = accessToken();
  if (!token) { showSignedOut(); return; }
  try {
    const res = await fetch(path, { headers: { Authorization: `Bearer ${token}` } });
    if (res.status === 401) { showSignedOut(); return; }
    if (!res.ok) {
      const body = (res.headers.get('content-type') || '').includes('application/json') ? await res.json() : null;
      throw new Error(body?.error || `Couldn't fetch label (${res.status})`);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = suggestedFilename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (err) {
    setError(err.message);
  }
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
        <td class="narrow"></td>
      `;
      const labelBtn = document.createElement('button');
      labelBtn.className = 'btn-link';
      labelBtn.textContent = 'Print';
      labelBtn.addEventListener('click', () => downloadLabel(`/production/labels/batch/${b.id}`, `batch-${b.batch_code}.zpl`));
      tr.querySelector('td.narrow').appendChild(labelBtn);
      tbody.appendChild(tr);
    }
  } catch (err) {
    if (err.message !== 'Not signed in' && err.message !== 'Session expired') setError(err.message);
  }
}

// -- Stocktake -------------------------------------------------------------

async function loadStocktake() {
  const tbody = document.getElementById('stocktake-tbody');
  const empty = document.getElementById('stocktake-empty');
  try {
    const { counts } = await api('/production/stocktake/counts');
    tbody.innerHTML = '';
    empty.hidden = counts.length > 0;
    for (const c of counts) {
      const tr = document.createElement('tr');
      const varianceText = c.variance == null ? '—' : (c.variance > 0 ? `+${c.variance}` : c.variance);
      tr.innerHTML = `
        <td>${escapeHtml(c.sku)}</td>
        <td>${escapeHtml(c.location || '—')}</td>
        <td>${escapeHtml(c.counted_qty)}</td>
        <td>${escapeHtml(c.cin7_on_hand_snapshot ?? '—')}</td>
        <td>${escapeHtml(varianceText)}</td>
        <td><span class="status-pill ${c.status}">${escapeHtml(c.status)}</span></td>
        <td>${fmtDate(c.counted_at)} (${escapeHtml(c.reported_via)})</td>
        <td class="narrow"></td>
      `;
      if (c.status === 'recorded') {
        const btn = document.createElement('button');
        btn.className = 'btn-link';
        btn.textContent = 'Push adjustment';
        btn.addEventListener('click', () => pushAdjustment(c.id, c.sku));
        tr.querySelector('td.narrow').appendChild(btn);
      } else if (c.cin7_adjustment_id) {
        tr.querySelector('td.narrow').textContent = c.cin7_adjustment_id;
      }
      tbody.appendChild(tr);
    }
  } catch (err) {
    if (err.message !== 'Not signed in' && err.message !== 'Session expired') setError(err.message);
  }
}

async function pushAdjustment(countId, sku) {
  setError('');
  try {
    const data = await api(`/production/stocktake/counts/${countId}/adjust`, { method: 'POST', body: JSON.stringify({}) });
    setSuccess(`Pushed adjustment for ${sku} (Cin7 id: ${data.cin7_adjustment_id}).`);
    await loadStocktake();
  } catch (err) {
    setError(err.message);
  }
}

// -- Warehouse locations -----------------------------------------------

async function loadLocations() {
  const tbody = document.getElementById('locations-tbody');
  const empty = document.getElementById('locations-empty');
  const stockType = document.getElementById('locations-filter').value;
  try {
    const qs = stockType ? `?stock_type=${encodeURIComponent(stockType)}` : '';
    const { locations } = await api(`/production/warehouse/locations${qs}`);
    tbody.innerHTML = '';
    empty.hidden = locations.length > 0;
    for (const loc of locations) {
      const tr = document.createElement('tr');
      const skusText = loc.current_skus && loc.current_skus.length ? loc.current_skus.join(', ') : '—';
      tr.innerHTML = `
        <td>${escapeHtml(loc.code)}</td>
        <td>${escapeHtml(loc.stock_type || '—')}</td>
        <td>${escapeHtml(loc.description || '—')}</td>
        <td>${escapeHtml(skusText)}</td>
        <td class="narrow"></td>
        <td class="narrow"></td>
      `;

      const assignCell = tr.querySelector('td.narrow');
      const skuInput = document.createElement('input');
      skuInput.type = 'text';
      skuInput.placeholder = 'SKU';
      skuInput.style.width = '110px';
      skuInput.style.display = 'inline-block';
      const assignBtn = document.createElement('button');
      assignBtn.className = 'btn-link';
      assignBtn.textContent = 'Assign';
      assignBtn.addEventListener('click', () => assignSkuLocation(skuInput.value.trim(), loc.code));
      assignCell.appendChild(skuInput);
      assignCell.appendChild(document.createTextNode(' '));
      assignCell.appendChild(assignBtn);

      const labelCell = tr.querySelectorAll('td.narrow')[1];
      const labelBtn = document.createElement('button');
      labelBtn.className = 'btn-link';
      labelBtn.textContent = 'Print';
      labelBtn.addEventListener('click', () => downloadLabel(`/production/labels/location/${encodeURIComponent(loc.code)}`, `location-${loc.code}.zpl`));
      labelCell.appendChild(labelBtn);

      tbody.appendChild(tr);
    }
  } catch (err) {
    if (err.message !== 'Not signed in' && err.message !== 'Session expired') setError(err.message);
  }
}

async function addLocation() {
  const code = document.getElementById('new-location-code').value.trim();
  const description = document.getElementById('new-location-description').value.trim();
  const stockType = document.getElementById('new-location-stock-type').value;
  if (!code) { setError('Enter a location code first.'); return; }
  setError('');
  try {
    await api('/production/warehouse/locations', {
      method: 'POST',
      body: JSON.stringify({ code, description: description || null, stock_type: stockType || null }),
    });
    document.getElementById('new-location-code').value = '';
    document.getElementById('new-location-description').value = '';
    document.getElementById('new-location-stock-type').value = '';
    setSuccess(`Added location ${code}.`);
    await loadLocations();
  } catch (err) {
    setError(err.message);
  }
}

async function assignSkuLocation(sku, locationCode) {
  if (!sku) { setError('Enter a SKU to assign to this location.'); return; }
  setError('');
  try {
    await api(`/production/warehouse/sku-locations/${encodeURIComponent(sku)}`, {
      method: 'PUT',
      body: JSON.stringify({ location_code: locationCode }),
    });
    setSuccess(`${sku}'s home is now ${locationCode}.`);
    await loadLocations();
  } catch (err) {
    setError(err.message);
  }
}

// -- Putaway scans -------------------------------------------------------

async function loadPutawayScans() {
  const tbody = document.getElementById('putaway-tbody');
  const empty = document.getElementById('putaway-empty');
  try {
    const { scans } = await api('/production/warehouse/putaway-scans');
    tbody.innerHTML = '';
    empty.hidden = scans.length > 0;
    for (const s of scans) {
      const tr = document.createElement('tr');
      if (!s.matched) tr.className = 'closed-row';
      const resultText = s.matched ? 'Match' : (s.expected_location_code ? 'Mismatch' : 'No home set');
      const resultPill = s.matched ? 'completed' : (s.expected_location_code ? 'mismatch' : 'issued');
      tr.innerHTML = `
        <td>${escapeHtml(s.sku)}</td>
        <td>${escapeHtml(s.scanned_location_code)}</td>
        <td>${escapeHtml(s.expected_location_code || '—')}</td>
        <td><span class="status-pill ${resultPill}">${resultText}</span></td>
        <td>${escapeHtml(s.scanned_by || '—')}</td>
        <td>${fmtDate(s.scanned_at)}</td>
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
document.getElementById('refresh-stocktake-btn').addEventListener('click', loadStocktake);
document.getElementById('refresh-locations-btn').addEventListener('click', loadLocations);
document.getElementById('locations-filter').addEventListener('change', loadLocations);
document.getElementById('refresh-putaway-btn').addEventListener('click', loadPutawayScans);
document.getElementById('add-demand-row-btn').addEventListener('click', () => addDemandRow());
document.getElementById('sync-demand-btn').addEventListener('click', syncDemand);
document.getElementById('add-location-btn').addEventListener('click', addLocation);
document.getElementById('download-sku-label-btn').addEventListener('click', () => {
  const sku = document.getElementById('sku-label-input').value.trim();
  if (!sku) { setError('Enter a SKU first.'); return; }
  downloadLabel(`/production/labels/sku/${encodeURIComponent(sku)}`, `sku-${sku}.zpl`);
});

(function init() {
  wireDashboardLink();
  importTokenFromUrlFragment();
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
  loadStocktake();
  loadLocations();
  loadPutawayScans();
})();
