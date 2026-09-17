-- 016_location_bay_code.sql
-- Groups shelf-level locations into their parent bay -- e.g. SRM-B1-04
-- and SRM-B1-07 both get bay_code 'SRM-B1'. An explicit field, not
-- parsed from the location code string, so it's reliable regardless of
-- naming quirks (confirmed choice, 2026-09-17).
--
-- Used only to classify a Putaway mismatch as same-bay (wrong shelf,
-- close enough -- always just a warning) vs a different bay entirely
-- (respects the admin-configured putaway_mismatch_mode, migration 015)
-- -- see production/planner/putaway.py's check_putaway and
-- refresh-service/warehouse.py's record_putaway_scan.
--
-- Nullable, same pattern as stock_type (migration 011) and cin7_bin
-- (migration 014): a location with no bay_code set just means "not
-- grouped into a bay" -- any mismatch involving it is treated as a
-- different bay (the cautious default when it can't be determined),
-- not an error.

alter table warehouse.locations
  add column bay_code text;

create index on warehouse.locations (bay_code);
