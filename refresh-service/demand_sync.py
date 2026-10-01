"""Pull backorder demand straight from live Cin7 open sales orders,
instead of production admin's manual demand-entry table.

Reuses open_orders_xlsx.py's open-order pulling wholesale (same
AUTHORISED-not-invoiced selection as "Total Product on Order"/"Sales on
hand" -- see that module's docstring for the exact Cin7 calls and
status-filter settings) rather than duplicating it: the per-line rows
it already extracts (sku, order #, order date, qty still to invoice)
map directly onto what production.targets.sync_targets wants as
demand_lines_by_sku ({sku: [{"so_number","order_date","qty_backordered"},
...]}) -- "qty still to invoice" IS "qty backordered" here, same
definition admin already uses for "Sales on hand".

This is what backorder_targets.extract_demand_lines() was always meant
to become once per-line Cin7 fields were confirmed -- that function
itself still raises (it was written for a single sale's detail, a
narrower shape than what open_orders_xlsx.py ended up confirming
against a whole open-orders pull), so this module supersedes it rather
than filling it in. Also shares open_orders_xlsx's in-memory per-sale
cache (keyed on Cin7's own listing signature) -- a demand pull run
shortly after (or before) a "Total Product on Order" download re-reads
nothing it doesn't have to, and vice versa.

READ ONLY against Cin7 -- this module only ever reads; the actual
target create/adjust/close writes happen in backorder_targets.
sync_targets, called separately once the pulled demand is reviewed and
the admin clicks "Sync targets" (see production/admin/app.js). Same
2-3-minutes-on-a-cold-cache / background-job-with-status-polling shape
as open_orders_xlsx.py, for the same reason (one sale detail per open
order at ~1/sec, past gunicorn's 120s timeout).
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from daily_refresh_supabase import cin7_list_signature
from open_orders_xlsx import _detail_cache, _session, cin7_get, open_lines_for_sale, open_sales

NZ = ZoneInfo('Pacific/Auckland')

_lock = threading.Lock()
_job = {
    'status': 'idle', 'done': 0, 'total': 0, 'started_at': None, 'finished_at': None,
    'error': None, 'orders': 0, 'sku_count': 0,
}
_demand_lines_by_sku: dict[str, list[dict]] = {}
_summary: list[dict] = []


def job_state() -> dict:
    with _lock:
        return dict(_job)


def get_result() -> tuple[dict, list[dict]]:
    """demand_lines_by_sku (what sync_targets wants) and a per-SKU
    summary (sku, total qty, SO count -- what the admin page shows for
    review before syncing)."""
    with _lock:
        return dict(_demand_lines_by_sku), list(_summary)


def _set(**kw):
    with _lock:
        _job.update(kw)


def _pull():
    global _demand_lines_by_sku, _summary
    try:
        session = _session()
        sales = open_sales(session)
        _set(total=len(sales))

        demand_lines_by_sku: dict[str, list[dict]] = {}
        fetched = 0
        for i, s in enumerate(sales, 1):
            sid = str(s['SaleID'])
            sig = cin7_list_signature(s)
            cached = _detail_cache.get(sid)
            if cached and cached[0] == sig:
                detail = cached[1]
            else:
                if fetched:
                    time.sleep(1.1)  # stay under Cin7's 60 calls/minute
                detail = cin7_get(session, 'sale', {'ID': sid})
                if isinstance(detail.get('Sale'), dict):
                    detail = detail['Sale']
                _detail_cache[sid] = (sig, detail)
                fetched += 1
            for row in open_lines_for_sale(s, detail):
                demand_lines_by_sku.setdefault(row['sku'], []).append({
                    'so_number': row['order'],
                    'order_date': row['date'].isoformat() if row['date'] else None,
                    'qty_backordered': row['qty'],
                })
            _set(done=i)

        summary = sorted(
            (
                {'sku': sku, 'qty': sum(l['qty_backordered'] for l in lines), 'so_count': len(lines)}
                for sku, lines in demand_lines_by_sku.items()
            ),
            key=lambda r: r['sku'],
        )

        with _lock:
            _demand_lines_by_sku = demand_lines_by_sku
            _summary = summary
            _job.update(
                status='done', finished_at=datetime.now(NZ).isoformat(),
                orders=len(sales), sku_count=len(summary),
            )
        print(f'demand_sync: pulled {len(sales)} orders -> {len(summary)} SKUs ({fetched} details fetched)', flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f'demand_sync: pull failed: {exc}', flush=True)
        _set(status='error', error=str(exc)[:500], finished_at=datetime.now(NZ).isoformat())


def start_pull() -> dict:
    """Starts a pull unless one is already running. Returns the state."""
    with _lock:
        if _job['status'] == 'running':
            return dict(_job)
        _job.update(status='running', done=0, total=0, error=None,
                    started_at=datetime.now(NZ).isoformat(), finished_at=None)
    threading.Thread(target=_pull, daemon=True).start()
    return job_state()
