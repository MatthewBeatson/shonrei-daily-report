-- 020_normalize_sku_location_casing.sql
-- Barcode/SKU search is now case-insensitive everywhere (2026-10-02) --
-- the app code normalizes new values to uppercase on write and compares
-- case-insensitively on read. This retroactively uppercases the two
-- columns where that actually matters: both are the target of an
-- `on conflict (...)` upsert (warehouse.set_home_location,
-- the admin "Add location" toolbar), which matches EXACTLY on the
-- unique constraint -- a pre-existing row stored in a different case
-- than a new case-insensitively-matched write would silently create a
-- DUPLICATE row instead of updating the real one. Normalizing existing
-- data first avoids that.
--
-- Every real SKU/location code in this tenant has always been typed in
-- uppercase by convention (confirmed throughout production/README.md's
-- live Cin7 examples), so this is not expected to change any row that
-- wasn't already a typo of the intended value.

update warehouse.locations set code = upper(code) where code <> upper(code);
update warehouse.sku_locations set sku = upper(sku) where sku <> upper(sku);
