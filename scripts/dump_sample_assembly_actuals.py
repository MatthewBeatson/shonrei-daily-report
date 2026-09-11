"""Live Cin7 WRITE test for complete_assembly's actual-yield mismatch
path (production/planner/cin7_client.py) -- calls the REAL Cin7Client
methods directly (create_assembly, authorise_assembly, complete_assembly),
not a hand-rolled duplicate, so this genuinely tests the shipped code.

Background -- two things this script does NOT attempt, because they're
now confirmed impossible/fixed rather than open questions:

  - Correcting a labour line's actual hours AFTER Authorise: confirmed
    impossible (2026-09-11, live 400: "Finished Goods task Status is
    AUTHORISED.") -- OrderLines can only be submitted once, at the
    Draft -> Authorised transition. Since Shonrei's real process enters
    actuals AFTER allocation (i.e. after this window has already
    closed), there's no API-level way to reflect real labour hours on a
    specific line once an assembly is authorised. Only the overall
    Quantity (actual yield) can still be adjusted post-Authorise, via
    adjust_assembly_qty.
  - adjust_assembly_qty's PUT 400'd the first time this was tried
    (2026-09-11, WIP110, planned 10 -> attempted actual 5):
    "Required attribute 'WIPDate'/'CompletionDate' not provided." --
    real stock ended up consumed at the ORIGINAL planned qty, not the
    smaller actual, because the adjustment silently never took effect.
    Now fixed in cin7_client.py (CompletionDate/WIPDate added). This
    script is what re-tests that fix actually works end to end.

Run this ON THIS MACHINE, credentials come out of Windows Credential
Manager (same as every other dump_sample_*.py script).

Usage:
    python scripts/dump_sample_assembly_actuals.py <SKU> <PLANNED_QTY> <ACTUAL_QTY>

    SKU           a real assembly SKU.
    PLANNED_QTY   quantity used for Create + Authorise.
    ACTUAL_QTY    the different (smaller, to genuinely test the case
                  that broke last time) quantity to complete at.

What to check in Cin7's UI afterwards, to confirm this actually worked
(not just that the calls returned 200):
    - The completed assembly's own Quantity/"Actual yield" should read
      ACTUAL_QTY, not PLANNED_QTY.
    - The component stock actually consumed should reflect ACTUAL_QTY
      worth of the BOM (not PLANNED_QTY worth) -- check the SKU's
      on-hand before/after, or the assembly's own Pick tab.

Output: printed to stdout and saved to sample_assembly_actuals_dump.json
(gitignored -- may contain real production data).
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import keyring

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'production' / 'planner'))
from cin7_client import Cin7Client  # noqa: E402

SERVICE = 'ShonreiDailyReport'


def main():
    if len(sys.argv) != 4:
        print('Usage: python scripts/dump_sample_assembly_actuals.py <SKU> <PLANNED_QTY> <ACTUAL_QTY>')
        sys.exit(1)
    sku, planned_qty_str, actual_qty_str = sys.argv[1:4]
    planned_qty = float(planned_qty_str)
    actual_qty = float(actual_qty_str)

    account_id = keyring.get_password(SERVICE, 'cin7_account_id')
    api_key = keyring.get_password(SERVICE, 'cin7_api_key')
    if not account_id or not api_key:
        print('Cin7 credentials not found in Windows Credential Manager '
              f'(service={SERVICE!r}, keys cin7_account_id/cin7_api_key).')
        sys.exit(1)

    client = Cin7Client(account_id=account_id, api_key=api_key)
    dump = {}

    print(f"\nPlan: create_assembly({sku!r}, {planned_qty}) -> authorise_assembly() -> "
          f"complete_assembly(assembly_id, {actual_qty}) -- a genuine actual_qty != planned_qty mismatch.")
    if not confirm('This will really create/authorise/complete a Cin7 assembly and consume real component stock.'):
        sys.exit(0)

    assembly = client.create_assembly(sku, planned_qty)
    dump['create_assembly'] = vars(assembly)
    print(f"Created: assembly_id={assembly.assembly_id} status={assembly.status} qty={assembly.qty}")

    if not confirm(f"About to authorise {assembly.assembly_id} at planned qty {planned_qty}."):
        save_and_exit(dump)

    assembly = client.authorise_assembly(assembly.assembly_id)
    dump['authorise_assembly'] = vars(assembly)
    print(f"Authorised: status={assembly.status} qty={assembly.qty}")

    if not confirm(f"About to complete {assembly.assembly_id} at actual_qty={actual_qty} "
                    f"(differs from planned {planned_qty} -- this should trigger adjust_assembly_qty "
                    "internally before completing)."):
        save_and_exit(dump)

    try:
        assembly = client.complete_assembly(assembly.assembly_id, actual_qty)
    except Exception as exc:  # noqa: BLE001 -- report and still save what we have
        print(f"complete_assembly FAILED: {exc}")
        dump['complete_assembly_error'] = str(exc)
        save_and_exit(dump)

    dump['complete_assembly'] = vars(assembly)
    print(f"Completed: status={assembly.status} qty={assembly.qty}")
    if assembly.qty == actual_qty:
        print(f"CONFIRMED: completed record's qty ({assembly.qty}) matches the actual_qty requested "
              f"({actual_qty}), not the planned qty ({planned_qty}).")
    else:
        print(f"MISMATCH: completed record's qty ({assembly.qty}) does NOT match actual_qty "
              f"({actual_qty}) -- something's still off, check the raw responses.")

    print(f"\nTaskID for this assembly (for voiding manually in Cin7's UI later, or via "
          f"scripts/void_test_assemblies.py): {assembly.assembly_id}")
    save_and_exit(dump)


def confirm(message: str) -> bool:
    answer = input(f"\n{message}\nType 'yes' to continue, anything else to stop here: ")
    return answer.strip().lower() == 'yes'


def save_and_exit(dump: dict):
    with open('sample_assembly_actuals_dump.json', 'w', encoding='utf-8') as f:
        json.dump(dump, f, indent=2, default=str)
    print('\nWrote sample_assembly_actuals_dump.json -- send its contents back to confirm/finalise.')
    sys.exit(0)


if __name__ == '__main__':
    main()
