"""Writes production/planner/batch_planner.py's suggested batches into
production.production_batches + batch_so_lines for one target.
"""
from __future__ import annotations
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'production' / 'planner'))
from batch_planner import DemandLine, split_into_batches  # noqa: E402


def _slug(sku: str) -> str:
    return re.sub(r'[^A-Z0-9]+', '', sku.upper())[:12] or 'SKU'


def stage_batches_for_target(conn, target_id: str, sku: str, suggested_run_size: float) -> list[dict]:
    """Reads the target's current priority-ordered demand lines
    (production.target_demand_lines), splits them into batches, and
    inserts fresh production_batches + batch_so_lines rows. Existing
    'planned' batches for this target are left alone -- call this once
    per fresh sync, not repeatedly, to avoid double-issuing the same
    demand (a real UI would show existing planned/issued batches before
    offering to plan more).
    """
    with conn.cursor() as cur:
        cur.execute(
            """select so_number, qty_backordered from production.target_demand_lines
               where target_id = %s order by priority_rank""",
            (target_id,),
        )
        demand_lines = [DemandLine(so_number, float(qty)) for so_number, qty in cur.fetchall()]

    suggested = split_into_batches(demand_lines, suggested_run_size)

    created = []
    with conn.cursor() as cur:
        for batch in suggested:
            batch_code = f'B-{_slug(sku)}-{uuid.uuid4().hex[:6].upper()}'
            cur.execute(
                """insert into production.production_batches
                       (target_id, batch_code, qty_planned, priority_rank, status)
                   values (%s, %s, %s, %s, 'planned') returning id""",
                (target_id, batch_code, batch.qty_planned, batch.priority_rank),
            )
            (batch_id,) = cur.fetchone()
            for line in batch.lines:
                cur.execute(
                    """insert into production.batch_so_lines (batch_id, so_number, qty_allocated)
                       values (%s, %s, %s)""",
                    (batch_id, line.so_number, line.qty_allocated),
                )
            created.append({
                'batch_id': str(batch_id), 'batch_code': batch_code,
                'qty_planned': batch.qty_planned, 'priority_rank': batch.priority_rank,
            })
    conn.commit()
    return created
