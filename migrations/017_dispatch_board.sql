-- 017_dispatch_board.sql
-- Dispatch Board: the dispatch-area TV (/board/) and the staff KPI entry
-- page (/kpi/).
--
-- Its own `board` schema, deliberately apart from `reporting`: the TV and
-- the KPI page are opened by warehouse staff with a shared access code
-- (DISPATCH_BOARD_SECRET), not a report_users login, so nothing they can
-- reach may contain a dollar figure or anything from the daily report.
-- The backend's /board/api routes only ever read from this schema (plus
-- production.batch_actuals' unit counts) -- never from reporting.*.
--
--   - plan_orders: one row per SO from the latest Monthly Dispatch Plan
--     run (refresh-service/dispatch_plan_run.py rewrites it in full each
--     run). Units and line counts instead of $ -- no value column at all.
--   - packaging_orders: open packaging SOs (any line SKU starting 'K') for
--     the board's two packaging lists -- plain (next working day) and
--     printed (a "Printed ..." additional charge, 5 working days).
--     Rewritten by every hourly refresh (refresh-service/board_packaging.py),
--     not just Monday's plan run, since a next-day deadline can't wait.
--   - so_picks: "SO picking done" (green sheet) ticks entered on /kpi/.
--     One row per SO; deleting the row is the undo.
--   - staff: the names in /kpi/'s "Picked by" dropdown.

create schema if not exists board;

create table board.plan_orders (
  order_number text primary key,
  customer text,
  reference text,
  group_label text,          -- the plan's group line, only when the SO is part of a multi-SO group
  order_date date,
  dispatch_date date,        -- the plan's DISPATCH DATE; null when held
  assigned_month text,       -- 'YYYY-MM'
  assigned_week int,
  hold boolean not null default false,
  units_remaining numeric,   -- ordered qty minus invoiced qty
  line_count int,
  is_packaging boolean not null default false,  -- shown in the packaging lists instead of the weekly plan
  generated_at timestamptz not null default now()
);
create index on board.plan_orders (dispatch_date);

create table board.packaging_orders (
  order_number text primary key,
  customer text,
  reference text,
  order_date date,
  printed boolean not null,  -- has a printing additional charge
  deadline date not null,    -- order_date + 1 (plain) or 5 (printed) NZ working days
  units_remaining numeric,   -- packaging (K) lines only
  line_count int,
  refreshed_at timestamptz not null default now()
);
create index on board.packaging_orders (deadline);

create table board.staff (
  id uuid primary key default gen_random_uuid(),
  name text not null unique,
  active boolean not null default true,
  created_at timestamptz not null default now()
);

create table board.so_picks (
  order_number text primary key,
  picked_by text,
  picked_at timestamptz not null default now()
);
create index on board.so_picks (picked_at);

-- Unit counts on the shared Cin7 cache, so the plan run gets them from the
-- same sale-detail fetch it already makes (null on rows cached before
-- this -- dispatch_plan_data refetches those once).
alter table reporting.cin7_sale_cache add column if not exists units_ordered numeric;
alter table reporting.cin7_sale_cache add column if not exists units_invoiced numeric;
alter table reporting.cin7_sale_cache add column if not exists line_count int;
-- {SKU: ordered minus invoiced qty} and Order.AdditionalCharges[].Description,
-- so packaging/printing classification can be redone against the current
-- settings below without refetching from Cin7.
alter table reporting.cin7_sale_cache add column if not exists sku_remaining jsonb;
alter table reporting.cin7_sale_cache add column if not exists charge_descriptions text[];

-- Packaging list rules, editable without a deploy. Lists are '|'-separated,
-- same as the other multi-value settings. Matching is case-insensitive.
insert into reporting.settings (key, value) values
  ('board_packaging_sku_prefixes', 'K'),
  ('board_printing_charge_keywords', 'PRINT'),
  ('board_packaging_plain_days', '1'),
  ('board_packaging_printed_days', '5')
on conflict (key) do nothing;

-- Same stance as cin7_sale_cache: RLS on, no policies -- only the backend
-- and refresh service (direct Postgres, table owner) touch these.
alter table board.plan_orders enable row level security;
alter table board.staff enable row level security;
alter table board.packaging_orders enable row level security;
alter table board.so_picks enable row level security;
