const express = require('express');
const { pool } = require('../config/db');
const { supabaseStorage } = require('../config/supabase');
const { asyncHandler } = require('../lib/asyncHandler');
const { ApiError } = require('../lib/errors');
const { requireReportAuth, requireEdit } = require('../middleware/reportAuth');

const router = express.Router();
router.use(requireReportAuth);

const STORAGE_BUCKET = 'dispatch-plans';
const SIGNED_URL_TTL_SECONDS = 300; // short-lived -- re-requested each time the frontend's download button is clicked

router.get('/', asyncHandler(async (req, res) => {
  const { rows } = await pool.query("select * from reporting.dispatch_plan_current where id = 'current'");
  const current = rows[0] || null;

  let downloadUrl = null;
  if (current?.status === 'ok' && current.storage_path) {
    if (!supabaseStorage) {
      throw new ApiError(500, 'SUPABASE_SERVICE_ROLE_KEY is not configured on this deploy -- cannot sign a download URL');
    }
    const { data, error } = await supabaseStorage.storage
      .from(STORAGE_BUCKET)
      .createSignedUrl(current.storage_path, SIGNED_URL_TTL_SECONDS);
    if (error) {
      throw new ApiError(502, `Couldn't sign a download URL for the dispatch plan: ${error.message}`);
    }
    downloadUrl = data.signedUrl;
  }

  res.json({
    generated_at: current?.generated_at || null,
    months_covered: current?.months_covered || [],
    status: current?.status || 'error',
    download_url: downloadUrl,
  });
}));

router.get('/log', asyncHandler(async (req, res) => {
  const limit = Math.min(Number(req.query.limit) || 20, 100);
  const { rows } = await pool.query(
    'select id, ts, step, status, detail, triggered_by from reporting.dispatch_plan_log order by ts desc limit $1',
    [limit]
  );
  res.json({ log: rows });
}));

router.post('/generate', requireEdit, asyncHandler(async (req, res) => {
  const refreshUrl = process.env.REFRESH_SERVICE_URL;
  const sharedSecret = process.env.REFRESH_SHARED_SECRET;
  if (!refreshUrl || !sharedSecret) {
    throw new ApiError(500, 'Refresh service is not configured (REFRESH_SERVICE_URL / REFRESH_SHARED_SECRET)');
  }

  // Same generous timeout + explicit network-error handling as
  // POST /reporting/refresh -- a Cin7-heavy dispatch plan pull can take a
  // while, but this endpoint itself only needs to survive long enough to
  // confirm the background run *started*, not to wait for it to finish.
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 45000);
  let response;
  try {
    response = await fetch(`${refreshUrl}/dispatch-plan/generate`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Refresh-Secret': sharedSecret },
      body: JSON.stringify({ triggered_by: req.reportUser.id }),
      signal: controller.signal,
    });
  } catch (err) {
    throw new ApiError(502, "Couldn't reach the refresh service -- it may be waking up. Try again in a moment.");
  } finally {
    clearTimeout(timeout);
  }

  if (response.status === 409) {
    throw new ApiError(409, 'A dispatch plan generation is already running');
  }

  const contentType = response.headers.get('content-type') || '';
  if (!response.ok || !contentType.includes('application/json')) {
    throw new ApiError(502, "Refresh service didn't respond correctly -- it may be waking up. Try again in a moment.");
  }

  res.status(202).json({ status: 'started' });
}));

router.get('/overrides', asyncHandler(async (req, res) => {
  const { rows } = await pool.query(
    'select order_number, group_label_override, hold, note, updated_by, updated_at ' +
    'from reporting.dispatch_plan_overrides order by updated_at desc'
  );
  res.json({ overrides: rows });
}));

router.put('/overrides/:orderNumber', requireEdit, asyncHandler(async (req, res) => {
  const { orderNumber } = req.params;
  const { group_label_override, hold, note } = req.body || {};

  if (group_label_override !== undefined && group_label_override !== null && typeof group_label_override !== 'string') {
    throw new ApiError(400, 'group_label_override must be a string');
  }
  if (hold !== undefined && typeof hold !== 'boolean') {
    throw new ApiError(400, 'hold must be a boolean');
  }
  if (note !== undefined && note !== null && typeof note !== 'string') {
    throw new ApiError(400, 'note must be a string');
  }

  const { rows } = await pool.query(
    `insert into reporting.dispatch_plan_overrides (order_number, group_label_override, hold, note, updated_by)
     values ($1, $2, coalesce($3, false), $4, $5)
     on conflict (order_number) do update set
       group_label_override = excluded.group_label_override,
       hold = excluded.hold,
       note = excluded.note,
       updated_by = excluded.updated_by
     returning *`,
    [orderNumber, group_label_override ?? null, hold, note ?? null, req.reportUser.id]
  );
  res.json({ override: rows[0] });
}));

router.delete('/overrides/:orderNumber', requireEdit, asyncHandler(async (req, res) => {
  await pool.query('delete from reporting.dispatch_plan_overrides where order_number = $1', [req.params.orderNumber]);
  res.status(204).end();
}));

module.exports = router;
