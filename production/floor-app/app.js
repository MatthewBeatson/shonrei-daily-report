// Floor-app mockup. Demonstrates the interaction shape (scan -> confirm
// -> submit) end to end with a fake scan and a stubbed submit call --
// there's no camera integration or backend route yet (see
// production/README.md "Not yet built"). Swap MOCK_SCAN_RESULT / the
// fetch() in submitRun() for real ones once production/backend exists.
//
// Design intent worth keeping when this gets wired up for real:
//   - one scan opens the screen already showing the PLANNED qty
//   - one tap ("Made as planned") is the entire interaction for the
//     common case -- typing a number is only needed for the exception
//   - reject/scrap entry is collapsed by default, one tap to reveal
//   - submit fires once per run/shift, not continuously

const MOCK_SCAN_RESULT = {
  run_id: 'demo-run-001',
  sku: 'FG-2400-BOX',
  product_name: '2400 Series - Assembled Box',
  planned_qty: 48,
};

let currentRun = null;
let actualQty = 0;
let usingPlannedQty = true;

const scanStep = document.getElementById('scanStep');
const confirmStep = document.getElementById('confirmStep');
const doneStep = document.getElementById('doneStep');
const rescanBtn = document.getElementById('rescanBtn');

document.getElementById('scanBtn').addEventListener('click', onScan);
document.getElementById('rejectToggle').addEventListener('click', toggleRejectBlock);
document.getElementById('qtyDown').addEventListener('click', () => adjustQty(-1));
document.getElementById('qtyUp').addEventListener('click', () => adjustQty(1));
document.getElementById('confirmPlannedBtn').addEventListener('click', () => submitRun());
document.getElementById('confirmActualBtn').addEventListener('click', () => submitRun());
rescanBtn.addEventListener('click', resetToScan);
document.getElementById('doneRescanBtn').addEventListener('click', resetToScan);

function onScan() {
  // Real version: open camera, decode QR -> { run_id, sku, ... } from the
  // day's schedule sheet. Here: just load the mock run.
  currentRun = MOCK_SCAN_RESULT;
  actualQty = currentRun.planned_qty;
  usingPlannedQty = true;

  document.getElementById('productName').textContent = currentRun.product_name;
  document.getElementById('plannedLine').textContent =
    `Planned: ${currentRun.planned_qty} units`;
  renderQty();

  scanStep.hidden = true;
  confirmStep.hidden = false;
  doneStep.hidden = true;
  rescanBtn.hidden = false;
}

function adjustQty(delta) {
  actualQty = Math.max(0, actualQty + delta);
  usingPlannedQty = actualQty === currentRun.planned_qty;
  renderQty();
}

function renderQty() {
  document.getElementById('qtyValue').textContent = actualQty;
  // Once the qty has been touched away from plan, the primary action
  // switches from the one-tap "as planned" button to a plain Submit --
  // still one tap, just no longer implying "unchanged".
  document.getElementById('confirmPlannedBtn').hidden = !usingPlannedQty;
  document.getElementById('confirmActualBtn').hidden = usingPlannedQty;
}

function toggleRejectBlock() {
  const block = document.getElementById('rejectBlock');
  block.hidden = !block.hidden;
}

async function submitRun() {
  const rejectQty = Number(document.getElementById('rejectValue').value || 0);

  const payload = {
    run_id: currentRun.run_id,
    actual_qty: actualQty,
    reject_qty: rejectQty,
    reported_via: 'floor_app',
  };

  // Stub: production/backend doesn't exist yet. Real call writes to
  // production.run_actuals; the planner's orchestrator picks it up and
  // fires the Cin7 Complete call automatically -- no further action from
  // whoever is on the floor.
  try {
    // await fetch('/api/production/run-actuals', {
    //   method: 'POST',
    //   headers: { 'Content-Type': 'application/json' },
    //   body: JSON.stringify(payload),
    // });
    console.log('[mock submit]', payload);
  } catch (err) {
    console.error('submit failed', err);
  }

  document.getElementById('doneMessage').textContent =
    `Reported ${actualQty} of ${currentRun.product_name}` +
    (rejectQty ? ` (${rejectQty} rejected)` : '') + '.';

  confirmStep.hidden = true;
  doneStep.hidden = false;
}

function resetToScan() {
  currentRun = null;
  scanStep.hidden = false;
  confirmStep.hidden = true;
  doneStep.hidden = true;
  rescanBtn.hidden = true;
  document.getElementById('rejectBlock').hidden = true;
  document.getElementById('rejectValue').value = 0;
}
