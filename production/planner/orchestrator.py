"""Ties bom_explode + Cin7Client together and drives production.* tables.

This is the loop that would run on a schedule (same pattern as
refresh-service's hourly refresh): read demand, explode BOMs, create/
authorise/allocate the resulting Cin7 assemblies in dependency order, and
apply any newly-landed floor-reported actuals as Complete calls.

Prototype-level: table/column names match migrations/007_production_schema
.sql; DB access is a plain psycopg2 connection like daily_refresh_supabase
.py uses elsewhere in this repo, intentionally not written out here since
there's no live schema to run it against yet -- the shape of each step is
what matters at this stage.
"""
from __future__ import annotations
import logging

from bom_explode import BuildStep, explode_bom
from cin7_client import Cin7Client

log = logging.getLogger(__name__)


def plan_and_stage_runs(conn, cin7: Cin7Client, demand: dict[str, float]) -> list[BuildStep]:
    """One planning pass: explode every demanded SKU, merge shared
    sub-assembly steps across all of them, and upsert each into
    production.production_runs as status='planned' if it doesn't already
    have an open run for that SKU.

    `demand` is SKU -> qty needed, e.g. sourced from the same min-stock
    check that already triggers admin's FG creation for outputs.
    """
    skus_touched = set(demand)
    bom = {sku: cin7.get_bom(sku) for sku in _all_descendant_skus(cin7, skus_touched)}
    on_hand = cin7.get_availability(list(bom))
    open_qty = cin7.get_open_assemblies(list(bom))

    merged: dict[str, BuildStep] = {}
    for sku, qty in demand.items():
        for step in explode_bom(
            sku, qty, bom=bom, on_hand=on_hand, open_assembly_qty=open_qty
        ):
            existing = merged.get(step.sku)
            if existing is None or step.level > existing.level:
                merged[step.sku] = BuildStep(
                    sku=step.sku,
                    qty_to_build=(existing.qty_to_build if existing else 0) + step.qty_to_build,
                    level=max(step.level, existing.level if existing else step.level),
                )
            else:
                merged[step.sku] = BuildStep(
                    step.sku, existing.qty_to_build + step.qty_to_build, existing.level
                )

    steps = sorted(merged.values(), key=lambda s: -s.level)
    for step in steps:
        _upsert_planned_run(conn, step)
    return steps


def advance_runs(conn, cin7: Cin7Client) -> None:
    """One 'tick' of the CAAC state machine, called on the same schedule
    as the planning pass:

      planned    -> create + authorise + allocate the Cin7 assembly,
                    but only once every prerequisite (deeper-level,
                    same-parent) run is already 'completed' -- never
                    create an assembly against stock a sibling step
                    further down the plan is still going to produce.
      allocated  -> if production.run_actuals has a row for this run,
                    call complete_assembly() with the reported actual
                    quantity and mark the run 'completed'. Otherwise
                    leave it -- this is the stage that waits on a human.
    """
    for run in _runs_ready_to_advance(conn):
        if run["status"] == "planned":
            assembly = cin7.create_assembly(run["sku"], run["qty_to_build"])
            assembly = cin7.authorise_assembly(assembly.assembly_id)
            assembly = cin7.allocate_assembly(assembly.assembly_id)
            _record_assembly(conn, run["id"], assembly)
        elif run["status"] == "allocated":
            actual = _pending_actual(conn, run["id"])
            if actual is not None:
                cin7.complete_assembly(run["cin7_assembly_id"], actual["actual_qty"])
                _mark_completed(conn, run["id"])
            else:
                log.info("run %s allocated, waiting on floor input", run["id"])


# -- DB helpers -- prototype stubs, real bodies are plain SQL against the
# production schema in migrations/007_production_schema.sql -------------

def _all_descendant_skus(cin7: Cin7Client, top_level_skus: set[str]) -> set[str]:
    raise NotImplementedError("walk BOM tree via cin7.get_bom to collect every SKU involved")


def _upsert_planned_run(conn, step: BuildStep) -> None:
    raise NotImplementedError("insert/update production.production_runs")


def _runs_ready_to_advance(conn) -> list[dict]:
    raise NotImplementedError(
        "select production_runs where status in ('planned','allocated') "
        "and, for 'planned', every same-plan run at a deeper level is 'completed'"
    )


def _record_assembly(conn, run_id: str, assembly) -> None:
    raise NotImplementedError("insert production.run_assembly_map, set run status='allocated'")


def _pending_actual(conn, run_id: str) -> dict | None:
    raise NotImplementedError("select production.run_actuals for this run_id")


def _mark_completed(conn, run_id: str) -> None:
    raise NotImplementedError("update production.production_runs set status='completed'")
