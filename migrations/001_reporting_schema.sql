-- 001_reporting_schema.sql
-- Shonrei Daily Management Summary: reporting schema.
--
-- CORRECTION (2026-09-03): this comment originally claimed this migration
-- runs against the SAME Supabase project as the Ordering Portal (JPL) repo
-- (vwbbkkwzehkfhurhluza). That was wrong -- this app has always had its
-- own separate project (zkwbapuclczezoxxtevk); the two were never shared.
-- Left here (rather than rewritten) since this migration already ran --
-- see README.md for the corrected, current description. The rest of this
-- comment (own `reporting` schema, own disjoint report_users) still holds.
--
-- Lives in its own Postgres schema, `reporting`, which is never referenced
-- by and never references the ordering-portal's `public` schema (a
-- different project entirely). Auth is this project's own Supabase Auth
-- user pool (auth.users); authorization is gated by membership in
-- reporting.report_users, not by anything in
-- public.users / user_store_roles / user_client_roles / is_portal_admin().
--
-- Run once via the session pooler connection (see .env.example) — this
-- project has no working Supabase CLI in the dev environment, so apply via
-- scripts/run_migration.py (psycopg2) or paste into the Supabase SQL editor.

create schema if not exists reporting;

-- ============================================================
-- Report users
-- Allowlist of who may view the Shonrei daily report. can_edit governs
-- the manual inputs (projected sales / breakeven / commentary). is_admin
-- is a super-admin flag (manage report_users + settings) — Matthew only.
-- ============================================================
create table reporting.report_users (
  id uuid primary key references auth.users(id) on delete cascade,
  email text not null,
  full_name text,
  can_edit boolean not null default false,
  is_admin boolean not null default false,
  created_at timestamptz not null default now()
);

create or replace function reporting.is_report_user()
returns boolean as $$
  select exists (select 1 from reporting.report_users where id = auth.uid());
$$ language sql security definer stable;

create or replace function reporting.can_edit_report()
returns boolean as $$
  select coalesce(
    (select can_edit or is_admin from reporting.report_users where id = auth.uid()),
    false
  );
$$ language sql security definer stable;

create or replace function reporting.is_report_admin()
returns boolean as $$
  select coalesce(
    (select is_admin from reporting.report_users where id = auth.uid()),
    false
  );
$$ language sql security definer stable;

-- ============================================================
-- Report snapshots
-- One row per refresh (scheduled or on-demand). The most recent row is
-- what the dashboard shows "live"; the full table is the trend history
-- (replaces the old "Daily History" Excel sheet). Derived/calculated
-- figures (net short-term position, variance to breakeven, working
-- capital ratio, etc.) are NOT stored here — they're computed from a
-- snapshot + the current manual_inputs at read time, same as the Excel
-- formulas did, so there's one source of truth instead of two.
-- ============================================================
create table reporting.report_snapshots (
  id uuid primary key default gen_random_uuid(),
  as_of timestamptz not null default now(),

  bank_balance numeric,
  bank_status text not null default 'error',           -- 'ok' | 'error'

  sales_mtd numeric,
  sales_prev_month numeric,
  sales_status text not null default 'error',

  sales_on_hand numeric,
  sales_on_hand_status text not null default 'error',

  debtors_total numeric,
  debtors_not_due numeric,
  debtors_overdue numeric,
  debtors_status text not null default 'error',

  creditors_total numeric,
  creditors_nzd_payables numeric,
  creditors_status text not null default 'error',

  overall_status text not null default 'error',        -- 'ok' | 'partial' | 'error'
  triggered_by uuid references reporting.report_users(id), -- null = scheduled cron

  created_at timestamptz not null default now()
);

create index idx_report_snapshots_as_of on reporting.report_snapshots(as_of desc);

-- ============================================================
-- Manual inputs (singleton row) + audit history
-- Replaces the blue "management input" cells in Excel. Only can_edit_report()
-- users may update the live row; every update is copied into
-- manual_inputs_history first via trigger, with updated_by forced from
-- auth.uid() (never trusted from the client payload).
-- ============================================================
create table reporting.manual_inputs (
  id text primary key default 'current' check (id = 'current'),
  projected_sales numeric,
  monthly_breakeven numeric,
  commentary text,
  updated_by uuid references reporting.report_users(id),
  updated_at timestamptz not null default now()
);

insert into reporting.manual_inputs (id) values ('current')
  on conflict (id) do nothing;

create table reporting.manual_inputs_history (
  id uuid primary key default gen_random_uuid(),
  projected_sales numeric,
  monthly_breakeven numeric,
  commentary text,
  updated_by uuid references reporting.report_users(id),
  updated_at timestamptz not null default now()
);

create index idx_manual_inputs_history_updated_at on reporting.manual_inputs_history(updated_at desc);

create or replace function reporting.log_manual_inputs_change()
returns trigger as $$
begin
  new.updated_by = auth.uid();
  new.updated_at = now();
  insert into reporting.manual_inputs_history
    (projected_sales, monthly_breakeven, commentary, updated_by, updated_at)
    values (new.projected_sales, new.monthly_breakeven, new.commentary, new.updated_by, new.updated_at);
  return new;
end;
$$ language plpgsql security definer;

create trigger trg_manual_inputs_audit
  before update on reporting.manual_inputs
  for each row execute function reporting.log_manual_inputs_change();

-- ============================================================
-- Refresh log (replaces the "Refresh Log" Excel sheet) and refresh_state
-- (single-row status used for the 5-minute on-demand debounce + to let the
-- frontend show "Refreshing...").
-- ============================================================
create table reporting.refresh_log (
  id uuid primary key default gen_random_uuid(),
  ts timestamptz not null default now(),
  source text not null,      -- 'Xero' | 'Cin7 Core'
  step text not null,
  status text not null,      -- 'OK' | 'ERROR'
  detail text,
  triggered_by uuid references reporting.report_users(id)
);

create index idx_refresh_log_ts on reporting.refresh_log(ts desc);

create table reporting.refresh_state (
  id text primary key default 'current' check (id = 'current'),
  status text not null default 'idle',   -- 'idle' | 'running'
  started_at timestamptz,
  last_completed_at timestamptz,
  last_triggered_by uuid references reporting.report_users(id)
);

insert into reporting.refresh_state (id) values ('current')
  on conflict (id) do nothing;

-- ============================================================
-- Cin7 sale cache
-- Replaces the local cin7_sales_cache.json file so the cache survives
-- redeploys/restarts and works from a stateless Render service. Internal
-- to the refresh service only — no client-facing RLS select policy.
-- ============================================================
create table reporting.cin7_sale_cache (
  sale_id text primary key,
  signature text not null,
  base_value numeric,
  customer_value numeric,
  rate numeric,
  order_before_tax numeric,
  invoiced_before_tax numeric,
  credited_before_tax numeric,
  order_number text,
  status text,
  order_status text,
  invoice_status text,
  cached_at timestamptz not null default now()
);

-- ============================================================
-- Settings (non-secret config only — mirrors the Excel "Settings" sheet
-- minus anything sensitive; Xero client secret/refresh token and the Cin7
-- API key stay in Render environment variables, never in this table).
-- ============================================================
create table reporting.settings (
  key text primary key,
  value text,
  updated_at timestamptz not null default now()
);

insert into reporting.settings (key, value) values
  ('timezone', 'Pacific/Auckland'),
  ('xero_tenant_id', 'c0500179-97d4-40a7-bf6f-1b103c59e399'),
  ('xero_pnl_income_row_labels', 'Total Trading Income'),
  ('xero_bank_account_names', 'ASB - NZ Commercial Flexible Finance Account|ANZ - AU Bank - Shonrei Products Ltd.'),
  ('cin7_order_statuses', 'ORDERED|BACKORDERED'),
  ('cin7_invoice_statuses', 'DRAFT|NOT AVAILABLE|NOT INVOICED|PARTIALLY INVOICED'),
  ('refresh_window_start_hour', '7'),
  ('refresh_window_end_hour', '19'),
  ('refresh_min_interval_minutes', '5')
on conflict (key) do nothing;

-- ============================================================
-- Row Level Security
-- ============================================================
alter table reporting.report_users enable row level security;
alter table reporting.report_snapshots enable row level security;
alter table reporting.manual_inputs enable row level security;
alter table reporting.manual_inputs_history enable row level security;
alter table reporting.refresh_log enable row level security;
alter table reporting.refresh_state enable row level security;
alter table reporting.cin7_sale_cache enable row level security;
alter table reporting.settings enable row level security;

-- ---- report_users ----
create policy "select own row or admin sees all"
  on reporting.report_users for select
  using (id = auth.uid() or reporting.is_report_admin());

create policy "admins manage report_users"
  on reporting.report_users for all
  using (reporting.is_report_admin())
  with check (reporting.is_report_admin());

-- ---- report_snapshots ----
-- Read-only from the client; only the refresh service (service_role,
-- bypasses RLS) inserts rows.
create policy "report users read snapshots"
  on reporting.report_snapshots for select
  using (reporting.is_report_user());

-- ---- manual_inputs ----
create policy "report users read manual inputs"
  on reporting.manual_inputs for select
  using (reporting.is_report_user());

create policy "editors update manual inputs"
  on reporting.manual_inputs for update
  using (reporting.can_edit_report())
  with check (reporting.can_edit_report());

-- ---- manual_inputs_history ----
create policy "report users read manual inputs history"
  on reporting.manual_inputs_history for select
  using (reporting.is_report_user());

-- ---- refresh_log ----
create policy "report users read refresh log"
  on reporting.refresh_log for select
  using (reporting.is_report_user());

-- ---- refresh_state ----
create policy "report users read refresh state"
  on reporting.refresh_state for select
  using (reporting.is_report_user());

-- Note: no insert/update policy on refresh_state for authenticated clients.
-- The Node backend's /reporting/refresh route flips it to 'running' and the
-- Python refresh service flips it back to 'idle' — both via service_role.

-- ---- cin7_sale_cache ----
-- No select/insert policy at all: this table is purely internal to the
-- refresh service (service_role), never read by the frontend.

-- ---- settings ----
create policy "admins read settings"
  on reporting.settings for select
  using (reporting.is_report_admin());

create policy "admins update settings"
  on reporting.settings for update
  using (reporting.is_report_admin())
  with check (reporting.is_report_admin());
