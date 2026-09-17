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


def get_putaway_mismatch_mode(conn) -> str:
    """'warn' (default) or 'block' -- see migration 015. Admin-set, read
    by the floor app before deciding whether a mismatched Putaway scan
    can be waved through or must be retried."""
    with conn.cursor() as cur:
        cur.execute("select putaway_mismatch_mode from warehouse.settings where id = true")
        row = cur.fetchone()
    return row[0] if row else 'warn'


def set_putaway_mismatch_mode(conn, mode: str, updated_by: str | None = None) -> dict:
    if mode not in ('warn', 'block'):
        raise WarehouseError(f"mode must be 'warn' or 'block', got {mode!r}")
    with conn.cursor() as cur:
        cur.execute(
            "update warehouse.settings set putaway_mismatch_mode = %s, updated_by = %s where id = true",
            (mode, updated_by),
        )
    conn.commit()
    return {'putaway_mismatch_mode': mode}


def record_putaway_scan(conn, sku: str, scanned_location_code: str, scanned_by: str | None = None) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """select l.id, l.code, l.bay_code from warehouse.sku_locations sl
               join warehouse.locations l on l.id = sl.location_id
               where sl.sku = %s""",
            (sku,),
        )
        row = cur.fetchone()
    home_location_id, home_location_code, home_bay_code = row if row else (None, None, None)

    # Bay code of wherever was actually scanned, if it resolves to a
    # known location at all -- used only to classify a genuine mismatch
    # as same-bay vs different-bay (see check_putaway). An unrecognised
    # scanned code just means scanned_bay_code stays None, which
    # check_putaway already treats as "not the same bay."
    with conn.cursor() as cur:
        cur.execute("select bay_code from warehouse.locations where upper(code) = upper(%s)", (scanned_location_code,))
        scanned_row = cur.fetchone()
    scanned_bay_code = scanned_row[0] if scanned_row else None

    result = check_putaway(
        scanned_location_code, home_location_code,
        scanned_bay_code=scanned_bay_code, home_bay_code=home_bay_code,
    )

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

    # The single field the floor app actually branches its UI on --
    # combines the pure match/same-bay classification (putaway.py) with
    # the admin-configured mode (migration 015) so the frontend doesn't
    # need to replicate this logic itself:
    #   match     -- scan matched, nothing to do
    #   no_home   -- this SKU has no home location assigned yet
    #   warn      -- mismatch, but same bay (always) or admin mode is
    #                'warn' -- staff can continue past it
    #   block     -- mismatch in a different bay AND admin mode is
    #                'block' -- staff must rescan until it matches
    if result.matched:
        action = 'match'
    elif result.matched is None:
        action = 'no_home'
    elif result.same_bay:
        action = 'warn'
    else:
        action = get_putaway_mismatch_mode(conn)

    return {
        'scan_id': str(scan_id),
        'sku': sku,
        'scanned_location_code': scanned_location_code,
        'expected_location_code': result.expected_location_code,
        'matched': result.matched,  # true / false / null (no home assigned yet) -- see putaway.py
        'same_bay': result.same_bay,  # true / false / null (not a mismatch, not applicable)
        'action': action,
    }


def sku_stock_type(conn, sku: str) -> str | None:
    """A SKU's RM/SA/FP classification, derived from its home location's
    own stock_type (migration 011) -- there's no per-SKU field for this,
    only per-location, so a SKU with no home location assigned, or whose
    home location has no stock_type set, returns None. Used to decide
    label-printing behaviour at batch completion (see backorder_targets.
    apply_batch_actual): FP gets an auto "print one label per item"
    prompt, SA/RM/unknown doesn't (matches how these are actually
    stored -- an SA like WIP110 sits 1000s-to-a-carton, one SKU label on
    request is enough; FP needs a label on the back of each unit).
    """
    with conn.cursor() as cur:
        cur.execute(
            """select l.stock_type from warehouse.sku_locations sl
               join warehouse.locations l on l.id = sl.location_id
               where sl.sku = %s""",
            (sku,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def set_home_location(conn, sku: str, location_code: str, cin7=None) -> dict:
    """Assigns (or reassigns) a SKU's home location -- what a Putaway
    scan is checked against. When `cin7` is given, also pushes the same
    location as Cin7's own product DefaultLocation (confirmed design,
    2026-09-17) -- best-effort: a failure there is logged and returned
    (`cin7_push_error`), never rolls back or blocks the local
    assignment, same "logging always wins" philosophy as
    record_count's Cin7 snapshot lookup.
    """
    with conn.cursor() as cur:
        cur.execute("select id, cin7_bin from warehouse.locations where code = %s", (location_code,))
        row = cur.fetchone()
        if row is None:
            raise WarehouseError(f'No such location code: {location_code}')
        location_id, cin7_bin = row

        cur.execute(
            """insert into warehouse.sku_locations (sku, location_id)
               values (%s, %s)
               on conflict (sku) do update set location_id = excluded.location_id
               returning sku, location_id""",
            (sku, location_id),
        )
        cur.fetchone()
    conn.commit()

    result = {'sku': sku, 'location_code': location_code}
    if cin7 is not None and cin7_bin:
        try:
            cin7.update_product_default_location(sku, cin7_bin)
            result['cin7_default_location_updated'] = True
        except Exception as exc:  # noqa: BLE001 -- best-effort, see docstring
            print(f'set_home_location: could not push DefaultLocation to Cin7 for {sku!r}: {exc}', flush=True)
            result['cin7_default_location_updated'] = False
            result['cin7_push_error'] = str(exc)
    elif cin7 is not None:
        result['cin7_default_location_updated'] = False
        result['cin7_push_error'] = f'Location {location_code!r} has no cin7_bin set yet'
    return result
