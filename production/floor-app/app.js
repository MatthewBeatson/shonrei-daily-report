// Floor app: pick an open production run and report what was actually
// made against it. Talks to the real backend routes in
// backend/src/routes/production.js -- no mock data, this is the "live
// concept" version (see production/README.md for what's still deferred:
// QR-scan-to-select, and the actual Cin7 write-back).

const FLOOR_SECRET = window.__FLOOR_CONFIG__?.FLOOR_SECRET;

let currentRun = null;
let actualQty = 0;
let usingPlannedQty = true;

const scanStep = document.getElementById('scanStep');
const confirmStep = document.getElementById('confirmStep');
const doneStep = document.getElementById('doneStep');
const rescanBtn = document.getElementById('rescanBtn');
const runList = document.getElementById('runList');
const runListEmpty = document.getElementById('runListEmpty');

document.getElementById('refreshBtn').addEventListener('click', loadOpenRuns);
document.getElementById('rejectToggle').addEventListener('click', toggleRejectBlock);
document.getElementById('qtyDown').addEventListener('click', () => adjustQty(-1));
document.getElementById('qtyUp').addEventListener('click', () => adjustQty(1));
document.getElementById('confirmPlannedBtn').addEventListener('click', () => submitRun());
document.getElementById('confirmActualBtn').addEventListener('click', () => submitRun());
rescanBtn.addEventListener('click', resetToScan);
document.getElementById('doneRescanBtn').addEventListener('click', resetToScan);

loadOpenRuns();

async function apiFetch(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      'X-Floor-Secret': FLOOR_SECRET || '',
      ...(options.headers || {}),
    },
  });
  const contentType = res.headers.get('content-type') || '';
  const body = contentType.includes('application/json') ? await res.json() : null;
  if (!res.ok) {
    throw new Error(body?.error || `Request failed (${res.status})`);
  }
  return body;
}

async function loadOpenRuns() {
  runList.hidden = true;
  runListEmpty.hidden = false;
  runListEmpty.textContent = 'Loading...';
  try {
    const { runs } = await apiFetch('/production/runs/open');
    if (!runs.length) {
      runListEmpty.textContent = 'No open runs right now.';
      runListEmpty.hidden = false;
      runList.hidden = true;
      return;
    }
    runList.innerHTML = '';
    for (const run of runs) {
      const btn = document.createElement('button');
      btn.className = 'run-list-item';
      btn.innerHTML =
        `<span>${escapeHtml(run.sku)}</span><span class="qty">planned ${run.qty_to_build}</span>`;
      btn.addEventListener('click', () => openRun(run));
      runList.appendChild(btn);
    }
    runListEmpty.hidden = true;
    runList.hidden = false;
  } catch (err) {
    runListEmpty.textContent = `Couldn't load runs: ${err.message}`;
    runListEmpty.hidden = false;
    runList.hidden = true;
  }
}

function openRun(run) {
  currentRun = run;
  actualQty = Number(run.qty_to_build);
  usingPlannedQty = true;

  document.getElementById('productName').textContent = run.sku;
  document.getElementById('plannedLine').textContent = `Planned: ${run.qty_to_build} units`;
  renderQty();

  scanStep.hidden = true;
  confirmStep.hidden = false;
  doneStep.hidden = true;
  rescanBtn.hidden = false;
}

function adjustQty(delta) {
  actualQty = Math.max(0, actualQty + delta);
  usingPlannedQty = actualQty === Number(currentRun.qty_to_build);
  renderQty();
}

function renderQty() {
  document.getElementById('qtyValue').textContent = actualQty;
  document.getElementById('confirmPlannedBtn').hidden = !usingPlannedQty;
  document.getElementById('confirmActualBtn').hidden = usingPlannedQty;
}

function toggleRejectBlock() {
  const block = document.getElementById('rejectBlock');
  block.hidden = !block.hidden;
}

async function submitRun() {
  const rejectQty = Number(document.getElementById('rejectValue').value || 0);
  const submitBtn = usingPlannedQty
    ? document.getElementById('confirmPlannedBtn')
    : document.getElementById('confirmActualBtn');
  submitBtn.disabled = true;

  try {
    await apiFetch('/production/run-actuals', {
      method: 'POST',
      body: JSON.stringify({
        run_id: currentRun.id,
        actual_qty: actualQty,
        reject_qty: rejectQty,
      }),
    });
    document.getElementById('doneMessage').textContent =
      `Reported ${actualQty} of ${currentRun.sku}` + (rejectQty ? ` (${rejectQty} rejected)` : '') + '.';
    confirmStep.hidden = true;
    doneStep.hidden = false;
  } catch (err) {
    alert(`Couldn't submit: ${err.message}`);
  } finally {
    submitBtn.disabled = false;
  }
}

function resetToScan() {
  currentRun = null;
  scanStep.hidden = false;
  confirmStep.hidden = true;
  doneStep.hidden = true;
  rescanBtn.hidden = true;
  document.getElementById('rejectBlock').hidden = true;
  document.getElementById('rejectValue').value = 0;
  loadOpenRuns();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}
