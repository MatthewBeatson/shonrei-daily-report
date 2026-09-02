-- 006_dispatch_plan.sql
-- Monthly Dispatch Plan: automated replacement for the manually-maintained
-- "<MONTH> ORDERS PLAN" Excel workbook.
--
-- Three tables, same conventions as the rest of the `reporting` schema:
--   - dispatch_plan_overrides: long-lived, keyed by Cin7 SO# (order_number),
--     survives every weekly rebuild since the rebuild re-reads this table
--     after each fresh Cin7 pull. Lets a report_users editor relabel/split
--     an auto-grouped line (e.g. "Grouped (28-Apr) - SYDNEY ORDERS" vs
--     "... QLD ORDERS" in the real workbook) or flag an order into the
--     Holding section by hand -- Cin7 has no field that reliably
--     distinguishes "genuinely stuck" from Shonrei's normal make-to-order/
--     backorder flow, so that's a manual call, not something inferred.
--   - dispatch_plan_current: singleton row (same pattern as manual_inputs /
--     refresh_state) pointing at the latest generated workbook in Supabase
--     Storage. Each Monday's run overwrites this row -- full rebuild, not
--     an incremental update, matching the daily-refresh snapshot model
--     except this one doesn't keep history rows (the workbook itself, one
--     file per run if kept, is the history; dispatch_plan_log is the
--     lightweight step-by-step audit trail).
--   - dispatch_plan_log: same shape as refresh_log, for troubleshooting a
--     run that failed partway through.

-- ============================================================
-- Overrides (editable by can_edit_report() users)
-- ============================================================
create table reporting.dispatch_plan_overrides (
  order_number text primary key,        -- Cin7 SO# (OrderNumber), stable across rebuilds
  group_label_override text,            -- replaces the auto "Customer + date" group label when set
  hold boolean not null default false,  -- true = always place in the Holding section, never scheduled
  note text,
  updated_by uuid references reporting.report_users(id),
  updated_at timestamptz not null default now()
);

create or replace function reporting.touch_dispatch_plan_override()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

create trigger trg_dispatch_plan_overrides_touch
  before update on reporting.dispatch_plan_overrides
  for each row execute function reporting.touch_dispatch_plan_override();

-- ============================================================
-- Latest generated workbook (singleton)
-- ============================================================
create table reporting.dispatch_plan_current (
  id text primary key default 'current' check (id = 'current'),
  storage_path text,                    -- path within the Supabase Storage 'dispatch-plans' bucket
  generated_at timestamptz,
  months_covered text[],                -- e.g. {'2026-06','2026-07'}, in order
  status text not null default 'error', -- 'ok' | 'error'
  triggered_by uuid references reporting.report_users(id)  -- null = scheduled Monday run
);

insert into reporting.dispatch_plan_current (id) values ('current')
  on conflict (id) do nothing;

-- ============================================================
-- Run log (replaces nothing -- there's no manual equivalent, this is new
-- visibility the Excel workflow never had)
-- ============================================================
create table reporting.dispatch_plan_log (
  id uuid primary key default gen_random_uuid(),
  ts timestamptz not null default now(),
  step text not null,
  status text not null,      -- 'OK' | 'ERROR'
  detail text,
  triggered_by uuid references reporting.report_users(id)
);

create index idx_dispatch_plan_log_ts on reporting.dispatch_plan_log(ts desc);

-- ============================================================
-- Settings addition -- editable target without a code change
-- ============================================================
insert into reporting.settings (key, value) values
  ('dispatch_plan_monthly_target', '220000')
on conflict (key) do nothing;

-- ============================================================
-- Row Level Security
-- ============================================================
alter table reporting.dispatch_plan_overrides enable row level security;
alter table reporting.dispatch_plan_current enable row level security;
alter table reporting.dispatch_plan_log enable row level security;

-- ---- dispatch_plan_overrides ----
create policy "report users read dispatch plan overrides"
  on reporting.dispatch_plan_overrides for select
  using (reporting.is_report_user());

create policy "editors manage dispatch plan overrides"
  on reporting.dispatch_plan_overrides for all
  using (reporting.can_edit_report())
  with check (reporting.can_edit_report());

-- ---- dispatch_plan_current ----
-- Read-only from the client; only the refresh service (service_role,
-- bypasses RLS) writes it, same as report_snapshots.
create policy "report users read dispatch plan current"
  on reporting.dispatch_plan_current for select
  using (reporting.is_report_user());

-- ---- dispatch_plan_log ----
create policy "report users read dispatch plan log"
  on reporting.dispatch_plan_log for select
  using (reporting.is_report_user());
