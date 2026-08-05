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
from flask import Flask, request, jsonify
from daily_refresh_supabase import get_conn, run_refresh, load_settings

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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8000)))
