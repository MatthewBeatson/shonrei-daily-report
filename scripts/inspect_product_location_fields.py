"""Live Cin7 READ-ONLY diagnostic -- what field(s) actually carry a
product's "Default location" (Location + Bin), now that real Bins exist
under Settings > Reference Books > Locations > Bins (e.g. "Stockroom -
Main", "Stockroom - Pads").

We've confirmed Bin as a concept from Cin7's own documented Lines models
(Stock Adjustment lines, FinishedGoods PickLines) and from a live
ref/productavailability row -- both show "Bin"/"BinID" fields, always
null/empty for every Shonrei SKU so far because bins were never created.
What we HAVEN'T seen yet is the *product* record's own "Default
location" field -- that's a different endpoint (GET /product) and isn't
covered by any doc snippet captured so far. This script just dumps it,
raw, for a real SKU, after you've set that SKU's Default location to a
bin in Cin7's UI -- purely a read, no writes, safe to run any time.

Run this ON THIS MACHINE, credentials come out of Windows Credential
Manager (same as every other dump_sample_*.py / scripts/*.py script).

Usage:
    python scripts/inspect_product_location_fields.py <SKU>

Before running: in Cin7's UI, open that SKU's product page and set
"Default location" to one of the new bins (e.g.
"Shonrei factory/main warehouse: Stockroom - Main"), save it, then run
this script against that same SKU.

What to look for in the output:
    - Any key on the /product response whose name suggests location/bin
      (Location, LocationID, Bin, BinID, DefaultLocation, etc) and
      whether it holds a GUID, a plain name, or a combined string.
    - The same fields on the /ref/productavailability row, for
      comparison (this one we've already seen return Bin: null).
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
    if len(sys.argv) != 2:
        print('Usage: python scripts/inspect_product_location_fields.py <SKU>')
        sys.exit(1)
    sku = sys.argv[1]

    account_id = keyring.get_password(SERVICE, 'cin7_account_id')
    api_key = keyring.get_password(SERVICE, 'cin7_api_key')
    if not account_id or not api_key:
        print('Cin7 credentials not found in Windows Credential Manager '
              f'(service={SERVICE!r}, keys cin7_account_id/cin7_api_key).')
        sys.exit(1)

    client = Cin7Client(account_id=account_id, api_key=api_key)

    print(f'--- GET /product?SKU={sku} (full record) ---')
    product = client._get_product(sku)  # noqa: SLF001 -- diagnostic script, reading the real method's own result
    print(json.dumps(product, indent=2, default=str))

    location_like_keys = [
        k for k in product
        if any(term in k.lower() for term in ('location', 'bin', 'warehouse'))
    ]
    print(f'\n--- Keys on /product that look location/bin-related: {location_like_keys or "(none found)"} ---')

    print(f'\n--- GET /ref/productavailability?SKU={sku} (for comparison) ---')
    availability = client._get_availability_row(sku)  # noqa: SLF001
    print(json.dumps(availability, indent=2, default=str))


if __name__ == '__main__':
    main()
