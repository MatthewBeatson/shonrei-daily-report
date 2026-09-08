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

// -- Tabs ----------------------------------------------------------------

const tabPanels = {
  batches: document.getElementById('tabBatches'),
  stocktake: document.getElementById('tabStocktake'),
  putaway: document.getElementById('tabPutaway'),
};
const tabBtns = {
  batches: document.getElementById('tabBtnBatches'),
  stocktake: document.getElementById('tabBtnStocktake'),
  putaway: document.getElementById('tabBtnPutaway'),
};

function showTab(name) {
  for (const key of Object.keys(tabPanels)) {
    tabPanels[key].hidden = key !== name;
    tabBtns[key].classList.toggle('active', key === name);
  }
  if (name === 'batches') batchCodeInput.focus();
  if (name === 'stocktake') document.getElementById('stSkuInput').focus();
  if (name === 'putaway') document.getElementById('paSkuInput').focus();
}
tabBtns.batches.addEventListener('click', () => showTab('batches'));
tabBtns.stocktake.addEventListener('click', () => showTab('stocktake'));
tabBtns.putaway.addEventListener('click', () => showTab('putaway'));

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

// Fetches a ZPL label file (with the floor secret, same as any other
// call here) and triggers a normal browser download -- whoever's at the
// label printer sends the downloaded .zpl file to it via whatever
// tool/driver Zebra's printer already uses (see production/README.md --
// this app doesn't push labels to the printer over the network itself).
async function downloadLabel(path, suggestedFilename) {
  const res = await fetch(path, { headers: { 'X-Floor-Secret': FLOOR_SECRET || '' } });
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
}

document.getElementById('printBatchLabelBtn').addEventListener('click', async () => {
  const btn = document.getElementById('printBatchLabelBtn');
  btn.disabled = true;
  try {
    await downloadLabel(`/production/labels/batch/${currentBatch.id}`, `batch-${currentBatch.batch_code}.zpl`);
  } catch (err) {
    alert(`Couldn't get label: ${err.message}`);
  } finally {
    btn.disabled = false;
  }
});

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

// -- Stocktake -------------------------------------------------------------
// Replaces the old Google Sheet + AppSheet workflow: scan/type any SKU,
// any time (not a session you open and close), enter the counted qty,
// submit. Recording a count never touches Cin7 by itself -- see
// refresh-service/stocktake.py -- an admin reviews the variance and
// pushes an adjustment separately, from the production admin screen.

let currentStSku = null;
let stQty = 0;
let stViaCode = false;

const stScanStep = document.getElementById('stScanStep');
const stCountStep = document.getElementById('stCountStep');
const stDoneStep = document.getElementById('stDoneStep');
const stCountAgainBtn = document.getElementById('stCountAgainBtn');
const stSkuInput = document.getElementById('stSkuInput');
const stScanError = document.getElementById('stScanError');

document.getElementById('stLookupBtn').addEventListener('click', () => openStocktakeSku(stSkuInput.value.trim(), false));
stSkuInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    openStocktakeSku(stSkuInput.value.trim(), true);
  }
});
document.getElementById('stQtyDown').addEventListener('click', () => adjustStQty(-1));
document.getElementById('stQtyUp').addEventListener('click', () => adjustStQty(1));
document.getElementById('stSubmitBtn').addEventListener('click', submitStocktakeCount);
stCountAgainBtn.addEventListener('click', resetToStScan);
document.getElementById('stDoneAgainBtn').addEventListener('click', resetToStScan);

async function openStocktakeSku(sku, viaCode) {
  stScanError.hidden = true;
  if (!sku) return;
  currentStSku = sku;
  stViaCode = viaCode;
  stQty = 0;

  document.getElementById('stSkuLine').textContent = sku;
  document.getElementById('stLastCountLine').textContent = 'Loading last count...';
  renderStQty();

  stScanStep.hidden = true;
  stCountStep.hidden = false;
  stDoneStep.hidden = true;
  stCountAgainBtn.hidden = false;
  stSkuInput.value = '';

  try {
    const { count } = await apiFetch(`/production/stocktake/counts/latest/${encodeURIComponent(sku)}`);
    if (count) {
      const when = new Date(count.counted_at).toLocaleDateString('en-NZ');
      document.getElementById('stLastCountLine').textContent =
        `Last counted: ${count.counted_qty} on ${when}`;
      stQty = Number(count.counted_qty);
      renderStQty();
    } else {
      document.getElementById('stLastCountLine').textContent = 'No previous count on file.';
    }
  } catch (err) {
    document.getElementById('stLastCountLine').textContent = '';
  }
}

function adjustStQty(delta) {
  stQty = Math.max(0, stQty + delta);
  renderStQty();
}

function renderStQty() {
  document.getElementById('stQtyValue').textContent = stQty;
}

async function submitStocktakeCount() {
  const btn = document.getElementById('stSubmitBtn');
  btn.disabled = true;
  const location = document.getElementById('stLocationInput').value.trim();

  try {
    const data = await apiFetch('/production/stocktake/counts', {
      method: 'POST',
      body: JSON.stringify({
        sku: currentStSku,
        counted_qty: stQty,
        location: location || null,
        reported_via: stViaCode ? 'barcode' : 'manual',
      }),
    });
    let msg = `Counted ${stQty} of ${currentStSku}.`;
    if (data.variance != null) {
      msg += data.variance === 0
        ? ' Matches Cin7.'
        : ` Variance: ${data.variance > 0 ? '+' : ''}${data.variance} vs Cin7 (${data.cin7_on_hand_snapshot}).`;
    }
    document.getElementById('stDoneMessage').textContent = msg;
    stCountStep.hidden = true;
    stDoneStep.hidden = false;
  } catch (err) {
    alert(`Couldn't submit count: ${err.message}`);
  } finally {
    btn.disabled = false;
  }
}

function resetToStScan() {
  currentStSku = null;
  stScanStep.hidden = false;
  stCountStep.hidden = true;
  stDoneStep.hidden = true;
  stCountAgainBtn.hidden = true;
  document.getElementById('stLocationInput').value = '';
  stScanError.hidden = true;
  stSkuInput.value = '';
  stSkuInput.focus();
}

// -- Putaway ---------------------------------------------------------------
// The "product placed anywhere" fix: scan the product's SKU barcode, then
// scan the bin's own location barcode -- two independent scans, told
// immediately whether they match that SKU's designated home location.
// See production/README.md "Labels & warehouse locations".

let paSku = null;

const paSkuStep = document.getElementById('paSkuStep');
const paLocationStep = document.getElementById('paLocationStep');
const paResultStep = document.getElementById('paResultStep');
const paAgainBtn = document.getElementById('paAgainBtn');
const paSkuInput = document.getElementById('paSkuInput');
const paLocationInput = document.getElementById('paLocationInput');

document.getElementById('paSkuNextBtn').addEventListener('click', () => goToPaLocationStep());
paSkuInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); goToPaLocationStep(); } });
document.getElementById('paLocationSubmitBtn').addEventListener('click', submitPutawayScan);
paLocationInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); submitPutawayScan(); } });
paAgainBtn.addEventListener('click', resetToPaScan);

function goToPaLocationStep() {
  const sku = paSkuInput.value.trim();
  if (!sku) return;
  paSku = sku;
  document.getElementById('paSkuLine').textContent = sku;
  paSkuStep.hidden = true;
  paLocationStep.hidden = false;
  paLocationInput.value = '';
  paLocationInput.focus();
  paAgainBtn.hidden = false;
}

async function submitPutawayScan() {
  const locationCode = paLocationInput.value.trim();
  if (!locationCode) return;
  const btn = document.getElementById('paLocationSubmitBtn');
  btn.disabled = true;

  try {
    const data = await apiFetch('/production/warehouse/putaway-scans', {
      method: 'POST',
      body: JSON.stringify({ sku: paSku, scanned_location_code: locationCode }),
    });

    const icon = document.getElementById('paResultIcon');
    const message = document.getElementById('paResultMessage');
    if (data.matched === true) {
      icon.textContent = '✓';
      icon.className = 'pa-result-icon match';
      message.textContent = `Correct -- ${paSku} belongs in ${locationCode}.`;
    } else if (data.matched === false) {
      icon.textContent = '✗';
      icon.className = 'pa-result-icon mismatch';
      message.textContent = `Wrong bin -- ${paSku}'s home is ${data.expected_location_code}, not ${locationCode}.`;
    } else {
      icon.textContent = '?';
      icon.className = 'pa-result-icon unknown';
      message.textContent = `${paSku} has no home location set yet -- scan recorded, ask admin to assign one.`;
    }

    paLocationStep.hidden = true;
    paResultStep.hidden = false;
  } catch (err) {
    alert(`Couldn't submit: ${err.message}`);
  } finally {
    btn.disabled = false;
  }
}

function resetToPaScan() {
  paSku = null;
  paSkuStep.hidden = false;
  paLocationStep.hidden = true;
  paResultStep.hidden = true;
  paAgainBtn.hidden = true;
  paSkuInput.value = '';
  paSkuInput.focus();
}
