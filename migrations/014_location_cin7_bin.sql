-- 014_location_cin7_bin.sql
-- Links a warehouse.locations row (one of our own counting
-- areas/shelves) to the real Cin7 Bin it now corresponds to
-- (Settings > Reference Books > Locations > Bins, under the one
-- "Shonrei factory/main warehouse" Location) -- see production/README.md
-- "Stocktake" for the confirmed live findings this is built on
-- (2026-09-16): Cin7 tracks quantity per bin as a real, separate figure
-- (not a label), and a stock adjustment line's `Bin` value resolves into
-- that bin's own distinct location entity.
--
-- Nullable, like stock_type (migration 011): a location created before
-- its matching Cin7 Bin exists still works for everything else
-- (putaway, printed labels) -- it just can't be counted against in a
-- formal stocktake sync until this is set, same "not categorised yet"
-- pattern as stock_type.
--
-- Once set, "scan this area's barcode" and "scan this Cin7 Bin" are the
-- same action -- see stocktake.py's module docstring for the design
-- this unlocks (each area's count syncs straight to its own bin, no
-- in-app aggregation across areas needed).

alter table warehouse.locations
  add column cin7_bin text;
