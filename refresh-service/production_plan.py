"""Production planning pass: explode demand into a build plan and stage
it in the `production` schema (see migrations/007_production_schema.sql).

Reuses production/planner/bom_explode.py -- that module is the tested,
canonical BOM-explosion logic (production/planner/test_bom_explode.py);
this file is just the plumbing that feeds it real request data and writes
the result to Postgres, following the same shape as dispatch_plan_data.py
et al. in this same service.

For this first "live concept" pass, demand/BOM/on-hand figures come in on
the request body rather than being pulled from Cin7 automatically -- the
real Cin7 BOM/assembly/availability endpoints haven't been confirmed
against live data yet (see scripts/dump_sample_bom.py). Swap
`run_production_plan`'s payload-reading for real Cin7Client calls once
that's done; nothing else about the flow changes.
"""
from __future__ import annotations
import sys
import uuid
from pathlib import Path

# production/planner is a sibling of this file's repo root, not a
# installed package -- see production/README.md for why the explosion
# logic lives there rather than duplicated here.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'production' / 'planner'))
from bom_explode import BomLine, BuildStep, explode_bom  # noqa: E402


class ProductionPlanError(ValueError):
    """A malformed planning request -- caller should turn this into a 400."""


def run_production_plan(conn, payload: dict) -> dict:
    """Explode every demanded SKU in `payload`, merge shared build steps,
    stage them into production.production_runs, and return the plan.

    Expected payload shape:
        {
          "demand": [{"sku": "FG-A", "qty": 10}, ...],
          "bom": {"FG-A": [{"component_sku": "SUB-B", "qty_per": 2}], ...},
          "on_hand": {"SUB-B": 4},              # optional, default 0
          "open_assembly_qty": {"SUB-B": 0},    # optional, default 0
          "batch_size": {"SUB-B": 50}           # optional
        }
    """
    demand = payload.get('demand')
    if not demand or not isinstance(demand, list):
        raise ProductionPlanError('payload.demand must be a non-empty list of {sku, qty}')

    bom_raw = payload.get('bom') or {}
    bom = {
        sku: [BomLine(line['component_sku'], line['qty_per']) for line in lines]
        for sku, lines in bom_raw.items()
    }
    on_hand = payload.get('on_hand') or {}
    open_assembly_qty = payload.get('open_assembly_qty') or {}
    batch_size = payload.get('batch_size') or {}

    merged: dict[str, BuildStep] = {}
    for item in demand:
        sku, qty = item.get('sku'), item.get('qty')
        if not sku or not qty:
            raise ProductionPlanError(f'invalid demand entry: {item!r}')
        for step in explode_bom(
            sku, qty, bom=bom, on_hand=on_hand,
            open_assembly_qty=open_assembly_qty, batch_size=batch_size,
        ):
            existing = merged.get(step.sku)
            if existing is None:
                merged[step.sku] = step
            else:
                merged[step.sku] = BuildStep(
                    step.sku, existing.qty_to_build + step.qty_to_build,
                    max(existing.level, step.level),
                )

    steps = sorted(merged.values(), key=lambda s: -s.level)
    plan_batch_id = str(uuid.uuid4())

    with conn.cursor() as cur:
        for step in steps:
            cur.execute(
                """
                insert into production.production_runs
                    (plan_batch_id, sku, qty_to_build, bom_level, status)
                values (%s, %s, %s, %s, 'planned')
                """,
                (plan_batch_id, step.sku, step.qty_to_build, step.level),
            )
    conn.commit()

    return {
        'plan_batch_id': plan_batch_id,
        'steps': [
            {'sku': s.sku, 'qty_to_build': s.qty_to_build, 'level': s.level}
            for s in steps
        ],
    }
