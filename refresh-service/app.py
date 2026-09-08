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
from stocktake import StocktakeError, record_count, apply_adjustment
from warehouse import WarehouseError, record_putaway_scan, set_home_location
from labels import batch_label_zpl, location_label_zpl, sku_label_zpl

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
    Demand extraction from live Cin7 SOs isn't wired in yet (see
    backorder_targets.extract_demand_lines) -- for now the caller supplies
    it directly, same "live concept, Cin7 reads deferred" approach as
    /production/plan. Runs against DryRunCin7Client so no real Cin7
    assembly is touched (see dry_run_cin7.py).
    """
    if not require_secret():
        return jsonify({'error': 'unauthorized'}), 401

    payload = request.get_json(silent=True) or {}
    demand_lines_by_sku = payload.get('demand_lines_by_sku') or {}

    conn = get_conn()
    try:
        results = sync_targets(conn, DryRunCin7Client(conn), demand_lines_by_sku)
    finally:
        conn.close()

    return jsonify({'actions': results}), 200


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
    """Body: {"actual_qty", "reject_qty", "reported_via", "reported_by"}.
    The floor-input endpoint's backend: completes a real (today: dry-run)
    small Cin7 assembly and decrements the parent target. Called by the
    Node backend's POST /production/batch-actuals, not directly by the
    floor app -- see backend/src/routes/production.js.
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
                conn, DryRunCin7Client(conn), batch_id,
                actual_qty, payload.get('reject_qty') or 0,
                payload.get('reported_via') or 'manual', payload.get('reported_by'),
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
    Runs against DryRunCin7Client (see dry_run_cin7.py) -- recording a
    count never touches live Cin7 on its own, see stocktake.py.
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
                conn, DryRunCin7Client(conn), sku, counted_qty,
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
    """Pushes one already-recorded count to Cin7 as a stock adjustment --
    a deliberate, separate step from recording it, see stocktake.py.
    """
    if not require_secret():
        return jsonify({'error': 'unauthorized'}), 401

    payload = request.get_json(silent=True) or {}
    conn = get_conn()
    try:
        try:
            result = apply_adjustment(conn, DryRunCin7Client(conn), count_id, payload.get('note'))
        except StocktakeError as exc:
            conn.rollback()
            return jsonify({'error': str(exc)}), 409
    finally:
        conn.close()

    return jsonify(result), 200


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
        result = record_putaway_scan(conn, sku, scanned_location_code, payload.get('scanned_by'))
    finally:
        conn.close()

    return jsonify(result), 201


@app.put('/warehouse/sku-locations/<path:sku>')
def warehouse_set_home_location(sku):
    """Body: {"location_code"}. Assigns (or reassigns) a SKU's home
    location -- what a putaway scan is checked against.
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
            result = set_home_location(conn, sku, location_code)
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
    """
    if not require_secret():
        return jsonify({'error': 'unauthorized'}), 401
    zpl = sku_label_zpl(sku, description=request.args.get('description'))
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
            cur.execute("select stock_type from warehouse.locations where code = %s", (location_code,))
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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8000)))
