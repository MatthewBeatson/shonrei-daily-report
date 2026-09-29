"""Dispatch Board -- packaging order lists.

Packaging SOs (boxes/bags) run on a much shorter clock than the Monthly
Dispatch Plan's 20-working-day pacing, so the TV board shows them in two
lists of their own:
  - plain (no printing charge): due the next NZ working day after the
    order date
  - printed (an additional charge like "Printed Lining ($0.48 per box)" /
    "Printed Logo ($0.48 per bag)"): due 5 NZ working days after

"Packaging" = any order line whose SKU starts with one of
board_packaging_sku_prefixes (default 'K' -- Matthew's call 2026-09-30:
~98% of K SKUs are packaging, easier than product categories). "Printed" =
any Order.AdditionalCharges[].Description containing one of
board_printing_charge_keywords (default 'PRINT'). SKUs starting with one
of board_packaging_sku_exclusions (default 'KS312' -- made in-house at
Shonrei, so standard 20-working-day plan lead time, Matthew's call
2026-09-30) never count as packaging. All of these, and the two lead
times, live in reporting.settings so they can change without a deploy.

Rewritten on every hourly refresh (called from daily_refresh_supabase.
cin7_sales(), which already walks every open order), not just Monday's
plan run -- a next-day deadline can't wait a week. No dollar values are
read or written here.
"""
from __future__ import annotations
from datetime import datetime, date

import psycopg2.extras

from dispatch_plan_schedule import working_days_after


def _split(v) -> list[str]:
    return [x.strip().upper() for x in str(v or '').split('|') if x.strip()]


def rules_from_settings(cfg: dict) -> dict:
    return {
        'prefixes': tuple(_split(cfg.get('board_packaging_sku_prefixes')) or ['K']),
        # None (setting absent) -> default; '' (cleared on purpose) -> no exclusions.
        'exclude': tuple(_split('KS312' if cfg.get('board_packaging_sku_exclusions') is None
                                else cfg.get('board_packaging_sku_exclusions'))),
        'keywords': _split(cfg.get('board_printing_charge_keywords')) or ['PRINT'],
        'plain_days': int(cfg.get('board_packaging_plain_days') or 1),
        'printed_days': int(cfg.get('board_packaging_printed_days') or 5),
    }


def classify(facts: dict | None, rules: dict) -> dict | None:
    """{'printed', 'units_remaining', 'line_count'} for a packaging SO with
    packaging units still to go out, else None. Units/lines count only the
    packaging (prefix-matching) SKUs, so a mixed order shows how much
    packaging is left, not its whole line count."""
    if not facts:
        return None
    pkg = {sku: qty for sku, qty in (facts.get('sku_remaining') or {}).items()
           if sku.upper().startswith(rules['prefixes'])
           and not (rules['exclude'] and sku.upper().startswith(rules['exclude']))}
    units = sum(max(0.0, q) for q in pkg.values())
    if units <= 0:
        return None
    printed = any(kw in (d or '').upper() for d in facts.get('charge_descriptions') or [] for kw in rules['keywords'])
    return {'printed': printed, 'units_remaining': units, 'line_count': sum(1 for q in pkg.values() if q > 0)}


def deadline_for(order_date: date, printed: bool, rules: dict) -> date:
    return working_days_after(order_date, rules['printed_days'] if printed else rules['plain_days'])


def _parse_date(v) -> date | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace('Z', '+00:00')).date()
    except ValueError:
        return None


def build_rows(candidates: list[tuple[dict, dict]], rules: dict, today: date) -> list[tuple]:
    """candidates: (saleList item, sale_board_facts) pairs."""
    rows = []
    for s, facts in candidates:
        c = classify(facts, rules)
        if not c:
            continue
        order_date = _parse_date(s.get('OrderDate') or s.get('SaleOrderDate')) or today
        rows.append((
            s.get('OrderNumber') or '', s.get('Customer') or '', s.get('CustomerReference') or '',
            order_date, c['printed'], deadline_for(order_date, c['printed'], rules),
            c['units_remaining'], c['line_count'],
        ))
    return rows


def write_packaging_orders(conn, cfg: dict, candidates: list[tuple[dict, dict]]):
    """Full replace of board.packaging_orders in one transaction."""
    rows = build_rows(candidates, rules_from_settings(cfg), date.today())
    with conn.cursor() as cur:
        cur.execute('delete from board.packaging_orders')
        if rows:
            psycopg2.extras.execute_values(
                cur,
                """insert into board.packaging_orders
                     (order_number, customer, reference, order_date, printed, deadline,
                      units_remaining, line_count)
                   values %s
                   on conflict (order_number) do nothing""",
                rows,
            )
    conn.commit()
    print(f'Dispatch Board: {len(rows)} packaging orders '
          f'({sum(1 for r in rows if r[4])} printed).', flush=True)
