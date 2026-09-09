"""Stocktake: record a physical count, snapshot it against Cin7's on-hand
qty, and (only once reviewed) push it to Cin7 as a stock adjustment.

Replaces the old Google Sheet + AppSheet workflow -- see
production/README.md "Stocktake" for the reasoning. Two deliberate design
choices carried over from that workflow rather than reinvented:

  - Recording a count and pushing the adjustment are two separate steps.
    AppSheet let anyone log a count at any time without it silently
    changing Cin7's stock; that stays true here -- record_count() never
    calls Cin7, only apply_adjustment() does, and only when someone
    (admin screen) explicitly asks for that one count.
  - A count is a single SKU at a single moment, not a session someone
    has to "open" and "close" -- matches how staff actually used the old
    sheet (whenever they noticed a discrepancy, not on a schedule).
"""
from __future__ import annotations


class StocktakeError(ValueError):
    pass


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
