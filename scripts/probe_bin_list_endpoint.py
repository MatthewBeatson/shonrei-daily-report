"""Live Cin7 READ-ONLY diagnostic -- find the endpoint that lists every
Bin under a Location, so warehouse.locations rows can be synced FROM
Cin7's own Bins list (created via Cin7's bulk Import on the Bins screen)
rather than re-typed by hand into our admin UI.

We've never confirmed this endpoint exists -- everywhere `Bin` has shown
up so far (ref/productavailability, stock adjustment lines, assembly
PickLines) is a value ALREADY ATTACHED to a stock record, never a plain
list of "every Bin configured under this Location." This script tries
several plausible paths/params and reports which (if any) return real
JSON, same probing pattern as scripts/probe_bin_level_stock.py.

Run this ON THIS MACHINE, credentials come out of Windows Credential
Manager (same as every other dump_sample_*.py / scripts/*.py script).
Purely a read -- no writes, safe to run any time.

Usage:
    python scripts/probe_bin_list_endpoint.py
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


def try_get(headers: dict, label: str, path: str, params: dict | None = None) -> None:
    print(f'\n--- {label}: GET {path} params={params or {}} ---')
    try:
        resp = requests.get(f'{CIN7_BASE_URL}/{path}', headers=headers, params=params or {}, timeout=60)
        print(f'HTTP {resp.status_code}, Content-Type: {resp.headers.get("Content-Type")}')
        if 'json' in (resp.headers.get('Content-Type') or ''):
            body = resp.json()
            print(json.dumps(body, indent=2, default=str)[:2500])
        else:
            print('(non-JSON response -- likely a fake-200 HTML page, i.e. this path/param does not exist)')
    except Exception as exc:  # noqa: BLE001 -- diagnostic, report plainly
        print(f'FAILED: {exc}')


def main():
    account_id = keyring.get_password(SERVICE, 'cin7_account_id')
    api_key = keyring.get_password(SERVICE, 'cin7_api_key')
    if not account_id or not api_key:
        print('Cin7 credentials not found in Windows Credential Manager '
              f'(service={SERVICE!r}, keys cin7_account_id/cin7_api_key).')
        sys.exit(1)

    client = Cin7Client(account_id=account_id, api_key=api_key)
    headers = client._headers()  # noqa: SLF001

    # Candidate list-style endpoints, guessing at Cin7's own naming
    # conventions from what we've already confirmed real (ref/
    # productavailability, finishedGoodsList, stockadjustmentList all
    # follow a "<noun>" or "<noun>List" pattern under /ExternalApi/v2).
    try_get(headers, 'ref/bin (plain)', 'ref/bin')
    try_get(headers, 'ref/binList', 'ref/binList')
    try_get(headers, 'bin', 'bin')
    try_get(headers, 'binList', 'binList')
    try_get(headers, 'ref/location', 'ref/location')
    try_get(headers, 'ref/locationList', 'ref/locationList')
    try_get(headers, 'location', 'location')
    try_get(headers, 'locationList', 'locationList')
    try_get(headers, 'ref/warehouse', 'ref/warehouse')
    try_get(headers, 'ref/warehouseList', 'ref/warehouseList')
    try_get(headers, 'ref/pickzone', 'ref/pickzone')
    try_get(headers, 'ref/pickZoneList', 'ref/pickZoneList')

    print('\n--- Done. Paste this whole output back -- whichever call(s) above returned real JSON '
          '(not a fake-200 HTML page) is the one we wire up to sync Bins into warehouse.locations. '
          'If NONE of them worked, that confirms Cin7 has no bin-listing endpoint at all -- the '
          'fallback then is exporting the Bins list as a CSV from Cin7\'s own UI (there\'s an '
          'Export button right there on the Bins screen) and importing that into our admin app '
          'instead of a live API sync. ---')


if __name__ == '__main__':
    main()
