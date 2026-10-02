"""Live Cin7 READ-ONLY diagnostic -- does Cin7's product availability
response break "Allocated" down into demand-side (sales orders) vs.
supply-side (purchase orders / finished-goods assembly orders), or is
it one blended figure?

Confirmed so far (2026-09-16, 14LSWL/NB): GET /ref/productavailability
returns OnHand, Allocated, Available (per row/bin) -- but nothing in
this codebase has ever looked past those three names at what else the
raw response carries. The admin page's "Invoiced, not shipped (est.)"
column (production/admin/app.js) currently computes Allocated minus
Total-on-order (a sales-order-only figure) and assumes Allocated is
ALSO sales-order-only -- if Cin7's Allocated actually blends in
component/stock reserved against an open purchase order or an open
assembly (production) order, that assumption is wrong and the column
would overstate "invoiced, not shipped" by whatever that other
allocation is.

This script just dumps the RAW GET /ref/productavailability response
for a SKU you name, unmodified -- no writes, safe to run any time.
Pick a SKU you know has an open PO or an open FG/assembly order right
now (not just open sales orders) if you want this to actually show the
distinction, if one exists.

Run this ON THIS MACHINE, credentials come out of Windows Credential
Manager (same as every other dump_sample_*.py / scripts/*.py script).

Usage:
    python scripts/probe_allocated_breakdown.py <SKU>

What to look for in the output: any key beyond OnHand/Allocated/
Available/Bin -- anything suggesting "SO"/"PO"/"Order"/"Committed"/
"Reserved" etc, and whether Allocated's own value changes as you
create/cancel a test PO or assembly order against this SKU in Cin7
(the surest way to tell what's actually feeding it). Paste the whole
output back.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import keyring
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'production' / 'planner'))
from cin7_client import Cin7Client, CIN7_BASE_URL  # noqa: E402

SERVICE = 'ShonreiDailyReport'


def main():
    if len(sys.argv) != 2:
        print('Usage: python scripts/probe_allocated_breakdown.py <SKU>')
        sys.exit(1)
    sku = sys.argv[1]

    account_id = keyring.get_password(SERVICE, 'cin7_account_id')
    api_key = keyring.get_password(SERVICE, 'cin7_api_key')
    if not account_id or not api_key:
        print('Cin7 credentials not found in Windows Credential Manager '
              f'(service={SERVICE!r}, keys cin7_account_id/cin7_api_key).')
        sys.exit(1)

    client = Cin7Client(account_id=account_id, api_key=api_key)

    print(f'GET ref/productavailability?SKU={sku}\n')
    resp = requests.get(
        f'{CIN7_BASE_URL}/ref/productavailability',
        headers=client._headers(),  # noqa: SLF001
        params={'SKU': sku},
        timeout=60,
    )
    print(f'HTTP {resp.status_code}')
    try:
        body = resp.json()
    except ValueError:
        print('(non-JSON response)')
        print(resp.text[:2000])
        return

    rows = body.get('ProductAvailabilityList') or []
    print(f'{len(rows)} row(s) returned (one per bin the SKU has any history in):\n')
    print(json.dumps(body, indent=2, default=str))

    if rows:
        print('\n--- Field names on the first row ---')
        for key in rows[0]:
            print(f'  {key}: {rows[0][key]!r}')


if __name__ == '__main__':
    main()
