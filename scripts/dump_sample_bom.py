"""Read-only Cin7 schema check for the production/planner Cin7 client.

Same convention as scripts/dump_sample_sale.py: run this ON THIS MACHINE,
credentials come out of Windows Credential Manager, nothing is written to
Cin7. Prints the raw JSON shape of a product's BOM and its stock
availability so production/planner/cin7_client.py's stubbed
get_bom/get_availability methods can be wired against real field names
instead of guessed ones.

Usage: python scripts/dump_sample_bom.py <SKU>
Output: printed to stdout and saved to sample_bom_dump.json (gitignored --
add it to .gitignore if it isn't already; it may contain real product data)
"""
import json
import sys

import keyring
import requests

SERVICE = 'ShonreiDailyReport'
BASE_URL = 'https://inventory.dearsystems.com/ExternalApi/v2'


def main():
    if len(sys.argv) != 2:
        print('Usage: python scripts/dump_sample_bom.py <SKU>')
        sys.exit(1)
    sku = sys.argv[1]

    account_id = keyring.get_password(SERVICE, 'cin7_account_id')
    api_key = keyring.get_password(SERVICE, 'cin7_api_key')
    if not account_id or not api_key:
        print('Cin7 credentials not found in Windows Credential Manager '
              f'(service={SERVICE!r}, keys cin7_account_id/cin7_api_key). '
              'Run scripts/print_local_credentials.py to check what is stored.')
        sys.exit(1)

    headers = {
        'api-auth-accountid': account_id,
        'api-auth-applicationkey': api_key,
        'Content-Type': 'application/json',
    }

    dump = {}

    # Candidate endpoints -- Cin7 Core's exact path/param names for BOM and
    # availability haven't been confirmed against this account yet (no
    # other feature in this repo has needed them so far). Try the
    # documented names; if one 404s, that's useful signal too, print it
    # and move on rather than stopping.
    for label, path, params in [
        ('bom', 'bom', {'SKU': sku}),
        ('product', 'product', {'SKU': sku}),
        ('availability', 'productavailability', {'SKU': sku}),
    ]:
        print(f'Fetching {label} ({path})...')
        try:
            resp = requests.get(f'{BASE_URL}/{path}', headers=headers, params=params, timeout=60)
            print(f'  status {resp.status_code}')
            dump[label] = {'status': resp.status_code, 'body': safe_json(resp)}
        except requests.RequestException as exc:
            print(f'  request failed: {exc}')
            dump[label] = {'error': str(exc)}

    with open('sample_bom_dump.json', 'w', encoding='utf-8') as f:
        json.dump(dump, f, indent=2, default=str)
    print('\nWrote sample_bom_dump.json -- inspect it, then update '
          'production/planner/cin7_client.py get_bom/get_availability '
          'with the real endpoint path + field names.')


def safe_json(resp):
    try:
        return resp.json()
    except ValueError:
        return resp.text[:2000]


if __name__ == '__main__':
    main()
