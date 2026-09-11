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

Two parallel ways a count reaches Cin7, confirmed design (2026-09-11),
both going through Cin7's real "Stock Adjustment" object either way:

  - **Formal stocktake** -- Shonrei's real process: admin starts a
    Cin7-native Stocktake (e.g. ST-00233) manually in Cin7's own UI,
    which locks the system against other inventory movements. Staff
    count by AREA (scan a warehouse.locations barcode to set which area
    they're counting in, then scan SKUs within it -- see
    production/README.md "Labels & warehouse locations"), so the same
    SKU can have several `stocktake.counts` rows, one per area, all
    sharing that SKU's stable on-hand snapshot (stable because Cin7
    locks movements during an active stocktake). `sync_stocktake_totals`
    sums a SKU's counts across every area and pushes ONE adjustment
    line per SKU, tagged with the active Stocktake number (from
    `stocktake.settings`, which admin sets/updates at any point in the
    cycle -- see `get_active_stocktake_number`/`set_active_stocktake_
    number`). Can be run repeatedly as areas finish counting, not just
    once at the end.
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
from collections import defaultdict


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


def aggregate_recorded_counts_by_sku(rows: list[tuple]) -> dict[str, float]:
    """Pure: sums counted_qty per SKU across however many rows (areas)
    each SKU has -- e.g. WIP110 counted 40 in "Stockroom - Main" and 15
    in "Pads & Linings Upstairs" sums to 55. `rows` is (sku, counted_qty)
    tuples (already filtered by caller to whichever counts should be
    included, normally status='recorded'). No DB/Cin7 access, unit
    tested in isolation the same way as everything in production/planner.
    """
    totals: dict[str, float] = defaultdict(float)
    for sku, counted_qty in rows:
        totals[sku] += float(counted_qty)
    return dict(totals)


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
    """Aggregates every currently-'recorded' count by SKU (across
    however many areas it was counted in -- see aggregate_recorded_
    counts_by_sku) and pushes ONE adjustment per SKU to Cin7, tagged
    with the active Stocktake number (StocktakeError if none is set --
    see set_active_stocktake_number). Safe to call repeatedly through a
    stocktake cycle as more areas finish counting; only SKUs still
    'recorded' at call time are included, so already-synced totals
    aren't re-pushed (and if someone counts more of an already-synced
    SKU afterwards, that new count is picked up on the next sync as its
    own fresh total for that SKU, not added to the old one -- Cin7's own
    Quantity-is-a-target-not-a-delta semantics mean the later push wins,
    not stacks).
    """
    stocktake_number = get_active_stocktake_number(conn)
    if not stocktake_number:
        raise StocktakeError('No active Cin7 stocktake number set -- see set_active_stocktake_number')

    with conn.cursor() as cur:
        cur.execute("select id, sku, counted_qty from stocktake.counts where status = 'recorded'")
        rows = cur.fetchall()
    if not rows:
        return []

    count_ids_by_sku: dict[str, list] = defaultdict(list)
    for count_id, sku, _counted_qty in rows:
        count_ids_by_sku[sku].append(count_id)
    totals = aggregate_recorded_counts_by_sku([(sku, qty) for _id, sku, qty in rows])

    results = []
    for sku, total_qty in totals.items():
        adjustment_id = cin7.adjust_stock_on_hand(
            sku, total_qty, note=f'Stocktake {stocktake_number}', stocktake_number=stocktake_number,
        )
        count_ids = count_ids_by_sku[sku]
        with conn.cursor() as cur:
            cur.execute(
                """update stocktake.counts
                   set status = 'adjusted', cin7_adjustment_id = %s, adjusted_at = now()
                   where id = any(%s)""",
                (adjustment_id, count_ids),
            )
        conn.commit()
        results.append({
            'sku': sku, 'total_counted_qty': total_qty, 'count_ids': [str(c) for c in count_ids],
            'cin7_adjustment_id': adjustment_id, 'stocktake_number': stocktake_number,
        })

    return results
