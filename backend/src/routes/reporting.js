const express = require('express');
const { pool } = require('../config/db');
const { asyncHandler } = require('../lib/asyncHandler');
const { ApiError } = require('../lib/errors');
const { requireReportAuth, requireEdit } = require('../middleware/reportAuth');

const router = express.Router();
router.use(requireReportAuth);

// Mirrors the Excel Dashboard sheet's formulas exactly (see
// daily_refresh.py / Shonrei_Daily_Key_Figures.xlsx):
//   Net short-term position = Net liquid working capital
//                            = bank + debtors_total - creditors_total
//   Variance to breakeven   = sales_mtd - projected_sales
//   Remaining sales after month-end
//                            = variance_to_breakeven - (sales_prev_month - sales_mtd)
//   Liquid working capital ratio = (bank + debtors_total) / creditors_total
// Computed here at read time from the latest snapshot + current manual
// inputs, rather than stored, so there's one source of truth instead of
// figures that can drift out of sync with their inputs.
function deriveCalculated(snapshot, manualInputs) {
  if (!snapshot) return null;

  const bank = Number(snapshot.bank_balance ?? 0);
  const debtorsTotal = Number(snapshot.debtors_total ?? 0);
  const creditorsTotal = Number(snapshot.creditors_total ?? 0);
  const salesMtd = Number(snapshot.sales_mtd ?? 0);
  const salesPrevMonth = Number(snapshot.sales_prev_month ?? 0);
  const projectedSales = manualInputs?.projected_sales != null ? Number(manualInputs.projected_sales) : null;

  const netShortTermPosition = bank + debtorsTotal - creditorsTotal;
  const varianceToBreakeven = projectedSales != null ? salesMtd - projectedSales : null;
  const remainingSalesAfterMonthEnd = varianceToBreakeven != null
    ? varianceToBreakeven - (salesPrevMonth - salesMtd)
    : null;
  const workingCapitalRatio = creditorsTotal ? (bank + debtorsTotal) / creditorsTotal : 0;

  return {
    net_short_term_position: netShortTermPosition,
    net_liquid_working_capital: netShortTermPosition,
    variance_to_breakeven: varianceToBreakeven,
    remaining_sales_after_month_end: remainingSalesAfterMonthEnd,
    liquid_working_capital_ratio: workingCapitalRatio,
  };
}

router.get('/snapshot', asyncHandler(async (req, res) => {
  const [{ rows: snapRows }, { rows: manualRows }, { rows: stateRows }] = await Promise.all([
    pool.query('select * from reporting.report_snapshots order by as_of desc limit 1'),
    pool.query("select * from reporting.manual_inputs where id = 'current'"),
    pool.query("select status, started_at, last_completed_at from reporting.refresh_state where id = 'current'"),
  ]);

  const snapshot = snapRows[0] || null;
  const manualInputs = manualRows[0] || null;

  res.json({
    snapshot,
    manual_inputs: manualInputs,
    calculated: deriveCalculated(snapshot, manualInputs),
    refresh_state: stateRows[0] || null,
  });
}));

router.get('/history', asyncHandler(async (req, res) => {
  const limit = Math.min(Number(req.query.limit) || 30, 200);
  const { rows } = await pool.query(
    'select * from reporting.report_snapshots order by as_of desc limit $1',
    [limit]
  );
  res.json({ snapshots: rows });
}));

router.get('/manual-inputs/history', asyncHandler(async (req, res) => {
  const limit = Math.min(Number(req.query.limit) || 50, 200);
  const { rows } = await pool.query(
    `select h.id, h.projected_sales, h.monthly_breakeven, h.commentary, h.updated_at,
            u.full_name, u.email
     from reporting.manual_inputs_history h
     left join reporting.report_users u on u.id = h.updated_by
     order by h.updated_at desc limit $1`,
    [limit]
  );
  res.json({ history: rows });
}));

router.put('/manual-inputs', requireEdit, asyncHandler(async (req, res) => {
  const { projected_sales, monthly_breakeven, commentary } = req.body || {};

  if (projected_sales !== undefined && projected_sales !== null && typeof projected_sales !== 'number') {
    throw new ApiError(400, 'projected_sales must be a number');
  }
  if (monthly_breakeven !== undefined && monthly_breakeven !== null && typeof monthly_breakeven !== 'number') {
    throw new ApiError(400, 'monthly_breakeven must be a number');
  }
  if (commentary !== undefined && commentary !== null && (typeof commentary !== 'string' || commentary.length > 4000)) {
    throw new ApiError(400, 'commentary must be a string up to 4000 characters');
  }

  const { rows } = await pool.query(
    `update reporting.manual_inputs
     set projected_sales = coalesce($1, projected_sales),
         monthly_breakeven = coalesce($2, monthly_breakeven),
         commentary = coalesce($3, commentary),
         updated_by = $4
     where id = 'current'
     returning *`,
    [projected_sales ?? null, monthly_breakeven ?? null, commentary ?? null, req.reportUser.id]
  );

  res.json({ manual_inputs: rows[0] });
}));

router.get('/refresh-log', asyncHandler(async (req, res) => {
  const limit = Math.min(Number(req.query.limit) || 50, 200);
  const { rows } = await pool.query(
    'select id, ts, source, step, status, detail, triggered_by from reporting.refresh_log order by ts desc limit $1',
    [limit]
  );
  res.json({ log: rows });
}));

router.post('/refresh', asyncHandler(async (req, res) => {
  const { rows: settingsRows } = await pool.query(
    "select value from reporting.settings where key = 'refresh_min_interval_minutes'"
  );
  const minIntervalMinutes = Number(settingsRows[0]?.value || 5);

  const { rows: stateRows } = await pool.query("select * from reporting.refresh_state where id = 'current'");
  const state = stateRows[0];

  if (state?.status === 'running') {
    throw new ApiError(409, 'A refresh is already running');
  }

  if (state?.last_completed_at) {
    const elapsedMs = Date.now() - new Date(state.last_completed_at).getTime();
    const minMs = minIntervalMinutes * 60 * 1000;
    if (elapsedMs < minMs) {
      const retryAfterSeconds = Math.ceil((minMs - elapsedMs) / 1000);
      res.set('Retry-After', String(retryAfterSeconds));
      throw new ApiError(429, `Please wait ${retryAfterSeconds}s before refreshing again`, { retryAfterSeconds });
    }
  }

  const refreshUrl = process.env.REFRESH_SERVICE_URL;
  const sharedSecret = process.env.REFRESH_SHARED_SECRET;
  if (!refreshUrl || !sharedSecret) {
    throw new ApiError(500, 'Refresh service is not configured (REFRESH_SERVICE_URL / REFRESH_SHARED_SECRET)');
  }

  const response = await fetch(`${refreshUrl}/refresh`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Refresh-Secret': sharedSecret },
    body: JSON.stringify({ triggered_by: req.reportUser.id }),
  });

  if (response.status === 409) {
    throw new ApiError(409, 'A refresh is already running');
  }
  if (!response.ok) {
    throw new ApiError(502, 'Failed to start refresh', await response.text());
  }

  res.status(202).json({ status: 'started' });
}));

module.exports = router;
