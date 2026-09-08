"""A drop-in stand-in for Cin7Client that never calls Cin7 -- logs every
call it would make and returns a deterministic fake Assembly instead.

This is what the backorder-target flow actually runs against today (see
production/README.md "Backorder targets and batches" for why): it proves
the whole chain -- target create/adjust/close, small-FG
create/authorise/allocate/complete -- end to end against the real
database, with zero risk to live Cin7 inventory while the real endpoint
shapes are still unconfirmed (scripts/dump_sample_bom.py is the next
step). Swap `DryRunCin7Client()` for a real `Cin7Client()` once
production/planner/cin7_client.py's stubs are wired for real -- nothing
else about the flow changes, since both share the same method signatures.

Every call is also written to production.cin7_dry_run_log so a planner
can see exactly what would have happened to live Cin7 data.
"""
from __future__ import annotations
import itertools
import json

_counter = itertools.count(1)


class FakeAssembly:
    def __init__(self, sku: str, qty: float, status: str):
        self.assembly_id = f'DRYRUN-{next(_counter):06d}'
        self.sku = sku
        self.qty = qty
        self.status = status


class DryRunCin7Client:
    def __init__(self, conn=None):
        self.conn = conn  # optional -- if given, calls are logged to the DB too

    def _log(self, action: str, **details):
        print(f'[dry-run Cin7] {action}: {details}', flush=True)
        if self.conn is not None:
            with self.conn.cursor() as cur:
                cur.execute(
                    "insert into production.cin7_dry_run_log (action, details) values (%s, %s)",
                    (action, json.dumps(details, default=str)),
                )
            self.conn.commit()

    def create_authorised_assembly(self, sku: str, qty: float) -> FakeAssembly:
        self._log('create_authorised_assembly', sku=sku, qty=qty)
        return FakeAssembly(sku, qty, 'AUTHORISED')

    def adjust_assembly_qty(self, assembly_id: str, new_qty: float) -> FakeAssembly:
        self._log('adjust_assembly_qty', assembly_id=assembly_id, new_qty=new_qty)
        return FakeAssembly('unknown', new_qty, 'AUTHORISED')

    def close_assembly(self, assembly_id: str) -> None:
        self._log('close_assembly', assembly_id=assembly_id)

    def complete_small_assembly(self, sku: str, qty: float) -> FakeAssembly:
        self._log('complete_small_assembly', sku=sku, qty=qty)
        return FakeAssembly(sku, qty, 'COMPLETED')
