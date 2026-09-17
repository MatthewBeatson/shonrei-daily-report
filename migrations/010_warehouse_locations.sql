-- 010_warehouse_locations.sql
-- Bin/location tracking + putaway confirmation -- the fix for the
-- "product placed anywhere" problem (see production/README.md
-- "Labels & warehouse locations"). Deliberately the simplest model that
-- solves the actual complaint: one designated home location per SKU, and
-- an audit trail of every scan confirming (or not) that a product landed
-- where it's supposed to. Not a full multi-bin quantity-tracking WMS --
-- that's a real option later if one home per SKU stops being enough, but
-- it's more scanning discipline for staff than the problem currently
-- calls for.

create schema if not exists warehouse;

create table warehouse.locations (
  id uuid primary key default gen_random_uuid(),
  code text not null unique,        -- printed on the location's own label, e.g. "A-03-02"
  description text,                 -- e.g. "Aisle A, Bay 3, Shelf 2"
  created_at timestamptz not null default now()
);

create table warehouse.sku_locations (
  sku text primary key,
  location_id uuid not null references warehouse.locations(id),
  updated_at timestamptz not null default now()
);

create or replace function warehouse.touch_sku_location()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

create trigger sku_locations_touch
  before update on warehouse.sku_locations
  for each row execute function warehouse.touch_sku_location();

-- Every putaway/relocate scan from the floor app -- scan the product's
-- SKU barcode, then scan the bin's location barcode. A row here is
-- written whether or not it matched; mismatches are the whole point --
-- they're what the old system had no way of catching.
create table warehouse.putaway_scans (
  id uuid primary key default gen_random_uuid(),
  sku text not null,
  scanned_location_code text not null,   -- what the bin's label actually said
  expected_location_id uuid references warehouse.locations(id),  -- null if this SKU has no home location set yet
  matched boolean not null,
  scanned_by text,
  scanned_at timestamptz not null default now()
);
create index on warehouse.putaway_scans (sku);
create index on warehouse.putaway_scans (matched);
create index on warehouse.putaway_scans (scanned_at desc);
