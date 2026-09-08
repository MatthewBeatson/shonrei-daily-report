"""Pulls open-order backorder demand, decides target actions
(target_sync.plan_target_actions) and applies them to both the
`production` schema and Cin7 (via whatever client is passed in --
DryRunCin7Client today, a real Cin7Client once its stubs are wired, see
dry_run_cin7.py).

Demand extraction (which SKU/qty is actually backordered on each open
order) is the one piece still marked TODO-confirm: dispatch_plan_data.
fetch_open_orders() gives confirmed order-level fields (OrderNumber,
OrderDate, Customer, ShipBy -- see that module's docstring) but not
per-line SKU/qty, which lives in the sale detail's line items under a
shape this repo hasn't confirmed against live data yet. extract_
demand_lines() is where that goes once scripts/dump_sample_bom.py (or a
sale-detail equivalent) confirms the real field names -- it currently
raises so a caller can't silently plan against guessed data.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'production' / 'planner'))
from target_sync import ExistingTarget, apply_actual_to_target, plan_target_actions  # noqa: E402


class DemandExtractionNotConfirmed(NotImplementedError):
    pass


def extract_demand_lines(sale_detail: dict) -> list[dict]:
    """Given one Cin7 sale's full detail (as returned by cin7_get('sale',
    ...), same shape dispatch_plan_data.py already fetches), return
    [{"sku": ..., "qty_backordered": ...}, ...] for its backordered
    lines.

    Not implemented: per-line backorder qty (ordered vs picked/invoiced)
    needs confirming against a real sale detail response first -- run
    scripts/dump_sample_bom.py-style inspection on a known-backordered
    sale before filling this in. Kept as a hard error rather than a
    guess so a bad field name doesn't silently produce a wrong plan.
    """
    raise DemandExtractionNotConfirmed(
        'extract_demand_lines: confirm the real per-line SKU/backorder-qty '
        'field names against a live Cin7 sale detail before wiring this in'
    )


def fetch_existing_targets(conn) -> dict[str, ExistingTarget]:
    with conn.cursor() as cur:
        cur.execute("select id, sku, outstanding_qty from production.targets where status = 'active'")
        return {sku: ExistingTarget(id=str(tid), outstanding_qty=float(qty)) for tid, sku, qty in cur.fetchall()}


def sync_targets(conn, cin7, demand_lines_by_sku: dict[str, list[dict]]) -> list[dict]:
    """demand_lines_by_sku: sku -> [{"so_number", "order_date", "qty_backordered"}, ...],
    already the caller's responsibility to have extracted (see
    extract_demand_lines) and sorted oldest-order-date-first per SKU.

    Applies target_sync's decisions: creates/adjusts/closes each SKU's
    `production.targets` row and calls the matching Cin7 method, then
    replaces that target's `target_demand_lines` with the fresh
    priority-ordered list. Returns one summary dict per action taken
    (kind != 'noop').
    """
    existing = fetch_existing_targets(conn)
    demand_totals = {sku: sum(l['qty_backordered'] for l in lines) for sku, lines in demand_lines_by_sku.items()}
    actions = plan_target_actions(demand_totals, existing)

    results = []
    for action in actions:
        if action.kind == 'noop':
            continue

        if action.kind == 'create':
            assembly = cin7.create_authorised_assembly(action.sku, action.new_outstanding_qty)
            with conn.cursor() as cur:
                cur.execute(
                    """insert into production.targets (sku, outstanding_qty, cin7_assembly_id)
                       values (%s, %s, %s) returning id""",
                    (action.sku, action.new_outstanding_qty, assembly.assembly_id),
                )
                (target_id,) = cur.fetchone()
        elif action.kind == 'adjust':
            with conn.cursor() as cur:
                cur.execute("select cin7_assembly_id from production.targets where id = %s", (action.target_id,))
                (assembly_id,) = cur.fetchone()
            cin7.adjust_assembly_qty(assembly_id, action.new_outstanding_qty)
            with conn.cursor() as cur:
                cur.execute(
                    "update production.targets set outstanding_qty = %s where id = %s",
                    (action.new_outstanding_qty, action.target_id),
                )
            target_id = action.target_id
        elif action.kind == 'close':
            with conn.cursor() as cur:
                cur.execute("select cin7_assembly_id from production.targets where id = %s", (action.target_id,))
                (assembly_id,) = cur.fetchone()
            if assembly_id:
                cin7.close_assembly(assembly_id)
            with conn.cursor() as cur:
                cur.execute(
                    "update production.targets set status = 'closed', outstanding_qty = 0, closed_at = now() where id = %s",
                    (action.target_id,),
                )
            target_id = action.target_id
        else:
            continue

        if action.kind in ('create', 'adjust'):
            lines = sorted(demand_lines_by_sku.get(action.sku, []), key=lambda l: l.get('order_date') or '')
            with conn.cursor() as cur:
                cur.execute("delete from production.target_demand_lines where target_id = %s", (target_id,))
                for rank, line in enumerate(lines, start=1):
                    cur.execute(
                        """insert into production.target_demand_lines
                               (target_id, so_number, order_date, qty_backordered, priority_rank)
                           values (%s, %s, %s, %s, %s)""",
                        (target_id, line['so_number'], line.get('order_date'), line['qty_backordered'], rank),
                    )

        conn.commit()
        results.append({'sku': action.sku, 'kind': action.kind, 'target_id': str(target_id)})

    return results


def apply_batch_actual(conn, cin7, batch_id: str, actual_qty: float, reject_qty: float, reported_via: str, reported_by: str | None) -> dict:
    """The floor-input side: records a batch's actual quantity, completes
    a real small Cin7 assembly for it (Create->Authorise->Allocate->
    Complete, see cin7_client.complete_small_assembly), and decrements
    the parent target's outstanding_qty (clamped at zero -- see
    target_sync.apply_actual_to_target), closing the target out via Cin7
    if that reaches zero.
    """
    with conn.cursor() as cur:
        cur.execute(
            """select b.status, t.id, t.sku, t.outstanding_qty, t.cin7_assembly_id
               from production.production_batches b
               join production.targets t on t.id = b.target_id
               where b.id = %s""",
            (batch_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f'No such batch: {batch_id}')
    status, target_id, sku, outstanding_qty, cin7_assembly_id = row
    if status != 'planned' and status != 'issued':
        raise ValueError(f"Batch is already '{status}' -- can't report actuals again")

    small_assembly = cin7.complete_small_assembly(sku, actual_qty)

    with conn.cursor() as cur:
        cur.execute(
            """insert into production.batch_actuals
                   (batch_id, actual_qty, reject_qty, reported_via, reported_by, cin7_small_assembly_id)
               values (%s, %s, %s, %s, %s, %s)""",
            (batch_id, actual_qty, reject_qty, reported_via, reported_by, small_assembly.assembly_id),
        )
        cur.execute("update production.production_batches set status = 'completed' where id = %s", (batch_id,))

    new_outstanding = apply_actual_to_target(
        ExistingTarget(id=str(target_id), outstanding_qty=float(outstanding_qty)), actual_qty
    )
    if new_outstanding <= 0:
        if cin7_assembly_id:
            cin7.close_assembly(cin7_assembly_id)
        with conn.cursor() as cur:
            cur.execute(
                "update production.targets set status = 'closed', outstanding_qty = 0, closed_at = now() where id = %s",
                (target_id,),
            )
    else:
        with conn.cursor() as cur:
            cur.execute("update production.targets set outstanding_qty = %s where id = %s", (new_outstanding, target_id))

    conn.commit()
    return {
        'batch_id': batch_id,
        'cin7_small_assembly_id': small_assembly.assembly_id,
        'target_outstanding_qty': new_outstanding,
        'target_closed': new_outstanding <= 0,
    }
