-- 013_stocktake_areas.sql
-- Area-based counting for the formal Cin7 Stocktake workflow (ST-XXXXX),
-- alongside the existing rolling ad-hoc count log -- see
-- production/README.md "Stocktake" for the full design and what's still
-- live-unconfirmed.
--
-- No change needed to stocktake.counts itself: its `location` column
-- (migration 009) already exists and is exactly what per-area counting
-- needs -- it now holds the scanned warehouse.locations.code for that
-- count, so multiple rows for the same SKU (one per area a staff member
-- counted it in) already work with zero schema change.
--
-- The only new piece is remembering which Cin7 Stocktake (there is only
-- ever one "IN PROGRESS" at a time, started manually by admin in Cin7's
-- own UI, not from this app) counts should be synced against, and
-- letting admin update that at any point during the stocktake cycle.
-- Singleton-row pattern (id boolean primary key, always true) since
-- there's only ever one active value, not a real table of rows.

create table stocktake.settings (
  id boolean primary key default true check (id),
  active_cin7_stocktake_number text,
  updated_at timestamptz not null default now(),
  updated_by text
);
insert into stocktake.settings (id) values (true);

create or replace function stocktake.touch_settings()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

create trigger settings_touch
  before update on stocktake.settings
  for each row execute function stocktake.touch_settings();
