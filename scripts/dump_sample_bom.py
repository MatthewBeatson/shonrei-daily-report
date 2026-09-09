"""Read-only Cin7 schema check for the production/planner Cin7 client.

Same convention as scripts/dump_sample_sale.py: run this ON THIS MACHINE,
credentials come out of Windows Credential Manager, nothing is written to
Cin7. Prints the raw JSON shape of a product's BOM and its stock
availability so production/planner/cin7_client.py's stubbed
get_bom/get_availability methods can be wired against real field names
instead of guessed ones.

Confirmed against a live account already (see production/README.md):
  - GET /ExternalApi/v2/product?SKU=<sku> is real and returns a list
    wrapper ({Total, Page, Products: [...]}), and BOM-ness metadata
    (BillOfMaterial, BOMType, QuantityToProduce,
    MinimumBeforeReorder/ReorderQuantity) lives directly on the product
    record. BUT: BillOfMaterialsProducts (where the actual component
    lines should be) came back empty here even for a SKU confirmed to
    have a real BOM configured in Cin7's own UI -- so this endpoint just
    doesn't expand BOM lines, the earlier assumption that it does was
    wrong. Fetching the same product by ID instead of SKU didn't change
    that either.
  - GET /ExternalApi/v2/ref/productavailability?SKU=<sku> is real and
    gives OnHand / Allocated / Available (= OnHand - Allocated) / OnOrder.
  - Guessed /bom, /product/availability, and /productavailability paths
    don't exist -- Cin7 doesn't send a real 404 status for a bad path,
    just its own "Page not found" HTML page dressed up as HTTP 200.
    safe_json() below detects that (checks Content-Type, not just
    status) so it isn't mistaken for a real 200 response.
  - This version tries several more candidates for the real BOM-lines
    endpoint, following the naming convention ref/productavailability
    just confirmed (a "ref/" prefix, lowercase, no separators) rather
    than guessing blind. If none of these hit either, the most reliable
    next step is opening this SKU's Bill of Materials tab in Cin7's own
    web UI with your browser's DevTools Network tab open -- Cin7's own
    frontend has to call *some* API to render that tab, and whatever URL
    shows up there is worth trying here next, confirmed rather than
    guessed.

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

    def fetch(label, path, params):
        print(f'Fetching {label} ({path} {params})...')
        try:
            resp = requests.get(f'{BASE_URL}/{path}', headers=headers, params=params, timeout=60)
            parsed = safe_json(resp)
            not_found = parsed is None  # Cin7 sends its own "Page not found" HTML as a fake 200
            print(f'  status {resp.status_code}' + (' (not a real endpoint -- got Cin7\'s HTML 404 page)' if not_found else ''))
            dump[label] = {'status': resp.status_code, 'not_found': not_found, 'body': parsed if parsed is not None else resp.text[:500]}
            return parsed
        except requests.RequestException as exc:
            print(f'  request failed: {exc}')
            dump[label] = {'error': str(exc)}
            return None

    # 1. Product by SKU (confirmed real -- a list wrapper).
    product_list = fetch('product_by_sku', 'product', {'SKU': sku})

    # 2. Same product again, by its own ID this time -- checking whether
    #    BillOfMaterialsProducts (the actual BOM component lines) only
    #    populates on a single-record fetch, not a SKU-filtered list.
    product_id = None
    if isinstance(product_list, dict):
        products = product_list.get('Products') or []
        if products:
            product_id = products[0].get('ID')
    if product_id:
        fetch('product_by_id', 'product', {'ID': product_id})
    else:
        print('No product ID found from the SKU fetch -- skipping the by-ID re-fetch.')

    # 3. ref/productavailability is confirmed real -- re-fetch it here too
    #    so one full run always captures both pieces in sample_bom_dump.json.
    fetch('availability_ref', 'ref/productavailability', {'SKU': sku})

    # 4. BOM-lines candidates, following the "ref/<lowercase, no
    #    separators>" convention ref/productavailability just confirmed,
    #    since a plain /product fetch doesn't expand BillOfMaterialsProducts
    #    even for a SKU with a real BOM configured (see module docstring).
    #    Tried by both SKU and ProductID where that distinction might matter.
    for label, path, params in [
        ('bom_ref_bom_sku', 'ref/bom', {'SKU': sku}),
        ('bom_ref_productbom_sku', 'ref/productbom', {'SKU': sku}),
        ('bom_ref_billofmaterial_sku', 'ref/billofmaterial', {'SKU': sku}),
        ('bom_ref_billofmaterials_sku', 'ref/billofmaterials', {'SKU': sku}),
        ('bom_nested_product_bom_sku', 'product/bom', {'SKU': sku}),
    ]:
        if product_id and 'SKU' in params:
            params = {**params, 'ProductID': product_id}
        fetch(label, path, params)

    with open('sample_bom_dump.json', 'w', encoding='utf-8') as f:
        json.dump(dump, f, indent=2, default=str)
    print('\nWrote sample_bom_dump.json -- inspect it, then update '
          'production/planner/cin7_client.py get_bom/get_availability '
          'with the real endpoint path + field names.')


def safe_json(resp):
    """Returns the parsed JSON body, or None if this wasn't really a JSON
    response -- Cin7 answers an unknown path with its own branded "Page
    not found" HTML page at HTTP 200, not a real 404, so checking
    resp.ok alone is not enough to tell a real endpoint from a wrong
    guess."""
    content_type = resp.headers.get('Content-Type', '')
    if 'json' not in content_type.lower():
        return None
    try:
        return resp.json()
    except ValueError:
        return None


if __name__ == '__main__':
    main()
