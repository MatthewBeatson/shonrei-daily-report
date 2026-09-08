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
    test_*.py                Unit tests for all of the above
    requirements.txt
  floor-app/           Static HTML/JS -- two tabs, barcode-or-manual entry
                       in both, no build step
    index.html            Batches tab + Stocktake tab
    app.js
    styles.css
  admin/               Static HTML/JS -- trigger sync/plan-batches, review
                       and push stocktake adjustments
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
    dry_run_cin7.py            What actually runs today instead of a real
                                Cin7Client -- logs every call, never touches Cin7
  ../migrations/
    007_production_schema.sql                  General path (production_runs)
    008_backorder_targets_and_batches.sql       Backorder path (targets, batches)
    009_stocktake.sql                           Stocktake (stocktake.counts)
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
end to end with zero risk to live inventory. Swapping in a real
`Cin7Client` once its stubbed methods are wired against confirmed
endpoints (`scripts/dump_sample_bom.py`) is the only change needed --
`backorder_targets.py` and `batch_staging.py` don't care which
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
- **`production-admin/` now has three sections**: TARGETS + SYNC
  BACKORDER DEMAND + BATCHES (see the Backorder targets section above)
  and STOCKTAKE (review a count's variance, push it as a Cin7
  adjustment -- see the Stocktake section above). No login of its own --
  reuses the main dashboard's Supabase session.
- **Deliberately not wired yet: real Cin7 calls, on any path.** See the
  dry-run explanations above for the backorder and stocktake paths; the
  general path is the same idea (`/production/plan` takes demand/BOM/
  on-hand on the request body, `run-actuals` marks a run `completed` in
  our own database only). All three are provably correct against a real
  database with zero risk to live Cin7 inventory while the real
  endpoints are still unconfirmed.

**To try it locally:** run all three migrations in order
(`migrations/007_production_schema.sql`, then `008_...sql`, then
`009_stocktake.sql`, each via `python scripts/run_migration.py`) against
your `.env`, set `FLOOR_APP_SHARED_SECRET` in both `backend/.env` and
wherever `refresh-service` runs, start both services, then open
`/production-floor/` for the floor app (Batches or Stocktake tab) or
`/production-admin/` for the admin screen (signed in via the main
dashboard first, same tab).

## Still not built

- **Real Cin7 calls everywhere they're currently dry-run**: SO backorder
  extraction (`backorder_targets.extract_demand_lines`), the
  Create/Authorise/Allocate/Complete/Cancel assembly calls, and
  stocktake's `get_stock_on_hand`/`adjust_stock_on_hand` -- all confirmed
  the same way, via `scripts/dump_sample_bom.py` (worth extending to
  cover the stock-adjustment endpoint shape too, not just BOM/assembly).
- **QR-scan-to-select on the general-path floor screen** -- Batches and
  Stocktake both already have barcode/manual entry; the general
  BOM-explosion path (`production_runs`) has no floor-app screen of its
  own yet, admin-triggered and API-only for now.
- An admin UI for `/production/plan` (the general path) -- everything on
  the backorder and stocktake paths now has one, this is the remaining
  raw API call.
- Photo/OCR fallback for the rare case even the one-tap floor app is too
  much friction.
- Stocktake locations are a free-text field today, not validated against
  Cin7's actual location list -- fine for Shonrei's single-location use
  so far, would need a real lookup if that changes.
