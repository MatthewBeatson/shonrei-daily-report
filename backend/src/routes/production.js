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

// Public config for the static floor-app, same pattern as the main
// frontend's /config.js.
router.get('/floor-config.js', (req, res) => {
  res.type('application/javascript').send(
    `window.__FLOOR_CONFIG__ = ${JSON.stringify({
      FLOOR_SECRET: process.env.FLOOR_APP_SHARED_SECRET || null,
    })};`
  );
});

// Runs currently waiting on a floor input -- what the floor app's "pick a
// run" screen lists. No barcode/QR scanning wired up yet (see
// production/README.md); this is the "not too much more setup" version --
// pick from a real, small, currently-open list instead.
router.get('/runs/open', requireFloorSecret, asyncHandler(async (req, res) => {
  const { rows } = await pool.query(
    `select id, sku, qty_to_build, bom_level, created_at
     from production.production_runs
     where status = 'planned'
     order by bom_level desc, created_at`
  );
  res.json({ runs: rows });
}));

// Floor submission: the one human input the whole flow waits on. Marks
// the run 'completed' directly -- this concept doesn't call Cin7's
// Allocate/Complete endpoints yet (see production/planner/orchestrator.py
// and production/README.md for why that's deliberately deferred), so
// "completed" here means "actual quantity recorded", not "Cin7 stock
// relieved".
router.post('/run-actuals', requireFloorSecret, asyncHandler(async (req, res) => {
  const { run_id, actual_qty, reject_qty, reported_by } = req.body || {};

  if (!run_id || typeof actual_qty !== 'number' || actual_qty < 0) {
    throw new ApiError(400, 'run_id and a non-negative numeric actual_qty are required');
  }

  const { rows: runRows } = await pool.query(
    "select id, status from production.production_runs where id = $1",
    [run_id]
  );
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
    await client.query(
      "update production.production_runs set status = 'completed' where id = $1",
      [run_id]
    );
    await client.query('commit');
    res.status(201).json({ actual: rows[0] });
  } catch (err) {
    await client.query('rollback');
    throw err;
  } finally {
    client.release();
  }
}));

// -- Admin/planner side, same Supabase login as the rest of the report --

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
  const refreshUrl = process.env.REFRESH_SERVICE_URL;
  const sharedSecret = process.env.REFRESH_SHARED_SECRET;
  if (!refreshUrl || !sharedSecret) {
    throw new ApiError(500, 'Refresh service is not configured (REFRESH_SERVICE_URL / REFRESH_SHARED_SECRET)');
  }

  // Demand/BOM/on-hand come in on the request body for this first concept
  // -- see refresh-service/production_plan.py for why the automatic Cin7
  // BOM pull isn't wired up yet. The admin UI (not built yet either) would
  // be what assembles this body; for now it's fine to POST it directly.
  let response;
  try {
    response = await fetch(`${refreshUrl}/production/plan`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Refresh-Secret': sharedSecret },
      body: JSON.stringify(req.body || {}),
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
    throw new ApiError(response.status, data.error || 'Planning failed');
  }
  res.status(201).json(data);
}));

module.exports = router;
