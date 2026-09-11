"""Live Cin7 WRITE test for Cin7Client.adjust_stock_on_hand
(production/planner/cin7_client.py) -- calls the REAL method directly,
not a hand-rolled duplicate, same pattern as void_test_assemblies.py.

Deliberately a genuine no-op: reads the SKU's current on-hand via the
real get_stock_on_hand(), then calls adjust_stock_on_hand() targeting
that EXACT SAME figure. Since Cin7's Quantity field on a stock
adjustment is the target on-hand level, not a delta (confirmed for
get_bom's sibling read-side methods -- see cin7_client.py's module
docstring), adjusting to the current on-hand should produce a real
Cin7 stock adjustment record with a zero-value transaction -- proving
the round trip (POST DRAFT -> PUT COMPLETED) works, without genuinely
changing any real inventory figure.

Run this ON THIS MACHINE, credentials come out of Windows Credential
Manager (same as every other dump_sample_*.py / scripts/*.py script).

Usage:
    python scripts/test_adjust_stock_on_hand.py <SKU>

What to check in Cin7's UI afterwards, to confirm this actually worked:
    - The SKU's on-hand qty should be unchanged from before this ran.
    - A new stock adjustment record should exist for this SKU with a
      transaction value of 0 (or very close to it, if something else
      changed the on-hand qty in the meantime -- re-run to check).
"""
from __future__ import annotations
import sys
from pathlib import Path

import keyring

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'production' / 'planner'))
from cin7_client import Cin7Client  # noqa: E402

SERVICE = 'ShonreiDailyReport'


def main():
    if len(sys.argv) != 2:
        print('Usage: python scripts/test_adjust_stock_on_hand.py <SKU>')
        sys.exit(1)
    sku = sys.argv[1]

    account_id = keyring.get_password(SERVICE, 'cin7_account_id')
    api_key = keyring.get_password(SERVICE, 'cin7_api_key')
    if not account_id or not api_key:
        print('Cin7 credentials not found in Windows Credential Manager '
              f'(service={SERVICE!r}, keys cin7_account_id/cin7_api_key).')
        sys.exit(1)

    client = Cin7Client(account_id=account_id, api_key=api_key)

    print(f'Reading current on-hand for {sku!r}...')
    current_on_hand = client.get_stock_on_hand(sku)
    print(f'Current on-hand: {current_on_hand}')

    answer = input(
        f"\nAbout to call adjust_stock_on_hand({sku!r}, {current_on_hand}) -- targeting the SAME "
        "figure Cin7 already has, so this should be a genuine no-op (zero-value transaction), "
        "not a real inventory change. Type 'yes' to continue, anything else to stop here: "
    )
    if answer.strip().lower() != 'yes':
        print('Stopped, nothing sent.')
        sys.exit(0)

    try:
        task_id = client.adjust_stock_on_hand(sku, current_on_hand, note='test_adjust_stock_on_hand.py -- no-op confirmation')
    except Exception as exc:  # noqa: BLE001 -- report plainly, this is a diagnostic run
        print(f'adjust_stock_on_hand FAILED: {exc}')
        sys.exit(1)

    print(f'adjust_stock_on_hand succeeded -- TaskID: {task_id}')

    print('\nRe-reading on-hand to confirm nothing actually changed...')
    after_on_hand = client.get_stock_on_hand(sku)
    print(f'On-hand after adjustment: {after_on_hand}')
    if after_on_hand == current_on_hand:
        print('CONFIRMED: on-hand unchanged, as expected for a no-op adjustment.')
    else:
        print(f'NOTE: on-hand changed from {current_on_hand} to {after_on_hand} -- if nothing else '
              'touched this SKU in the meantime, check the adjustment record in Cin7\'s UI directly.')


if __name__ == '__main__':
    main()
