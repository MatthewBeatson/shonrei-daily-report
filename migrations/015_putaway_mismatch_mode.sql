-- 015_putaway_mismatch_mode.sql
-- Admin-configurable: what happens in the floor app when a Putaway scan
-- lands in a DIFFERENT BAY than a SKU's assigned home location --
-- confirmed design, 2026-09-17 (see production/README.md "Labels &
-- warehouse locations"). Scoped to wrong-bay mismatches only: a
-- same-bay-but-wrong-shelf mismatch (see migration 016's bay_code)
-- ALWAYS just warns, regardless of this setting -- close enough, staff
-- can continue. This setting only decides how strict wrong-bay
-- enforcement is.
--
-- 'warn' (default): a wrong-bay mismatch is shown and logged, but staff
--   can continue past it immediately (matches the floor app's original,
--   pre-this-feature behaviour for every mismatch).
-- 'block': the floor app refuses to let staff move on to the next SKU
--   until a wrong-bay scan actually matches -- each attempt is still
--   logged (warehouse.putaway_scans keeps every scan either way,
--   matches or not -- see migration 010's comment, "mismatches are the
--   whole point" of logging still holds), it just won't show a "done"
--   screen for a mismatched one.
--
-- Singleton-row pattern, same as stocktake.settings (migration 013) --
-- one value, not a real table of rows.

create table warehouse.settings (
  id boolean primary key default true check (id),
  putaway_mismatch_mode text not null default 'warn' check (putaway_mismatch_mode in ('warn', 'block')),
  updated_at timestamptz not null default now(),
  updated_by text
);
insert into warehouse.settings (id) values (true);

create or replace function warehouse.touch_settings()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

create trigger settings_touch
  before update on warehouse.settings
  for each row execute function warehouse.touch_settings();
