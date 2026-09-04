-- 007_production_schema.sql
-- Production MRP / assembly-automation prototype schema (see
-- production/README.md). Not yet run against any Supabase project --
-- included so the table shapes referenced by production/planner/
-- orchestrator.py exist somewhere concrete, and to keep the same
-- "migrations run once each, in order" convention as the `reporting`
-- schema.
--
-- Four tables:
--   - production_runs: this repo's own source of truth for "what is
--     actually happening on the floor", one row per build step out of a
--     BOM explosion. Cin7's assembly object is generated FROM this, not
--     the other way round -- Cin7 has no multi-level-BOM or partial-
--     completion concept, so it can't be the primary model.
--   - run_assembly_map: which Cin7 AssemblyID(s) a run resulted in, once
--     Create/Authorise/Allocate has run. Kept separate (not a column on
--     production_runs) because a run could in principle map to more than
--     one Cin7 assembly over its life (e.g. a failed allocation retried).
--   - run_actuals: the one human input this whole thing waits on --
--     actual quantity made, reported by the floor app. The orchestrator
--     applies this as the Cin7 Complete call the moment it lands.
--   - bom_cache: last-known BOM tree per SKU, refreshed each planning
--     pass. Exists so explode_bom's `bom` argument doesn't require a live
--     Cin7 call per SKU per explosion -- BOMs change rarely.

create schema if not exists production;

create table production.production_runs (
  id uuid primary key default gen_random_uuid(),
  plan_batch_id uuid not null,          -- groups every run from one planning pass together
  sku text not null,
  qty_to_build numeric not null check (qty_to_build > 0),
  bom_level int not null,               -- 0 = top-level demand, higher = deeper component
  status text not null default 'planned'
    check (status in ('planned', 'allocated', 'completed', 'cancelled')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index on production.production_runs (status);
create index on production.production_runs (plan_batch_id);

create table production.run_assembly_map (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references production.production_runs(id),
  cin7_assembly_id text not null,
  cin7_status text not null,            -- DRAFT | AUTHORISED | ALLOCATED | COMPLETED, mirrors Cin7
  created_at timestamptz not null default now()
);
create index on production.run_assembly_map (run_id);

create table production.run_actuals (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references production.production_runs(id),
  actual_qty numeric not null check (actual_qty >= 0),
  reject_qty numeric not null default 0 check (reject_qty >= 0),
  reported_via text not null default 'floor_app',  -- 'floor_app' | 'ocr_fallback' | 'manual'
  reported_by text,                      -- free text; floor staff, no login system assumed
  reported_at timestamptz not null default now()
);
create index on production.run_actuals (run_id);

create table production.bom_cache (
  sku text primary key,
  bom_lines jsonb not null,             -- [{component_sku, qty_per}, ...], empty array = raw material
  refreshed_at timestamptz not null default now()
);

create or replace function production.touch_production_run()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

create trigger production_runs_touch
  before update on production.production_runs
  for each row execute function production.touch_production_run();
