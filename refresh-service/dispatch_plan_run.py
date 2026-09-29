"""Monthly Dispatch Plan -- orchestration.

Ties together dispatch_plan_data (Cin7 pull) + dispatch_plan_schedule (pure
scheduling) + dispatch_plan_xlsx (workbook writer), then uploads the result
to Supabase Storage and records it in reporting.dispatch_plan_current /
dispatch_plan_log. This is a full rebuild every run, not an incremental
update -- matches the "full rebuild each Monday" requirement, and means a
failed run never leaves a half-updated plan (dispatch_plan_current is only
written at the very end, after a successful upload).

Reuses:
  - get_conn / load_settings / log_step from daily_refresh_supabase.py
  - the same Cin7 header construction as daily_refresh_supabase.cin7_sales()
  - the latest reporting.report_snapshots row's sales_mtd as "invoiced to
    date this month" (rather than re-querying Xero -- the hourly daily
    refresh already keeps that fresh) and reporting.manual_inputs'
    monthly_breakeven, per Matthew's call this session.

Also writes board.plan_orders -- one row per SO, deliberately with NO
dollar columns -- which is all the dispatch-area TV board (/board/) ever
reads about the plan. See migrations/017_dispatch_board.sql.
"""
from __future__ import annotations
import io
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg2.extras
import requests

from daily_refresh_supabase import get_conn, load_settings, log_step
from dispatch_plan_data import fetch_open_orders
from dispatch_plan_schedule import group_orders, schedule_groups, month_key
from dispatch_plan_xlsx import build_workbook
from board_packaging import classify, rules_from_settings

STORAGE_BUCKET = 'dispatch-plans'


def _cin7_headers():
    aid = os.environ.get('CIN7_ACCOUNT_ID')
    key = os.environ.get('CIN7_API_KEY')
    if not aid or not key:
        raise RuntimeError('Cin7 credentials missing (CIN7_ACCOUNT_ID / CIN7_API_KEY).')
    return {'api-auth-accountid': aid, 'api-auth-applicationkey': key, 'Content-Type': 'application/json'}


def _load_overrides(conn) -> dict[str, dict]:
    with conn.cursor() as cur:
        cur.execute(
            'select order_number, group_label_override, hold, note, dispatch_date_override '
            'from reporting.dispatch_plan_overrides'
        )
        return {
            row[0]: {'group_label_override': row[1], 'hold': row[2], 'note': row[3], 'dispatch_date_override': row[4]}
            for row in cur.fetchall()
        }


def _latest_sales_mtd(conn) -> float:
    with conn.cursor() as cur:
        cur.execute(
            "select sales_mtd from reporting.report_snapshots "
            "where sales_status = 'ok' order by as_of desc limit 1"
        )
        row = cur.fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0


def _monthly_breakeven(conn) -> float | None:
    with conn.cursor() as cur:
        cur.execute("select monthly_breakeven from reporting.manual_inputs where id = 'current'")
        row = cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None


def _write_board_snapshot(conn, cfg, orders, schedule, holding):
    """Full replace of board.plan_orders from this run's schedule. Same
    transaction as the delete, so the TV never sees a half-empty plan.
    No value_excl_gst anywhere in here -- the board has no $ by design."""
    by_so = {o['order_number']: o for o in orders}
    rules = rules_from_settings(cfg)
    rows = []
    groups = [g for month in schedule.values() for wk in month.values() for g in wk] + list(holding)
    for g in groups:
        for so in g.order_numbers:
            o = by_so.get(so)
            if not o:
                continue
            rows.append((
                so, o['customer'], o['reference'], g.label if len(g.order_numbers) > 1 else None,
                o['order_date'], None if g.hold else g.scheduling_date,
                g.assigned_month, g.assigned_week, g.hold,
                o.get('units_remaining'), o.get('line_count'),
                # Packaging SOs get their own hourly-updated lists on the
                # board (board_packaging.py) -- flagged here so the weekly
                # plan list doesn't show them a second time.
                classify(o.get('board_facts'), rules) is not None,
            ))
    with conn.cursor() as cur:
        cur.execute('delete from board.plan_orders')
        if rows:
            psycopg2.extras.execute_values(
                cur,
                """insert into board.plan_orders
                     (order_number, customer, reference, group_label, order_date, dispatch_date,
                      assigned_month, assigned_week, hold, units_remaining, line_count, is_packaging)
                   values %s""",
                rows,
            )
    conn.commit()


def _upload_to_storage(path: str, content: bytes):
    supabase_url = os.environ['SUPABASE_URL'].rstrip('/')
    service_key = os.environ['SUPABASE_SERVICE_ROLE_KEY']
    r = requests.post(
        f'{supabase_url}/storage/v1/object/{STORAGE_BUCKET}/{path}',
        headers={
            'Authorization': f'Bearer {service_key}',
            'apikey': service_key,
            'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'x-upsert': 'true',
        },
        data=content,
        timeout=60,
    )
    if not r.ok:
        raise RuntimeError(f'Supabase Storage upload failed ({r.status_code}): {r.text[:1000]}')


def run_dispatch_plan(triggered_by=None):
    conn = get_conn()
    try:
        cfg = load_settings(conn)
        tz = ZoneInfo(cfg.get('timezone') or 'Pacific/Auckland')
        now = datetime.now(tz)
        today = now.date()
        monthly_target = float(cfg.get('dispatch_plan_monthly_target') or 220000)
        small_order_threshold = float(cfg.get('dispatch_plan_small_order_threshold') or 1000)
        large_order_carveout_threshold = float(cfg.get('dispatch_plan_large_order_carveout_threshold') or 7500)

        try:
            headers = _cin7_headers()
            orders = fetch_open_orders(conn, cfg, headers)
            overrides = _load_overrides(conn)
            groups = group_orders(orders, overrides, large_order_carveout_threshold)

            invoiced_to_date = _latest_sales_mtd(conn)
            breakeven = _monthly_breakeven(conn)

            schedule, holding = schedule_groups(
                groups, today=today, monthly_target=monthly_target,
                invoiced_to_date_for_current_month=invoiced_to_date,
            )

            current_month = month_key(today)
            invoiced_by_month = {current_month: invoiced_to_date}
            for mk in schedule.keys():
                invoiced_by_month.setdefault(mk, 0.0)

            wb = build_workbook(schedule, holding, monthly_target, invoiced_by_month, breakeven, today, small_order_threshold)

            buf = io.BytesIO()
            wb.save(buf)
            content = buf.getvalue()

            storage_path = f'dispatch-plan-{today.isoformat()}.xlsx'
            _upload_to_storage(storage_path, content)

            _write_board_snapshot(conn, cfg, orders, schedule, holding)

            months_covered = sorted(schedule.keys())
            with conn.cursor() as cur:
                cur.execute(
                    """
                    update reporting.dispatch_plan_current
                    set storage_path = %s, generated_at = now(), months_covered = %s,
                        status = 'ok', triggered_by = %s
                    where id = 'current'
                    """,
                    (storage_path, months_covered, triggered_by),
                )
            conn.commit()

            order_count = sum(len(g.order_numbers) for month in schedule.values() for wk in month.values() for g in wk)
            order_count += sum(len(g.order_numbers) for g in holding)
            log_detail = (f'{order_count} orders across {len(months_covered)} month(s) '
                          f'({", ".join(months_covered)}); {len(holding)} held; storage_path={storage_path}')
            with conn.cursor() as cur:
                cur.execute(
                    "insert into reporting.dispatch_plan_log (step, status, detail, triggered_by) "
                    "values (%s, %s, %s, %s)",
                    ('Generate dispatch plan', 'OK', log_detail, triggered_by),
                )
            conn.commit()
            print(f'Dispatch plan complete: {log_detail}', flush=True)
            return {'status': 'ok', 'storage_path': storage_path, 'months_covered': months_covered}

        except Exception:
            import traceback
            detail = traceback.format_exc()
            with conn.cursor() as cur:
                cur.execute(
                    "insert into reporting.dispatch_plan_log (step, status, detail, triggered_by) "
                    "values (%s, %s, %s, %s)",
                    ('Generate dispatch plan', 'ERROR', detail, triggered_by),
                )
                cur.execute(
                    "update reporting.dispatch_plan_current set status = 'error', triggered_by = %s where id = 'current'",
                    (triggered_by,),
                )
            conn.commit()
            print(f'Dispatch plan failed: {detail}', flush=True)
            return {'status': 'error', 'detail': detail}
    finally:
        conn.close()


if __name__ == '__main__':
    run_dispatch_plan()
