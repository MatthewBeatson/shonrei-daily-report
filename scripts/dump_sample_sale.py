"""Read-only Cin7 schema check for the Monthly Dispatch Plan feature.

Run this ON THIS MACHINE -- it reads Cin7 credentials out of Windows
Credential Manager the same way scripts/print_local_credentials.py does
(never printed, never sent anywhere except straight to Cin7's own API over
HTTPS). It makes two GET requests (saleList, then one sale's full detail)
and writes the raw JSON to a local file so we can see the *real* field
names for customer, reference/PO, and promised ship date -- nothing is
changed in Cin7, nothing is written back.

Usage: python scripts/dump_sample_sale.py
Output: sample_sale_dump.json in the current directory (gitignored -- add
it to .gitignore if it isn't already, since it contains one real
customer's order data)
"""
import json
import sys

import keyring
import requests

SERVICE = 'ShonreiDailyReport'


def main():
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

    print('Fetching saleList (page 1, limit 5, most recently created)...')
    list_resp = requests.get(
        'https://inventory.dearsystems.com/ExternalApi/v2/saleList',
        headers=headers,
        params={'Page': 1, 'Limit': 5},
        timeout=60,
    )
    list_resp.raise_for_status()
    list_data = list_resp.json()
    items = list_data.get('SaleList') or list_data.get('Sales') or list_data.get('SaleListItems') or []
    if not items:
        print('No sales returned from saleList -- nothing to inspect.')
        sys.exit(1)

    sample_list_item = items[0]
    sale_id = sample_list_item.get('ID') or sample_list_item.get('SaleID') or sample_list_item.get('SaleId')
    print(f'Fetching full detail for sale {sale_id}...')

    detail_resp = requests.get(
        'https://inventory.dearsystems.com/ExternalApi/v2/sale',
        headers=headers,
        params={'ID': sale_id},
        timeout=60,
    )
    detail_resp.raise_for_status()
    detail_data = detail_resp.json()

    out = {
        'saleList_item_keys': sorted(sample_list_item.keys()),
        'saleList_item_sample': sample_list_item,
        'sale_detail_top_level_keys': sorted(detail_data.keys()) if isinstance(detail_data, dict) else None,
        'sale_detail_sample': detail_data,
    }

    out_path = 'sample_sale_dump.json'
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(out, f, indent=2, default=str)

    print(f'\nWrote {out_path}. Share this file (or its content) back so the real Cin7 '
          'field names for customer, reference/PO, and promised ship date can be confirmed.')


if __name__ == '__main__':
    main()
