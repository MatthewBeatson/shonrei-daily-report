-- 009_stocktake.sql
-- Stocktake, replacing the old Google Sheet + AppSheet workflow (see
-- production/README.md "Stocktake" section). One table: a plain log of
-- physical counts, each optionally reconciled against a Cin7 on-hand
-- snapshot taken at count time, and optionally pushed to Cin7 as a stock
-- adjustment once someone's reviewed the variance.
--
-- Deliberately NOT modelled as a Cin7-style "stocktake session" object
-- (open a session, count every line, close it) -- AppSheet's actual usage
-- was a rolling log of ad-hoc counts, any SKU, any time, and that's what
-- this preserves. A count is reviewed and pushed individually, not as a
-- batch close-out.

create schema if not exists stocktake;

create table stocktake.counts (
  id uuid primary key default gen_random_uuid(),
  sku text not null,
  location text,                     -- nullable -- single-location today, room for more later
  counted_qty numeric not null check (counted_qty >= 0),
  cin7_on_hand_snapshot numeric,     -- Cin7's on-hand for this SKU at count time, null if not pulled
  variance numeric generated always as (
    case when cin7_on_hand_snapshot is null then null
         else counted_qty - cin7_on_hand_snapshot end
  ) stored,
  status text not null default 'recorded' check (status in ('recorded', 'adjusted', 'ignored')),
  reported_via text not null default 'manual' check (reported_via in ('barcode', 'manual')),
  reported_by text,
  cin7_adjustment_id text,           -- null until the (currently dry-run) Cin7 adjustment call is live
  counted_at timestamptz not null default now(),
  adjusted_at timestamptz
);
create index on stocktake.counts (sku);
create index on stocktake.counts (status);
create index on stocktake.counts (counted_at desc);
