# Production MRP & Assembly Automation (prototype)

Scaffold for the two problems raised alongside the daily report and dispatch
plan work:

1. Cin7 Core's assembly module (Create -> Authorise -> Allocate -> Complete,
   "CAAC" below) has no multi-level BOM explosion, no scheduling, and no
   partial-completion model -- running it by hand for concurrent, multi-level
   production is a lot of repetitive clicking.
2. Production *inputs* (what was actually made, by whom, how much) never
   make it back into Cin7 -- staff are busy running the physical lines, not
   filling in forms. Outputs (FG creation at min-stock) are already handled
   by admin, so this scaffold doesn't touch that side.

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
  planner/            Python: BOM explosion (MRP) + Cin7 API orchestration
    bom_explode.py       Pure function: multi-level BOM -> ordered build plan
    test_bom_explode.py  Unit tests for the above (no Cin7/DB needed)
    cin7_client.py       Thin wrapper over Cin7's assembly + BOM endpoints
    orchestrator.py      Ties the two together, drives Create/Authorise/Allocate,
                          and applies floor-reported actuals as Complete
    requirements.txt
  floor-app/           Static HTML/JS mockup of the floor input screen
    index.html
    app.js
    styles.css
  ../migrations/007_production_schema.sql   New `production` schema
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
  to the existing refresh-service, reusing `bom_explode.py` to turn a
  demand list into a staged plan.
- `floor-app/` now talks to these real endpoints instead of mock data --
  it lists real open runs (`GET /production/runs/open`) instead of
  scanning a QR code (camera integration is still deferred, see below),
  and submitting reports a real actual quantity that flips the run to
  `completed` in the database.
- **Deliberately not wired yet: real Cin7 calls.** Demand/BOM/on-hand
  figures for `/production/plan` come in on the request body rather
  than being pulled from Cin7 automatically, and `run-actuals` marks a
  run `completed` in our own database without calling Cin7's
  Allocate/Complete endpoints. This proves the whole flow (explosion ->
  staged runs -> floor input -> completion) against a real database
  with zero risk to live Cin7 inventory while it's still being
  validated. `scripts/dump_sample_bom.py` is the next step when ready --
  same pattern as `scripts/dump_sample_sale.py`, run locally with real
  Cin7 credentials to confirm the real BOM/availability endpoint shapes
  before wiring them into `cin7_client.py` for real.

**To try it locally:** run `python scripts/run_migration.py
migrations/007_production_schema.sql` against your `.env`, set
`FLOOR_APP_SHARED_SECRET` in both `backend/.env` and wherever
`refresh-service` runs, start both services as usual, then open
`/production-floor/` for the floor app or `POST /production/plan` (with
a report-admin's bearer token) with a demand/BOM body to stage a plan.

## Still not built

- QR-scan-to-select on the floor app (currently: pick from a short real
  list of open runs -- functionally equivalent for a small number of
  concurrent runs, less setup than camera integration).
- Real Cin7 BOM/availability pull and the actual Create/Authorise/
  Allocate/Complete API calls (see above).
- An admin UI for `/production/plan` -- for now it's a raw API call; the
  6-user report app would be the natural place to add a page for it.
- Photo/OCR fallback for the rare case even the one-tap floor app is too
  much friction.
