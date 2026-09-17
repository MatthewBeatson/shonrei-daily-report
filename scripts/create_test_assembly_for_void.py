"""Creates a minimal real Cin7 assembly (Create + Authorise only, never
Completed) purely so scripts/void_test_assemblies.py has something fresh
to test close_assembly() against, without consuming any real component
stock (Completing is the only stage that does that -- see cin7_client.
py's module docstring).

Calls the real Cin7Client.create_assembly/authorise_assembly directly,
same pattern as void_test_assemblies.py.

Run this ON THIS MACHINE, credentials come out of Windows Credential
Manager (same as every other dump_sample_*.py / scripts/*.py script).

Usage:
    python scripts/create_test_assembly_for_void.py <SKU> <QTY>
"""
from __future__ import annotations
import sys
from pathlib import Path

import keyring

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'production' / 'planner'))
from cin7_client import Cin7Client  # noqa: E402

SERVICE = 'ShonreiDailyReport'


def main():
    if len(sys.argv) != 3:
        print('Usage: python scripts/create_test_assembly_for_void.py <SKU> <QTY>')
        sys.exit(1)
    sku, qty_str = sys.argv[1], sys.argv[2]
    qty = float(qty_str)

    account_id = keyring.get_password(SERVICE, 'cin7_account_id')
    api_key = keyring.get_password(SERVICE, 'cin7_api_key')
    if not account_id or not api_key:
        print('Cin7 credentials not found in Windows Credential Manager '
              f'(service={SERVICE!r}, keys cin7_account_id/cin7_api_key).')
        sys.exit(1)

    client = Cin7Client(account_id=account_id, api_key=api_key)

    answer = input(
        f"About to create+authorise (NOT complete) {qty} x {sku} -- no real stock will move, "
        "this stays Authorised so it's ready to test close_assembly() against. "
        "Type 'yes' to continue, anything else to stop here: "
    )
    if answer.strip().lower() != 'yes':
        print('Stopped, nothing sent.')
        sys.exit(0)

    assembly = client.create_assembly(sku, qty)
    print(f'Created: assembly_id={assembly.assembly_id} status={assembly.status}')

    assembly = client.authorise_assembly(assembly.assembly_id)
    print(f'Authorised: status={assembly.status}')

    print(f'\nTaskID ready to void: {assembly.assembly_id}')
    print(f'Run: python scripts/void_test_assemblies.py {assembly.assembly_id}')


if __name__ == '__main__':
    main()
