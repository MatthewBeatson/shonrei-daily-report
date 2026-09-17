"""Live Cin7 WRITE diagnostic -- can we update a product's Default
Location via the API? Nothing in this repo has ever attempted a Cin7
product WRITE before -- only GET /product is confirmed. This is needed
for the "assign a SKU's home location, push it to Cin7 too" feature
(admin app, confirmed design 2026-09-17): setting a location in our own
app should also update Cin7's own product record, not just our DB.

Genuinely a real write to a real product record -- reversible (the
script reads the CURRENT DefaultLocation first and offers to restore it
after confirming the write worked), but not a no-op like some earlier
probes. Only run this against a test SKU you're comfortable touching,
or be ready to set it back manually in Cin7's UI if this script's
restore step doesn't work for some reason.

Run this ON THIS MACHINE, credentials come out of Windows Credential
Manager (same as every other dump_sample_*.py / scripts/*.py script).

Usage:
    python scripts/probe_product_default_location_write.py <SKU> "<Bin name to set>"

Example:
    python scripts/probe_product_default_location_write.py 14LSWL/NB "Stockroom - Main"
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


def try_put(headers: dict, label: str, payload: dict) -> requests.Response:
    print(f'\n--- {label}: PUT product ---')
    print(json.dumps(payload, indent=2))
    resp = requests.put(f'{CIN7_BASE_URL}/product', headers=headers, json=payload, timeout=60)
    print(f'HTTP {resp.status_code}')
    try:
        print(json.dumps(resp.json(), indent=2, default=str)[:2000])
    except ValueError:
        print(resp.text[:500])
    return resp


def main():
    if len(sys.argv) != 3:
        print('Usage: python scripts/probe_product_default_location_write.py <SKU> "<Bin name to set>"')
        sys.exit(1)
    sku, new_default_location = sys.argv[1], sys.argv[2]

    account_id = keyring.get_password(SERVICE, 'cin7_account_id')
    api_key = keyring.get_password(SERVICE, 'cin7_api_key')
    if not account_id or not api_key:
        print('Cin7 credentials not found in Windows Credential Manager '
              f'(service={SERVICE!r}, keys cin7_account_id/cin7_api_key).')
        sys.exit(1)

    client = Cin7Client(account_id=account_id, api_key=api_key)
    headers = client._headers()  # noqa: SLF001

    product = client._get_product(sku)  # noqa: SLF001
    product_id = product.get('ID')
    original_default_location = product.get('DefaultLocation')
    print(f'Product ID={product_id!r}')
    print(f'Current DefaultLocation={original_default_location!r}')

    answer = input(
        f"\nAbout to try setting {sku!r}'s DefaultLocation to {new_default_location!r} via a REAL "
        f"PUT to Cin7. This IS a real write to a real product record (reversible -- this script "
        f"will offer to restore {original_default_location!r} afterward, but that restore is a "
        "second real write, not guaranteed if something goes wrong in between). "
        "Type 'yes' to continue, anything else to stop here: "
    )
    if answer.strip().lower() != 'yes':
        print('Stopped, nothing sent.')
        sys.exit(0)

    # Minimal-patch attempt first -- ID + the one field we're trying to
    # change. If Cin7 requires the whole product payload resent (some
    # REST APIs do), this 400 will very likely say so explicitly, same
    # as every other write endpoint confirmed this session -- read the
    # error body, don't guess further blind.
    resp = try_put(headers, 'Minimal patch (ID + DefaultLocation only)', {
        'ID': product_id,
        'DefaultLocation': new_default_location,
    })

    if resp.status_code == 200:
        print('\n--- Re-reading product to confirm the change actually landed ---')
        confirm_product = client._get_product(sku)  # noqa: SLF001
        print(f'DefaultLocation is now: {confirm_product.get("DefaultLocation")!r}')

        restore = input(
            f"\nRestore DefaultLocation back to {original_default_location!r}? Type 'yes' to restore, "
            "anything else to leave it as the new value: "
        )
        if restore.strip().lower() == 'yes':
            try_put(headers, 'Restore original DefaultLocation', {
                'ID': product_id,
                'DefaultLocation': original_default_location,
            })
    else:
        print('\n--- Minimal patch failed. Paste this whole output back -- the error body above '
              'names exactly what Cin7 needs (same pattern as every other write confirmed this '
              'session), then we retry with that field included. ---')


if __name__ == '__main__':
    main()
