const express = require('express');
const { pool } = require('../config/db');
const { asyncHandler } = require('../lib/asyncHandler');
const { ApiError } = require('../lib/errors');
const { requireReportAuth, requireEdit } = require('../middleware/reportAuth');

const router = express.Router();

// Floor-app secret is separate from the reporting Supabase login --
// there's no per-user auth on a shared production-line tablet, this is
// only abuse-deterrence for a route sitting on a public Render URL, same
// rationale as SUPABASE_ANON_KEY being safe to serve publicly in
// /config.js. See production/README.md.
function requireFloorSecret(req, res, next) {
  const secret = process.env.FLOOR_APP_SHARED_SECRET;
  if (!secret) {
    return next(new ApiError(500, 'FLOOR_APP_SHARED_SECRET is not configured on this deploy'));
  }
  if (req.headers['x-floor-secret'] !== secret) {
    return next(new ApiError(401, 'Missing or incorrect floor-app secret'));
  }
  next();
}

async function callRefreshService(path, body) {
  const refreshUrl = process.env.REFRESH_SERVICE_URL;
  const sharedSecret = process.env.REFRESH_SHARED_SECRET;
  if (!refreshUrl || !sharedSecret) {
    throw new ApiError(500, 'Refresh service is not configured (REFRESH_SERVICE_URL / REFRESH_SHARED_SECRET)');
  }
  let response;
  try {
    response = await fetch(`${refreshUrl}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Refresh-Secret': sharedSecret },
      body: JSON.stringify(body || {}),
    });
  } catch (err) {
    throw new ApiError(502, "Couldn't reach the refresh service -- it may be waking up. Try again in a moment.");
  }
  const contentType = response.headers.get('content-type') || '';
  if (!contentType.includes('application/json')) {
    throw new ApiError(502, "Refresh service didn't respond correctly -- it may be waking up. Try again in a moment.");
  }
  const data = await response.json();
  if (!response.ok) {
    throw new ApiError(response.status, data.error || 'Request failed');
  }
  return data;
}

// Public config for the static floor-app, same pattern as the main
// frontend's /config.js.
router.get('/floor-config.js', (req, res) => {
  res.type('application/javascript').send(
    `window.__FLOOR_CONFIG__ = ${JSON.stringify({
      FLOOR_SECRET: process.env.FLOOR_APP_SHARED_SECRET || null,
    })};`
  );
});

// -- Floor-app routes (X-Floor-Secret, no Supabase login) --------------

// Batches currently issued/planned, ordered by priority (oldest SO
// first) -- what the floor app's list view shows below the barcode/
// manual-entry field.
router.get('/batches/open', requireFloorSecret, asyncHandler(async (req, res) => {
  const { rows } = await pool.query(
    `select b.id, b.batch_code, b.qty_planned, b.priority_rank, t.sku
     from production.production_batches b
     join production.targets t on t.id = b.target_id
     where b.status in ('planned', 'issued')
     order by t.sku, b.priority_rank`
  );
  res.json({ batches: rows });
}));

// Looks up a single batch by its code -- what a barcode scan (or typed
// entry) resolves to. Barcode scanners wedge keystrokes + Enter into
// whatever text input has focus, so the floor app just needs one text
// field wired to this lookup -- no camera/scanning library needed.
router.get('/batches/by-code/:batchCode', requireFloorSecret, asyncHandler(async (req, res) => {
  const { rows } = await pool.query(
    `select b.id, b.batch_code, b.qty_planned, b.priority_rank, b.status, t.sku
     from production.production_batches b
     join production.targets t on t.id = b.target_id
     where b.batch_code = $1`,
    [req.params.batchCode]
  );
  if (!rows[0]) throw new ApiError(404, `No batch found for code "${req.params.batchCode}"`);
  res.json({ batch: rows[0] });
}));

// Floor submission: the one human input the whole flow waits on.
// Proxies to refresh-service, which completes a (currently dry-run)
// small Cin7 FG assembly for the actual quantity and decrements the
// parent target -- see refresh-service/backorder_targets.py.
router.post('/batch-actuals', requireFloorSecret, asyncHandler(async (req, res) => {
  const { batch_id, actual_qty, reject_qty, reported_via, reported_by } = req.body || {};
  if (!batch_id || typeof actual_qty !== 'number' || actual_qty < 0) {
    throw new ApiError(400, 'batch_id and a non-negative numeric actual_qty are required');
  }
  if (reported_via && !['barcode', 'manual'].includes(reported_via)) {
    throw new ApiError(400, "reported_via must be 'barcode' or 'manual'");
  }

  const data = await callRefreshService(`/production/batches/${batch_id}/actual`, {
    actual_qty, reject_qty: reject_qty ?? 0,
    reported_via: reported_via || 'manual', reported_by: reported_by || null,
  });
  res.status(201).json(data);
}));

// -- General multi-level-BOM explosion path (production.production_runs)
//    -- runs alongside the backorder-target path below, for admin-
//    triggered replenishment (e.g. min-stock) rather than SO-backorder-
//    driven demand. See production/README.md. -----------------------

router.get('/runs/open', requireFloorSecret, asyncHandler(async (req, res) => {
  const { rows } = await pool.query(
    `select id, sku, qty_to_build, bom_level, created_at
     from production.production_runs
     where status = 'planned'
     order by bom_level desc, created_at`
  );
  res.json({ runs: rows });
}));

router.post('/run-actuals', requireFloorSecret, asyncHandler(async (req, res) => {
  const { run_id, actual_qty, reject_qty, reported_by } = req.body || {};
  if (!run_id || typeof actual_qty !== 'number' || actual_qty < 0) {
    throw new ApiError(400, 'run_id and a non-negative numeric actual_qty are required');
  }

  const { rows: runRows } = await pool.query('select id, status from production.production_runs where id = $1', [run_id]);
  const run = runRows[0];
  if (!run) throw new ApiError(404, 'No such run');
  if (run.status !== 'planned') {
    throw new ApiError(409, `Run is already '${run.status}' -- can't report actuals again`);
  }

  const client = await pool.connect();
  try {
    await client.query('begin');
    const { rows } = await client.query(
      `insert into production.run_actuals (run_id, actual_qty, reject_qty, reported_via, reported_by)
       values ($1, $2, coalesce($3, 0), 'floor_app', $4)
       returning *`,
      [run_id, actual_qty, reject_qty ?? 0, reported_by ?? null]
    );
    await client.query("update production.production_runs set status = 'completed' where id = $1", [run_id]);
    await client.query('commit');
    res.status(201).json({ actual: rows[0] });
  } catch (err) {
    await client.query('rollback');
    throw err;
  } finally {
    client.release();
  }
}));

router.get('/runs', requireReportAuth, asyncHandler(async (req, res) => {
  const { rows } = await pool.query(
    `select r.id, r.plan_batch_id, r.sku, r.qty_to_build, r.bom_level, r.status,
            r.created_at, a.actual_qty, a.reject_qty, a.reported_at
     from production.production_runs r
     left join production.run_actuals a on a.run_id = r.id
     order by r.created_at desc
     limit 200`
  );
  res.json({ runs: rows });
}));

router.post('/plan', requireEdit, asyncHandler(async (req, res) => {
  const data = await callRefreshService('/production/plan', req.body);
  res.status(201).json(data);
}));

// -- Backorder-target / batch (sublist) admin routes, same Supabase
//    login as the rest of the report --------------------------------

router.get('/targets', requireReportAuth, asyncHandler(async (req, res) => {
  const { rows } = await pool.query(
    `select id, sku, status, outstanding_qty, cin7_assembly_id, created_at, closed_at
     from production.targets order by status, updated_at desc limit 200`
  );
  res.json({ targets: rows });
}));

router.get('/targets/:targetId/demand-lines', requireReportAuth, asyncHandler(async (req, res) => {
  const { rows } = await pool.query(
    `select so_number, order_date, qty_backordered, priority_rank
     from production.target_demand_lines where target_id = $1 order by priority_rank`,
    [req.params.targetId]
  );
  res.json({ demand_lines: rows });
}));

router.get('/batches', requireReportAuth, asyncHandler(async (req, res) => {
  const { rows } = await pool.query(
    `select b.id, b.batch_code, b.qty_planned, b.priority_rank, b.status, b.created_at,
            t.sku, a.actual_qty, a.reject_qty, a.reported_via, a.reported_at
     from production.production_batches b
     join production.targets t on t.id = b.target_id
     left join production.batch_actuals a on a.batch_id = b.id
     order by b.created_at desc limit 200`
  );
  res.json({ batches: rows });
}));

// Body: {"demand_lines_by_sku": {sku: [{so_number, order_date, qty_backordered}, ...]}}
// See refresh-service/backorder_targets.py -- demand extraction from live
// Cin7 SOs isn't wired in yet, so this is supplied directly for now.
router.post('/targets/sync', requireEdit, asyncHandler(async (req, res) => {
  const data = await callRefreshService('/production/targets/sync', req.body);
  res.status(200).json(data);
}));

router.post('/targets/:targetId/plan-batches', requireEdit, asyncHandler(async (req, res) => {
  const { suggested_run_size } = req.body || {};
  if (!suggested_run_size || suggested_run_size <= 0) {
    throw new ApiError(400, 'suggested_run_size must be a positive number');
  }
  const data = await callRefreshService(`/production/targets/${req.params.targetId}/plan-batches`, { suggested_run_size });
  res.status(201).json(data);
}));

// -- Stocktake -- replaces the old Google Sheet + AppSheet workflow, see
//    production/README.md "Stocktake". ---------------------------------

// Floor: record a count (barcode/manual, same pattern as batch reporting).
// Never touches Cin7 by itself -- see refresh-service/stocktake.py.
router.post('/stocktake/counts', requireFloorSecret, asyncHandler(async (req, res) => {
  const { sku, counted_qty, location, reported_via, reported_by } = req.body || {};
  if (!sku || typeof counted_qty !== 'number' || counted_qty < 0) {
    throw new ApiError(400, 'sku and a non-negative numeric counted_qty are required');
  }
  if (reported_via && !['barcode', 'manual'].includes(reported_via)) {
    throw new ApiError(400, "reported_via must be 'barcode' or 'manual'");
  }
  const data = await callRefreshService('/stocktake/counts', {
    sku, counted_qty, location: location || null,
    reported_via: reported_via || 'manual', reported_by: reported_by || null,
  });
  res.status(201).json(data);
}));

// Floor: this SKU's most recent count, so the tab can show "last counted
// as X, Y ago" instead of a blank form every time.
router.get('/stocktake/counts/latest/:sku', requireFloorSecret, asyncHandler(async (req, res) => {
  const { rows } = await pool.query(
    `select sku, counted_qty, cin7_on_hand_snapshot, variance, counted_at
     from stocktake.counts where sku = $1 order by counted_at desc limit 1`,
    [req.params.sku]
  );
  res.json({ count: rows[0] || null });
}));

// Admin: every count, most recent first, for reviewing variances.
router.get('/stocktake/counts', requireReportAuth, asyncHandler(async (req, res) => {
  const { rows } = await pool.query(
    `select id, sku, location, counted_qty, cin7_on_hand_snapshot, variance, status,
            reported_via, reported_by, cin7_adjustment_id, counted_at, adjusted_at
     from stocktake.counts order by counted_at desc limit 200`
  );
  res.json({ counts: rows });
}));

// Admin: push one reviewed count to Cin7 as a stock adjustment.
router.post('/stocktake/counts/:countId/adjust', requireEdit, asyncHandler(async (req, res) => {
  const data = await callRefreshService(`/stocktake/counts/${req.params.countId}/adjust`, {
    note: (req.body || {}).note || null,
  });
  res.status(200).json(data);
}));

module.exports = router;
