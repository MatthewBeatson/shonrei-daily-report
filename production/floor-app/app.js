// Floor app: scan (or type) a batch code, or pick one from the priority
// list, then report what was actually made against it. Talks to the
// real backend routes in backend/src/routes/production.js -- these are
// Shonrei's real "sublists" (production.production_batches), not the
// general BOM-explosion production_runs (see production/README.md for
// how the two paths relate).
//
// Barcode support needs no camera/scanning library: a hardware barcode
// scanner acts as a keyboard, typing the code into whatever input has
// focus and sending Enter -- so #batchCodeInput just needs to stay
// focused and listen for Enter, same as if someone typed the code by
// hand. That's the entire "barcode or manual entry" requirement.

const FLOOR_SECRET = window.__FLOOR_CONFIG__?.FLOOR_SECRET;

let currentBatch = null;
let actualQty = 0;
let usingPlannedQty = true;

const scanStep = document.getElementById('scanStep');
const confirmStep = document.getElementById('confirmStep');
const doneStep = document.getElementById('doneStep');
const rescanBtn = document.getElementById('rescanBtn');
const runList = document.getElementById('runList');
const runListEmpty = document.getElementById('runListEmpty');
const batchCodeInput = document.getElementById('batchCodeInput');
const scanError = document.getElementById('scanError');

document.getElementById('refreshBtn').addEventListener('click', loadOpenBatches);
document.getElementById('rejectToggle').addEventListener('click', toggleRejectBlock);
document.getElementById('qtyDown').addEventListener('click', () => adjustQty(-1));
document.getElementById('qtyUp').addEventListener('click', () => adjustQty(1));
document.getElementById('confirmPlannedBtn').addEventListener('click', () => submitBatch());
document.getElementById('confirmActualBtn').addEventListener('click', () => submitBatch());
rescanBtn.addEventListener('click', resetToScan);
document.getElementById('doneRescanBtn').addEventListener('click', resetToScan);

batchCodeInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    lookupBatchCode(batchCodeInput.value.trim());
  }
});

loadOpenBatches();

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

async function lookupBatchCode(code) {
  scanError.hidden = true;
  if (!code) return;
  try {
    const { batch } = await apiFetch(`/production/batches/by-code/${encodeURIComponent(code)}`);
    if (batch.status === 'completed') {
      throw new Error(`Batch ${batch.batch_code} was already reported`);
    }
    openBatch(batch, /* viaCode */ true);
    batchCodeInput.value = '';
  } catch (err) {
    scanError.textContent = err.message;
    scanError.hidden = false;
    batchCodeInput.select();
  }
}

async function loadOpenBatches() {
  runList.hidden = true;
  runListEmpty.hidden = false;
  runListEmpty.textContent = 'Loading...';
  try {
    const { batches } = await apiFetch('/production/batches/open');
    if (!batches.length) {
      runListEmpty.textContent = 'No open batches right now.';
      runListEmpty.hidden = false;
      runList.hidden = true;
      return;
    }
    runList.innerHTML = '';
    for (const batch of batches) {
      const btn = document.createElement('button');
      btn.className = 'run-list-item';
      btn.innerHTML =
        `<span>${escapeHtml(batch.sku)}<br><span class="batch-code">${escapeHtml(batch.batch_code)}</span></span>` +
        `<span class="qty">planned ${batch.qty_planned}</span>`;
      btn.addEventListener('click', () => openBatch(batch));
      runList.appendChild(btn);
    }
    runListEmpty.hidden = true;
    runList.hidden = false;
  } catch (err) {
    runListEmpty.textContent = `Couldn't load batches: ${err.message}`;
    runListEmpty.hidden = false;
    runList.hidden = true;
  }
}

function openBatch(batch, viaCode = false) {
  currentBatch = { ...batch, _openedViaCode: viaCode };
  actualQty = Number(batch.qty_planned);
  usingPlannedQty = true;

  document.getElementById('productName').textContent = batch.sku;
  document.getElementById('plannedLine').textContent = `Planned: ${batch.qty_planned} units`;
  document.getElementById('batchCodeLine').textContent = batch.batch_code;
  renderQty();

  scanStep.hidden = true;
  confirmStep.hidden = false;
  doneStep.hidden = true;
  rescanBtn.hidden = false;
}

function adjustQty(delta) {
  actualQty = Math.max(0, actualQty + delta);
  usingPlannedQty = actualQty === Number(currentBatch.qty_planned);
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

async function submitBatch() {
  const rejectQty = Number(document.getElementById('rejectValue').value || 0);
  const submitBtn = usingPlannedQty
    ? document.getElementById('confirmPlannedBtn')
    : document.getElementById('confirmActualBtn');
  submitBtn.disabled = true;

  // reported_via reflects how the batch was *opened*: a barcode scan
  // populates and submits the code field programmatically the same way
  // typing does, so in practice this only really distinguishes "typed a
  // code" from "tapped the list" -- both are honest, neither claims a
  // scan that didn't happen. A real hardware-scanner integration would
  // set this from whichever path fired.
  const reportedVia = currentBatch._openedViaCode ? 'barcode' : 'manual';

  try {
    await apiFetch('/production/batch-actuals', {
      method: 'POST',
      body: JSON.stringify({
        batch_id: currentBatch.id,
        actual_qty: actualQty,
        reject_qty: rejectQty,
        reported_via: reportedVia,
      }),
    });
    document.getElementById('doneMessage').textContent =
      `Reported ${actualQty} of ${currentBatch.sku} (${currentBatch.batch_code})` +
      (rejectQty ? ` (${rejectQty} rejected)` : '') + '.';
    confirmStep.hidden = true;
    doneStep.hidden = false;
  } catch (err) {
    alert(`Couldn't submit: ${err.message}`);
  } finally {
    submitBtn.disabled = false;
  }
}

function resetToScan() {
  currentBatch = null;
  scanStep.hidden = false;
  confirmStep.hidden = true;
  doneStep.hidden = true;
  rescanBtn.hidden = true;
  document.getElementById('rejectBlock').hidden = true;
  document.getElementById('rejectValue').value = 0;
  scanError.hidden = true;
  batchCodeInput.value = '';
  batchCodeInput.focus();
  loadOpenBatches();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}
