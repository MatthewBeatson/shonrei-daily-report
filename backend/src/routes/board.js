const crypto = require('crypto');
const express = require('express');
const { pool } = require('../config/db');
const { asyncHandler } = require('../lib/asyncHandler');
const { ApiError } = require('../lib/errors');

// Dispatch Board -- the dispatch-area TV (/board/) and the staff KPI entry
// page (/kpi/). Both are opened by warehouse staff with a shared access
// code, not a report_users login, so every query in here reads the
// `board` schema (plus production.batch_actuals' unit counts) and nothing
// from `reporting` -- no dollar figure can reach these screens. See
// migrations/017_dispatch_board.sql.

const router = express.Router();

const TZ = 'Pacific/Auckland';

// Shared code, entered once per device and kept in its localStorage.
// Unlike the floor app's secret it is never served to the page by
// config.js -- whoever sets up the TV types it in (or opens
// /board/?code=... once).
function requireBoardCode(req, res, next) {
  const secret = process.env.DISPATCH_BOARD_SECRET;
  if (!secret) {
    return next(new ApiError(500, 'DISPATCH_BOARD_SECRET is not configured on this deploy'));
  }
  const given = Buffer.from(String(req.headers['x-board-code'] || ''));
  const expected = Buffer.from(secret);
  if (given.length !== expected.length || !crypto.timingSafeEqual(given, expected)) {
    return next(new ApiError(401, 'Missing or incorrect board access code'));
  }
  next();
}

router.use(requireBoardCode);

// NZ-local "today" and the Monday-Sunday week / calendar month around it,
// computed in Postgres so it tracks daylight saving without JS date math.
const DATES_CTE = `
  with d as (
    select (now() at time zone '${TZ}')::date as today
  ), b as (
    select today,
           date_trunc('week', today)::date as wk_start,
           date_trunc('week', today)::date + 6 as wk_end,
           date_trunc('week', today)::date + 7 as nwk_start,
           date_trunc('week', today)::date + 13 as nwk_end,
           date_trunc('month', today)::date as mo_start,
           (date_trunc('month', today) + interval '1 month - 1 day')::date as mo_end
    from d
  )`;

function num(v) {
  return v === null || v === undefined ? 0 : Number(v);
}

router.get('/check', (req, res) => res.json({ ok: true }));

// Every open SO the board knows about, one row each, in one shape:
//   list 'plan'    -- the weekly Monthly Dispatch Plan (Monday snapshot),
//                     minus packaging SOs
//   list 'plain'   -- packaging, no printing: due next working day
//   list 'printed' -- packaging with a printing charge: due in 5 working days
// Packaging rows come from the hourly refresh, so they win over a plan
// row for the same SO. `due` is the plan's dispatch date or the packaging
// deadline.
const ALL_CTE = `
  alls as (
    select p.order_number, p.customer, p.reference, p.group_label, p.dispatch_date as due,
           p.units_remaining, p.line_count, 'plan' as list
    from board.plan_orders p
    where not p.hold and not p.is_packaging
      and not exists (select 1 from board.packaging_orders k where k.order_number = p.order_number)
    union all
    select order_number, customer, reference, null, deadline,
           units_remaining, line_count, case when printed then 'printed' else 'plain' end
    from board.packaging_orders
  ), withpick as (
    select a.*, s.picked_at is not null as picked, s.picked_by, s.picked_at
    from alls a left join board.so_picks s on s.order_number = a.order_number
  )`;

router.get('/summary', asyncHandler(async (req, res) => {
  // Dates go out as plain 'YYYY-MM-DD' text -- node-pg turns a bare date
  // into a local-midnight Date, which JSON-serializes as the previous day
  // in UTC and would shift every due date on the TV back by one.
  const { rows: [dates] } = await pool.query(
    `${DATES_CTE} select today::text as today, wk_start::text as wk_start, wk_end::text as wk_end from b`
  );

  // Plan list: this week's SOs plus anything from an earlier week still not
  // picked (carried over, shown as late). Packaging lists: every open
  // packaging SO. Unpicked first, soonest due first.
  const { rows: listed } = await pool.query(
    `${DATES_CTE}, ${ALL_CTE}
     select w.order_number, w.customer, w.reference, w.group_label, w.due::text as due, w.units_remaining,
            w.line_count, w.list, w.picked, w.picked_by, w.picked_at,
            (not w.picked and w.due < b.today) as late
     from withpick w cross join b
     where w.list <> 'plan'
        or w.due between b.wk_start and b.wk_end
        or (w.due < b.wk_start and not w.picked)
     order by w.picked, w.due, w.customer, w.order_number`
  );

  const { rows: [k] } = await pool.query(
    `${DATES_CTE}, ${ALL_CTE},
     picks as (select order_number, (picked_at at time zone '${TZ}')::date as day from board.so_picks),
     open as (select * from withpick where not picked)
     select
       (select count(*) from picks, b where day between b.wk_start and b.wk_end) as picked_week,
       (select count(*) from picks, b where day = b.today) as picked_today,
       (select count(*) from withpick, b where due between b.wk_start and b.wk_end) as planned_week,
       (select count(*) from withpick, b where picked and due between b.wk_start and b.wk_end) as planned_week_picked,
       (select count(*) from open, b where due <= b.wk_end) as togo_week_sos,
       (select coalesce(sum(units_remaining), 0) from open, b where due <= b.wk_end) as togo_week_units,
       (select count(*) from open, b where due < b.today) as late_sos,
       (select count(*) from open, b where due <= b.mo_end) as togo_month_sos,
       (select coalesce(sum(units_remaining), 0) from open, b where due <= b.mo_end) as togo_month_units,
       (select count(*) from open, b where list = 'plan' and due between b.nwk_start and b.nwk_end) as next_week_sos,
       (select coalesce(sum(units_remaining), 0) from open, b
          where list = 'plan' and due between b.nwk_start and b.nwk_end) as next_week_units,
       (select count(*) from board.plan_orders where hold) as held_sos,
       (select max(generated_at) from board.plan_orders) as plan_generated_at,
       (select max(refreshed_at) from board.packaging_orders) as packaging_refreshed_at`
  );

  // Picks per weekday this week, Mon..Fri (weekend picks fold into Fri so
  // they still count without adding two mostly-empty bars).
  const { rows: byDay } = await pool.query(
    `${DATES_CTE}
     select least(extract(isodow from (picked_at at time zone '${TZ}')::date)::int, 5) as dow, count(*) as n
     from board.so_picks, b
     where (picked_at at time zone '${TZ}')::date between b.wk_start and b.wk_end
     group by 1`
  );
  const picksByDay = [0, 0, 0, 0, 0];
  byDay.forEach((r) => { picksByDay[r.dow - 1] += num(r.n); });

  // Production output is optional -- if the production schema isn't in use
  // on this deploy, the tile just shows "--" rather than failing the board.
  let producedWeek = null;
  try {
    const { rows: [p] } = await pool.query(
      `${DATES_CTE}
       select coalesce(sum(actual_qty), 0) as units from production.batch_actuals, b
       where (reported_at at time zone '${TZ}')::date between b.wk_start and b.wk_end`
    );
    producedWeek = num(p.units);
  } catch (err) {
    producedWeek = null;
  }

  res.json({
    today: dates.today,
    week_start: dates.wk_start,
    week_end: dates.wk_end,
    plan_generated_at: k.plan_generated_at,
    packaging_refreshed_at: k.packaging_refreshed_at,
    orders: listed.map((o) => ({ ...o, units_remaining: o.units_remaining === null ? null : num(o.units_remaining) })),
    kpis: {
      picked_week: num(k.picked_week),
      picked_today: num(k.picked_today),
      picks_by_day: picksByDay,
      planned_week: num(k.planned_week),
      planned_week_picked: num(k.planned_week_picked),
      togo_week_sos: num(k.togo_week_sos),
      togo_week_units: num(k.togo_week_units),
      late_sos: num(k.late_sos),
      togo_month_sos: num(k.togo_month_sos),
      togo_month_units: num(k.togo_month_units),
      next_week_sos: num(k.next_week_sos),
      next_week_units: num(k.next_week_units),
      held_sos: num(k.held_sos),
      produced_week_units: producedWeek,
    },
  });
}));

// For /kpi/'s SO dropdown: every open SO (plan + packaging) not yet
// picked, soonest due first, plus the most recent picks for undo.
router.get('/orders', asyncHandler(async (req, res) => {
  const { rows: open } = await pool.query(
    `with ${ALL_CTE.trim()}
     select order_number, customer, reference, due::text as due, list
     from withpick where not picked
     order by due nulls last, customer, order_number`
  );
  const { rows: recent } = await pool.query(
    `select s.order_number, s.picked_by, s.picked_at, coalesce(k.customer, p.customer) as customer
     from board.so_picks s
     left join board.plan_orders p on p.order_number = s.order_number
     left join board.packaging_orders k on k.order_number = s.order_number
     order by s.picked_at desc limit 30`
  );
  res.json({ open, recent });
}));

// Same normalization as the dispatch plan overrides: a hand-typed "16633"
// should match Cin7's "SO-16633".
function normalizeOrderNumber(v) {
  const trimmed = String(v || '').trim().toUpperCase();
  if (trimmed && !trimmed.startsWith('SO-') && /^[0-9-]+$/.test(trimmed) && /\d/.test(trimmed)) {
    return `SO-${trimmed}`;
  }
  return trimmed;
}

router.post('/picks', asyncHandler(async (req, res) => {
  const orderNumber = normalizeOrderNumber(req.body?.order_number);
  const pickedBy = String(req.body?.picked_by || '').trim().slice(0, 80) || null;
  if (!orderNumber || orderNumber.length > 40) {
    throw new ApiError(400, 'Choose or type an SO number');
  }
  const { rows } = await pool.query(
    `insert into board.so_picks (order_number, picked_by) values ($1, $2)
     on conflict (order_number) do nothing
     returning *`,
    [orderNumber, pickedBy]
  );
  if (!rows[0]) {
    throw new ApiError(409, `${orderNumber} is already marked as picked`);
  }
  res.status(201).json({ pick: rows[0] });
}));

router.delete('/picks/:orderNumber', asyncHandler(async (req, res) => {
  await pool.query('delete from board.so_picks where order_number = $1', [normalizeOrderNumber(req.params.orderNumber)]);
  res.status(204).end();
}));

router.get('/staff', asyncHandler(async (req, res) => {
  const { rows } = await pool.query('select name from board.staff where active order by name');
  res.json({ staff: rows.map((r) => r.name) });
}));

router.post('/staff', asyncHandler(async (req, res) => {
  const name = String(req.body?.name || '').trim().replace(/\s+/g, ' ').slice(0, 80);
  if (!name) throw new ApiError(400, 'Enter a name');
  await pool.query(
    `insert into board.staff (name) values ($1)
     on conflict (name) do update set active = true`,
    [name]
  );
  res.status(201).json({ name });
}));

module.exports = router;
