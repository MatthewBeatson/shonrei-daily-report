"""Live Cin7 WRITE test: create + authorise an assembly at a PLANNED
quantity, then complete it at a smaller ACTUAL YIELD with a smaller
actual-hours figure entered against one specific labour line ("Labour -
Engineering" by default).

This is exploratory, deliberately NOT part of cin7_client.py yet -- it
tests something not yet confirmed: PickLines (the Complete call) only
accepts physical components (a labour line's ProductID 404s there, see
cin7_client.py's module docstring), so there's no way to correct a
labour line's actual hours *at* Complete. The only place labour lines
live is OrderLines (the Authorise call). So this script's hypothesis is:
Cin7 lets you re-submit OrderLines (POST /finishedGoods/order again)
after an assembly is already Authorised, to update a line's actual
figures before Complete -- Cin7's own "Assembly order" screen shows that
table as still editable (a "+", "Load BOM", "Scan") even once the
assembly is in "Work in progress" (Authorised) status, which is what
this hypothesis is based on. Not yet proven as a real API call.

Run this ON THIS MACHINE, credentials come out of Windows Credential
Manager (same as every other dump_sample_*.py script).

Usage:
    python scripts/dump_sample_assembly_actuals.py <SKU> <PLANNED_QTY> <ACTUAL_YIELD> <ENGINEERING_HOURS> [LABOUR_LINE_NAME_SUBSTRING]

    SKU                    a real assembly SKU with a labour line to test against.
    PLANNED_QTY            the quantity used for Create + first Authorise.
    ACTUAL_YIELD            the smaller actual completed quantity (< PLANNED_QTY
                           to genuinely test the "smaller yield" case).
    ENGINEERING_HOURS      the real hours actually spent on that labour line --
                           per-unit Quantity sent = ENGINEERING_HOURS / ACTUAL_YIELD,
                           TotalQuantity = ENGINEERING_HOURS straight through
                           (same formula discussed for the future floor-app field).
    LABOUR_LINE_NAME_SUBSTRING   optional, defaults to "engineering" (case-
                           insensitive substring match against BillOfMaterialsServices
                           line Names) -- which labour line to override.

Sequence (confirmation prompt before each write):
    1. Create at PLANNED_QTY (Status DRAFT).
    2. Authorise with standard BOM-derived OrderLines at PLANNED_QTY
       (the normal, already-confirmed path).
    3. adjust_assembly_qty -> set Quantity to ACTUAL_YIELD (the
       confirmed "Actual yield" mechanism, see cin7_client.py's
       adjust_assembly_qty docstring).
    4. Re-submit Order (POST /finishedGoods/order again) with lines
       recalculated at ACTUAL_YIELD, EXCEPT the matched labour line,
       which uses ENGINEERING_HOURS instead of the BOM standard rate.
       This is the actual hypothesis under test.
    5. Complete (Pick) with physical-only lines at ACTUAL_YIELD.

Output: printed to stdout and saved to sample_assembly_actuals_dump.json
(gitignored -- may contain real production data).
"""
from __future__ import annotations
import json
import sys
from datetime import datetime, timezone

import keyring
import requests

SERVICE = 'ShonreiDailyReport'
BASE_URL = 'https://inventory.dearsystems.com/ExternalApi/v2'

# Must match cin7_client.py's CIN7_FINISHED_GOODS_ACCOUNT / CIN7_WIP_ACCOUNT.
FINISHED_GOODS_ACCOUNT = '720'  # "720: Stock on Hand - Cin7 Core"
WIP_ACCOUNT = '721B'  # "721B: Work in Progress Cin7 Core"

dump = {}


def main():
    if len(sys.argv) not in (5, 6):
        print('Usage: python scripts/dump_sample_assembly_actuals.py <SKU> <PLANNED_QTY> <ACTUAL_YIELD> <ENGINEERING_HOURS> [LABOUR_LINE_NAME_SUBSTRING]')
        sys.exit(1)
    sku, planned_qty_str, actual_yield_str, hours_str = sys.argv[1:5]
    planned_qty = float(planned_qty_str)
    actual_yield = float(actual_yield_str)
    actual_hours = float(hours_str)
    labour_match = (sys.argv[5] if len(sys.argv) == 6 else 'engineering').lower()

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

    # -- 1. Look up the product, read-only -------------------------------
    product = get_json(headers, 'product', {'SKU': sku, 'IncludeBOM': 'true'})
    products = product.get('Products') or []
    if not products:
        print(f'No product found for SKU {sku!r} -- aborting.')
        sys.exit(1)
    p = products[0]
    dump['assembly_product'] = p

    services = p.get('BillOfMaterialsServices') or []
    target_line = next((s for s in services if labour_match in (s.get('Name') or '').lower()), None)
    if target_line is None:
        print(f"No labour line matching {labour_match!r} found in BillOfMaterialsServices for {sku!r}. "
              f"Available: {[s.get('Name') for s in services]}")
        sys.exit(1)

    print(f"\nPlan: Create+Authorise {planned_qty} x {sku} ({p.get('Name')}), then complete at "
          f"actual yield {actual_yield}, with {actual_hours} actual hours on {target_line.get('Name')!r} "
          f"(standard BOM rate for that line would be {target_line.get('Quantity')} x {actual_yield} = "
          f"{(target_line.get('Quantity') or 0) * actual_yield}; using {actual_hours} instead).")
    for line in p.get('BillOfMaterialsProducts') or []:
        print(f"  consumes {line.get('Quantity')} x {line.get('ProductCode')} ({line.get('Name')}) per unit")
    for line in services:
        marker = ' <-- OVERRIDING' if line is target_line else ''
        print(f"  labour: {line.get('Quantity')} x {line.get('Name')} per unit{marker}")
    if not confirm('This will really create/authorise/complete a Cin7 assembly and consume real component stock.'):
        save_and_exit()

    # -- 2. Create at PLANNED_QTY -----------------------------------------
    created = post_json(headers, 'finishedGoods', {
        'ProductID': p['ID'],
        'ProductCode': sku,
        'Quantity': planned_qty,
        'Location': p.get('DefaultLocation'),
        'Status': 'DRAFT',
        'Account': FINISHED_GOODS_ACCOUNT,
        'WIPAccount': WIP_ACCOUNT,
    }, label='create_assembly')
    task_id = created.get('TaskID')
    if not task_id:
        print('No TaskID came back from create -- aborting.')
        save_and_exit()
    print(f"Created TaskID={task_id} Status={created.get('Status')}")

    # -- 3. Authorise with standard BOM-derived lines at PLANNED_QTY -----
    standard_order_lines = build_order_lines(p, planned_qty, override_line=None, actual_hours=None)
    if not confirm(f"About to authorise TaskID={task_id} with {len(standard_order_lines)} order line(s) "
                    f"at the standard BOM rates for planned qty {planned_qty}."):
        save_and_exit()
    authorised = post_json(headers, 'finishedGoods/order', {
        'TaskID': task_id,
        'Status': 'AUTHORISED',
        'OrderLines': standard_order_lines,
    }, label='authorise_assembly_standard')
    print(f"Authorised: Status={authorised.get('Status')}")

    # -- 4. adjust_assembly_qty -> set Quantity to ACTUAL_YIELD -----------
    if actual_yield != planned_qty:
        full = get_json(headers, 'finishedGoods', {'TaskID': task_id}, label='full_before_adjust')
        if not confirm(f"About to adjust TaskID={task_id}'s Quantity from {full.get('Quantity')} to "
                        f"{actual_yield} (the confirmed 'Actual yield' mechanism)."):
            save_and_exit()
        adjusted = put_json(headers, 'finishedGoods', {
            'ID': full.get('ID') or task_id,
            'ProductCode': full.get('ProductCode'),
            'ProductID': full.get('ProductID'),
            'Quantity': actual_yield,
            'Location': full.get('Location'),
            'LocationID': full.get('LocationID'),
        }, label='adjust_assembly_qty')
        print(f"Adjusted: Quantity={adjusted.get('Quantity')}")

    # -- 5. THE HYPOTHESIS UNDER TEST: re-submit Order with the actual- --
    #    hours override, now that the assembly is Authorised (not Draft).
    actual_order_lines = build_order_lines(p, actual_yield, override_line=target_line, actual_hours=actual_hours)
    if not confirm(f"About to RE-authorise (re-POST /finishedGoods/order) TaskID={task_id} with "
                    f"{len(actual_order_lines)} order line(s) reflecting actual yield {actual_yield} and "
                    f"{actual_hours} actual hours on {target_line.get('Name')!r} -- this is the untested part."):
        save_and_exit()
    re_authorised = post_json(headers, 'finishedGoods/order', {
        'TaskID': task_id,
        'Status': 'AUTHORISED',
        'OrderLines': actual_order_lines,
    }, label='authorise_assembly_with_actuals')
    print(f"Re-authorise with actuals: Status={re_authorised.get('Status')}")

    # -- 6. Complete (Pick) -- physical components only, at ACTUAL_YIELD -
    pick_lines = [
        {'ProductID': line['ProductID'], 'ProductCode': line['ProductCode'], 'Name': line['Name'],
         'Quantity': line['TotalQuantity'], 'Unit': ''}
        for line in build_component_lines(p, actual_yield)
        if line['ProductCode']
    ]
    if not confirm(f"About to COMPLETE TaskID={task_id} with {len(pick_lines)} pick line(s) at actual yield "
                    f"{actual_yield} -- this is the step that actually consumes component stock."):
        save_and_exit()
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
    completed = post_json(headers, 'finishedGoods/pick', {
        'TaskID': task_id,
        'Status': 'COMPLETED',
        'PickLines': pick_lines,
        'Account': FINISHED_GOODS_ACCOUNT,
        'WIPAccount': WIP_ACCOUNT,
        'CompletionDate': now,
        'WIPDate': now,
    }, label='complete_assembly')
    print(f"Completed: Status={completed.get('Status')}")

    full = get_json(headers, 'finishedGoods', {'TaskID': task_id}, label='full_assembly_after_complete')
    print(f"Final assembly record: Status={full.get('Status')} AssemblyNumber={full.get('AssemblyNumber')} "
          f"Quantity={full.get('Quantity')}")
    print(f"\nTaskID for this assembly (for voiding manually in Cin7's UI later, or via "
          f"scripts/void_test_assemblies.py): {task_id}")

    save_and_exit()


def build_component_lines(product, build_qty):
    """Same as cin7_client.py's _component_lines_for_build -- standard
    BOM rates, no overrides. Used for PickLines (physical only)."""
    lines = []
    for line in product.get('BillOfMaterialsProducts') or []:
        qty_per = line.get('Quantity') or 0
        lines.append({
            'ProductID': line.get('ComponentProductID'), 'ProductCode': line.get('ProductCode'),
            'Name': line.get('Name'), 'Quantity': qty_per, 'TotalQuantity': qty_per * build_qty,
            'WastagePercent': line.get('WastagePercent') or 0, 'WastageQuantity': line.get('WastageQuantity') or 0,
            'ExpenseAccount': '',
        })
    for line in product.get('BillOfMaterialsServices') or []:
        qty_per = line.get('Quantity') or 0
        lines.append({
            'ProductID': line.get('ComponentProductID'), 'ProductCode': '',
            'Name': line.get('Name'), 'Quantity': qty_per, 'TotalQuantity': qty_per * build_qty,
            'WastagePercent': 0, 'WastageQuantity': 0,
            'ExpenseAccount': line.get('ExpenseAccount') or '', 'PriceTier': line.get('PriceTier') or 1,
        })
    return lines


def build_order_lines(product, build_qty, *, override_line, actual_hours):
    """Component lines for OrderLines, with one labour line (override_line,
    by identity) replaced with actual_hours-derived figures instead of the
    standard BOM rate -- TotalQuantity = actual_hours straight through,
    per-unit Quantity = actual_hours / build_qty."""
    lines = build_component_lines(product, build_qty)
    if override_line is not None:
        for line in lines:
            if line['ProductID'] == override_line.get('ComponentProductID') and not line['ProductCode']:
                line['Quantity'] = actual_hours / build_qty if build_qty else 0
                line['TotalQuantity'] = actual_hours
    return lines


def confirm(message: str) -> bool:
    answer = input(f"\n{message}\nType 'yes' to continue, anything else to stop here: ")
    return answer.strip().lower() == 'yes'


def get_json(headers, path, params, label=None):
    resp = requests.get(f'{BASE_URL}/{path}', headers=headers, params=params, timeout=60)
    body = safe_json(resp)
    if label:
        dump[label] = {'status': resp.status_code, 'body': body if body is not None else resp.text[:1000]}
    print(f"GET {path} {params} -> {resp.status_code}")
    return body if isinstance(body, dict) else {}


def post_json(headers, path, payload, label=None):
    resp = requests.post(f'{BASE_URL}/{path}', headers=headers, json=payload, timeout=60)
    body = safe_json(resp)
    if label:
        dump[label] = {'status': resp.status_code, 'request': payload, 'body': body if body is not None else resp.text[:1000]}
    print(f"POST {path} -> {resp.status_code}")
    if resp.status_code >= 400:
        print(f"  Cin7 returned an error body: {body if body is not None else resp.text[:1000]}")
    elif isinstance(body, dict) and body.get('Errors'):
        print(f"  Cin7 returned Errors: {body['Errors']}")
    return body if isinstance(body, dict) else {}


def put_json(headers, path, payload, label=None):
    resp = requests.put(f'{BASE_URL}/{path}', headers=headers, json=payload, timeout=60)
    body = safe_json(resp)
    if label:
        dump[label] = {'status': resp.status_code, 'request': payload, 'body': body if body is not None else resp.text[:1000]}
    print(f"PUT {path} -> {resp.status_code}")
    if resp.status_code >= 400:
        print(f"  Cin7 returned an error body: {body if body is not None else resp.text[:1000]}")
    elif isinstance(body, dict) and body.get('Errors'):
        print(f"  Cin7 returned Errors: {body['Errors']}")
    return body if isinstance(body, dict) else {}


def safe_json(resp):
    content_type = resp.headers.get('Content-Type', '')
    if 'json' not in content_type.lower():
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def save_and_exit():
    with open('sample_assembly_actuals_dump.json', 'w', encoding='utf-8') as f:
        json.dump(dump, f, indent=2, default=str)
    print('\nWrote sample_assembly_actuals_dump.json -- send its contents back so we can confirm/finalise '
          'the actual-hours-override mechanism.')
    sys.exit(0)


if __name__ == '__main__':
    main()
