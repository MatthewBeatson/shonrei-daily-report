"""Tiny HTTP wrapper around daily_refresh_supabase.run_refresh().

One endpoint, POST /refresh, used by both:
  - Render's Cron Job (scheduled runs, hourly during business hours)
  - The Node backend's POST /reporting/refresh route (on-demand "Refresh
    Now" clicks from the web app)

Both call this with the same shared secret (REFRESH_SHARED_SECRET) in the
X-Refresh-Secret header -- this service is never exposed to end users
directly, only to the other two trusted callers.

The refresh itself (Xero pagination + Cin7 sale-by-sale detail fetches) can
take a while, so it runs in a background thread and the request returns
202 immediately. Callers watch reporting.refresh_state / the newest
reporting.report_snapshots row to see when it's done.

Run with a single worker (see render.yaml) -- refresh_state is used as an
in-DB lock to stop overlapping runs, which only works if one process at a
time is allowed to flip it.
"""
from __future__ import annotations
import os, threading
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify, Response
from daily_refresh_supabase import get_conn, run_refresh, load_settings
from dispatch_plan_run import run_dispatch_plan
from production_plan import ProductionPlanError, run_production_plan
from backorder_targets import sync_targets, apply_batch_actual
from batch_staging import stage_batches_for_target
from dry_run_cin7 import DryRunCin7Client
from stocktake import (
    StocktakeError, record_count, apply_adjustment,
    get_active_stocktake_number, set_active_stocktake_number, sync_stocktake_totals,
)
from warehouse import (
    WarehouseError, record_putaway_scan, set_home_location,
    get_putaway_mismatch_mode, set_putaway_mismatch_mode,
)
from cin7_read import get_on_hand_for_skus, get_availability_for_skus, get_stock_by_bin, sku_exists, get_real_cin7_client
from labels import batch_label_zpl, location_label_zpl, sku_label_zpl
import open_orders_xlsx
import demand_sync

app = Flask(__name__)

REFRESH_SHARED_SECRET = os.environ['REFRESH_SHARED_SECRET']


def require_secret():
    return request.headers.get('X-Refresh-Secret') == REFRESH_SHARED_SECRET


def within_scheduled_window(conn):
    """True if "now" (in the configured timezone) falls inside the
    scheduled refresh window. Only applies to unattended/cron calls
    (triggered_by is null) -- an on-demand click from a user is honored
    any time of day. Computed at call time via ZoneInfo, so this correctly
    tracks NZ daylight saving without a hand-tuned UTC cron expression."""
    cfg = load_settings(conn)
    tz = ZoneInfo(cfg.get('timezone') or 'Pacific/Auckland')
    now_hour = datetime.now(tz).hour
    start_hour = int(cfg.get('refresh_window_start_hour') or 7)
    end_hour = int(cfg.get('refresh_window_end_hour') or 19)
    return start_hour <= now_hour < end_hour


def _run_in_background(triggered_by):
    try:
        run_refresh(triggered_by=triggered_by)
    except Exception as exc:  # noqa: BLE001 -- last-resort net, run_refresh already logs per-source errors
        print(f'Unhandled error during refresh: {exc}', flush=True)
    finally:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    update reporting.refresh_state
                    set status = 'idle', last_completed_at = now()
                    where id = 'current'
                    """
                )
            conn.commit()
        finally:
            conn.close()


def _run_dispatch_plan_in_background(triggered_by):
    try:
        run_dispatch_plan(triggered_by=triggered_by)
    except Exception as exc:  # noqa: BLE001 -- last-resort net, run_dispatch_plan already logs internally
        print(f'Unhandled error during dispatch plan generation: {exc}', flush=True)


@app.get('/cin7/on-hand')
def cin7_on_hand():
    """Real Cin7 on-hand for the given SKUs -- READ ONLY, and the one
    route on this whole service that hits real Cin7 rather than
    DryRunCin7Client (see cin7_read.py). Confirmed live, 2026-09-28 --
    the first real-Cin7 call the deployed app itself makes, not just a
    local diagnostic script.

    Query param: skus=SKU1,SKU2,... (comma-separated).
    """
    if not require_secret():
        return jsonify({'error': 'unauthorized'}), 401
    skus_param = request.args.get('skus', '')
    skus = [s.strip() for s in skus_param.split(',') if s.strip()]
    if not skus:
        return jsonify({'error': 'skus query param is required (comma-separated)'}), 400
    return jsonify({'on_hand': get_on_hand_for_skus(skus)}), 200


@app.get('/cin7/availability')
def cin7_availability():
    """Real Cin7 on-hand/allocated/available for the given SKUs -- READ
    ONLY, same "always real Cin7" carve-out as /cin7/on-hand. Unlike
    /cin7/on-hand (just the on-hand figure), this also surfaces
    Allocated -- Cin7's own figure for stock already committed to
    orders, distinct from this app's own "qty backordered" (computed
    from order/invoice status) -- see Cin7Client.get_availability_
    detail's docstring. Feeds the admin page's pulled-demand review
    table.

    Query param: skus=SKU1,SKU2,... (comma-separated).
    """
    if not require_secret():
        return jsonify({'error': 'unauthorized'}), 401
    skus_param = request.args.get('skus', '')
    skus = [s.strip() for s in skus_param.split(',') if s.strip()]
    if not skus:
        return jsonify({'error': 'skus query param is required (comma-separated)'}), 400
    return jsonify({'availability': get_availability_for_skus(skus)}), 200


@app.get('/cin7/stock-by-bin')
def cin7_stock_by_bin():
    """Real, live per-bin stock for one SKU -- READ ONLY, same "always
    real Cin7" carve-out as /cin7/on-hand. Backs the floor app's and
    admin's "check stock on hand, by bin" SKU search (2026-10-02).

    Query param: sku=<one SKU>.
    """
    if not require_secret():
        return jsonify({'error': 'unauthorized'}), 401
    sku = (request.args.get('sku') or '').strip()
    if not sku:
        return jsonify({'error': 'sku query param is required'}), 400
    try:
        rows = get_stock_by_bin(sku)
    except ValueError:
        return jsonify({'error': f'{sku!r} is not a real Cin7 SKU -- check for a typo'}), 404
    except Exception as exc:  # noqa: BLE001
        return jsonify({'error': f"Couldn't read Cin7 right now: {exc}"}), 502
    return jsonify({'sku': sku, 'bins': rows}), 200


@app.get('/cin7/sku-exists')
def cin7_sku_exists():
    """Real, live Cin7 product-existence check -- READ ONLY, same
    "always real Cin7" carve-out as /cin7/on-hand. Used by the floor/
    admin apps to stop accepting a scanned/typed SKU that isn't a real
    Cin7 product before it's even submitted, rather than only rejecting
    it after the fact (record_putaway_scan/set_home_location/
    record_count also enforce this server-side regardless).

    Query param: sku=<one SKU>.
    """
    if not require_secret():
        return jsonify({'error': 'unauthorized'}), 401
    sku = (request.args.get('sku') or '').strip()
    if not sku:
        return jsonify({'error': 'sku query param is required'}), 400
    try:
        exists = sku_exists(sku)
    except Exception as exc:  # noqa: BLE001
        return jsonify({'error': f"Couldn't verify against Cin7 right now: {exc}"}), 502
    return jsonify({'sku': sku, 'exists': exists}), 200


@app.get('/health')
def health():
    return jsonify({'status': 'ok'})


@app.post('/refresh')
def refresh():
    if not require_secret():
        return jsonify({'error': 'unauthorized'}), 401

    triggered_by = (request.get_json(silent=True) or {}).get('triggered_by')  # report_users.id or null for cron

    conn = get_conn()
    try:
        if triggered_by is None and not within_scheduled_window(conn):
            return jsonify({'status': 'skipped_outside_window'}), 200

        with conn.cursor() as cur:
            cur.execute("select status from reporting.refresh_state where id = 'current' for update")
            (status,) = cur.fetchone()
            if status == 'running':
                conn.rollback()
                return jsonify({'error': 'already_running'}), 409
            cur.execute(
                """
                update reporting.refresh_state
                set status = 'running', started_at = now(), last_triggered_by = %s
                where id = 'current'
                """,
                (triggered_by,),
            )
        conn.commit()
    finally:
        conn.close()

    thread = threading.Thread(target=_run_in_background, args=(triggered_by,), daemon=True)
    thread.start()
    return jsonify({'status': 'started'}), 202


@app.post('/dispatch-plan/generate')
def dispatch_plan_generate():
    if not require_secret():
        return jsonify({'error': 'unauthorized'}), 401

    triggered_by = (request.get_json(silent=True) or {}).get('triggered_by')

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("select status from reporting.dispatch_plan_current where id = 'current' for update")
            row = cur.fetchone()
            if row and row[0] == 'running':
                conn.rollback()
                return jsonify({'error': 'already_running'}), 409
            cur.execute(
                "update reporting.dispatch_plan_current set status = 'running', triggered_by = %s where id = 'current'",
                (triggered_by,),
            )
        conn.commit()
    finally:
        conn.close()

    thread = threading.Thread(target=_run_dispatch_plan_in_background, args=(triggered_by,), daemon=True)
    thread.start()
    return jsonify({'status': 'started'}), 202


@app.post('/production/plan')
def production_plan():
    """Explode a demand list into a build plan and stage it in the
    `production` schema. Unlike /refresh and /dispatch-plan/generate this
    runs synchronously and returns the plan in the response -- BOM
    explosion is fast (pure in-memory recursion, see bom_explode.py), no
    need for the background-thread + poll pattern those use for the much
    slower Xero/Cin7 pulls.
    """
    if not require_secret():
        return jsonify({'error': 'unauthorized'}), 401

    payload = request.get_json(silent=True) or {}
    conn = get_conn()
    try:
        try:
            plan = run_production_plan(conn, payload)
        except ProductionPlanError as exc:
            conn.rollback()
            return jsonify({'error': str(exc)}), 400
    finally:
        conn.close()

    return jsonify(plan), 200


@app.post('/production/targets/sync')
def production_targets_sync():
    """Body: {"demand_lines_by_sku": {sku: [{"so_number","order_date","qty_backordered"}, ...]}}.
    Caller supplies the demand lines directly -- either typed by hand on
    the admin page, or (since 2026-10-01) pulled straight from live Cin7
    via POST /production/demand/pull + GET /production/demand/status
    below (see demand_sync.py). This route itself doesn't care which.

    Runs against the REAL Cin7Client (switched over 2026-10-01) -- create
    and close are both confirmed live; adjust (an existing target's
    quantity changing) falls back to close+recreate inside sync_targets
    if the in-place adjust call itself fails, since that one specific
    call was never independently confirmed to succeed. This really does
    create/adjust/close a real Cin7 assembly.
    """
    if not require_secret():
        return jsonify({'error': 'unauthorized'}), 401

    payload = request.get_json(silent=True) or {}
    demand_lines_by_sku = payload.get('demand_lines_by_sku') or {}

    conn = get_conn()
    try:
        results = sync_targets(conn, get_real_cin7_client(), demand_lines_by_sku)
    finally:
        conn.close()

    return jsonify({'actions': results}), 200


# -- Pull backorder demand from live Cin7 (production admin) ----------
# Real Cin7, READ ONLY -- same carve-out as /cin7/on-hand and Total
# Product on Order. Same background-job-with-status-polling shape as
# /reports/open-orders/* (minutes on a cold cache, shares its per-sale
# cache too) -- see demand_sync.py.

@app.post('/production/demand/pull')
def production_demand_pull():
    if not require_secret():
        return jsonify({'error': 'unauthorized'}), 401
    return jsonify(demand_sync.start_pull()), 202


@app.get('/production/demand/status')
def production_demand_status():
    """Once status is 'done', also carries `summary` (one row per SKU:
    sku, qty, so_count) for the admin page to show before syncing, and
    `demand_lines_by_sku` in the exact shape POST /production/targets/
    sync wants -- the admin page's "Sync targets" button sends this
    straight back rather than re-deriving it.
    """
    if not require_secret():
        return jsonify({'error': 'unauthorized'}), 401
    state = demand_sync.job_state()
    if state['status'] == 'done':
        demand_lines_by_sku, summary = demand_sync.get_result()
        state['summary'] = summary
        state['demand_lines_by_sku'] = demand_lines_by_sku
    return jsonify(state), 200


@app.post('/production/targets/<target_id>/plan-batches')
def production_plan_batches(target_id):
    """Body: {"suggested_run_size": 50}. Splits the target's current
    priority-ordered demand into batches (production.production_batches).
    """
    if not require_secret():
        return jsonify({'error': 'unauthorized'}), 401

    payload = request.get_json(silent=True) or {}
    run_size = payload.get('suggested_run_size')
    if not run_size or run_size <= 0:
        return jsonify({'error': 'suggested_run_size must be a positive number'}), 400

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("select sku from production.targets where id = %s and status = 'active'", (target_id,))
            row = cur.fetchone()
        if row is None:
            return jsonify({'error': 'No such active target'}), 404
        (sku,) = row
        batches = stage_batches_for_target(conn, target_id, sku, run_size)
    finally:
        conn.close()

    return jsonify({'batches': batches}), 201


@app.post('/production/batches/<batch_id>/actual')
def production_batch_actual(batch_id):
    """Body: {"actual_qty", "reject_qty", "reported_via", "reported_by",
    "labour_hours_overrides", "pick_line_overrides"}. The floor-input
    endpoint's backend: completes a REAL small Cin7 assembly (switched
    over 2026-10-01 -- this is the most-proven Cin7 write in the whole
    app, a full Create->Authorise->Complete run succeeded live against a
    real test assembly) and decrements the parent target. This genuinely
    consumes real component stock and produces real finished-goods stock
    in Cin7. Called by the Node backend's POST /production/batch-actuals,
    not directly by the floor app -- see backend/src/routes/production.js.

    labour_hours_overrides/pick_line_overrides are optional, for a
    future floor-app quick-add UI (see cin7_client.complete_small_
    assembly's docstring) -- no floor-app screen sends these yet.
    """
    if not require_secret():
        return jsonify({'error': 'unauthorized'}), 401

    payload = request.get_json(silent=True) or {}
    actual_qty = payload.get('actual_qty')
    if actual_qty is None or actual_qty < 0:
        return jsonify({'error': 'actual_qty must be a non-negative number'}), 400

    conn = get_conn()
    try:
        try:
            result = apply_batch_actual(
                conn, get_real_cin7_client(), batch_id,
                actual_qty, payload.get('reject_qty') or 0,
                payload.get('reported_via') or 'manual', payload.get('reported_by'),
                labour_hours_overrides=payload.get('labour_hours_overrides'),
                pick_line_overrides=payload.get('pick_line_overrides'),
            )
        except ValueError as exc:
            conn.rollback()
            return jsonify({'error': str(exc)}), 409
    finally:
        conn.close()

    return jsonify(result), 201


@app.post('/stocktake/counts')
def stocktake_record_count():
    """Body: {"sku", "counted_qty", "location", "reported_via", "reported_by"}.
    Recording a count never writes to Cin7 on its own, see stocktake.py --
    the only Cin7 call this makes is the on-hand snapshot for the
    variance shown back, a pure read, so this runs against the REAL
    Cin7Client (switched over 2026-10-01, zero write risk either way).
    """
    if not require_secret():
        return jsonify({'error': 'unauthorized'}), 401

    payload = request.get_json(silent=True) or {}
    sku = payload.get('sku')
    counted_qty = payload.get('counted_qty')
    if not sku or counted_qty is None:
        return jsonify({'error': 'sku and counted_qty are required'}), 400

    conn = get_conn()
    try:
        try:
            result = record_count(
                conn, get_real_cin7_client(), sku, counted_qty,
                location=payload.get('location'),
                reported_via=payload.get('reported_via') or 'manual',
                reported_by=payload.get('reported_by'),
            )
        except StocktakeError as exc:
            conn.rollback()
            return jsonify({'error': str(exc)}), 400
    finally:
        conn.close()

    return jsonify(result), 201


@app.post('/stocktake/counts/<count_id>/adjust')
def stocktake_apply_adjustment(count_id):
    """Pushes one already-recorded count to Cin7 as a REAL stock
    adjustment (switched over 2026-10-01 -- adjust_stock_on_hand,
    including bin-tagging, is confirmed live) -- a deliberate, separate
    step from recording it, see stocktake.py.
    """
    if not require_secret():
        return jsonify({'error': 'unauthorized'}), 401

    payload = request.get_json(silent=True) or {}
    conn = get_conn()
    try:
        try:
            result = apply_adjustment(conn, get_real_cin7_client(), count_id, payload.get('note'))
        except StocktakeError as exc:
            conn.rollback()
            return jsonify({'error': str(exc)}), 409
    finally:
        conn.close()

    return jsonify(result), 200


@app.get('/stocktake/active-number')
def stocktake_get_active_number():
    """Which Cin7 Stocktake (e.g. "ST-00233") sync_stocktake_totals
    tags its adjustments with -- admin-entered, since admin starts the
    real Stocktake manually in Cin7's own UI, not from this app."""
    if not require_secret():
        return jsonify({'error': 'unauthorized'}), 401
    conn = get_conn()
    try:
        number = get_active_stocktake_number(conn)
    finally:
        conn.close()
    return jsonify({'active_cin7_stocktake_number': number})


@app.post('/stocktake/active-number')
def stocktake_set_active_number():
    """Body: {"stocktake_number", "updated_by"}. Admin can set/clear/
    replace this at any point during a stocktake cycle."""
    if not require_secret():
        return jsonify({'error': 'unauthorized'}), 401
    payload = request.get_json(silent=True) or {}
    conn = get_conn()
    try:
        result = set_active_stocktake_number(
            conn, payload.get('stocktake_number') or None, payload.get('updated_by'),
        )
    finally:
        conn.close()
    return jsonify(result), 200


@app.post('/stocktake/sync')
def stocktake_sync():
    """Pushes every currently-'recorded' count to Cin7 as its own
    adjustment (each count is already scoped to one area, and each area
    IS a real Cin7 Bin -- no aggregation across areas), tagged with the
    active Stocktake number -- 409s if none is set. A count whose area
    has no Cin7 Bin linked yet comes back `skipped: True` rather than
    erroring the whole sync. Safe to call repeatedly through a stocktake
    cycle. See stocktake.sync_stocktake_totals.

    Runs against the REAL Cin7Client (switched over 2026-10-01) -- the
    bin-tagged adjustment itself is confirmed live; tagging it with a
    StocktakeNumber specifically has not been independently live-tested,
    so the first real formal-stocktake sync is worth watching closely --
    a failure there surfaces as a normal per-count error, it can't
    corrupt anything (a count only flips to 'adjusted' after Cin7's own
    adjustment call succeeds and returns a real TaskID).
    """
    if not require_secret():
        return jsonify({'error': 'unauthorized'}), 401
    conn = get_conn()
    try:
        try:
            results = sync_stocktake_totals(conn, get_real_cin7_client())
        except StocktakeError as exc:
            conn.rollback()
            return jsonify({'error': str(exc)}), 409
    finally:
        conn.close()
    return jsonify({'synced': results}), 200


@app.post('/warehouse/putaway-scans')
def warehouse_putaway_scan():
    """Body: {"sku", "scanned_location_code", "scanned_by"}. Records a
    putaway/relocate scan and reports whether it matched the SKU's
    designated home location -- see warehouse.py / putaway.py.
    """
    if not require_secret():
        return jsonify({'error': 'unauthorized'}), 401

    payload = request.get_json(silent=True) or {}
    sku = payload.get('sku')
    scanned_location_code = payload.get('scanned_location_code')
    if not sku or not scanned_location_code:
        return jsonify({'error': 'sku and scanned_location_code are required'}), 400

    conn = get_conn()
    try:
        try:
            result = record_putaway_scan(conn, sku, scanned_location_code, payload.get('scanned_by'))
        except WarehouseError as exc:
            conn.rollback()
            return jsonify({'error': str(exc)}), 400
    finally:
        conn.close()

    return jsonify(result), 201


@app.get('/warehouse/putaway-mismatch-mode')
def warehouse_get_putaway_mismatch_mode():
    """'warn' or 'block' -- see migration 015. Floor-accessible read,
    same pattern as GET /stocktake/active-number."""
    if not require_secret():
        return jsonify({'error': 'unauthorized'}), 401
    conn = get_conn()
    try:
        mode = get_putaway_mismatch_mode(conn)
    finally:
        conn.close()
    return jsonify({'putaway_mismatch_mode': mode}), 200


@app.post('/warehouse/putaway-mismatch-mode')
def warehouse_set_putaway_mismatch_mode():
    """Body: {"mode": "warn"|"block", "updated_by"}. Admin-only -- see
    migration 015 / production/README.md."""
    if not require_secret():
        return jsonify({'error': 'unauthorized'}), 401
    payload = request.get_json(silent=True) or {}
    mode = payload.get('mode')
    conn = get_conn()
    try:
        try:
            result = set_putaway_mismatch_mode(conn, mode, payload.get('updated_by'))
        except WarehouseError as exc:
            conn.rollback()
            return jsonify({'error': str(exc)}), 400
    finally:
        conn.close()
    return jsonify(result), 200


@app.put('/warehouse/sku-locations/<path:sku>')
def warehouse_set_home_location(sku):
    """Body: {"location_code"}. Assigns (or reassigns) a SKU's home
    location -- what a putaway scan is checked against -- and also
    pushes it as Cin7's own product DefaultLocation (best-effort, see
    warehouse.set_home_location's docstring). The ONE remaining dry-run
    Cin7 call left on this service (2026-10-01, every other write
    switched to real) -- not a caution, there's simply no real
    implementation to switch to yet: Cin7Client.update_product_default_
    location deliberately raises NotImplementedError until its real
    request shape is confirmed live (scripts/probe_product_default_
    location_write.py). The local DB assignment and label printing both
    work fully regardless -- this only affects whether Cin7's own
    product record shows the location too.
    """
    if not require_secret():
        return jsonify({'error': 'unauthorized'}), 401

    payload = request.get_json(silent=True) or {}
    location_code = payload.get('location_code')
    if not location_code:
        return jsonify({'error': 'location_code is required'}), 400

    conn = get_conn()
    try:
        try:
            result = set_home_location(conn, sku, location_code, cin7=DryRunCin7Client(conn))
        except WarehouseError as exc:
            conn.rollback()
            return jsonify({'error': str(exc)}), 400
    finally:
        conn.close()

    return jsonify(result), 200


def _zpl_response(zpl: str, filename: str) -> Response:
    return Response(
        zpl, mimetype='text/plain',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


@app.get('/labels/sku/<path:sku>')
def label_sku(sku):
    """Same barcode payload (the literal SKU text) whether this label
    ends up on a bin/location label or stuck straight onto the product --
    see production/planner/labels.py.

    `count` prints that many copies in one file (default 1) -- used for
    FP's "one label per item" auto-print (see /production/batches/
    <id>/actual's stock_type in the response, and floor-app/app.js),
    a plain repeat of the same ^XA...^XZ block, which is how Zebra
    printers expect multiple labels in one job.

    Looks up this SKU's assigned home location (warehouse.sku_locations)
    and prints it as large plain text on the label -- confirmed design,
    2026-09-17 (see labels.sku_label_zpl's docstring). A SKU with no
    home location assigned yet just prints without that line, same as
    omitting `description`.
    """
    if not require_secret():
        return jsonify({'error': 'unauthorized'}), 401
    try:
        count = max(1, int(request.args.get('count', 1)))
    except ValueError:
        return jsonify({'error': 'count must be a whole number'}), 400

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """select l.code from warehouse.sku_locations sl
                   join warehouse.locations l on l.id = sl.location_id
                   where upper(sl.sku) = upper(%s)""",
                (sku,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    home_location_code = row[0] if row else None

    zpl = sku_label_zpl(
        sku, description=request.args.get('description'), home_location_code=home_location_code,
    ) * count
    return _zpl_response(zpl, f'sku-{sku}.zpl')


@app.get('/labels/location/<path:location_code>')
def label_location(location_code):
    """Shelf/area label: its own fixed barcode, plus its stock type
    (RM/SA/FP) as plain text if set -- no "current SKU" barcode, since a
    shelf here commonly holds several different SKUs at once. See
    labels.location_label_zpl for why."""
    if not require_secret():
        return jsonify({'error': 'unauthorized'}), 401

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("select stock_type from warehouse.locations where upper(code) = upper(%s)", (location_code,))
            row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return jsonify({'error': 'No such location'}), 404
    stock_type = row[0]

    zpl = location_label_zpl(location_code, stock_type=stock_type)
    return _zpl_response(zpl, f'location-{location_code}.zpl')


@app.get('/labels/batch/<batch_id>')
def label_batch(batch_id):
    """Batch sticker -- the direct fix for a production run having no
    physical identifier as it moves through the factory."""
    if not require_secret():
        return jsonify({'error': 'unauthorized'}), 401

    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "select batch_code, sku, qty_planned, priority_rank from production.production_batches "
                "b join production.targets t on t.id = b.target_id where b.id = %s",
                (batch_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return jsonify({'error': 'No such batch'}), 404
    batch_code, sku, qty_planned, priority_rank = row

    zpl = batch_label_zpl(batch_code, sku, float(qty_planned), priority_rank)
    return _zpl_response(zpl, f'batch-{batch_code}.zpl')


# -- Total Product on Order (production admin download) --------------
# Real Cin7, READ ONLY -- same carve-out as /cin7/on-hand. Takes minutes
# on a cold cache, so: POST .../start kicks off a background build, GET
# .../status is polled, GET .../download returns the finished .xlsx.
# See open_orders_xlsx.py.

@app.post('/reports/open-orders/start')
def open_orders_start():
    if not require_secret():
        return jsonify({'error': 'unauthorized'}), 401
    return jsonify(open_orders_xlsx.start_build()), 202


@app.get('/reports/open-orders/status')
def open_orders_status():
    if not require_secret():
        return jsonify({'error': 'unauthorized'}), 401
    return jsonify(open_orders_xlsx.job_state()), 200


@app.get('/reports/open-orders/download')
def open_orders_download():
    if not require_secret():
        return jsonify({'error': 'unauthorized'}), 401
    data, filename = open_orders_xlsx.get_file()
    if not data:
        return jsonify({'error': 'No file built yet -- click Download to build one.'}), 404
    return Response(
        data,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8000)))
