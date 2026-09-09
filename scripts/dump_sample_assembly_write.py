"""Live Cin7 WRITE test for the assembly (Finished Goods) endpoints in
production/planner/cin7_client.py.

Unlike dump_sample_bom.py / dump_sample_sale.py, this one is NOT
read-only -- it genuinely creates, authorises, and completes a real Cin7
assembly (consuming real component stock and producing real finished-
good stock). Confirmed against Cin7's own documented endpoint shapes
(see production/README.md "Confirmed Cin7 writes" and cin7_client.py's
module docstring) before this script existed -- this run is what turns
"matches the docs" into "proven against this real tenant", same
discipline as dump_sample_bom.py.

Stock adjustment and the automatic cancel/void test are deliberately not
in this script -- voiding the built assembly is being done manually in
Cin7's own UI instead this round. See adjust_stock_on_hand in
cin7_client.py for the stock-adjustment shape whenever that needs its
own live confirmation later.

Run this ON THIS MACHINE, credentials come out of Windows Credential
Manager (same as every other dump_sample_*.py script).

Usage:
    python scripts/dump_sample_assembly_write.py <ASSEMBLY_SKU> <QTY>

    ASSEMBLY_SKU  a real assembly SKU (BillOfMaterial=true) to actually
                  build -- pick something cheap/low-value, QTY should be
                  the smallest amount Cin7's UI will let you build (often
                  1). This step is genuinely irreversible in the normal
                  sense: it consumes real component stock. If you build
                  something you don't want, you'd correct it the same way
                  as any other production/stock error in Cin7's own UI
                  (voiding it, per this run's plan).
    QTY           quantity to build, e.g. 1.

This script pauses and asks you to type "yes" before doing anything that
writes to Cin7 -- read each step's printed output before confirming.

Output: printed to stdout and saved to sample_assembly_write_dump.json
(gitignored -- may contain real production data).
"""
from __future__ import annotations
import json
import sys

import keyring
import requests

SERVICE = 'ShonreiDailyReport'
BASE_URL = 'https://inventory.dearsystems.com/ExternalApi/v2'

dump = {}


def main():
    if len(sys.argv) != 3:
        print('Usage: python scripts/dump_sample_assembly_write.py <ASSEMBLY_SKU> <QTY>')
        sys.exit(1)
    assembly_sku, qty_str = sys.argv[1], sys.argv[2]
    qty = float(qty_str)

    account_id = keyring.get_password(SERVICE, 'cin7_account_id')
    api_key = keyring.get_password(SERVICE, 'cin7_api_key')
    if not account_id or not api_key:
        print('Cin7 credentials not found in Windows Credential Manager '
              f'(service={SERVICE!r}, keys cin7_account_id/cin7_api_key).')
        sys.exit(1)

    headers = {
        'api-auth-accountid': account_id,
        'api-auth-applicationkey': api_key,
        'Content-Type': 'application/json',
    }

    # -- 1. Look up the product first, read-only, so you can see what --
    #    this is about to build before anything is written.
    product = get_json(headers, 'product', {'SKU': assembly_sku, 'IncludeBOM': 'true'})
    products = product.get('Products') or []
    if not products:
        print(f'No product found for SKU {assembly_sku!r} -- aborting.')
        sys.exit(1)
    p = products[0]
    dump['assembly_product'] = p
    print(f"\nAbout to build {qty} x {assembly_sku} ({p.get('Name')})")
    print(f"BillOfMaterial={p.get('BillOfMaterial')}  DefaultLocation={p.get('DefaultLocation')!r}")
    for line in p.get('BillOfMaterialsProducts') or []:
        print(f"  consumes {line.get('Quantity')} x {line.get('ProductCode')} ({line.get('Name')}) per unit")
    if not confirm('This will really create/authorise/complete a Cin7 assembly and consume real component stock.'):
        save_and_exit()

    # -- 2. Create -------------------------------------------------------
    created = post_json(headers, 'finishedGoods', {
        'ProductID': p['ID'],
        'ProductCode': assembly_sku,
        'Quantity': qty,
        'Location': p.get('DefaultLocation'),
    }, label='create_assembly')
    task_id = created.get('TaskID')
    if not task_id:
        print('No TaskID came back from create -- aborting before authorising/completing anything.')
        save_and_exit()
    print(f"Created TaskID={task_id} Status={created.get('Status')}")

    # -- 3. Authorise ------------------------------------------------------
    order = get_json(headers, 'finishedGoods/order', {'TaskID': task_id}, label='order_before_authorise')
    if not confirm(f"About to authorise TaskID={task_id} with {len(order.get('OrderLines') or [])} order line(s) "
                    "(printed above in the saved JSON)."):
        save_and_exit()
    authorised = post_json(headers, 'finishedGoods/order', {
        'TaskID': task_id,
        'Status': 'AUTHORISED',
        'OrderLines': order.get('OrderLines') or [],
    }, label='authorise_assembly')
    print(f"Authorised: Status={authorised.get('Status')}")

    # -- 4. Complete (no separate Allocate call -- see cin7_client.py's --
    #    allocate_assembly docstring for why that stage is skipped here)
    pick = get_json(headers, 'finishedGoods/pick', {'TaskID': task_id}, label='pick_before_complete')
    if not confirm(f"About to COMPLETE TaskID={task_id} with {len(pick.get('PickLines') or [])} pick line(s) -- "
                    "this is the step that actually consumes component stock and creates finished-good stock."):
        save_and_exit()
    completed = post_json(headers, 'finishedGoods/pick', {
        'TaskID': task_id,
        'Status': 'COMPLETED',
        'PickLines': pick.get('PickLines') or [],
    }, label='complete_assembly')
    print(f"Completed: Status={completed.get('Status')}")

    full = get_json(headers, 'finishedGoods', {'TaskID': task_id}, label='full_assembly_after_complete')
    print(f"Final assembly record: Status={full.get('Status')} AssemblyNumber={full.get('AssemblyNumber')}")
    print(f"\nTaskID for this assembly (for voiding manually in Cin7's UI later): {task_id}")

    save_and_exit()


def confirm(message: str) -> bool:
    answer = input(f"\n{message}\nType 'yes' to continue, anything else to stop here: ")
    return answer.strip().lower() == 'yes'


def get_json(headers, path, params, label=None):
    resp = requests.get(f'{BASE_URL}/{path}', headers=headers, params=params, timeout=60)
    body = safe_json(resp)
    if label:
        dump[label] = {'status': resp.status_code, 'body': body if body is not None else resp.text[:1000]}
    print(f"GET {path} {params} -> {resp.status_code}")
    return body or {}


def post_json(headers, path, payload, label=None):
    resp = requests.post(f'{BASE_URL}/{path}', headers=headers, json=payload, timeout=60)
    body = safe_json(resp)
    if label:
        dump[label] = {'status': resp.status_code, 'request': payload, 'body': body if body is not None else resp.text[:1000]}
    print(f"POST {path} -> {resp.status_code}")
    if body and body.get('Errors'):
        print(f"  Cin7 returned Errors: {body['Errors']}")
    return body or {}


def safe_json(resp):
    content_type = resp.headers.get('Content-Type', '')
    if 'json' not in content_type.lower():
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def save_and_exit():
    with open('sample_assembly_write_dump.json', 'w', encoding='utf-8') as f:
        json.dump(dump, f, indent=2, default=str)
    print('\nWrote sample_assembly_write_dump.json -- send its contents back so cin7_client.py can be '
          'confirmed/finalised against it.')
    sys.exit(0)


if __name__ == '__main__':
    main()
