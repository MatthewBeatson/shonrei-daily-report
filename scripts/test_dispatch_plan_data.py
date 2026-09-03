"""Local-only smoke test for refresh-service/dispatch_plan_data.py.

Reads Cin7 credentials out of Windows Credential Manager (same as
scripts/print_local_credentials.py / scripts/dump_sample_sale.py -- never
printed, never sent anywhere but Cin7's own API), sets them as env vars for
this process only, then runs the real cin7_open_orders() pull and prints a
summary + a few sample rows so you can eyeball it against what you'd
expect to see in Cin7 before this feeds the scheduling/xlsx stages.

Usage: python scripts/test_dispatch_plan_data.py
"""
import os
import sys

import keyring

SERVICE = 'ShonreiDailyReport'

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'refresh-service'))


def main():
    account_id = keyring.get_password(SERVICE, 'cin7_account_id')
    api_key = keyring.get_password(SERVICE, 'cin7_api_key')
    if not account_id or not api_key:
        print('Cin7 credentials not found in Windows Credential Manager '
              f'(service={SERVICE!r}, keys cin7_account_id/cin7_api_key).')
        sys.exit(1)

    os.environ['CIN7_ACCOUNT_ID'] = account_id
    os.environ['CIN7_API_KEY'] = api_key

    from dispatch_plan_data import cin7_open_orders

    # Same defaults cin7_sales()/cin7_open_orders() fall back to when
    # reporting.settings isn't loaded here -- good enough for a local
    # smoke test without a DB connection.
    cfg = {
        'cin7_order_statuses': 'ORDERED|BACKORDERED',
        'cin7_invoice_statuses': 'DRAFT|NOT AVAILABLE|NOT INVOICED|PARTIALLY INVOICED',
    }

    print('Pulling every open order from Cin7 (this fetches full detail for each one, so it can take a while)...')
    orders = cin7_open_orders(cfg)

    print(f'\n{len(orders)} open orders found.')
    print(f'Total value excl GST (remaining un-invoiced balance): {sum(o["value_excl_gst"] for o in orders):,.2f}')

    missing_customer = [o for o in orders if not o['customer']]
    missing_order_date = [o for o in orders if not o['order_date']]
    has_ship_by = [o for o in orders if o['ship_by']]
    print(f'Missing customer name: {len(missing_customer)}')
    print(f'Missing order date: {len(missing_order_date)}')
    print(f'Have a promised ship date (ShipBy) set: {len(has_ship_by)}')

    print('\n--- Sample rows (first 10) ---')
    for o in orders[:10]:
        print(f"  {o['order_number']:<12} {o['customer']:<35} ref={o['reference'] or '(none)':<25} "
              f"order_date={o['order_date']} ship_by={o['ship_by']} "
              f"value={o['value_excl_gst']:>10,.2f} status={o['invoice_status']}")

    if has_ship_by:
        print('\n--- Sample rows WITH a promised ship date ---')
        for o in has_ship_by[:5]:
            print(f"  {o['order_number']:<12} {o['customer']:<35} ship_by={o['ship_by']} value={o['value_excl_gst']:>10,.2f}")


if __name__ == '__main__':
    main()
