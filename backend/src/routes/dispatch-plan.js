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
    'select order_number, group_label_override, hold, note, dispatch_date_override, updated_by, updated_at ' +
    'from reporting.dispatch_plan_overrides order by updated_at desc'
  );
  res.json({ overrides: rows });
}));

// Cin7 SO#s always carry an 'SO-' prefix, but a hand-typed override can
// easily omit it (seen live 2026-09-03: '16633' stored instead of
// 'SO-16633', so the hold silently never matched the real order -- no
// error, it just stayed in normal scheduling). Normalizing on write keeps
// what's stored in the DB matching what dispatch_plan_schedule.py will
// compare it against, even though that module also normalizes on its own
// side as a second line of defense for rows written before this existed.
function normalizeOrderNumber(v) {
  const trimmed = String(v || '').trim().toUpperCase();
  if (trimmed && !trimmed.startsWith('SO-') && /^[0-9-]+$/.test(trimmed) && /\d/.test(trimmed)) {
    return `SO-${trimmed}`;
  }
  return trimmed;
}

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

router.put('/overrides/:orderNumber', requireEdit, asyncHandler(async (req, res) => {
  const orderNumber = normalizeOrderNumber(req.params.orderNumber);
  const { group_label_override, hold, note, dispatch_date_override } = req.body || {};

  if (group_label_override !== undefined && group_label_override !== null && typeof group_label_override !== 'string') {
    throw new ApiError(400, 'group_label_override must be a string');
  }
  if (hold !== undefined && typeof hold !== 'boolean') {
    throw new ApiError(400, 'hold must be a boolean');
  }
  if (note !== undefined && note !== null && typeof note !== 'string') {
    throw new ApiError(400, 'note must be a string');
  }
  // A promised/forced date is a hard pin the scheduler never moves -- 'YYYY-
  // MM-DD' only (an <input type="date"> already gives this shape), no free-
  // text parsing that could silently land on the wrong day.
  if (dispatch_date_override !== undefined && dispatch_date_override !== null && !DATE_RE.test(dispatch_date_override)) {
    throw new ApiError(400, 'dispatch_date_override must be a YYYY-MM-DD date string');
  }

  const { rows } = await pool.query(
    `insert into reporting.dispatch_plan_overrides
       (order_number, group_label_override, hold, note, dispatch_date_override, updated_by)
     values ($1, $2, coalesce($3, false), $4, $5, $6)
     on conflict (order_number) do update set
       group_label_override = excluded.group_label_override,
       hold = excluded.hold,
       note = excluded.note,
       dispatch_date_override = excluded.dispatch_date_override,
       updated_by = excluded.updated_by
     returning *`,
    [orderNumber, group_label_override ?? null, hold, note ?? null, dispatch_date_override ?? null, req.reportUser.id]
  );
  res.json({ override: rows[0] });
}));

router.delete('/overrides/:orderNumber', requireEdit, asyncHandler(async (req, res) => {
  const orderNumber = normalizeOrderNumber(req.params.orderNumber);
  await pool.query('delete from reporting.dispatch_plan_overrides where order_number = $1', [orderNumber]);
  res.status(204).end();
}));

module.exports = router;
