"""Void the real test assembly records created by dump_sample_assembly_write.py,
using the actual production/planner/cin7_client.py Cin7Client.close_assembly
implementation -- not a separate hand-rolled DELETE call, so this genuinely
confirms that method against a live tenant (see cin7_client.py's module
docstring: whether "ID" and "TaskID" are really the same identifier for
DELETE was the one open question on close_assembly).

Run this ON THIS MACHINE, credentials come out of Windows Credential
Manager (same as every other dump_sample_*.py / scripts/*.py script).

Usage:
    python scripts/void_test_assemblies.py <TaskID> [<TaskID> ...]

Known test TaskIDs from this session's dump_sample_assembly_write.py
runs (2026-09-11), in case they're handy to paste as arguments:
    f0386aee-2860-4fae-9137-0506e9c7d43a  DRAFT (WIP110, qty 1, never authorised)
    4ffcdb4a-ce46-4ff4-8884-a74252556058  COMPLETED (WIP110, qty 1, FG-6233)
    fe91550e-04cd-47b7-b196-2cea0de6d756  AUTHORISED (WIP110, qty 1, FG-6234)
    19087c50-3c48-483e-88e2-1fd205543519  AUTHORISED (WIP110, qty 10000, FG-6235)
    07d42d9a-f24f-419c-b3d3-ef3c8c924b27  AUTHORISED (WIP110, qty 100, FG-6236)
    d8e98673-c35f-44b5-ae2c-088e81150ed6  COMPLETED (WIP110, qty 1000, FG-6237)
If there are others from runs not shown back to this session, find their
TaskIDs/AssemblyNumbers in Cin7's own UI and pass those too.

For each TaskID: prints its current record (AssemblyNumber/Status/SKU/
Quantity), asks for confirmation, calls the real close_assembly(), then
re-fetches to show the resulting status.

Note: Void is Cin7's terminal cancel (not the "Undo" alternative --
see close_assembly's docstring) -- once voided, a record stays voided.
COMPLETED assemblies (FG-6233, FG-6237) genuinely moved real component
stock; voiding is expected to reverse that (Cin7's own documented
behaviour for a completed record), but this script doesn't independently
verify the stock reversal -- check Cin7's on-hand for the affected SKUs
afterwards if you want that confirmed too.
"""
from __future__ import annotations
import sys
from pathlib import Path

import keyring

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'production' / 'planner'))
from cin7_client import Cin7Client  # noqa: E402

SERVICE = 'ShonreiDailyReport'


def main():
    if len(sys.argv) < 2:
        print('Usage: python scripts/void_test_assemblies.py <TaskID> [<TaskID> ...]')
        sys.exit(1)
    task_ids = sys.argv[1:]

    account_id = keyring.get_password(SERVICE, 'cin7_account_id')
    api_key = keyring.get_password(SERVICE, 'cin7_api_key')
    if not account_id or not api_key:
        print('Cin7 credentials not found in Windows Credential Manager '
              f'(service={SERVICE!r}, keys cin7_account_id/cin7_api_key).')
        sys.exit(1)

    client = Cin7Client(account_id=account_id, api_key=api_key)

    for task_id in task_ids:
        try:
            full = client._get_full_assembly(task_id)
        except Exception as exc:  # noqa: BLE001 -- just reporting, moving to the next one
            print(f'\n{task_id}: could not fetch record ({exc}) -- skipping')
            continue

        print(f"\n{task_id}: AssemblyNumber={full.get('AssemblyNumber')} Status={full.get('Status')} "
              f"ProductCode={full.get('ProductCode')} Quantity={full.get('Quantity')}")
        if full.get('Status') == 'VOIDED':
            print('  already VOIDED -- skipping')
            continue

        answer = input("  Void this one via Cin7Client.close_assembly()? "
                        "Type 'yes' to continue, anything else to skip: ")
        if answer.strip().lower() != 'yes':
            print('  skipped')
            continue

        try:
            client.close_assembly(task_id)
        except Exception as exc:  # noqa: BLE001 -- report and move on, don't stop the batch
            print(f'  close_assembly() FAILED: {exc}')
            continue

        full_after = client._get_full_assembly(task_id)
        print(f"  close_assembly() succeeded -- now Status={full_after.get('Status')}")


if __name__ == '__main__':
    main()
