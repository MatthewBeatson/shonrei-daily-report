"""Stocktake: record a physical count, snapshot it against Cin7's on-hand
qty, and (only once reviewed) push it to Cin7 as a stock adjustment.

Replaces the old Google Sheet + AppSheet workflow -- see
production/README.md "Stocktake" for the reasoning. Two deliberate design
choices carried over from that workflow rather than reinvented:

  - Recording a count and pushing the adjustment are two separate steps.
    AppSheet let anyone log a count at any time without it silently
    changing Cin7's stock; that stays true here -- record_count() never
    calls Cin7, only apply_adjustment()/sync_stocktake_totals() do, and
    only when someone (admin screen) explicitly asks.
  - A count is a single SKU at a single moment, not a session someone
    has to "open" and "close" -- matches how staff actually used the old
    sheet (whenever they noticed a discrepancy, not on a schedule).

Two parallel ways a count reaches Cin7, both going through Cin7's real
"Stock Adjustment" object either way:

  - **Formal stocktake** -- Shonrei's real process: admin starts a
    Cin7-native Stocktake (e.g. ST-00233) manually in Cin7's own UI,
    which locks the system against other inventory movements. Staff
    count by AREA (scan a warehouse.locations barcode to set which area
    they're counting in, then scan SKUs within it -- see
    production/README.md "Labels & warehouse locations"), so the same
    SKU can have several `stocktake.counts` rows, one per area.

    CONFIRMED LIVE (2026-09-16, 14LSWL/NB): each of our counting areas
    IS a real Cin7 Bin (Settings > Reference Books > Locations > Bins),
    and Cin7 tracks quantity PER BIN as its own real figure, not just a
    label -- see cin7_client.adjust_stock_on_hand's docstring. Scanning
    an area's barcode really is scanning its Cin7 Bin. That changes the
    design from an earlier draft (this module used to sum a SKU's counts
    across every area into one combined adjustment): now each count
    stays scoped to its own area/bin and `sync_stocktake_totals` pushes
    it AS-IS, tagged with that area's own `cin7_bin` (migration 014) and
    the active Stocktake number (from `stocktake.settings`, admin sets/
    updates any time during the cycle -- see
    `get_active_stocktake_number`/`set_active_stocktake_number`). No
    in-app aggregation needed or wanted: Cin7 already keeps its own
    per-bin total, and a combined figure would have blended two
    genuinely distinct bin quantities into one, leaving Cin7's own
    per-bin numbers stale even though the SKU-wide total came out right.
    Can be run repeatedly as areas finish counting, not just once at the
    end -- a count whose area has no `cin7_bin` linked yet is reported
    back as skipped (not silently dropped, not left to error the whole
    sync) so admin can see it needs the location's Cin7 Bin set first.
  - **Ad-hoc** -- the original rolling log, unchanged: any SKU, any
    time, no formal stocktake event needed, pushed one count at a time
    via `apply_adjustment`. Cin7 treats this as its own separate Stock
    Adjustment (never carrying a Stocktake number -- there's only ever
    one Stocktake "IN PROGRESS" in Cin7 at a time, and ad-hoc corrections
    aren't part of it).

Still open (see production/README.md): whether Cin7 accepts a Stock
Adjustment tagged with a StocktakeNumber the way this assumes (the field
itself is documented, but not yet live-tested with a real ST number),
and whether an ad-hoc adjustment can be appended to an already-open one
(cin7_client.get_open_stock_adjustments lists candidates, but nothing
here re-opens one yet -- every push still creates its own fresh
Draft->Completed adjustment, the one proven-safe pattern so far).
"""
from __future__ import annotations


class StocktakeError(ValueError):
    pass


def get_active_stocktake_number(conn) -> str | None:
    with conn.cursor() as cur:
        cur.execute("select active_cin7_stocktake_number from stocktake.settings where id = true")
        row = cur.fetchone()
    return row[0] if row else None


def set_active_stocktake_number(conn, stocktake_number: str | None, updated_by: str | None = None) -> dict:
    """Admin sets/clears/replaces which Cin7 Stocktake (e.g. "ST-00233")
    future sync_stocktake_totals() calls tag their adjustments with --
    can be changed any time during a stocktake cycle (e.g. if the number
    Cin7 assigned turns out different from what was first entered)."""
    with conn.cursor() as cur:
        cur.execute(
            "update stocktake.settings set active_cin7_stocktake_number = %s, updated_by = %s where id = true",
            (stocktake_number, updated_by),
        )
    conn.commit()
    return {'active_cin7_stocktake_number': stocktake_number}


def record_count(
    conn, cin7, sku: str, counted_qty: float, *,
    location: str | None = None, reported_via: str = 'manual', reported_by: str | None = None,
) -> dict:
    """Writes the count, snapshotting Cin7's on-hand qty for `sku` at this
    moment if the get_stock_on_hand call succeeds -- a variance is
    nice-to-have, not a precondition for logging a count. Deliberately
    broad except: NotImplementedError (a client that hasn't wired this
    up yet), a bad/unknown SKU, and a live Cin7 network/API hiccup all
    fail the same way here -- on_hand stays None and the count still
    gets written. Only counted_qty's own validation above is allowed to
    actually stop the count from being recorded.
    """
    if counted_qty < 0:
        raise StocktakeError('counted_qty must be >= 0')

    try:
        on_hand = cin7.get_stock_on_hand(sku)
    except Exception as exc:  # noqa: BLE001 -- see docstring: recording the count always wins
        print(f'stocktake: could not snapshot Cin7 on-hand for {sku!r}: {exc}', flush=True)
        on_hand = None

    with conn.cursor() as cur:
        cur.execute(
            """insert into stocktake.counts
                   (sku, location, counted_qty, cin7_on_hand_snapshot, reported_via, reported_by)
               values (%s, %s, %s, %s, %s, %s)
               returning id, variance""",
            (sku, location, counted_qty, on_hand, reported_via, reported_by),
        )
        count_id, variance = cur.fetchone()
    conn.commit()

    return {
        'count_id': str(count_id),
        'sku': sku,
        'counted_qty': counted_qty,
        'cin7_on_hand_snapshot': on_hand,
        'variance': float(variance) if variance is not None else None,
    }


def apply_adjustment(conn, cin7, count_id: str, note: str | None = None) -> dict:
    """Pushes one already-recorded count to Cin7 as a stock adjustment,
    setting on-hand to the counted quantity. Only valid on a count still
    in 'recorded' status -- each count can be pushed at most once.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select sku, counted_qty, status from stocktake.counts where id = %s",
            (count_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise StocktakeError(f'No such count: {count_id}')
    sku, counted_qty, status = row
    if status != 'recorded':
        raise StocktakeError(f"Count is already '{status}' -- can't adjust again")

    adjustment_id = cin7.adjust_stock_on_hand(sku, float(counted_qty), note)

    with conn.cursor() as cur:
        cur.execute(
            """update stocktake.counts
               set status = 'adjusted', cin7_adjustment_id = %s, adjusted_at = now()
               where id = %s""",
            (adjustment_id, count_id),
        )
    conn.commit()

    return {'count_id': count_id, 'sku': sku, 'cin7_adjustment_id': adjustment_id}


def sync_stocktake_totals(conn, cin7) -> list[dict]:
    """Pushes every currently-'recorded' count to Cin7 as its OWN
    adjustment, one per count -- each count is already scoped to a
    single area, and each area IS a real Cin7 Bin (see this module's
    docstring), so there's no aggregation step: `adjust_stock_on_hand`
    is called with that count's own counted_qty as the TARGET on-hand
    for that specific bin (`bin_name=`), tagged with the active
    Stocktake number (StocktakeError if none is set -- see
    set_active_stocktake_number).

    A count whose area has no `cin7_bin` linked yet (migration 014) is
    reported back with `skipped: True` rather than erroring the whole
    sync or silently dropping it -- admin needs to link that location's
    Cin7 Bin before it can sync (see production/admin's Warehouse
    Locations section).

    Safe to call repeatedly through a stocktake cycle as more areas
    finish counting; only counts still 'recorded' at call time are
    included, so already-synced ones aren't re-pushed.
    """
    stocktake_number = get_active_stocktake_number(conn)
    if not stocktake_number:
        raise StocktakeError('No active Cin7 stocktake number set -- see set_active_stocktake_number')

    with conn.cursor() as cur:
        cur.execute(
            """select c.id, c.sku, c.counted_qty, l.cin7_bin
               from stocktake.counts c
               left join warehouse.locations l on l.code = c.location
               where c.status = 'recorded'"""
        )
        rows = cur.fetchall()
    if not rows:
        return []

    results = []
    for count_id, sku, counted_qty, cin7_bin in rows:
        if not cin7_bin:
            results.append({
                'count_id': str(count_id), 'sku': sku, 'skipped': True,
                'reason': "This count's area has no Cin7 Bin linked yet -- set one in "
                          'Warehouse Locations before syncing.',
            })
            continue

        adjustment_id = cin7.adjust_stock_on_hand(
            sku, float(counted_qty), note=f'Stocktake {stocktake_number}',
            bin_name=cin7_bin, stocktake_number=stocktake_number,
        )
        with conn.cursor() as cur:
            cur.execute(
                """update stocktake.counts
                   set status = 'adjusted', cin7_adjustment_id = %s, adjusted_at = now()
                   where id = %s""",
                (adjustment_id, count_id),
            )
        conn.commit()
        results.append({
            'count_id': str(count_id), 'sku': sku, 'counted_qty': float(counted_qty),
            'cin7_bin': cin7_bin, 'cin7_adjustment_id': adjustment_id, 'stocktake_number': stocktake_number,
        })

    return results
