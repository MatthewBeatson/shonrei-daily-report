-- 008_backorder_targets_and_batches.sql
-- Backorder-priority production model (see production/README.md
-- "Backorder targets and batches" section for the full design). Adds
-- three new concepts alongside the existing production.production_runs
-- (which stays as-is for the general multi-level-BOM explosion path):
--
--   - targets: one long-lived row per FG SKU currently affected by SO
--     backorders. Mirrors a single Cin7 assembly sitting in Authorised
--     status whose quantity tracks total outstanding backorder demand --
--     Cin7 itself is never allocated/completed against this assembly,
--     it only ever exists to represent "how much of this SKU is still
--     owed to open orders" in a form Cin7's own reporting can see.
--     Closed out (status='closed') when outstanding_qty reaches 0, and a
--     fresh target is created the next time backorder demand reappears
--     for that SKU -- see production/README.md for why not to reuse it.
--   - target_demand_lines: which SO lines make up a target's current
--     outstanding_qty, refreshed on every sync pass. Priority order
--     (oldest order date first) drives how production_batches get split.
--   - production_batches: the actual unit of work communicated to the
--     factory floor -- small, physically-run-sized "sublists" that are
--     LIST STATE ONLY, never their own Cin7 object. A batch's qty comes
--     out of one target's outstanding demand.
--   - batch_so_lines: which SO(s)/qty a batch is meant to cover, purely
--     for factory-facing traceability ("why are we making this").
--   - batch_actuals: the one human input this whole thing waits on --
--     actual quantity a batch produced. Recording one is what creates a
--     real small Cin7 FG (Create->Authorise->Allocate->Complete for that
--     actual qty, see cin7_client.py) and decrements the parent target.

create table production.targets (
  id uuid primary key default gen_random_uuid(),
  sku text not null,
  status text not null default 'active' check (status in ('active', 'closed')),
  outstanding_qty numeric not null default 0 check (outstanding_qty >= 0),
  cin7_assembly_id text,          -- null until the (currently dry-run) Cin7 create call is live
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  closed_at timestamptz
);
-- Only one *active* target per SKU at a time -- a closed one doesn't
-- block a fresh target being created later for the same SKU.
create unique index targets_one_active_per_sku on production.targets (sku) where status = 'active';

create or replace function production.touch_target()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

create trigger targets_touch
  before update on production.targets
  for each row execute function production.touch_target();

create table production.target_demand_lines (
  id uuid primary key default gen_random_uuid(),
  target_id uuid not null references production.targets(id),
  so_number text not null,
  order_date date,
  qty_backordered numeric not null check (qty_backordered > 0),
  priority_rank int not null,     -- 1 = highest priority (oldest order_date first)
  synced_at timestamptz not null default now()
);
create index on production.target_demand_lines (target_id);

create table production.production_batches (
  id uuid primary key default gen_random_uuid(),
  target_id uuid not null references production.targets(id),
  batch_code text not null unique,   -- short, barcode-friendly (e.g. "B-000042")
  qty_planned numeric not null check (qty_planned > 0),
  priority_rank int not null,        -- lower = do first, inherited from the demand lines it covers
  status text not null default 'planned'
    check (status in ('planned', 'issued', 'completed', 'cancelled')),
  created_at timestamptz not null default now(),
  issued_at timestamptz
);
create index on production.production_batches (target_id);
create index on production.production_batches (status);

create table production.batch_so_lines (
  id uuid primary key default gen_random_uuid(),
  batch_id uuid not null references production.production_batches(id),
  so_number text not null,
  qty_allocated numeric not null check (qty_allocated > 0)
);
create index on production.batch_so_lines (batch_id);

-- Every call DryRunCin7Client would otherwise have made against live
-- Cin7 -- see refresh-service/dry_run_cin7.py. Purely observational; lets
-- a planner sanity-check "what would this have done to Cin7" before the
-- real endpoints are wired in.
create table production.cin7_dry_run_log (
  id uuid primary key default gen_random_uuid(),
  action text not null,
  details jsonb not null,
  logged_at timestamptz not null default now()
);

create table production.batch_actuals (
  id uuid primary key default gen_random_uuid(),
  batch_id uuid not null unique references production.production_batches(id),
  actual_qty numeric not null check (actual_qty >= 0),
  reject_qty numeric not null default 0 check (reject_qty >= 0),
  reported_via text not null default 'manual' check (reported_via in ('barcode', 'manual')),
  reported_by text,
  cin7_small_assembly_id text,    -- null until the (currently dry-run) small-FG completion is live
  reported_at timestamptz not null default now()
);
