"""Locations, home-location assignment, and putaway-scan recording -- the
DB side of the "product placed anywhere" fix. Pure decision logic lives
in production/planner/putaway.py; this module is just the plumbing that
reads/writes warehouse.* around it, same split as every other feature in
this project.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'production' / 'planner'))
from putaway import check_putaway  # noqa: E402


class WarehouseError(ValueError):
    pass


def record_putaway_scan(conn, sku: str, scanned_location_code: str, scanned_by: str | None = None) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """select l.id, l.code from warehouse.sku_locations sl
               join warehouse.locations l on l.id = sl.location_id
               where sl.sku = %s""",
            (sku,),
        )
        row = cur.fetchone()
    home_location_id, home_location_code = row if row else (None, None)

    result = check_putaway(scanned_location_code, home_location_code)

    with conn.cursor() as cur:
        cur.execute(
            """insert into warehouse.putaway_scans
                   (sku, scanned_location_code, expected_location_id, matched, scanned_by)
               values (%s, %s, %s, %s, %s)
               returning id""",
            (sku, scanned_location_code, home_location_id, bool(result.matched), scanned_by),
        )
        (scan_id,) = cur.fetchone()
    conn.commit()

    return {
        'scan_id': str(scan_id),
        'sku': sku,
        'scanned_location_code': scanned_location_code,
        'expected_location_code': result.expected_location_code,
        'matched': result.matched,  # true / false / null (no home assigned yet) -- see putaway.py
    }


def set_home_location(conn, sku: str, location_code: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("select id from warehouse.locations where code = %s", (location_code,))
        row = cur.fetchone()
        if row is None:
            raise WarehouseError(f'No such location code: {location_code}')
        (location_id,) = row

        cur.execute(
            """insert into warehouse.sku_locations (sku, location_id)
               values (%s, %s)
               on conflict (sku) do update set location_id = excluded.location_id
               returning sku, location_id""",
            (sku, location_id),
        )
        cur.fetchone()
    conn.commit()
    return {'sku': sku, 'location_code': location_code}
