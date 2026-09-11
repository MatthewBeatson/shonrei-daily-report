"""Live Cin7 diagnostic -- find out how to READ per-bin on-hand quantity.

Confirmed so far (2026-09-11, 14LSWL/NB, via Cin7's own "Products Stock
Level Report" UI screen):
  - Cin7 DOES track a real per-bin quantity ledger -- the report broke
    10.00 units on hand into a blank-bin row (10.00) and two named-bin
    rows (Stockroom - Main: 0.00, Stockroom - Pads: 0.00).
  - Setting a product's "Default location" (Location: Bin) does NOT
    move any stock into that bin -- it's a label on the product record,
    unrelated to the transactional bin ledger.
  - GET /ref/productavailability (what get_stock_on_hand/_get_availability_row
    already use) only returns Location-level totals with Bin always
    null -- it does NOT expose the per-bin breakdown the UI report
    shows. So there's a real gap: we know bin-level quantity exists,
    but not yet how to read it via the API.

This script is read-only against a SKU you name, EXCEPT for one
explicit, confirmed step you approve interactively: pushing a tiny
(default 1 unit) stock adjustment tagged with a real Bin, so there's a
genuine non-zero, bin-assigned figure in Cin7 to test reads against
(14LSWL/NB currently has all of its stock in the unassigned/blank bin,
so every read attempt would come back looking identical whether or not
bin-level reads even work). It then tries several candidate ways of
reading it back and prints exactly what each returns, so we can see
which one (if any) actually reflects the bin split -- rather than
guessing at an endpoint/param name in code before wiring it for real.

Run this ON THIS MACHINE, credentials come out of Windows Credential
Manager (same as every other dump_sample_*.py / scripts/*.py script).

Usage:
    python scripts/probe_bin_level_stock.py <SKU> <BIN NAME>

Example:
    python scripts/probe_bin_level_stock.py 14LSWL/NB "Stockroom - Main"
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


def try_get(client: Cin7Client, label: str, path: str, params: dict) -> None:
    print(f'\n--- {label}: GET {path} params={params} ---')
    try:
        resp = requests.get(f'{CIN7_BASE_URL}/{path}', headers=client._headers(), params=params, timeout=60)  # noqa: SLF001
        print(f'HTTP {resp.status_code}, Content-Type: {resp.headers.get("Content-Type")}')
        if 'json' in (resp.headers.get('Content-Type') or ''):
            print(json.dumps(resp.json(), indent=2, default=str)[:3000])
        else:
            print('(non-JSON response -- likely a fake-200 HTML page, i.e. this path/param does not exist)')
    except Exception as exc:  # noqa: BLE001 -- diagnostic, report plainly
        print(f'FAILED: {exc}')


def main():
    if len(sys.argv) != 3:
        print('Usage: python scripts/probe_bin_level_stock.py <SKU> "<BIN NAME>"')
        sys.exit(1)
    sku, bin_name = sys.argv[1], sys.argv[2]

    account_id = keyring.get_password(SERVICE, 'cin7_account_id')
    api_key = keyring.get_password(SERVICE, 'cin7_api_key')
    if not account_id or not api_key:
        print('Cin7 credentials not found in Windows Credential Manager '
              f'(service={SERVICE!r}, keys cin7_account_id/cin7_api_key).')
        sys.exit(1)

    client = Cin7Client(account_id=account_id, api_key=api_key)

    print(f'Current on-hand (Location-level, from get_stock_on_hand): {client.get_stock_on_hand(sku)}')

    answer = input(
        f"\nAbout to push a REAL +1 unit stock adjustment for {sku!r} tagged Bin={bin_name!r}, "
        "so there's a genuine bin-assigned figure to test reads against (Cin7's own Stock Level "
        "Report currently shows this SKU's stock all sitting in the unassigned/blank bin). "
        "This is a real, reversible inventory change (you can adjust it back after). "
        "Type 'yes' to push it, anything else to skip straight to the read probes: "
    )
    if answer.strip().lower() == 'yes':
        current = client.get_stock_on_hand(sku)
        # adjust_stock_on_hand doesn't take a Bin param yet -- this is exactly
        # why we're probing: does Lines[].Bin on the write side even work?
        # Hand-rolled here (not the real method) so we can pass Bin without
        # committing that shape into cin7_client.py before confirming it lands.
        product = client._get_product(sku)  # noqa: SLF001
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
        payload = {
            "EffectiveDate": now,
            "Lines": [{
                "SKU": sku,
                "Quantity": current + 1,
                "UnitCost": product.get("AverageCost") or 0,
                "Bin": bin_name,
            }],
            "Reference": "probe_bin_level_stock.py -- testing Bin-tagged adjustment",
            "Status": "DRAFT",
        }
        print(f'\nPOST stockadjustment: {json.dumps(payload, indent=2)}')
        resp = requests.post(f'{CIN7_BASE_URL}/stockadjustment', headers=client._headers(), json=payload, timeout=60)
        try:
            client._raise_for_status_with_body(resp)  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            print(f'POST FAILED: {exc}')
            sys.exit(1)
        draft = resp.json()
        task_id = draft['TaskID']
        print(f'DRAFT created, TaskID={task_id}')
        complete_payload = {"TaskID": task_id, "Status": "COMPLETED"}
        resp = requests.put(f'{CIN7_BASE_URL}/stockadjustment', headers=client._headers(), json=complete_payload, timeout=60)
        try:
            client._raise_for_status_with_body(resp)  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            print(f'PUT (complete) FAILED: {exc}')
            sys.exit(1)
        print(f'Adjustment COMPLETED, TaskID={task_id}. Check Cin7\'s Stock Level Report UI now -- '
              f'{bin_name!r} should show a real quantity for {sku!r}.')
    else:
        print('Skipped pushing the adjustment -- probes below will only be meaningful if this SKU '
              'already has some real bin-assigned stock from before.')

    # -- now try several candidate ways of reading bin-level stock back --

    try_get(client, 'productavailability, default', 'ref/productavailability', {'SKU': sku})
    try_get(client, 'productavailability, with Bin param', 'ref/productavailability', {'SKU': sku, 'Bin': bin_name})
    try_get(client, 'productavailability, ShowBin-style flag', 'ref/productavailability', {'SKU': sku, 'IncludeBin': 'true'})
    try_get(client, 'stock level report guess', 'ref/stocklevel', {'SKU': sku})
    try_get(client, 'stock level report guess 2', 'ref/productstocklevel', {'SKU': sku})
    try_get(client, 'stock by bin guess', 'ref/stockbybin', {'SKU': sku})
    try_get(client, 'bin list guess', 'ref/bin', {'SKU': sku})

    print('\n--- Done. Paste this whole output back -- whichever call(s) above returned real JSON '
          'with a Bin breakdown (not a fake-200 HTML page) is the one we wire up for real. ---')


if __name__ == '__main__':
    main()
