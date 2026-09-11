# Production MRP & Assembly Automation (prototype)

Scaffold for the problems raised alongside the daily report and dispatch
plan work:

1. Cin7 Core's assembly module (Create -> Authorise -> Allocate -> Complete,
   "CAAC" below) has no multi-level BOM explosion, no scheduling, and no
   partial-completion model -- running it by hand for concurrent, multi-level
   production is a lot of repetitive clicking.
2. Production *inputs* (what was actually made, by whom, how much) never
   make it back into Cin7 -- staff are busy running the physical lines, not
   filling in forms. Outputs (FG creation at min-stock) are already handled
   by admin, so this scaffold doesn't touch that side.
3. Stocktake used to mean a Google Sheet plus an AppSheet front end,
   disconnected from Cin7 -- someone had to manually reconcile counts
   against Cin7's on-hand and key in any correction by hand.
4. Product physically ends up wherever there's space, not in a
   consistent bin -- there's no label or scan step confirming a product
   landed where it's supposed to. Production runs have no physical
   identifier travelling with them through the factory either, so
   in-progress batches get mixed up on the floor.

This is a **prototype layout**, not a deployed system yet -- it shows the
shape the real thing would take, following the same conventions as
`refresh-service` / `backend` / `frontend` elsewhere in this repo (a Python
worker talking to Cin7 + Supabase, a thin Node/Express API, a static
no-build frontend). When it's ready to run for real it's intended to become
its own Render service(s), deployed at `systems.shonrei.co.nz` alongside
(not replacing) this reporting app.

## Layout

```
production/
  planner/            Python: pure planning/decision logic, unit tested,
                       no Cin7 or DB calls anywhere in this directory
    bom_explode.py       Multi-level BOM -> ordered build plan (general path)
    batch_planner.py      Splits a target's priority-ordered demand into
                           factory-sized batches (backorder path)
    target_sync.py         Decides create/adjust/close per SKU + the
                           clamp-at-zero actual-vs-target math
    cin7_client.py          Documents/stubs every Cin7 call either path needs
    orchestrator.py         General-path CAAC driver (see below)
    labels.py                 ZPL text for the three label types (pure templating)
    putaway.py                 Decides whether a putaway scan matches a SKU's home
    test_*.py                Unit tests for all of the above
    requirements.txt
  floor-app/           Static HTML/JS -- three tabs, barcode-or-manual
                       entry in all of them, no build step
    index.html            Batches tab + Stocktake tab + Putaway tab
    app.js
    styles.css
  admin/               Static HTML/JS -- trigger sync/plan-batches, review
                       and push stocktake adjustments, manage locations
                       and print labels
    index.html
    app.js
    styles.css
  ../refresh-service/
    production_plan.py       General path: stages a BOM explosion
    backorder_targets.py     Backorder path: applies target_sync's decisions
                              to the DB + Cin7 (via whatever client is passed)
    batch_staging.py          Backorder path: applies batch_planner's output
    stocktake.py               Stocktake: record a count, snapshot Cin7's
                                on-hand, and (separately) push an adjustment
    warehouse.py                Locations: home-location assignment + putaway
                                 scan recording, applies putaway.py's decision
    dry_run_cin7.py            What actually runs today instead of a real
                                Cin7Client -- logs every call, never touches Cin7
    (labels served straight from app.py's /labels/* routes, no separate module)
  ../migrations/
    007_production_schema.sql                  General path (production_runs)
    008_backorder_targets_and_batches.sql       Backorder path (targets, batches)
    009_stocktake.sql                           Stocktake (stocktake.counts)
    010_warehouse_locations.sql                 Locations + putaway (warehouse.*)
    011_location_stock_type.sql                 warehouse.locations.stock_type (RM/SA/FP)
```

## How the CAAC automation works

Cin7 only understands one flat assembly at a time. The planner owns the
part Cin7 can't: knowing the *real* multi-level structure and the correct
build order.

1. **Explode.** Pull each finished good's full BOM tree via Cin7's API
   (`bom_explode.py`), and net it off current on-hand + already-open
   assemblies. Output is an ordered list of "build steps" -- sub-assemblies
   before the parents that consume them, exactly like a classic MRP pass.
   This is a pure function precisely so it can be unit tested against known
   BOM trees without touching Cin7 or a database (see
   `test_bom_explode.py`) -- the explosion logic is the part most worth
   getting right and cheapest to get wrong.
2. **Stage locally.** Each build step becomes a row in
   `production.production_runs` (this repo's own source of truth for "what
   is actually happening on the floor" -- Cin7's assembly object is treated
   as a generated record, not the primary model, because it doesn't map
   1:1 onto a real production run split across a shift or a partial build).
3. **Drive Cin7.** For each run whose prerequisite level is complete, the
   orchestrator calls Cin7's assembly API to Create, Authorise and Allocate
   automatically (`cin7_client.py`), and records the resulting Cin7
   `AssemblyID` in `production.run_assembly_map`. A run only proceeds once
   its component sub-assembly is confirmed complete in Cin7 -- no assembly
   is created against stock that doesn't exist yet.
4. **Complete on actuals, not on plan.** The Complete step is the one stage
   that legitimately needs a human number (what was actually made). That
   number comes from the floor app below, not from someone re-entering it
   into Cin7 -- the orchestrator applies it directly via the Complete API
   call the moment it lands in `production.run_actuals`.

## How the floor input works

The friction isn't the UI, it's that staff are on the line, not at a
screen. `floor-app/` is a mockup of the shape that minimises that:

- One scan (the run's QR code, printed on the day's schedule sheet) opens
  a screen pre-filled with the *planned* product and quantity for that run.
- One tap confirms "made as planned"; overtyping the quantity is the only
  path that takes more than one tap, and it's for the exception (short/over
  run), not the common case.
- Optional reject/scrap quantity field, collapsed by default.
- Submission is one entry per run/shift (at the natural stop -- changeover
  or pack-down), not continuous logging.

That entry writes straight to `production.run_actuals`, which the
orchestrator then pushes into Cin7 as the Complete call -- so the floor
never touches Cin7, and admin never re-keys anything.

## Backorder targets and batches -- how Shonrei actually runs production

The sections above cover the general case: explode a demand quantity
through a multi-level BOM and drive Cin7's CAAC lifecycle for it. In
practice Shonrei doesn't run production that way -- floor staff work
**small runs prioritised by which SO is most overdue**, not one big
assembly per SKU. This section is that real model, and it runs
**alongside** the general path above, not instead of it (min-stock-
triggered admin FG creation is untouched).

**The concept, end to end:**

1. **Target** -- one long-lived row in `production.targets` per FG SKU
   currently affected by SO backorders, mirroring a single Cin7 assembly
   sitting in **Authorised** status (created + authorised, deliberately
   never allocated) whose quantity equals total outstanding backorder
   demand for that SKU. This is what makes "how much of this SKU is
   still owed to customers" visible inside Cin7 itself, without it being
   a real production commitment yet.
2. **Demand lines** -- every SO contributing to a target's outstanding
   quantity is recorded in `production.target_demand_lines`, ranked by
   priority (oldest order date first -- the SO that's been waiting
   longest gets built first, full stop). Recomputed on every sync pass.
3. **Batches (the sublists)** -- a target's demand gets split into small,
   factory-sized batches (`production.production_batches`,
   `batch_planner.split_into_batches`) that consume the demand lines
   strictly in priority order -- a batch never covers a younger SO ahead
   of an older one, even when that means splitting one SO's line across
   two batches. **A batch is list state only -- it is never its own Cin7
   object.** This is the actual list communicated to the factory floor.
4. **Floor reports an actual** -- barcode scan or manual entry (see
   below) of a batch, then the actual quantity made. This is the only
   step that touches Cin7 for real: it completes a genuinely new, small
   Cin7 assembly (Create -> Authorise -> Allocate -> Complete, all four
   stages, for the actual qty -- `cin7_client.complete_small_assembly`)
   and decrements the parent target's outstanding quantity by that same
   amount.
5. **Target upkeep** -- three policy calls, encoded in `target_sync.py`:
   - Decrementing a target **clamps at zero, never negative** -- if the
     floor reports more than was left on the target, the surplus just
     becomes ordinary stock ahead of the next backorder wave, not a
     credit against this target.
   - A target that reaches zero is **closed** (and its Cin7 assembly
     cancelled), not kept open and reused -- the next time backorder
     demand reappears for that SKU, a fresh target and a fresh Cin7
     assembly are created. Keeps each target's life span matched to one
     wave of real demand instead of accumulating history in one object.
   - This entire flow runs **alongside** the general BOM-explosion path
     and the existing min-stock-triggered admin FG creation -- a SKU can
     have an open target for its SO-driven demand and still get a
     separate admin-created FG for general replenishment; they don't
     interact.

**Barcode or manual entry, without a scanning library.** A hardware
barcode scanner is just a keyboard: it types the scanned code into
whatever text input has focus and sends Enter. `floor-app/index.html`'s
one text field (`#batchCodeInput`) is wired to exactly that -- scan or
type a batch's code, hit Enter (or the scanner does), done. No camera
permission, no barcode-decoding library, no extra setup on the tablet.
Every batch also shows up in a plain tappable list ordered by priority,
for when there's no printed code handy.

**What's dry-run vs. real today:** the DB side (targets, demand lines,
batches, actuals, the clamp-at-zero/close-at-zero math) is fully real --
see the local end-to-end run below. Every Cin7 call
(`create_authorised_assembly`, `adjust_assembly_qty`, `close_assembly`,
`complete_small_assembly`) currently goes through
`refresh-service/dry_run_cin7.py` instead of live Cin7 -- it logs
exactly what it would have done (also written to
`production.cin7_dry_run_log` for inspection) and returns a fake
assembly ID, so the whole chain -- sync demand, create a target, split
it into batches, report an actual, watch the target close -- is provable
end to end with zero risk to live inventory. `cin7_client.py`'s real
implementations of these methods are now wired from Cin7's own
documented Finished Goods / Stock Adjustment endpoint shapes (see its
module docstring), but not yet proven against a live tenant --
`scripts/dump_sample_assembly_write.py` is the write-side counterpart to
`dump_sample_bom.py`, run once against a real throwaway assembly to
confirm before switching `DryRunCin7Client()` for a real `Cin7Client()`
here. `backorder_targets.py` and `batch_staging.py` don't care which
implementation they're given.

**Not yet wired:** `backorder_targets.extract_demand_lines()` -- turning
a Cin7 sale's full detail into per-SKU backordered quantities -- is a
hard `NotImplementedError` on purpose. `dispatch_plan_data.py` already
confirms order-level fields (OrderNumber, OrderDate, Customer, ShipBy),
but the per-line SKU/backorder-qty shape hasn't been confirmed against a
live sale detail yet. Until then, `POST /production/targets/sync` takes
`demand_lines_by_sku` directly on the request body (see the worked
example below) rather than pulling it from Cin7 automatically.

**Verified locally, real HTTP, real Postgres:** ran both migrations,
booted refresh-service + the Node backend together, and drove the whole
thing exactly as an admin and a floor tablet would --
`POST /production/targets/sync` (created a target for 80 units across 3
SOs), confirmed `SO-1002` (oldest, `2026-08-18`) got priority rank 1 despite
being listed last in the input, `POST /production/targets/:id/plan-batches`
(split into batches of 30/30/20 in priority order, correctly splitting
`SO-1001`'s line across two batches to keep the older SO's demand ahead
of the younger one), then reported actuals through
`GET /production/batches/open` -> `GET /production/batches/by-code/:code`
-> `POST /production/batch-actuals` for all three batches: the target
decremented correctly, an intentional overrun (20 planned vs. 25
reported on the last batch) clamped at zero rather than going negative,
and the target closed with `close_assembly` logged. A duplicate
submission on an already-completed batch correctly returned 409, and an
unknown batch code correctly returned 404.

## Stocktake

Replaces the old Google Sheet + AppSheet setup with a second tab in
`floor-app/` and a review step in `admin/`, connected to Cin7 via API
instead of a manual reconciliation:

1. **Count** -- floor staff scan or type any SKU (same one-field
   barcode-or-manual pattern as batch reporting, no camera library),
   enter the counted quantity, and submit. This is deliberately a
   **rolling log, not a session** -- no "open a stocktake, count every
   line, close it out" ceremony, matching how the old sheet was actually
   used: whenever someone noticed a discrepancy, not on a fixed schedule.
2. **Snapshot** -- the moment a count is recorded, `stocktake.py` also
   captures Cin7's on-hand qty for that SKU (`cin7_client.get_stock_on_hand`,
   dry-run today) as `cin7_on_hand_snapshot`, and the difference is a
   generated `variance` column -- computed once, at count time, so the
   variance shown later reflects what Cin7 said *then*, not whatever it
   says by the time someone gets around to reviewing it. A count still
   gets recorded even if the snapshot call fails -- the variance just
   comes back null, logging the count always wins.
3. **Review, then push** -- recording a count **never touches Cin7 by
   itself**. The `production-admin` screen's STOCKTAKE table lists every
   count with its variance and a "Push adjustment" button per still-
   `recorded` row; only that explicit click calls
   `cin7_client.adjust_stock_on_hand` (dry-run today) to set Cin7's
   on-hand to the counted quantity. This mirrors how a person reviewed
   AppSheet's numbers before touching Cin7 by hand -- the difference is
   the push itself is now one click instead of a manual edit, and every
   pushed count is logged (`cin7_adjustment_id`), not lost in a sheet.

**Verified locally, real HTTP, real Postgres:** recorded a count for a
SKU (42 counted against a dry-run Cin7 on-hand of 62, variance -20 written
correctly), confirmed the floor app's "last count" lookup returns it and
an uncounted SKU correctly returns null, pushed the adjustment through
the admin flow (`DryRunCin7Client` logged both the on-hand read and the
adjustment write), and confirmed a duplicate push on an already-`adjusted`
count correctly returns 409.

## Labels & warehouse locations

Shonrei's real layout: fixed shelves/areas for RM (raw material), SA
(sub-assembly), and FP (finished product), with containers on those
shelves that just carry product around -- the container itself has no
identity worth tracking, only **which shelf/area** matters. Three barcode
types, and a fix for "product placed anywhere" that matches that layout:

1. **SKU barcode** -- one per SKU, generated once, printed **identically
   everywhere that SKU needs identifying**: stuck straight onto the
   product (or its container), and printed on its own whenever admin
   downloads a SKU label. Same Code128 payload (the literal SKU text)
   every time -- no reason to mint two different codes for one SKU, and
   doing so would just be something else to keep in sync.
2. **Location barcode** -- one per shelf/area, completely independent of
   whichever SKU(s) currently live there. A shelf's code doesn't move
   when product does, and Shonrei's shelves/areas routinely hold several
   different SKUs at once in separate containers -- so the shelf label
   deliberately does **not** try to show "the" SKU there (there often
   isn't one); it prints the location barcode plus its stock type
   (RM/SA/FP) as plain readable text, nothing more.
3. **Batch barcode** -- already existed (`batch_code`, see "Backorder
   targets and batches" above); this is what gets printed onto a sticker
   and travels with a physical run through the factory, the direct fix
   for production runs having no physical identifier.

**What actually catches a misplaced product is the scan, not the printed
label.** Since a shared shelf's label can't encode "the" SKU, the
Putaway tab does the real check: scan the product's own SKU barcode,
scan the shelf's barcode, and the app looks up whether that pairing is
in `warehouse.sku_locations` -- no need for the label itself to carry
that information.

**Zebra/ZPL, not a barcode-image library.** Zebra printers render
Code128 barcodes natively from ZPL text commands (`^BC`), so
`production/planner/labels.py` just generates that text -- no
barcode-rendering library anywhere in this app, on either the server or
client side. `sku_label_zpl`, `location_label_zpl`, and `batch_label_zpl`
are pure string templating, unit tested the same way as `bom_explode.py`.

**Getting the ZPL to the physical printer is deliberately left as a
download, not a network push.** This repo doesn't know whether the
printer is reachable over the network from wherever staff browse the
admin screen or the floor app -- that depends on real-world network
topology this session can't see. So every `/production/labels/*` route
returns a downloadable `.zpl` file; sending that to the printer is
whatever tool/driver you already use for it (e.g. Zebra's ZDesigner
Windows driver mapped as a normal printer, which happily accepts a raw
ZPL file as a print job). If the printer turns out to be reachable at a
known address from where these apps run, pushing directly via its raw
port-9100 listener is a small, contained change to `_zpl_response` in
`refresh-service/app.py` -- worth doing once the network setup is
confirmed, not guessed at now.

**The "product placed anywhere" fix, specifically:** every SKU gets one
designated home shelf/area (`warehouse.sku_locations` -- deliberately the
simplest model, not a full multi-bin quantity-tracking WMS, see
migration 010's comments for why; several SKUs can share one shelf, a
shelf just can't be "the" SKU's-worth of barcode on its own label). The
floor app's Putaway tab is a two-scan confirmation: scan the product's
SKU barcode, then scan the shelf's own location barcode, and get told
immediately whether they match (`production/planner/putaway.py`, pure
logic, unit tested). Every scan is logged either way
(`warehouse.putaway_scans`) -- a match, a mismatch, or "this SKU has no
home location assigned yet" are all recorded, and the admin screen's
PUTAWAY SCANS table surfaces the mismatches, which is the entire point
of scanning in the first place. `warehouse.locations.stock_type`
(RM/SA/FP, migration 011) is a real filterable field, not just a naming
convention baked into location codes -- the admin screen's WAREHOUSE
LOCATIONS table can filter to just one type.

**Verified locally, real HTTP, real Postgres:** generated real ZPL for
all three label types, confirmed a shared shelf assigned to two
different SKUs prints a label with only the location barcode and its
stock type (no attempt at a "current SKU" barcode), confirmed both SKUs
independently check out as a match against that same shelf on a putaway
scan, and confirmed the admin locations listing aggregates every SKU
assigned to a shelf into one row (not one duplicated row per SKU).
Separately verified a wrong-bin scan (mismatch, correctly reporting the
expected location) and a scan for a SKU with no home location yet
(correctly recorded with `matched: null`, not a false mismatch), and
that the batch-label download route accepts both the floor secret and an
admin bearer token (it's printed from both apps) and correctly 401s with
neither.

## Live concept -- what actually runs today

To get something real running with minimal new setup, the concept
deliberately reuses this repo's existing Supabase project and Render
account rather than standing up anything new, and defers the riskiest
piece (writing to live Cin7 inventory) until the rest is proven:

- `migrations/007_production_schema.sql` runs against the same Supabase
  project as `reporting` (new `production` schema, no new project).
- `backend/src/routes/production.js` is mounted on the existing Node
  API: `GET/POST /production/*` for the admin/planner side (same
  Supabase-login auth as the rest of the report) and
  `GET /production/runs/open` + `POST /production/run-actuals` for the
  floor app, gated by a separate `FLOOR_APP_SHARED_SECRET` header
  instead of a per-user login -- there's no login system on a shared
  shop tablet, so this is abuse-deterrence on a public URL, not real
  auth (same reasoning as `SUPABASE_ANON_KEY` being safe to serve
  publicly).
- `refresh-service/production_plan.py` adds `POST /production/plan`
  (general path), and `backorder_targets.py` + `batch_staging.py` add
  `POST /production/targets/sync`, `POST /production/targets/:id/plan-batches`
  and `POST /production/batches/:id/actual` (backorder path) to the
  existing refresh-service.
- **`floor-app/` now has two tabs**: Batches (barcode/manual entry
  against `production.production_batches`, see above) and Stocktake
  (barcode/manual entry against `stocktake.counts`, see above) -- both
  match how staff actually work the floor. The general BOM-explosion
  path (`GET /production/runs/open` + `POST /production/run-actuals`)
  still exists as an API for admin-triggered replenishment, just without
  its own floor-app screen yet -- see "Still not built".
- **`floor-app/` has a third tab, Putaway** (scan a SKU, scan a bin's
  location, get told match/mismatch -- see "Labels & warehouse
  locations" above), and a "Print batch label" button on the Batches
  tab's confirm screen.
- **`production-admin/` now has five sections**: TARGETS + SYNC
  BACKORDER DEMAND + BATCHES (see the Backorder targets section above),
  STOCKTAKE (review a count's variance, push it as a Cin7 adjustment),
  and WAREHOUSE LOCATIONS + PUTAWAY SCANS (add locations, assign SKUs'
  home locations, download SKU/location labels, review mismatches). No
  login of its own -- reuses the main dashboard's Supabase session.
- **Deliberately not wired yet: real Cin7 calls, on any path.** See the
  dry-run explanations above for the backorder and stocktake paths; the
  general path is the same idea (`/production/plan` takes demand/BOM/
  on-hand on the request body, `run-actuals` marks a run `completed` in
  our own database only). All are provably correct against a real
  database with zero risk to live Cin7 inventory while the real
  endpoints are still unconfirmed. Labels/locations/putaway don't touch
  Cin7 at all -- there's no dry-run needed there, it's a genuinely
  separate concern from the Cin7 sync paths.

**To try it locally:** run all four migrations in order
(`007_production_schema.sql`, `008_...sql`, `009_stocktake.sql`,
`010_warehouse_locations.sql`, `011_location_stock_type.sql`, each via
`python scripts/run_migration.py`)
against your `.env`, set `FLOOR_APP_SHARED_SECRET` in both
`backend/.env` and wherever `refresh-service` runs, start both services,
then open `/production-floor/` for the floor app (Batches, Stocktake, or
Putaway tab) or `/production-admin/` for the admin screen (signed in via
the main dashboard first, same tab).

## A dedicated URL

`/production-admin` and `/production-floor` read as paths bolted onto
the daily report rather than their own system, which they now genuinely
are. `PRODUCTION_HOST` (e.g. `production.shonrei.co.nz`) gives them a
real subdomain **without** a second Render service or a second Supabase
project -- same backend process, same deploy, just a second hostname
routed to it (`backend/src/app.js` picks which app to serve based on the
request's Host header). Unset, everything behaves exactly as it did
before this existed -- verified locally by booting the backend both with
and without `PRODUCTION_HOST` set and confirming the old paths, and a
random Host header, still land on the daily report when it's unset.

**Setup, once you have a domain/subdomain picked:**
1. Add a DNS CNAME for that hostname pointing at this Render service.
2. Add the same hostname as a Custom Domain on **this same service** in
   Render's dashboard (Settings -> Custom Domains) -- `render.yaml` can't
   provision that step, it just needs to already be there for requests
   with that Host header to actually arrive.
3. Set `PRODUCTION_HOST` (the bare hostname) and `MAIN_APP_URL` (the
   daily report's own public URL) as env vars on `shonrei-report-web`.

**Verified locally** by booting the backend with `PRODUCTION_HOST` set
and a matching Host header: the admin app correctly serves at `/`,
`/styles.css` correctly still resolves to the *shared* base stylesheet
(not admin's own `admin.css` -- these two used to collide once admin
moved to serving at root, fixed by renaming admin's page-specific
stylesheet), and the floor app serves at `/floor`.

## Its own login

Production admin used to reuse the daily report's Supabase session
(same-origin `sessionStorage`, no login form of its own). Once
production access was confirmed to be widening to warehouse/floor-admin
staff who must **never** be able to see the daily report, that stopped
being viable -- reusing the dashboard's session is exactly the kind of
thing that quietly grants access nobody meant to grant. Two changes:

1. **`production.production_users`** (migration 012) is a real, separate
   authorization table -- same pattern this repo already uses to keep
   `reporting.report_users` disjoint from ordering-portal's own users
   table: one shared Supabase Auth pool (one set of email/password
   accounts), but a different list deciding who's actually let into each
   app. Someone can be in `production_users`, `report_users`, both, or
   neither -- being in one implies nothing about the other.
   `backend/src/middleware/productionAuth.js` (`requireProductionAuth`
   checks membership, `requireProductionEdit` checks `can_edit`) mirrors
   `reportAuth.js` exactly, on purpose. `scripts/create_production_users.py`
   mirrors `scripts/create_report_users.py` for provisioning.
2. **`production/admin/index.html` has its own login form** -- plain
   email/password against the same Supabase project (no PIN/MFA/
   remember-me, unlike the daily report; add those later the same way it
   did, if it turns out worth it here too), served its Supabase
   credentials via `/production-config.js` (same reasoning as the daily
   report's `/config.js` -- the anon key is safe to serve publicly, it
   only exchanges credentials for a session, it grants nothing on its
   own). The dashboard's "Production admin" link is now a **plain link**,
   not a session handoff -- there's nothing to hand over once the two
   apps' user lists are meant to diverge.

**Verified locally**: inserted a fake `auth.users` row and a matching
`production.production_users` row with no corresponding
`reporting.report_users` row, and confirmed directly against the DB that
this "warehouse staff" account is a real `production_users` member while
having zero presence in `report_users` -- the actual authorization
boundary the whole redesign exists to guarantee. Confirmed
`/production-config.js` serves real Supabase credentials plus the
dashboard URL on the dedicated subdomain (and `DASHBOARD_URL: "/"` on
the legacy `/production-admin` path, where "back to dashboard" genuinely
is just `/`), and confirmed every production API route still correctly
401s with no `Authorization` header.

## Confirmed Cin7 reads (2026-09-09)

First real run of `scripts/dump_sample_bom.py` against a live account.
The whole Cin7-read side is now wired for real in `cin7_client.py`, unit
tested against the exact captured response shapes (`test_cin7_client.py`,
mocked `requests`, no live Cin7 needed to run them):

- **`GET /ExternalApi/v2/product?SKU=<sku>`** -- real. `BillOfMaterial`,
  `BOMType`, `QuantityToProduce`, `MinimumBeforeReorder`/`ReorderQuantity`
  are all real fields directly on the product record.
- **`GET /ExternalApi/v2/product?SKU=<sku>&IncludeBOM=true`** -- real,
  and the fix for the BOM-lines mystery below: `GET /product` is
  deliberately lean by default, `BillOfMaterialsProducts` only populates
  with its own `IncludeBOM=true` query flag (confirmed from Cin7's own
  published API docs, https://dearinventory.docs.apiary.io/, the
  "Product" reference page). Confirmed live too, not just from docs: a
  second run against SKU `WIPMT20T` with `IncludeBOM=true` came back
  with 4 real `BillOfMaterialsProducts` lines (`RMFPE6BK`, `RMAD1181`,
  `RMC-400-NS`, `RMT195`, each with `ComponentProductID`/`ProductCode`/
  `Quantity`/`WastagePercent`/`WastageQuantity`/`CostPercentage`, exactly
  the documented "Bill Of Material Product Model" shape), plus a sibling
  `BillOfMaterialsServices` array of labour lines (`LABOUR - Gluing
  Room`, `LABOUR - FACTORY`) that `get_bom`/`bom_explode` don't model --
  not needed, since the explosion only cares about physical components.
  `get_bom`'s mapping is now confirmed against real data, not a guess --
  see `test_cin7_client.py`'s `test_real_live_bom_lines_for_wipmt20t_map_correctly`.
- **`GET /ExternalApi/v2/ref/productavailability?SKU=<sku>`** -- real.
  `OnHand`, `Allocated`, `Available` (= `OnHand - Allocated`, Cin7's own
  netted figure), `OnOrder`. `get_availability` (feeds `bom_explode`'s
  on-hand netting) uses `Available`; `get_stock_on_hand` (stocktake's
  variance) uses the raw `OnHand` -- deliberately different fields for
  different purposes, see each method's docstring.
- **Ruled out**: `/bom`, `/product/availability`, `/productavailability`,
  `ref/bom`, `ref/productbom`, `ref/billofmaterial(s)`, `product/bom` --
  all 404 -- except Cin7 doesn't send a real 404 status, it serves its
  own branded "Page not found" HTML page at HTTP 200. `safe_json()` in
  the dump script checks `Content-Type`, not just status, so this
  doesn't get mistaken for a real response.

This closes out the Cin7-read side entirely: `get_bom`, `get_availability`,
and `get_stock_on_hand` are all wired against confirmed live field names,
no more open questions on any of the three.

## Confirmed Cin7 writes (documented, live-testing in progress)

`cin7_client.py`'s Create/Authorise/Complete/Cancel assembly calls,
`get_open_assemblies`, and `adjust_stock_on_hand` are wired from Cin7's
own documented "Finished Goods" (= standard assembly) and "Stock
Adjustment" resources -- see the module docstring for the full endpoint
list. Two things this discipline deliberately did NOT wire from docs
alone:

- **`allocate_assembly`** -- Cin7's docs only show one pick-stage
  endpoint (`POST /finishedGoods/pick`) whose one worked example jumps
  straight to `Status: "COMPLETED"`; there's no documented status value
  for "picked/allocated but not yet completed", even though Cin7's own
  UI has that as a separate step. Raises rather than guess the string.
  Not needed for the backorder-target/batch flow (`complete_small_assembly`
  goes straight from Authorised to Completed) -- only blocks
  `orchestrator.py`'s general path, which has no floor screen yet anyway.
- Whether **Account/WIPAccount** fields are actually required on
  Authorise/Complete/stock-adjustment (Cin7's worked examples use
  tenant-specific-looking codes like `"714"`/`"715"` -- omitted here
  rather than guessed) and whether **`ID` and `TaskID`** are really the
  same identifier for the cancel endpoint's `DELETE ?ID=...`.

**Live findings so far (2026-09-11):**
- Against MTS57013WH: Create's documented request example showed
  `"Status": "..."` (literally left blank) -- a live 400 confirmed it's
  a required field ("Required attribute 'Status' not provided.").
  `create_assembly` now sends `Status: "DRAFT"`, the natural value for a
  brand new assembly.
- Against WIP110: a second live 400 showed Create also requires
  `Account` and `WIPAccount` -- real GL account codes, not documented as
  part of the Create body at all. Read directly off Cin7's own manual
  "New Assembly" screen rather than guessed: WIP Account = `"721B"`
  ("Work in Progress Cin7 Core"), Finished Goods Account = `"720"`
  ("Stock on Hand - Cin7 Core", maps to the API's generic `Account`
  field). Since Cin7's docs show the Complete call
  (`POST /finishedGoods/pick`) carrying these same two fields (plus
  `CompletionDate`/`WIPDate`), `complete_assembly` now sends them too,
  pre-emptively, rather than wait to hit the same 400 a second time. See
  `CIN7_FINISHED_GOODS_ACCOUNT`/`CIN7_WIP_ACCOUNT` in cin7_client.py.
- Against WIP110 again: authorising with the (empty) OrderLines Cin7
  handed back 400'd -- `"Should be at least one order line."`. Confirmed
  Cin7 does NOT auto-populate OrderLines/PickLines from the BOM at
  Create, matching what its own "New Assembly" screen shows: an
  `OrderLines` table with a manual **"Load BOM"** button and "Click the
  'Load BOM' button to view the bill of materials" until you do.
  `authorise_assembly`/`complete_assembly` now build these lines
  themselves from the product's own BOM (via a new
  `_component_lines_for_build` helper), scaled to the assembly's build
  quantity, instead of trusting Cin7 to have already populated them.
  Cin7's own UI also shows a separate "Allocate" stage after Authorise
  (with its own load-table step) -- not exercised here:
  `complete_assembly` calls `POST /finishedGoods/pick` with
  `Status: "COMPLETED"` directly from Authorised, which per Cin7's one
  documented worked example does pick + allocate + complete together in
  one call (see `allocate_assembly`'s docstring for why that stage is
  deliberately unwired).

**A full Create -> Authorise -> Complete run succeeded live against
WIP110 (2026-09-11)** -- the whole assembly write path works end to end.
One gap found on that first successful run: it only included
`BillOfMaterialsProducts` (physical components), not
`BillOfMaterialsServices` (labour lines, e.g. "LABOUR - Gluing Room" seen
earlier on WIPMT20T) -- Cin7 didn't require them to complete, but
omitting them means labour cost never gets allocated to the assembly.
`_component_lines_for_build` now includes both.

`scripts/dump_sample_assembly_write.py` is the write-side counterpart to
`dump_sample_bom.py` -- it walks a real throwaway assembly through
Create -> Authorise -> Complete (with a confirmation prompt before each
write); stock adjustment and the Void/cancel test are deliberately not
in it this round (voiding is being done manually in Cin7's UI instead).
Now that Create -> Authorise -> Complete has succeeded live, the
remaining step is re-running it with the labour-line fix included, then
swapping `DryRunCin7Client` for a real `Cin7Client` in the
backorder-target/batch flow -- see "What's dry-run vs. real today" above.

**Possible follow-up (not yet scoped):** a quick-add field on batch
completion (floor app) for actual labour hours, since Cin7's BOM-derived
labour quantity is only the standard/planned figure, not what actually
happened on the floor. Actuals in Shonrei's real process are always
entered after allocation, which lines up with where `apply_batch_actual`
(`backorder_targets.py`) already sits -- the natural place to add an
optional labour override before it reaches `complete_small_assembly`.

## Still not built

- **SO backorder extraction**
  (`backorder_targets.extract_demand_lines`) -- turning a Cin7 sale's
  full detail into per-SKU backordered quantities, needs its own
  confirm-first pass against a live sale detail (see
  `backorder_targets.py`'s module docstring).
- **QR-scan-to-select on the general-path floor screen** -- Batches and
  Stocktake both already have barcode/manual entry; the general
  BOM-explosion path (`production_runs`) has no floor-app screen of its
  own yet, admin-triggered and API-only for now.
- An admin UI for `/production/plan` (the general path) -- everything on
  the backorder and stocktake paths now has one, this is the remaining
  raw API call.
- Photo/OCR fallback for the rare case even the one-tap floor app is too
  much friction.
- **Stocktake's `location` field is still free text**, not linked to
  `warehouse.locations` -- the two were built separately and haven't
  been reconciled. Worth wiring stocktake's location field to the same
  location picker/barcode once both have been used for a while and it's
  clear they should be the same list.
- **Direct network printing** -- every label is a downloadable `.zpl`
  file today (see "Labels & warehouse locations" above); pushing
  straight to the printer over the network is a small change once the
  printer's actual network reachability from these apps is confirmed.
- **Label size/layout is a guess** (4in x 2in, hand-placed field
  coordinates in `labels.py`) -- adjust `LABEL_WIDTH_DOTS`/
  `LABEL_HEIGHT_DOTS` and the `^FO` coordinates once real label stock is
  in hand and a test print shows what needs to move.
