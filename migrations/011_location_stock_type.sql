-- 011_location_stock_type.sql
-- Adds RM/SA/FP as a real, filterable field on each location, rather
-- than leaving it as a naming convention embedded in the location code.
-- See production/README.md "Labels & warehouse locations".
--
-- Nullable rather than required: existing locations (if any were already
-- created before this migration ran) don't suddenly need backfilling to
-- keep working -- an unset stock_type just means "not categorised yet",
-- not an error.

alter table warehouse.locations
  add column stock_type text check (stock_type in ('RM', 'SA', 'FP'));

create index on warehouse.locations (stock_type);
